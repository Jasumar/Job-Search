from __future__ import annotations

import json

import pytest
from jobsearch_core.ingest.scrapers.ats_json import (
    AshbyScraper,
    GreenhouseScraper,
    LeverScraper,
    SmartRecruitersScraper,
    TeamtailorScraper,
)


class FakeApiClient:
    """Stands in for PoliteClient: maps a URL substring to a canned payload."""

    def __init__(self, routes: dict[str, object]):
        self.routes = routes
        self.calls: list[str] = []

    def fetch_json(self, url: str) -> str:
        self.calls.append(url)
        for key, value in self.routes.items():
            if key in url:
                if isinstance(value, Exception):
                    raise value
                if isinstance(value, str):
                    return value
                return json.dumps(value)
        raise RuntimeError(f"no route for {url}")

    def fetch(self, url: str) -> str:
        """Same bodies for callers that only want the text (detail fetches)."""
        return self.fetch_json(url)


GREENHOUSE_JOB = {
    "title": "Backend Engineer",
    "absolute_url": "https://boards.greenhouse.io/acme/jobs/1001",
    "location": {"name": "Helsinki, Finland"},
    "content": "<h2>About</h2><p>We build Python services for our platform.</p>",
    "first_published": "2026-08-01T10:00:00-04:00",
    "updated_at": "2026-08-20T10:00:00-04:00",
}

LEVER_POSTING = {
    "text": "Data Engineer",
    "hostedUrl": "https://jobs.lever.co/acme/abaf60d3",
    "applyUrl": "https://jobs.lever.co/acme/abaf60d3/apply",
    "categories": {"commitment": "Full Time", "location": "Remote", "team": "Data"},
    "createdAt": 1686151633394,
    "descriptionPlain": "We are hiring a data engineer.",
    "description": "<p>We are hiring a data engineer.</p>",
    "workplaceType": "remote",
}

ASHBY_JOB = {
    "id": "8fb1615c",
    "title": "Platform Engineer",
    "employmentType": "FullTime",
    "location": "Stockholm",
    "isListed": True,
    "publishedAt": "2026-03-12T16:38:15.322+00:00",
    "jobUrl": "https://jobs.ashbyhq.com/acme/8fb1615c",
    "applyUrl": "https://jobs.ashbyhq.com/acme/8fb1615c/application",
    "descriptionHtml": "<p>Infrastructure work.</p>",
    "descriptionPlain": "Infrastructure work.",
    "compensation": {"compensationTierSummary": "5000-6500 EUR"},
}


class TestGreenhouseScraper:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://boards.greenhouse.io/acme", True),
            ("https://job-boards.eu.greenhouse.io/acme", True),
            ("https://boards.greenhouse.io/embed/job_board?for=acme", True),
            ("https://boards.greenhouse.io", False),
            ("https://jobs.lever.co/acme", False),
            ("https://greenhouse.io/acme", False),
        ],
    )
    def test_matches(self, url, expected):
        assert GreenhouseScraper().matches(url, "<html></html>") is expected

    def test_parse_jobs(self):
        client = FakeApiClient({"boards-api.greenhouse.io": {"jobs": [GREENHOUSE_JOB], "meta": {"total": 1}}})
        jobs = GreenhouseScraper().scrape_listings_with_client("", "https://boards.greenhouse.io/acme", client)
        assert len(jobs) == 1
        job = jobs[0]
        assert job.title == "Backend Engineer"
        assert job.url == "https://boards.greenhouse.io/acme/jobs/1001"
        assert job.location == "Helsinki, Finland"
        assert job.posted_date == "2026-08-01"
        assert "We build Python services" in job.description

    def test_paginates_until_total_reached(self):
        payload_p1 = {"jobs": [GREENHOUSE_JOB, GREENHOUSE_JOB], "meta": {"total": 4}}
        payload_p2 = {"jobs": [GREENHOUSE_JOB, GREENHOUSE_JOB], "meta": {"total": 4}}
        client = FakeApiClient({"page=1": payload_p1, "page=2": payload_p2})
        jobs = GreenhouseScraper().scrape_listings_with_client("", "https://boards.greenhouse.io/acme", client)
        assert len(jobs) == 4
        assert len(client.calls) == 2

    def test_fetch_failure_returns_empty(self):
        client = FakeApiClient({"boards-api.greenhouse.io": RuntimeError("blocked")})
        jobs = GreenhouseScraper().scrape_listings_with_client("", "https://boards.greenhouse.io/acme", client)
        assert jobs == []

    def test_scrape_listings_requires_client(self):
        with pytest.raises(RuntimeError, match="requires a PoliteClient"):
            GreenhouseScraper().scrape_listings("<html></html>", "https://boards.greenhouse.io/acme")


class TestLeverScraper:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://jobs.lever.co/acme", True),
            ("https://jobs.eu.lever.co/acme", True),
            ("https://jobs.lever.co", False),
            ("https://boards.greenhouse.io/acme", False),
        ],
    )
    def test_matches(self, url, expected):
        assert LeverScraper().matches(url, "<html></html>") is expected

    def test_parse_postings(self):
        client = FakeApiClient({"api.lever.co": [LEVER_POSTING]})
        jobs = LeverScraper().scrape_listings_with_client("", "https://jobs.lever.co/acme", client)
        assert len(jobs) == 1
        job = jobs[0]
        assert job.title == "Data Engineer"
        assert job.url == "https://jobs.lever.co/acme/abaf60d3"
        assert job.location == "Remote"
        assert job.posted_date == "2023-06-07"
        assert job.employment_type == "Full Time"
        assert job.description == "We are hiring a data engineer."

    def test_unexpected_payload_returns_empty(self):
        client = FakeApiClient({"api.lever.co": {"oops": True}})
        jobs = LeverScraper().scrape_listings_with_client("", "https://jobs.lever.co/acme", client)
        assert jobs == []

    def test_fetch_failure_returns_empty(self):
        client = FakeApiClient({"api.lever.co": RuntimeError("blocked")})
        jobs = LeverScraper().scrape_listings_with_client("", "https://jobs.lever.co/acme", client)
        assert jobs == []


class TestAshbyScraper:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://jobs.ashbyhq.com/acme", True),
            ("https://jobs.ashbyhq.com/acme/8fb1615c", True),
            ("https://jobs.ashbyhq.com/archive", False),
            ("https://jobs.ashbyhq.com", False),
            ("https://jobs.lever.co/acme", False),
        ],
    )
    def test_matches(self, url, expected):
        assert AshbyScraper().matches(url, "<html></html>") is expected

    def test_parse_jobs(self):
        client = FakeApiClient({"posting-api/job-board/acme": {"jobs": [ASHBY_JOB], "apiVersion": "1"}})
        jobs = AshbyScraper().scrape_listings_with_client("", "https://jobs.ashbyhq.com/acme", client)
        assert len(jobs) == 1
        job = jobs[0]
        assert job.title == "Platform Engineer"
        assert job.url == "https://jobs.ashbyhq.com/acme/8fb1615c"
        assert job.location == "Stockholm"
        assert job.posted_date == "2026-03-12"
        assert job.employment_type == "FullTime"
        assert job.salary == "5000-6500 EUR"
        assert job.description == "Infrastructure work."

    def test_unlisted_jobs_skipped(self):
        unlisted = dict(ASHBY_JOB, isListed=False)
        client = FakeApiClient({"posting-api/job-board/acme": {"jobs": [unlisted, ASHBY_JOB]}})
        jobs = AshbyScraper().scrape_listings_with_client("", "https://jobs.ashbyhq.com/acme", client)
        assert len(jobs) == 1


class TestSmartRecruitersScraper:
    def _scraper(self):
        return SmartRecruitersScraper()

    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://careers.smartrecruiters.com/acme", True),
            ("https://jobs.smartrecruiters.com/acme/123-title", True),
            ("https://careers.smartrecruiters.com", False),
            ("https://jobs.lever.co/acme", False),
        ],
    )
    def test_matches(self, url, expected):
        assert self._scraper().matches(url, "<html></html>") is expected

    LISTING = {
        "offset": 0,
        "limit": 100,
        "totalFound": 2,
        "content": [
            {
                "id": "744000146134319",
                "name": "eCommerce Vendor Management Lead",
                "releasedDate": "2026-08-28T08:10:53.368Z",
                "location": {"city": "Helsinki", "country": "fi", "fullLocation": "Helsinki, Finland"},
                "typeOfEmployment": {"id": "permanent", "label": "Full-time"},
                "experienceLevel": {"id": "mid_senior_level", "label": "Mid-Senior Level"},
            },
            {
                "id": "744000146134320",
                "name": "Backend Developer",
                "releasedDate": "2026-08-27T08:10:53.368Z",
                "location": {"city": "Tallinn", "country": "ee", "fullLocation": "Tallinn, Estonia"},
            },
        ],
    }

    DETAIL = {
        "id": "744000146134319",
        "jobAd": {
            "sections": {
                "companyDescription": {"title": "Company Description", "text": "<p>Acme corp.</p>"},
                "jobDescription": {"title": "Job Description", "text": "<p>Python developer needed.</p>"},
                "qualifications": {"title": "Qualifications", "text": "<ul><li>3 years experience</li></ul>"},
            }
        },
    }

    def test_parse_and_detail_fetch(self):
        routes = {
            "postings?limit=100&offset=0": self.LISTING,
            "postings/744000146134319": self.DETAIL,
            "postings/744000146134320": self.DETAIL,
        }
        client = FakeApiClient(routes)
        jobs = self._scraper().scrape_listings_with_client("", "https://careers.smartrecruiters.com/acme", client)
        assert len(jobs) == 2
        first = jobs[0]
        assert first.url == "https://jobs.smartrecruiters.com/acme/744000146134319"
        assert first.title == "eCommerce Vendor Management Lead"
        assert first.location == "Helsinki, Finland"
        assert first.posted_date == "2026-08-28"
        assert first.employment_type == "Full-time"
        assert first.seniority == "Mid-Senior Level"
        assert "Python developer needed." in first.description
        assert "3 years experience" in first.description
        # one listing request + one detail request per job
        assert len(client.calls) == 3

    def test_paginates_with_page_size(self):
        scraper = self._scraper()
        scraper._PAGE_SIZE = 2
        page1 = dict(self.LISTING, offset=0, limit=2, totalFound=4)
        page2 = {
            "offset": 2,
            "limit": 2,
            "totalFound": 4,
            "content": [
                {"id": "744000146134321", "name": "DevOps Engineer", "location": {}},
                {"id": "744000146134322", "name": "QA Engineer", "location": {}},
            ],
        }
        routes = {
            "postings?limit=2&offset=0": page1,
            "postings?limit=2&offset=2": page2,
            "postings/744000146134319": self.DETAIL,
            "postings/744000146134320": self.DETAIL,
            "postings/744000146134321": self.DETAIL,
            "postings/744000146134322": self.DETAIL,
        }
        client = FakeApiClient(routes)
        jobs = scraper.scrape_listings_with_client("", "https://careers.smartrecruiters.com/acme", client)
        assert len(jobs) == 4
        listing_calls = [c for c in client.calls if "offset=" in c]
        assert len(listing_calls) == 2

    def test_detail_failure_leaves_description_none(self):
        routes = {
            "postings?limit=100&offset=0": self.LISTING,
            "postings/744000146134319": RuntimeError("blocked"),
            "postings/744000146134320": RuntimeError("blocked"),
        }
        client = FakeApiClient(routes)
        jobs = self._scraper().scrape_listings_with_client("", "https://careers.smartrecruiters.com/acme", client)
        assert len(jobs) == 2
        assert all(job.description is None for job in jobs)


class TestTeamtailorScraper:
    ITEM = {
        "id": "41467195",
        "title": "Backend Developer",
        "url": "https://acme.teamtailor.com/jobs/8265225-backend-developer",
        "date_published": "2026-08-25T11:04:49+02:00",
        "content_html": "<p><span>We build the bank of the future.</span></p>",
        "_jobposting": {"@type": "JobPosting", "description": "<p>Full description here.</p>"},
    }

    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://acme.teamtailor.com", True),
            ("https://acme.teamtailor.com/jobs", True),
            ("https://teamtailor.com", False),
            ("https://jobs.lever.co/acme", False),
        ],
    )
    def test_matches(self, url, expected):
        assert TeamtailorScraper().matches(url, "<html></html>") is expected

    def test_parse_items(self):
        client = FakeApiClient({"jobs.json?page=1": {"items": [self.ITEM]}, "jobs.json?page=2": {"items": []}})
        jobs = TeamtailorScraper().scrape_listings_with_client("", "https://acme.teamtailor.com/jobs", client)
        assert len(jobs) == 1
        job = jobs[0]
        assert job.title == "Backend Developer"
        assert job.url == "https://acme.teamtailor.com/jobs/8265225-backend-developer"
        assert job.posted_date == "2026-08-25"
        assert "We build the bank of the future." in job.description
        assert len(client.calls) == 2

    def test_content_html_preferred_over_jobposting_description(self):
        client = FakeApiClient({"jobs.json?page=1": {"items": [self.ITEM]}})
        jobs = TeamtailorScraper().scrape_listings_with_client("", "https://acme.teamtailor.com/jobs", client)
        assert jobs[0].description == "We build the bank of the future."

    def test_jobposting_description_fallback(self):
        item = dict(self.ITEM, content_html="")
        client = FakeApiClient({"jobs.json?page=1": {"items": [item]}})
        jobs = TeamtailorScraper().scrape_listings_with_client("", "https://acme.teamtailor.com/jobs", client)
        assert jobs[0].description == "Full description here."

    def test_fetch_failure_returns_empty(self):
        client = FakeApiClient({"jobs.json?page=1": RuntimeError("blocked")})
        jobs = TeamtailorScraper().scrape_listings_with_client("", "https://acme.teamtailor.com/jobs", client)
        assert jobs == []


class TestScraperContract:
    @pytest.mark.parametrize(
        "scraper,url",
        [
            (GreenhouseScraper(), "https://boards.greenhouse.io/acme"),
            (LeverScraper(), "https://jobs.lever.co/acme"),
            (AshbyScraper(), "https://jobs.ashbyhq.com/acme"),
            (SmartRecruitersScraper(), "https://careers.smartrecruiters.com/acme"),
            (TeamtailorScraper(), "https://acme.teamtailor.com/jobs"),
        ],
    )
    def test_requires_client_flag_and_scrape_listings_guard(self, scraper, url):
        assert scraper.requires_client is True
        with pytest.raises(RuntimeError, match="requires a PoliteClient"):
            scraper.scrape_listings("<html></html>", url)
