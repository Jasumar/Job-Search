"""The scrape filter drops links that are not postings (master_plan §Drop non-postings at scrape).

The generic scrape stores what the extractor returns. These tests pin the rule
that judges it: a link that is page chrome or fails the posting check is dropped
before any detail fetch, every drop is recorded on the run's ``run_sources`` row,
and a scrape that keeps nothing is ``zero_yield`` and does not promote the company.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from jobsearch_core.db.models import Company, JobRun, RunSource
from jobsearch_core.ingest.scrapers.base import ScrapedJob
from jobsearch_core.ingest.scrapers.entry_resolution import filter_postings, real_postings
from jobsearch_core.ingest.scrapers.pipeline import scrape_company_entries

ENTRY = "https://acme.example/careers"
STATS = {"added": 1, "flagged": 0, "skipped_dup": 0, "no_description": 0, "linked": 1}


def _job(path: str, title: str, **kwargs) -> ScrapedJob:
    return ScrapedJob(url=f"https://acme.example{path}", title=title, company="Acme", **kwargs)


def _page(body: str, nav: str = "") -> str:
    return f"<html><body><nav>{nav}</nav><main>{body}</main></body></html>"


def _links(jobs: list[ScrapedJob]) -> str:
    return "".join(f'<p><a href="{job.url}">{job.title}</a></p>' for job in jobs)


def _site_links(count: int) -> list[ScrapedJob]:
    """Digia / Volvo shape: one-segment site pages, no ids, no repeated shape."""
    return [_job(f"/page{'x' * i}", f"Reasons to join {i}") for i in range(count)]


def _scrape(session, scraped: list[ScrapedJob], html: str, *, dry_run: bool = False, **company):
    """Run ``scrape_company_entries`` over one accepted company with a canned scrape."""
    session.add(Company(name="Acme", status="accepted", open_positions_url=ENTRY, **company))
    run = JobRun(run_type="collect", status="running", params="{}")
    session.add(run)
    session.commit()

    def fake_scrape(*_args, page_html_out=None, **_kwargs):
        if page_html_out is not None:
            page_html_out.append(html)
        return scraped

    with (
        patch("jobsearch_core.ingest.scrapers.pipeline.PoliteClient", return_value=MagicMock()),
        patch("jobsearch_core.ingest.scrapers.pipeline.scrape_company", side_effect=fake_scrape),
        patch(
            "jobsearch_core.ingest.scrapers.pipeline.ingest_scraped",
            side_effect=lambda jobs, **_kw: {**STATS, "added": len(jobs)},
        ) as ingest,
    ):
        scrape_company_entries(run_id=run.id, dry_run=dry_run, fetch_descriptions=False)
    return run, ingest


def _ingested(ingest) -> list[ScrapedJob]:
    return [job for call in ingest.call_args_list for job in call.args[0]]


def test_site_links_only_is_zero_yield_and_the_company_is_not_promoted(session):
    junk = _site_links(22)
    run, ingest = _scrape(session, junk, _page(_links(junk)))

    assert _ingested(ingest) == []
    row = session.query(RunSource).filter(RunSource.run_id == run.id).one()
    assert row.status == "zero_yield"
    assert row.identities_found == 0
    dropped = json.loads(row.dropped)
    assert dropped["total"] == 22
    assert dropped["dropped"] == 22
    assert dropped["reasons"] == {"no_posting_signal": 22}
    assert len(dropped["examples"]) == 5
    assert "22 of 22 links dropped" in row.error
    assert session.query(Company).one().status == "accepted"


def test_postings_are_kept_and_chrome_links_are_recorded(session):
    postings = [_job(f"/jobs/{10000 + i}", f"Software Engineer {i}") for i in range(18)]
    chrome = [_job(f"/about{'x' * i}", f"About us {i}") for i in range(4)]
    nav = "".join(f'<a href="{job.url}">{job.title}</a>' for job in chrome)
    run, ingest = _scrape(session, postings + chrome, _page(_links(postings), nav=nav))

    assert _ingested(ingest) == postings
    row = session.query(RunSource).filter(RunSource.run_id == run.id).one()
    assert row.status == "completed"
    assert row.identities_found == 18
    dropped = json.loads(row.dropped)
    assert (dropped["total"], dropped["dropped"]) == (22, 4)
    assert dropped["reasons"] == {"chrome": 4}
    assert row.error is None


def test_short_titles_in_a_repeated_card_list_are_kept(session):
    names = ("Designer", "Analyst", "Recruiter", "Plumber", "Welder", "Tester", "Nurse", "Cook")
    cards = [_job(f"/open/role-{name}", name) for name in names]
    run, ingest = _scrape(session, cards, _page(_links(cards)))

    assert _ingested(ingest) == cards
    row = session.query(RunSource).filter(RunSource.run_id == run.id).one()
    assert row.status == "completed"
    assert row.dropped is None


def test_ats_board_jobs_under_an_employer_page_are_kept(session):
    board = [
        ScrapedJob(
            url="https://boards.greenhouse.io/acme/jobs/1",
            title="Engineer",
            company="Acme",
            ats_platform="greenhouse",
        )
    ]
    run, ingest = _scrape(session, board, _page(""))

    assert _ingested(ingest) == board
    row = session.query(RunSource).filter(RunSource.run_id == run.id).one()
    assert row.status == "completed"
    assert row.dropped is None


def test_dry_run_writes_no_run_source(session):
    junk = _site_links(6)
    run, _ingest = _scrape(session, junk, _page(_links(junk)), dry_run=True)

    assert session.query(RunSource).filter(RunSource.run_id == run.id).count() == 0


def test_a_scrape_that_finds_nothing_is_zero_yield_without_a_drop_record(session):
    run, _ingest = _scrape(session, [], _page(""))

    row = session.query(RunSource).filter(RunSource.run_id == run.id).one()
    assert row.status == "zero_yield"
    assert row.dropped is None


def test_filter_uses_the_jobs_own_origin_so_off_domain_never_fires():
    off = ScrapedJob(url="https://jobs.other.example/apply/12345", title="Data Engineer", company="Acme")
    kept, dropped = filter_postings([off], ENTRY, _page(""))

    assert kept == [off]
    assert dropped == []


def test_the_entry_resolver_still_judges_an_off_domain_job():
    off = ScrapedJob(url="https://jobs.other.example/apply/12345", title="Data Engineer", company="Acme")
    assert real_postings([off], ENTRY, _page("")) == []
