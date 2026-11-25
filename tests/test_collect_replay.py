"""R5a wiring: a collect run replays a verified recipe instead of scraping.

Properties under test:
- A company with a verified recipe is replayed; the generic scraper is not
  called, and the run source row records the recipe, the count, and the
  verified URL set.
- A replay that fails retires the recipe and falls back to the generic scraper
  for this run.
- A company without a recipe is scraped generically and its run source row
  carries no recipe evidence.
- A dry run writes no manifest and replays nothing.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import patch

from jobsearch_core.db.models import Company, CompanyRecipe, JobRun, RunSource
from jobsearch_core.ingest.scrapers.base import ScrapedJob
from jobsearch_core.ingest.scrapers.pipeline import scrape_company_entries

CAREERS = "https://acme.example/careers"
API_PAGE = "https://api.acme.example/jobs"


class FakeClient:
    def __init__(self, payload: dict | None = None):
        self.payload = payload

    def fetch(self, url: str) -> str:
        raise RuntimeError("static fetch failed")

    def fetch_rendered(self, url: str) -> str:
        raise RuntimeError("render failed")

    def fetch_json(self, url: str) -> str:
        if self.payload is None:
            raise RuntimeError("api fetch failed")
        return json.dumps(self.payload)

    def close(self) -> None:
        pass


def _api_body() -> dict:
    return {
        "kind": "api",
        "allowed_hosts": ["api.acme.example"],
        "verification": {
            "expected_count": 2,
            "sample_titles": ["Backend Engineer"],
            "url_pattern": r"^https://acme\.example/jobs/\d+$",
        },
        "url_template": API_PAGE,
        "list_path": ["jobs"],
        "field_map": {
            "url": {"path": ["url"]},
            "title": {"path": ["title"]},
        },
    }


def _payload() -> dict:
    return {
        "jobs": [
            {"url": "https://acme.example/jobs/1", "title": "Backend Engineer"},
            {"url": "https://acme.example/jobs/2", "title": "Data Engineer"},
        ]
    }


def _seed(session, *, with_recipe: bool) -> tuple[Company, JobRun]:
    company = Company(name="Acme", status="accepted", careers_url=CAREERS)
    session.add(company)
    session.commit()
    if with_recipe:
        now = datetime.now(UTC).replace(tzinfo=None)
        session.add(
            CompanyRecipe(
                scope="company_careers",
                company_id=company.id,
                kind="api",
                status="verified",
                body=json.dumps(_api_body()),
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()
    run = JobRun(run_type="collect", status="running", params="{}")
    session.add(run)
    session.commit()
    return company, run


STATS = {"added": 2, "flagged": 0, "skipped_dup": 0, "no_description": 0, "linked": 2}


def test_a_verified_recipe_is_replayed_instead_of_scraped(session):
    company, run = _seed(session, with_recipe=True)
    with (
        patch("jobsearch_core.ingest.scrapers.pipeline.PoliteClient", return_value=FakeClient(_payload())),
        patch("jobsearch_core.ingest.scrapers.pipeline.scrape_company") as mock_scrape,
        patch("jobsearch_core.ingest.scrapers.pipeline.ingest_scraped", return_value=STATS) as mock_ingest,
    ):
        scrape_company_entries(run_id=run.id)

    mock_scrape.assert_not_called()
    replayed = mock_ingest.call_args.args[0]
    assert [job.url for job in replayed] == [
        "https://acme.example/jobs/1",
        "https://acme.example/jobs/2",
    ]

    row = session.query(RunSource).filter(RunSource.run_id == run.id).one()
    assert row.company_id == company.id
    assert row.status == "completed"
    assert row.extraction == "recipe"
    assert row.identities_found == 2
    assert row.recipe_id is not None
    assert json.loads(row.verified_urls) == [
        "https://acme.example/jobs/1",
        "https://acme.example/jobs/2",
    ]


def test_replayed_postings_get_descriptions_and_known_ones_are_not_fetched(session):
    from jobsearch_core.db.models import Job
    from jobsearch_core.ingest.scrapers.pipeline import dedup_key, normalize

    company, run = _seed(session, with_recipe=True)
    known = dedup_key(*normalize("Backend Engineer", "Acme", "")[:3])
    session.add(Job(url="https://acme.example/jobs/1", raw_title="Backend Engineer",
                    raw_company="Acme", dedup_hash=known, status="new"))
    session.commit()
    fetched: list[str] = []

    def fake_detail(client, url):
        fetched.append(url)
        return "A real description"

    with (
        patch("jobsearch_core.ingest.scrapers.pipeline.PoliteClient", return_value=FakeClient(_payload())),
        patch("jobsearch_core.ingest.scrapers.pipeline.fetch_detail", side_effect=fake_detail),
        patch("jobsearch_core.ingest.scrapers.pipeline.ingest_scraped", return_value=STATS) as mock_ingest,
    ):
        scrape_company_entries(run_id=run.id)

    assert fetched == ["https://acme.example/jobs/2"]
    # Known postings and fresh batches are ingested in separate calls.
    replayed = {job.url: job for call in mock_ingest.call_args_list for job in call.args[0]}
    assert replayed["https://acme.example/jobs/2"].description == "A real description"
    assert replayed["https://acme.example/jobs/1"].description is None


def test_a_failed_replay_retires_the_recipe_and_falls_back(session):
    company, run = _seed(session, with_recipe=True)
    fallback = [ScrapedJob(url="https://acme.example/jobs/7001", title="Backend Engineer", company="Acme")]
    with (
        patch("jobsearch_core.ingest.scrapers.pipeline.PoliteClient", return_value=FakeClient(None)),
        patch("jobsearch_core.ingest.scrapers.pipeline.scrape_company", return_value=fallback) as mock_scrape,
        patch("jobsearch_core.ingest.scrapers.pipeline.ingest_scraped", return_value=STATS),
    ):
        scrape_company_entries(run_id=run.id)

    mock_scrape.assert_called_once()
    recipe = session.query(CompanyRecipe).one()
    assert recipe.status == "stale"

    row = session.query(RunSource).filter(RunSource.run_id == run.id).one()
    assert row.status == "completed"
    assert row.extraction is None
    assert row.identities_found == 1
    assert row.verified_urls is None


def test_a_company_without_a_recipe_is_scraped_generically(session):
    company, run = _seed(session, with_recipe=False)
    fallback = [ScrapedJob(url="https://acme.example/jobs/7001", title="Backend Engineer", company="Acme")]
    with (
        patch("jobsearch_core.ingest.scrapers.pipeline.PoliteClient", return_value=FakeClient(_payload())),
        patch("jobsearch_core.ingest.scrapers.pipeline.scrape_company", return_value=fallback) as mock_scrape,
        patch("jobsearch_core.ingest.scrapers.pipeline.ingest_scraped", return_value=STATS),
    ):
        scrape_company_entries(run_id=run.id)

    mock_scrape.assert_called_once()
    row = session.query(RunSource).filter(RunSource.run_id == run.id).one()
    assert row.status == "completed"
    assert row.extraction is None
    assert row.verified_urls is None


def test_a_refused_company_is_recorded_not_dropped(session):
    company = Company(
        name="BoardCo", status="accepted", careers_url="https://boards.greenhouse.io/boardco"
    )
    session.add(company)
    session.commit()
    run = JobRun(run_type="collect", status="running", params="{}")
    session.add(run)
    session.commit()

    with (
        patch("jobsearch_core.ingest.scrapers.pipeline.PoliteClient", return_value=FakeClient(_payload())),
        patch("jobsearch_core.ingest.scrapers.pipeline.scrape_company") as mock_scrape,
        patch("jobsearch_core.ingest.scrapers.pipeline.ingest_scraped", return_value=STATS),
    ):
        scrape_company_entries(run_id=run.id)

    mock_scrape.assert_not_called()
    row = session.query(RunSource).filter(RunSource.run_id == run.id).one()
    assert row.company_id == company.id
    assert row.status == "failed"
    assert "ATS board URL" in row.error


def test_a_dry_run_writes_no_manifest_and_replays_nothing(session):
    company, run = _seed(session, with_recipe=True)
    with (
        patch("jobsearch_core.ingest.scrapers.pipeline.PoliteClient", return_value=FakeClient(_payload())),
        patch("jobsearch_core.ingest.scrapers.pipeline.scrape_company", return_value=[]) as mock_scrape,
        patch("jobsearch_core.ingest.scrapers.pipeline.ingest_scraped", return_value=STATS),
    ):
        scrape_company_entries(run_id=run.id, dry_run=True)

    mock_scrape.assert_called_once()
    assert session.query(RunSource).count() == 0
