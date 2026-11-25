from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from jobsearch_core.cli import main
from jobsearch_core.db.models import Company, Job
from jobsearch_core.ingest.scrapers.base import ScrapedJob
from jobsearch_core.ingest.scrapers.entry_resolution import EntryReport, EntryResolution
from jobsearch_core.ingest.scrapers.pipeline import (
    _select_and_scrape,
    ingest_scraped,
    scrape_company,
    scrape_company_entries,
)
from jobsearch_core.run import RunSummary


def _client() -> MagicMock:
    """A stand-in for PoliteClient: ``fetch_json`` serves whatever ``fetch`` serves.

    The scrape pipeline asks for JSON-typed bodies now, but each test scripts the
    canned response on ``fetch``; delegating keeps one script per test.
    """
    client = MagicMock()
    client.fetch_json.side_effect = lambda url: client.fetch(url)
    return client


class TestIngestScraped:
    def test_ingest_adds_new_job(self, session):
        jobs = [
            ScrapedJob(
                url="https://example.com/jobs/1",
                title="Backend Engineer",
                company="ExampleCorp",
                location="Helsinki, Finland",
                description="We need a Python developer with Django experience.",
            )
        ]
        stats = ingest_scraped(jobs, source_url="https://example.com/careers")
        assert stats["added"] == 1
        assert stats["skipped_dup"] == 0
        assert stats["flagged"] == 0

        db_job = session.query(Job).first()
        assert db_job is not None
        assert db_job.raw_title == "Backend Engineer"
        assert db_job.raw_company == "ExampleCorp"
        assert db_job.source_url == "https://example.com/careers"
        assert db_job.status == "new"

    def test_ingest_flags_senior_title_but_keeps_the_job(self, session):
        jobs = [
            ScrapedJob(
                url="https://example.com/jobs/2",
                title="Senior Staff Engineer",
                company="BigCorp",
                location="Berlin, Germany",
                description="10+ years experience required.",
            )
        ]
        stats = ingest_scraped(jobs, source_url="https://bigcorp.example.com/careers")
        assert stats["flagged"] == 1
        assert stats["added"] == 1

        # M0: a filter hit is a fact on the row, never a verdict.
        db_job = session.query(Job).first()
        assert db_job.status == "new"
        flags = json.loads(db_job.fit_flags)
        assert flags and flags[0]["reason"]

    def test_ingest_skips_duplicate(self, session):
        job = ScrapedJob(
            url="https://example.com/jobs/1",
            title="Backend Engineer",
            company="ExampleCorp",
            location="Helsinki, Finland",
            description="Python developer needed.",
        )
        ingest_scraped([job], source_url="https://example.com/careers")
        stats = ingest_scraped([job], source_url="https://example.com/careers")
        assert stats["skipped_dup"] == 1
        assert stats["added"] == 0

    def test_ingest_drops_no_description(self, session):
        jobs = [
            ScrapedJob(
                url="https://example.com/jobs/3",
                title="Engineer",
                company="Corp",
                location="Finland",
                description=None,
            )
        ]
        stats = ingest_scraped(jobs, source_url="https://corp.example.com/careers")
        assert stats["no_description"] == 1
        assert stats["added"] == 0
        assert session.query(Job).count() == 0

    def test_ingest_drops_unverified_job(self, session):
        job = ScrapedJob(
            url="https://ats.example.com/jobs/4",
            title="Backend Engineer",
            company="Corp",
            description="Python developer needed.",
            verified=False,
        )
        stats = ingest_scraped([job], source_url="https://corp.example.com/careers")
        assert stats["unverified"] == 1
        assert session.query(Job).count() == 0

    def test_ingest_drops_job_with_relative_url(self, session):
        job = ScrapedJob(
            url="/info@corp.example",
            title="Backend Engineer",
            company="Corp",
            description="Python developer needed.",
        )
        stats = ingest_scraped([job], source_url="https://corp.example.com/careers")
        assert stats["unverified"] == 1
        assert session.query(Job).count() == 0

    def test_ingest_dry_run_no_db_write(self, session):
        jobs = [
            ScrapedJob(
                url="https://example.com/jobs/1",
                title="Backend Engineer",
                company="Corp",
                location="Helsinki",
                description="Python developer with 2 years experience.",
            )
        ]
        stats = ingest_scraped(jobs, source_url="https://corp.example.com/careers", dry_run=True)
        assert stats["added"] == 1
        assert session.query(Job).count() == 0

    def test_ingest_multiple_jobs(self, session):
        jobs = [
            ScrapedJob(
                url=f"https://example.com/jobs/{i}",
                title=f"Engineer {i}",
                company="Corp",
                location="Helsinki, Finland",
                description=f"Python developer role number {i} with sufficient description text.",
            )
            for i in range(5)
        ]
        stats = ingest_scraped(jobs, source_url="https://corp.example.com/careers")
        assert stats["added"] == 5
        assert session.query(Job).count() == 5


class TestScrapeCompany:
    @patch("jobsearch_core.ingest.scrapers.pipeline.PoliteClient")
    def test_scrape_company_basic(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.fetch.return_value = """
        <html><body>
            <a href="/jobs/backend-engineer">Backend Engineer</a>
            <a href="/jobs/data-scientist">Data Scientist</a>
        </body></html>
        """
        mock_client.close = MagicMock()

        jobs = scrape_company(
            "TestCorp",
            "https://careers.testcorp.com",
            client=mock_client,
            fetch_descriptions=False,
            verified=True,
        )
        assert len(jobs) == 2
        assert jobs[0].company == "TestCorp"
        assert jobs[1].company == "TestCorp"

    @patch("jobsearch_core.ingest.scrapers.pipeline.PoliteClient")
    def test_scrape_company_fetch_failure(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.fetch.side_effect = RuntimeError("Connection refused")

        jobs = scrape_company("TestCorp", "https://careers.testcorp.com", client=mock_client)
        assert jobs == []

    @patch("jobsearch_core.ingest.scrapers.pipeline.PoliteClient")
    def test_scrape_company_unverified_default_refuses(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        # Phase 4.1e: verified defaults to False and gates scraping — an
        # unverified call is refused before any fetch instead of returning
        # jobs annotated unverified.
        jobs = scrape_company("TestCorp", "https://careers.testcorp.com", client=mock_client, fetch_descriptions=False)

        assert jobs == []
        mock_client.fetch.assert_not_called()


class TestScrapeCLI:
    def test_scrape_no_args_runs_collection(self, runner, db_url):
        from jobsearch_core.db.session import get_engine

        # Phase 4.0a: bare `scrape` is the confirmed-source collection phase,
        # so no-args runs the collection workflow (mocked here) instead of
        # showing a usage hint.
        #
        # Patch targets matter: collect_workflow (run.py) resolves Settings,
        # load_filters_config from its own module namespace, so patching
        # jobsearch_core.cli.Settings alone does NOT cover it. Scoring is no
        # longer part of collection, so match_jobs is patched to prove it is
        # never reached from a scrape run.
        with (
            patch("jobsearch_core.run.Settings") as mock_settings,
            patch("jobsearch_core.run.match_jobs", return_value=RunSummary(dry_run=False, mode="match")) as mock_match,
            patch("jobsearch_core.ingest.scrapers.pipeline.scrape_company_entries") as mock_entries,
        ):
            mock_settings.return_value.database_url = db_url
            mock_entries.return_value = {
                "companies": 0, "total_added": 0, "total_flagged": 0,
                "total_dup": 0, "total_no_desc": 0, "promoted": 0,
            }
            result = runner.invoke(main, ["scrape"])
            assert result.exit_code == 0, result.output
            assert "Scraping accepted and verified company sources..." in result.output
            # Job search gathers; it does not score.
            assert "Matching collected jobs against the profile..." not in result.output
            mock_match.assert_not_called()
        engine = get_engine()
        engine.dispose()

    def test_scrape_dry_run(self, runner, db_url):
        from jobsearch_core.db.session import get_engine

        with (
            patch("jobsearch_core.cli.Settings") as mock_settings,
            patch("jobsearch_core.ingest.scrapers.pipeline.scrape_company") as mock_scrape,
        ):
            mock_settings.return_value.database_url = db_url
            mock_scrape.return_value = [
                ScrapedJob(
                    url="https://example.com/jobs/1",
                    title="Backend Engineer",
                    company="Corp",
                    location="Helsinki",
                    description="Python developer needed.",
                    verified=True,
                )
            ]
            result = runner.invoke(
                main,
                ["scrape", "https://example.com/careers", "--company", "Corp", "--dry-run"],
            )
            assert result.exit_code == 0
            # Phase 4.1e: a manually passed URL is a human-verified origin, so
            # the job counts as added (verified), not unverified.
            assert "1 added" in result.output
            assert "0 unverified" in result.output
            _, kwargs = mock_scrape.call_args
            assert kwargs["verified"] is True
        engine = get_engine()
        engine.dispose()

    def test_scrape_raw_url_refuses_ats_board(self, runner, db_url):
        """Phase 4.1e: a board URL on the CLI is an explicit refusal, not a
        silent 'No job listings found'."""
        from jobsearch_core.db.session import get_engine

        with (
            patch("jobsearch_core.cli.Settings") as mock_settings,
            patch("jobsearch_core.ingest.scrapers.pipeline.scrape_company") as mock_scrape,
        ):
            mock_settings.return_value.database_url = db_url
            result = runner.invoke(
                main,
                ["scrape", "https://boards.greenhouse.io/acme", "--company", "Acme"],
            )
            assert result.exit_code == 1
            assert "ATS board URL" in result.output
            mock_scrape.assert_not_called()
        engine = get_engine()
        engine.dispose()


class TestScrapeCompanyEntriesPromotion:
    def _seed(self, session, name, status, careers_url="https://example.example/careers"):
        session.add(Company(name=name, status=status, careers_url=careers_url))
        session.commit()

    def test_promotes_accepted_to_known_when_jobs_added(self, session):
        self._seed(session, "AcceptedCo", "accepted")
        stats = {"added": 2, "flagged": 0, "skipped_dup": 0, "no_description": 0, "linked": 2}
        with (
            patch("jobsearch_core.ingest.scrapers.pipeline.scrape_company", return_value=[]),
            patch("jobsearch_core.ingest.scrapers.pipeline.ingest_scraped", return_value=stats),
        ):
            result = scrape_company_entries()

        assert result["companies"] == 1
        assert result["promoted"] == 1
        status = session.query(Company).filter(Company.name == "AcceptedCo").first().status
        assert status == "known"

    def test_does_not_promote_when_no_jobs_added(self, session):
        self._seed(session, "AcceptedCo", "accepted")
        stats = {"added": 0, "flagged": 2, "skipped_dup": 0, "no_description": 0, "linked": 0}
        with (
            patch("jobsearch_core.ingest.scrapers.pipeline.scrape_company", return_value=[]),
            patch("jobsearch_core.ingest.scrapers.pipeline.ingest_scraped", return_value=stats),
        ):
            result = scrape_company_entries()

        assert result["companies"] == 1
        assert result["promoted"] == 0
        status = session.query(Company).filter(Company.name == "AcceptedCo").first().status
        assert status == "accepted"

    def test_dry_run_never_promotes(self, session):
        self._seed(session, "AcceptedCo", "accepted")
        stats = {"added": 2, "flagged": 0, "skipped_dup": 0, "no_description": 0, "linked": 2}
        with (
            patch("jobsearch_core.ingest.scrapers.pipeline.scrape_company", return_value=[]),
            patch("jobsearch_core.ingest.scrapers.pipeline.ingest_scraped", return_value=stats),
        ):
            result = scrape_company_entries(dry_run=True)

        assert result["promoted"] == 0
        status = session.query(Company).filter(Company.name == "AcceptedCo").first().status
        assert status == "accepted"

    def test_known_refresh_keeps_known_without_promotion(self, session):
        self._seed(session, "KnownCo", "known")
        stats = {"added": 1, "flagged": 0, "skipped_dup": 0, "no_description": 0, "linked": 1}
        with (
            patch("jobsearch_core.ingest.scrapers.pipeline.scrape_company", return_value=[]),
            patch("jobsearch_core.ingest.scrapers.pipeline.ingest_scraped", return_value=stats),
        ):
            result = scrape_company_entries(refresh_known=True)

        assert result["companies"] == 1
        assert result["promoted"] == 0
        status = session.query(Company).filter(Company.name == "KnownCo").first().status
        assert status == "known"

    def test_pending_never_scraped(self, session):
        self._seed(session, "PendingCo", "pending")
        with (
            patch("jobsearch_core.ingest.scrapers.pipeline.scrape_company") as mock_scrape,
            patch("jobsearch_core.ingest.scrapers.pipeline.ingest_scraped") as mock_ingest,
        ):
            result = scrape_company_entries()

        assert result["companies"] == 0
        mock_scrape.assert_not_called()
        mock_ingest.assert_not_called()

    def test_accepted_batch_and_refresh_known_combine(self, session):
        self._seed(session, "A", "accepted", careers_url="https://a.example/careers")
        self._seed(session, "B", "accepted", careers_url="https://b.example/careers")
        self._seed(session, "C", "known", careers_url="https://c.example/careers")
        stats = {"added": 0, "flagged": 0, "skipped_dup": 0, "no_description": 0, "linked": 0}
        with (
            patch("jobsearch_core.ingest.scrapers.pipeline.scrape_company", return_value=[]),
            patch("jobsearch_core.ingest.scrapers.pipeline.ingest_scraped", return_value=stats),
        ):
            result = scrape_company_entries(accepted_batch=1, refresh_known=True)

        # accepted_batch limits the accepted set to one; the known row is also scraped.
        assert result["companies"] == 2

    def test_company_ids_scrape_exactly_those_rows_whatever_their_status(self, session):
        self._seed(session, "A", "accepted", careers_url="https://a.example/careers")
        self._seed(session, "P", "pending", careers_url="https://p.example/careers")
        self._seed(session, "K", "known", careers_url="https://k.example/careers")
        ids = {c.name: c.id for c in session.query(Company).all()}
        stats = {"added": 1, "flagged": 0, "skipped_dup": 0, "no_description": 0, "linked": 1}
        with (
            patch("jobsearch_core.ingest.scrapers.pipeline.scrape_company", return_value=[]),
            patch("jobsearch_core.ingest.scrapers.pipeline.ingest_scraped", return_value=stats),
        ):
            result = scrape_company_entries(
                company_ids=[ids["P"], ids["K"]], accepted_batch=1, refresh_known=True
            )

        # A and nothing else is left out; batch/refresh_known are ignored.
        assert result["companies"] == 2
        # Only an accepted row is promoted: the pending row stays pending.
        assert result["promoted"] == 0
        status = session.query(Company).filter(Company.name == "P").first().status
        assert status == "pending"

    def test_scrapes_the_resolved_entry_when_one_is_recorded(self, session):
        session.add(
            Company(
                name="EntryCo",
                status="accepted",
                careers_url="https://entry.example/careers",
                open_positions_url="https://entry.example/avoimet-tyopaikat",
            )
        )
        session.commit()
        stats = {"added": 0, "flagged": 0, "skipped_dup": 0, "no_description": 0, "linked": 0}
        with (
            patch(
                "jobsearch_core.ingest.scrapers.pipeline.scrape_company", return_value=[]
            ) as mock_scrape,
            patch(
                "jobsearch_core.ingest.scrapers.pipeline.ingest_scraped", return_value=stats
            ) as mock_ingest,
        ):
            scrape_company_entries()

        assert mock_scrape.call_args.args[1] == "https://entry.example/avoimet-tyopaikat"
        assert mock_ingest.call_args.kwargs["source_url"] == (
            "https://entry.example/avoimet-tyopaikat"
        )

    def test_falls_back_to_careers_url_without_a_recorded_entry(self, session):
        self._seed(session, "PlainCo", "accepted", careers_url="https://plain.example/careers")
        stats = {"added": 0, "flagged": 0, "skipped_dup": 0, "no_description": 0, "linked": 0}
        with (
            patch(
                "jobsearch_core.ingest.scrapers.pipeline.scrape_company", return_value=[]
            ) as mock_scrape,
            patch("jobsearch_core.ingest.scrapers.pipeline.ingest_scraped", return_value=stats),
        ):
            scrape_company_entries()

        assert mock_scrape.call_args.args[1] == "https://plain.example/careers"


class TestAtsAdapterDispatch:
    GREENHOUSE_PAYLOAD = json.dumps(
        {
            "jobs": [
                {
                    "title": "Backend Engineer",
                    "absolute_url": "https://boards.greenhouse.io/acme/jobs/1001",
                    "location": {"name": "Helsinki, Finland"},
                    "content": "<p>Python developer needed.</p>",
                    "first_published": "2026-08-01T10:00:00Z",
                }
            ],
            "meta": {"total": 1},
        }
    )

    def test_greenhouse_url_uses_json_adapter(self):
        client = _client()
        client.fetch.return_value = self.GREENHOUSE_PAYLOAD

        jobs = _select_and_scrape(
            "https://boards.greenhouse.io/acme", "<html>irrelevant</html>", client
        )

        assert len(jobs) == 1
        assert jobs[0].title == "Backend Engineer"
        client.fetch.assert_called_once()

    def test_adapter_failure_falls_through_to_generic(self, monkeypatch):
        from jobsearch_core.ingest.scrapers import pipeline

        client = _client()
        client.fetch.side_effect = RuntimeError("blocked")
        called = {}

        def fake_generic(self, html, url):
            called["generic"] = True
            return []

        monkeypatch.setattr(pipeline.CareersPageScraper, "scrape_listings", fake_generic)
        jobs = _select_and_scrape("https://boards.greenhouse.io/acme", "<html></html>", client)
        assert jobs == []
        assert called.get("generic") is True

    def test_non_ats_url_skips_json_adapters(self):
        client = MagicMock()
        html = "<html><body><a href='/jobs/backend'>Backend</a></body></html>"
        client.fetch.return_value = html

        def no_json_fetch(url):
            raise AssertionError(f"JSON adapter must not fetch {url} for a non-ATS URL")

        client.fetch.side_effect = no_json_fetch
        jobs = _select_and_scrape("https://careers.testcorp.com", html, client)
        assert len(jobs) == 1
        assert jobs[0].title == "Backend"


class TestAtsProvenance:
    """Phase 4.1e: ATS boards are reached only via employer careers pages."""

    def _seed(self, session, name, status, careers_url="https://example.example/careers"):
        session.add(Company(name=name, status=status, careers_url=careers_url))
        session.commit()

    def test_classify_ats_url_platforms(self):
        from jobsearch_core.ingest.scrapers.pipeline import _classify_ats_url

        assert _classify_ats_url("https://boards.greenhouse.io/acme/jobs/1") == "greenhouse"
        assert _classify_ats_url("https://job-boards.greenhouse.io/acme") == "greenhouse"
        assert _classify_ats_url("https://jobs.lever.co/acme/123") == "lever"
        assert _classify_ats_url("https://jobs.eu.lever.co/acme/1") == "lever"
        assert _classify_ats_url("https://jobs.ashbyhq.com/acme") == "ashby"
        assert _classify_ats_url("https://careers.smartrecruiters.com/acme") == "smartrecruiters"
        assert _classify_ats_url("https://acme.teamtailor.com/jobs") == "teamtailor"
        # Root URLs and non-ATS hosts are not boards.
        assert _classify_ats_url("https://jobs.lever.co") is None
        assert _classify_ats_url("https://example.com/jobs") is None
        assert _classify_ats_url("https://www.greenhouse.io/blog") is None

    def test_find_ats_board_links_extracts_and_dedupes(self):
        from jobsearch_core.ingest.scrapers.pipeline import _find_ats_board_links

        html = (
            '<html><body>'
            '<a href="https://jobs.lever.co/acme">Careers</a>'
            '<a href="https://jobs.lever.co/acme">Careers again</a>'
            '<a href="https://example.com/jobs">Internal</a>'
            '</body></html>'
        )
        links = _find_ats_board_links(html, "https://acme.com/careers")
        assert links == [("lever", "https://jobs.lever.co/acme")]

    def test_find_ats_board_links_meta_refresh(self):
        from jobsearch_core.ingest.scrapers.pipeline import _find_ats_board_links

        html = '<meta http-equiv="refresh" content="0; url=https://acme.teamtailor.com/jobs">'
        links = _find_ats_board_links(html, "https://acme.com/careers")
        assert links == [("teamtailor", "https://acme.teamtailor.com/jobs")]

    def test_scrape_via_ats_links_follows_board(self):
        from jobsearch_core.ingest.scrapers.pipeline import _scrape_via_ats_links

        html = '<a href="https://boards.greenhouse.io/acme">Open positions</a>'
        client = _client()
        client.fetch.return_value = json.dumps(
            {"jobs": [{"title": "Backend Engineer", "absolute_url": "https://boards.greenhouse.io/acme/jobs/7"}]}
        )
        jobs = _scrape_via_ats_links("https://acme.com/careers", html, client)
        assert jobs
        assert all(job.ats_platform == "greenhouse" for job in jobs)
        client.fetch.assert_any_call("https://boards.greenhouse.io/acme")

    def test_scrape_via_ats_links_no_board_noop(self):
        from jobsearch_core.ingest.scrapers.pipeline import _scrape_via_ats_links

        client = MagicMock()
        jobs = _scrape_via_ats_links("https://acme.com/careers", "<html></html>", client)
        assert jobs == []
        client.fetch.assert_not_called()

    def test_scrape_company_follows_careers_page_board_link(self):
        careers_html = '<a href="https://boards.greenhouse.io/acme">Jobs</a>'
        payload = json.dumps(
            {
                "jobs": [{"title": "Backend Engineer", "absolute_url": "https://boards.greenhouse.io/acme/jobs/5"}],
                "meta": {"total": 1},
            }
        )
        client = _client()

        def fake_fetch(url, **_kwargs):
            if url == "https://acme.com/careers":
                return careers_html
            return payload

        client.fetch.side_effect = fake_fetch

        jobs = scrape_company(
            "Acme", "https://acme.com/careers", client=client, fetch_descriptions=False, verified=True
        )

        assert jobs
        assert all(job.ats_platform == "greenhouse" for job in jobs)
        assert all(job.verified is True for job in jobs)

    def test_direct_ats_url_records_platform(self):
        payload = json.dumps(
            {"jobs": [{"title": "Backend Engineer", "absolute_url": "https://boards.greenhouse.io/acme/jobs/9"}]}
        )
        client = _client()
        client.fetch.return_value = payload
        jobs = scrape_company("Acme", "https://boards.greenhouse.io/acme", client=client, fetch_descriptions=False)
        assert all(job.ats_platform == "greenhouse" for job in jobs)

    def test_company_row_gets_ats_provenance(self, session, monkeypatch):
        from jobsearch_core.ingest.scrapers import pipeline

        monkeypatch.setattr(pipeline, "get_session", lambda: session)
        self._seed(session, "Acme", "accepted", careers_url="https://acme.com/careers")
        scraped = [
            ScrapedJob(
                url="https://jobs.lever.co/acme/1",
                title="Backend Engineer",
                company="Acme",
                description="Python work.",
                ats_platform="lever",
            )
        ]
        with (
            patch("jobsearch_core.ingest.scrapers.pipeline.scrape_company", return_value=scraped),
            patch("jobsearch_core.ingest.scrapers.pipeline.ingest_scraped", return_value={
                "added": 1, "flagged": 0, "skipped_dup": 0, "no_description": 0, "linked": 1,
            }),
        ):
            scrape_company_entries()
        company = session.query(Company).filter_by(name="Acme").one()
        session.refresh(company)
        assert company.ats_platform == "lever"

    def test_company_row_without_ats_jobs_keeps_null(self, session, monkeypatch):
        from jobsearch_core.ingest.scrapers import pipeline

        monkeypatch.setattr(pipeline, "get_session", lambda: session)
        self._seed(session, "Beta", "accepted", careers_url="https://beta.example/careers")
        with (
            patch("jobsearch_core.ingest.scrapers.pipeline.scrape_company", return_value=[]),
            patch("jobsearch_core.ingest.scrapers.pipeline.ingest_scraped", return_value={
                "added": 0, "flagged": 0, "skipped_dup": 0, "no_description": 0, "linked": 0,
            }),
        ):
            scrape_company_entries()
        company = session.query(Company).filter_by(name="Beta").one()
        session.refresh(company)
        assert company.ats_platform is None


class TestRecordOpenPositionsEntry:
    """Phase 4.0d: the resolved entry is persisted as one unit with its provenance."""

    def test_writes_url_landing_and_link_text(self, session, monkeypatch):
        from jobsearch_core.ingest.scrapers import pipeline

        monkeypatch.setattr(pipeline, "get_session", lambda: session)
        company = Company(name="EntryCo", status="accepted", careers_url="https://e.example/ura")
        session.add(company)
        session.commit()
        company_id = company.id

        pipeline.record_open_positions_entry(
            company_id,
            EntryResolution(
                url="https://e.example/avoimet-tyopaikat",
                landing_url="https://e.example/ura",
                link_text="Avoimet työpaikat",
                hops=1,
                postings=4,
            ),
        )
        stored = session.get(Company, company_id)
        assert stored.open_positions_url == "https://e.example/avoimet-tyopaikat"
        assert stored.open_positions_landing_url == "https://e.example/ura"
        assert stored.open_positions_link_text == "Avoimet työpaikat"
        # The verified careers page is never rewritten by a descent.
        assert stored.careers_url == "https://e.example/ura"

    def test_missing_company_is_a_no_op(self, session, monkeypatch):
        from jobsearch_core.ingest.scrapers import pipeline

        monkeypatch.setattr(pipeline, "get_session", lambda: session)
        pipeline.record_open_positions_entry(
            999,
            EntryResolution(url="u", landing_url="l", link_text="t", hops=1, postings=1),
        )
        assert session.query(Company).count() == 0


class TestResolveScrapingEntry:
    """Phase 4.0d: the public wrapper wires the production dispatcher and never raises."""

    def test_injects_the_dispatcher_and_returns_the_report(self, monkeypatch):
        from jobsearch_core.ingest.scrapers import pipeline

        captured = {}

        def fake_resolve(url, *, client, scrape, vocabulary, start_html=None, render=None):
            captured.update(
                url=url,
                client=client,
                scrape=scrape,
                vocabulary=vocabulary,
                start_html=start_html,
                render=render,
            )
            return EntryReport(
                resolution=EntryResolution(
                    url="https://e.example/jobs",
                    landing_url=url,
                    link_text="Jobs",
                    hops=1,
                    postings=2,
                ),
                reason="resolved",
            )

        dispatched = []
        monkeypatch.setattr(
            pipeline,
            "_select_and_scrape",
            lambda url, html, client: dispatched.append((url, html)) or [],
        )
        monkeypatch.setattr(pipeline, "resolve_entry", fake_resolve)
        client = MagicMock()

        report = pipeline.resolve_scraping_entry("https://e.example/ura", client=client)

        assert report.resolution is not None and report.resolution.url == "https://e.example/jobs"
        assert captured["client"] is client
        # The browser tier is the retry the descent is allowed to spend.
        assert captured["render"] is client.fetch_rendered
        # The hop judges candidates with the same dispatch the scrape will use.
        captured["scrape"]("https://e.example/jobs", "<html></html>")
        assert dispatched == [("https://e.example/jobs", "<html></html>")]

    def test_page_html_with_postings_keeps_the_entry_unless_a_candidate_wins(
        self, monkeypatch
    ):
        """B2 + item 4: a careers page that is itself the listing (or carries
        JobPosting JSON-LD the dispatcher reads) is the entry at hop 0. The
        descent still probes its links, but an equal count never displaces it."""

        from jobsearch_core.ingest.scrapers import pipeline
        from jobsearch_core.ingest.scrapers.base import ScrapedJob

        fetched = []
        monkeypatch.setattr(
            pipeline,
            "_select_and_scrape",
            lambda url, html, client: [
                ScrapedJob(url="https://e.example/ura/1001", title="Backend developer", company="E")
            ],
        )
        client = MagicMock()
        client.fetch.side_effect = lambda url: fetched.append(url) or "<html></html>"

        report = pipeline.resolve_scraping_entry(
            "https://e.example/ura", client=client, page_html="<html></html>"
        )

        assert report.resolution is not None
        assert report.resolution.url == "https://e.example/ura"
        assert report.resolution.landing_url == "https://e.example/ura"
        assert report.resolution.hops == 0
        assert report.resolution.postings == 1
        assert report.landing_postings == 1
        # The probe ran; nothing beat the landing page, so nothing moved.
        assert client.fetch.called

    def test_page_html_without_postings_is_descended_with_that_html(self, monkeypatch):
        from jobsearch_core.ingest.scrapers import pipeline

        captured = {}

        def fake_resolve(url, *, client, scrape, vocabulary, start_html=None, render=None):
            captured["start_html"] = start_html
            return EntryReport(
                resolution=EntryResolution(
                    url=url + "/jobs", landing_url=url, link_text="Jobs", hops=1, postings=2
                ),
                reason="resolved",
            )

        monkeypatch.setattr(pipeline, "resolve_entry", fake_resolve)
        monkeypatch.setattr(pipeline, "_select_and_scrape", lambda url, html, client: [])

        report = pipeline.resolve_scraping_entry(
            "https://e.example/ura", client=MagicMock(), page_html="<html>landing</html>"
        )

        assert report.resolution is not None
        assert report.resolution.url == "https://e.example/ura/jobs"
        # The descent reuses the page the caller already fetched.
        assert captured["start_html"] == "<html>landing</html>"

    def test_resolution_failure_is_reported_not_raised(self, monkeypatch):
        from jobsearch_core.ingest.scrapers import pipeline

        def boom(*args, **kwargs):
            raise RuntimeError("network down")

        monkeypatch.setattr(pipeline, "resolve_entry", boom)
        report = pipeline.resolve_scraping_entry("https://e.example/ura", client=MagicMock())

        assert report.resolution is None
        assert "network down" in report.reason


class TestSitemapEntryExpansion:
    """P2: a sitemap entry is expanded into its posting pages, never parsed as HTML."""

    SITEMAP = "https://belimo.example/sitemap.xml"

    def _seed(self, session, name="BelimoCo"):
        session.add(
            Company(
                name=name,
                status="accepted",
                careers_url="https://belimo.example/careers",
                open_positions_url=self.SITEMAP,
            )
        )
        session.commit()

    def _locs(self, count: int) -> list[str]:
        return [f"https://belimo.example/job/role-{n}-{1000 + n}/" for n in range(count)]

    def _urlset(self, locs: list[str]) -> str:
        entries = "".join(f"<url><loc>{loc}</loc></url>" for loc in locs)
        return f'<?xml version="1.0"?><urlset>{entries}</urlset>'

    def _posting(self, title: str, body: str | None = None) -> str:
        text = body if body is not None else (
            "We are hiring a backend engineer to build our platform in Helsinki. "
            "You will work with Python, PostgreSQL and the team."
        )
        return (
            f"<html><head><title>{title}</title></head>"
            f"<body><main><h1>{title}</h1><p>{text}</p></main></body></html>"
        )

    def _scrape(self, session, monkeypatch, pages: dict[str, str], locs: list[str]):
        client = MagicMock()
        client.fetch.side_effect = lambda url: pages[url]
        monkeypatch.setattr(
            "jobsearch_core.ingest.scrapers.pipeline.PoliteClient", lambda: client
        )
        stats = {"added": 0, "flagged": 0, "skipped_dup": 0, "no_description": 0, "linked": 0}
        with (
            patch(
                "jobsearch_core.ingest.scrapers.pipeline.scrape_company"
            ) as mock_scrape,
            patch(
                "jobsearch_core.ingest.scrapers.pipeline.ingest_scraped",
                return_value=stats,
            ) as mock_ingest,
        ):
            result = scrape_company_entries()
        return result, mock_scrape, mock_ingest

    def test_expands_postings_with_title_and_description_from_each_page(self, session, monkeypatch):
        self._seed(session)
        locs = self._locs(6)
        pages = {self.SITEMAP: self._urlset(locs)}
        pages.update({loc: self._posting(f"Engineer {index}") for index, loc in enumerate(locs)})

        result, mock_scrape, mock_ingest = self._scrape(session, monkeypatch, pages, locs)

        mock_scrape.assert_not_called()
        jobs = mock_ingest.call_args.args[0]
        assert [job.url for job in jobs] == locs
        assert jobs[0].title == "Engineer 0"
        assert "We are hiring" in (jobs[0].description or "")
        assert jobs[0].authority_url == self.SITEMAP
        assert jobs[0].verified is True
        assert result["sitemap_listed"] == 6
        assert result["sitemap_scraped"] == 6
        assert result["sitemap_truncated"] == 0

    def test_truncation_is_counted_when_the_ceiling_cuts_the_listing(self, session, monkeypatch):
        self._seed(session)
        locs = self._locs(6)
        pages = {self.SITEMAP: self._urlset(locs)}
        pages.update({loc: self._posting(f"Engineer {index}") for index, loc in enumerate(locs)})
        monkeypatch.setattr(
            "jobsearch_core.ingest.scrapers.pipeline.SITEMAP_PAGE_CEILING", 2
        )

        result, _mock_scrape, mock_ingest = self._scrape(session, monkeypatch, pages, locs)

        assert result["sitemap_listed"] == 6
        assert result["sitemap_scraped"] == 2
        assert result["sitemap_truncated"] == 1
        assert len(mock_ingest.call_args.args[0]) == 2

    def test_a_page_without_a_title_or_description_is_dropped_by_reason(self, session, monkeypatch):
        self._seed(session)
        locs = self._locs(6)
        pages = {self.SITEMAP: self._urlset(locs)}
        pages.update({loc: self._posting(f"Engineer {index}") for index, loc in enumerate(locs)})
        pages[locs[1]] = (
            "<html><body><main><p>No headings here at all, just prose.</p></main></body></html>"
        )
        pages[locs[2]] = self._posting("Engineer 2", body="Too short.")

        result, _mock_scrape, mock_ingest = self._scrape(session, monkeypatch, pages, locs)

        assert result["sitemap_listed"] == 6
        assert result["sitemap_scraped"] == 4
        assert result["sitemap_no_title"] == 1
        assert result["sitemap_no_description"] == 1
        scraped = [job.url for job in mock_ingest.call_args.args[0]]
        assert locs[1] not in scraped and locs[2] not in scraped
        assert len(scraped) == 4

    def test_a_stored_posting_page_is_not_fetched_again(self, session, monkeypatch):
        self._seed(session)
        locs = self._locs(6)
        session.add(
            Job(url=locs[1], raw_title="Engineer 1", raw_company="BelimoCo", description="x" * 80)
        )
        session.commit()
        pages = {self.SITEMAP: self._urlset(locs)}
        pages.update({loc: self._posting(f"Engineer {index}") for index, loc in enumerate(locs)})

        result, _mock_scrape, mock_ingest = self._scrape(session, monkeypatch, pages, locs)

        assert result["sitemap_known"] == 1
        assert [job.url for job in mock_ingest.call_args.args[0]] == [loc for n, loc in enumerate(locs) if n != 1]

    def test_an_xml_url_that_answers_html_is_scraped_as_the_page_it_is(self, session, monkeypatch):
        self._seed(session)
        pages = {self.SITEMAP: "<html><body>not a sitemap</body></html>"}
        client = MagicMock()
        client.fetch.side_effect = lambda url: pages[url]
        monkeypatch.setattr(
            "jobsearch_core.ingest.scrapers.pipeline.PoliteClient", lambda: client
        )
        stats = {"added": 0, "flagged": 0, "skipped_dup": 0, "no_description": 0, "linked": 0}
        with (
            patch(
                "jobsearch_core.ingest.scrapers.pipeline.scrape_company", return_value=[]
            ) as mock_scrape,
            patch(
                "jobsearch_core.ingest.scrapers.pipeline.ingest_scraped",
                return_value=stats,
            ),
        ):
            scrape_company_entries()

        mock_scrape.assert_called_once()
        assert mock_scrape.call_args.args[1] == self.SITEMAP


def test_scrape_company_fetches_no_detail_page_for_a_known_posting(session):
    from jobsearch_core.ingest.scrapers.pipeline import _fill_new_descriptions
    from jobsearch_core.normalize import dedup_key, normalize

    title, company, location = "Backend Engineer", "Acme", "Helsinki"
    dkey = dedup_key(*normalize(title, company, location)[:3])
    session.add(
        Job(
            url="https://acme.example/jobs/1",
            raw_title=title,
            raw_company=company,
            dedup_hash=dkey,
            description="x" * 80,
        )
    )
    session.commit()
    known = ScrapedJob(url="https://acme.example/jobs/1", title=title, company=company, location=location)
    fresh = ScrapedJob(url="https://acme.example/jobs/2", title="Data Engineer", company=company, location=location)
    client = MagicMock()
    with patch("jobsearch_core.ingest.scrapers.pipeline.fetch_detail", return_value="d" * 80) as fetch:
        _fill_new_descriptions([known, fresh], client, "https://acme.example/careers")

    assert [call.args[1] for call in fetch.call_args_list] == ["https://acme.example/jobs/2"]
    assert known.description in (None, "")


def test_fill_and_ingest_commits_each_batch_before_the_next_fetch(session, monkeypatch):
    """A run stopped mid-company keeps every finished batch (INGEST_BATCH)."""
    from jobsearch_core.ingest.scrapers import pipeline

    monkeypatch.setattr(pipeline, "INGEST_BATCH", 2)
    jobs = [
        ScrapedJob(
            url=f"https://acme.example/jobs/{i}",
            title=f"Backend Engineer {i}",
            company="Acme",
            location="Helsinki",
            verified=True,
            authority_url="https://acme.example/careers",
        )
        for i in range(5)
    ]
    stored_at_fetch: list[int] = []

    def fake_fetch(client, url):
        session.expire_all()
        stored_at_fetch.append(session.query(Job).count())
        return "d" * 80

    with patch.object(pipeline, "fetch_detail", side_effect=fake_fetch):
        stats = pipeline._fill_and_ingest(
            jobs,
            MagicMock(),
            "https://acme.example/careers",
            fetch_descriptions=True,
            dry_run=False,
            company_id=None,
        )

    assert stats["added"] == 5
    # Fetches 0-1 see 0 rows, 2-3 see 2, 4 sees 4: a batch is stored first.
    assert stored_at_fetch == [0, 0, 2, 2, 4]
