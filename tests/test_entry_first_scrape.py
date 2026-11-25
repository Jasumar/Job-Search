"""Scrape gate: the resolved entry is a first-class scraping URL (demo gate).

Phase 4.0d already preferred ``open_positions_url`` over ``careers_url`` when
choosing where to scrape; the gate in front of it still demanded a
``careers_url``, so an accepted company whose entry was resolved but whose
careers page was never filled stayed unscrapable. These tests pin the ordering
that fixes it:

- a company with an entry and no ``careers_url`` is scraped, at the entry;
- a company with neither is skipped and the skip is recorded on the run, never
  silently dropped;
- the ATS-board refusal applies to whichever URL the scrape would have used,
  the entry included.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from jobsearch_core.db.models import Company, JobRun, RunSource
from jobsearch_core.ingest.scrapers.base import ScrapedJob
from jobsearch_core.ingest.scrapers.pipeline import scrape_company_entries

STATS = {"added": 1, "flagged": 0, "skipped_dup": 0, "no_description": 0, "linked": 1}
ENTRY = "https://entry.example/jobs"
BOARD = "https://boards.greenhouse.io/acme"


def _run(session) -> JobRun:
    run = JobRun(run_type="collect", status="running", params="{}")
    session.add(run)
    session.commit()
    return run


def _company(session, name: str, **kwargs) -> Company:
    company = Company(name=name, status="accepted", **kwargs)
    session.add(company)
    session.commit()
    return company


def _patched(jobs: list[ScrapedJob]):
    return (
        patch(
            "jobsearch_core.ingest.scrapers.pipeline.PoliteClient",
            return_value=MagicMock(),
        ),
        patch(
            "jobsearch_core.ingest.scrapers.pipeline.scrape_company",
            return_value=jobs,
        ),
        patch(
            "jobsearch_core.ingest.scrapers.pipeline.ingest_scraped",
            return_value=STATS,
        ),
    )


def test_an_entry_only_company_is_scraped_at_its_entry(session):
    _company(session, "Entry Only", open_positions_url=ENTRY)
    run = _run(session)
    jobs = [ScrapedJob(url=f"{ENTRY}/1001", title="Backend Engineer", company="Entry Only")]

    client, scrape, ingest = _patched(jobs)
    with client, scrape as mock_scrape, ingest:
        stats = scrape_company_entries(run_id=run.id)

    assert stats["companies"] == 1
    assert mock_scrape.call_args.args[1] == ENTRY
    row = session.query(RunSource).filter(RunSource.run_id == run.id).one()
    assert row.status == "completed"


def test_a_company_with_neither_url_is_skipped_and_recorded(session):
    _company(session, "Nowhere Co")
    run = _run(session)

    client, scrape, ingest = _patched([])
    with client, scrape as mock_scrape, ingest:
        stats = scrape_company_entries(run_id=run.id)

    assert stats["companies"] == 0
    mock_scrape.assert_not_called()
    row = session.query(RunSource).filter(RunSource.run_id == run.id).one()
    assert row.status == "failed"
    assert row.error == "no careers_url or entry"


def test_the_board_refusal_follows_the_entry(session):
    """An entry pointing at an ATS board is refused like a board careers_url."""
    _company(session, "Board Entry", open_positions_url=BOARD)
    run = _run(session)

    client, scrape, ingest = _patched([])
    with client, scrape as mock_scrape, ingest:
        stats = scrape_company_entries(run_id=run.id)

    assert stats["board_url_rejected"] == 1
    mock_scrape.assert_not_called()
    row = session.query(RunSource).filter(RunSource.run_id == run.id).one()
    assert row.status == "failed"
    assert row.error == f"{BOARD} is an ATS board URL"
