from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from jobsearch_core.db.models import Company
from jobsearch_core.ingest.scrapers.pipeline import (
    scrape_company,
    scrape_company_entries,
)


def _board_payload() -> str:
    import json

    return json.dumps(
        {
            "jobs": [
                {
                    "title": "Backend Engineer",
                    "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
                }
            ],
            "meta": {"total": 1},
        }
    )


def _seed_company(session, name, careers_url, status="accepted"):
    session.add(Company(name=name, status=status, careers_url=careers_url))
    session.commit()
    return session.query(Company).filter(Company.name == name).one()


class TestDirectBoardUrlRejected:
    """A board URL handed straight to scrape_company is not a verified origin."""

    def test_direct_board_url_yields_no_jobs(self):
        client = MagicMock()
        client.fetch.return_value = _board_payload()

        jobs = scrape_company(
            "Acme",
            "https://boards.greenhouse.io/acme",
            client=client,
            fetch_descriptions=False,
            verified=True,
        )

        assert jobs == []

    def test_direct_board_url_no_ats_platform_provenance(self):
        client = MagicMock()
        client.fetch.return_value = _board_payload()

        jobs = scrape_company(
            "Acme",
            "https://jobs.lever.co/acme",
            client=client,
            fetch_descriptions=False,
            verified=True,
        )

        assert jobs == []

    def test_collection_skips_company_stored_with_board_url(self, session, monkeypatch):
        from jobsearch_core.ingest.scrapers import pipeline

        monkeypatch.setattr(pipeline, "get_session", lambda: session)
        _seed_company(session, "Acme", "https://boards.greenhouse.io/acme")

        scraped_calls = []
        with patch(
            "jobsearch_core.ingest.scrapers.pipeline.scrape_company",
            side_effect=lambda *a, **k: scraped_calls.append(a) or [],
        ):
            result = scrape_company_entries()

        assert result["companies"] == 0
        assert scraped_calls == []
        assert result["board_url_rejected"] == 1

    def test_collection_still_scrapes_employer_careers_url(self, session, monkeypatch):
        from jobsearch_core.ingest.scrapers import pipeline

        monkeypatch.setattr(pipeline, "get_session", lambda: session)
        _seed_company(session, "GoodCo", "https://goodco.example/careers")

        with (
            patch(
                "jobsearch_core.ingest.scrapers.pipeline.scrape_company",
                return_value=[],
            ) as mock_scrape,
            patch(
                "jobsearch_core.ingest.scrapers.pipeline.ingest_scraped",
                return_value={
                    "added": 0, "flagged": 0, "skipped_dup": 0,
                    "no_description": 0, "linked": 0,
                },
            ),
        ):
            result = scrape_company_entries()

        assert result["companies"] == 1
        mock_scrape.assert_called_once()

    def test_company_board_skip_does_not_promote_or_ingest(self, session, monkeypatch):
        from jobsearch_core.ingest.scrapers import pipeline

        monkeypatch.setattr(pipeline, "get_session", lambda: session)
        _seed_company(session, "Acme", "https://boards.greenhouse.io/acme")

        with patch(
            "jobsearch_core.ingest.scrapers.pipeline.ingest_scraped"
        ) as mock_ingest:
            scrape_company_entries()

        mock_ingest.assert_not_called()
        status = (
            session.query(Company).filter(Company.name == "Acme").one().status
        )
        assert status == "accepted"


class TestUnverifiedGate:
    """verified=False must gate scraping, not just annotate results."""

    def test_unverified_scrape_is_refused_before_any_fetch(self):
        client = MagicMock()

        jobs = scrape_company(
            "TestCorp",
            "https://careers.testcorp.com",
            client=client,
            fetch_descriptions=False,
            verified=False,
        )

        assert jobs == []
        client.fetch.assert_not_called()


class TestManualAddOriginCheck:
    """add_company must not store an ATS board URL as a verified careers_url."""

    def test_add_company_rejects_board_url(self, session):
        from jobsearch_core.search.discover import add_company

        with pytest.raises(ValueError, match="ATS board URL"):
            add_company("Acme", "https://boards.greenhouse.io/acme")

    def test_add_company_accepts_employer_careers_url(self, session):
        from jobsearch_core.search.discover import add_company

        company = add_company("Acme", "https://acme.example/careers")
        assert company.careers_url == "https://acme.example/careers"
        assert company.status == "known"

    def test_cli_company_add_has_no_ats_platform_option(self, runner):
        """Phase 4.1e: ats_platform is scrape-time provenance — the CLI must
        not offer a manual write path."""
        from jobsearch_core.cli import main

        result = runner.invoke(main, ["company", "add", "--help"])
        assert result.exit_code == 0
        assert "--ats-platform" not in result.output

    def test_api_company_create_has_no_ats_platform_field(self):
        """Phase 4.1e: CompanyCreate must not accept ats_platform; extra
        fields are ignored, so a POST carrying it writes no provenance."""
        from jobsearch_api.schemas import CompanyCreate

        payload = CompanyCreate(
            name="Acme",
            careers_url="https://acme.example/careers",
            ats_platform="greenhouse",  # type: ignore[call-arg]
        )
        assert not hasattr(payload, "ats_platform")


class TestDiscoveryRejectsAtsResults:
    """A search result pointing at an ATS board is not a hiring source."""

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://boards.greenhouse.io/acme", False),
            ("https://jobs.lever.co/acme", False),
            ("https://acme.teamtailor.com/jobs", False),
            ("https://acme.example/careers", True),
        ],
    )
    def test_ats_results_are_not_hiring_sources(self, url, expected):
        from jobsearch_core.search.discover import SearchResult, _is_likely_hiring_source

        assert (
            _is_likely_hiring_source(SearchResult(url=url, title="Acme Careers"))
            is expected
        )
