"""position_extraction (benchmark funnel) scorer rules and the careers-page nav guards.

Covers the A3 posting-shape rule in ``benchmarks/funnel/position_extraction/run.py`` and the
production extractor guard that keeps nav/auth/self links out of
``CareersPageScraper`` (2026-10-11 Apple/Google/Nordea/Kesko smoke).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from jobsearch_core.ingest.scrapers.base import ScrapedJob
from jobsearch_core.ingest.scrapers.careers_page import CareersPageScraper

POSITION_EXTRACTION = Path(__file__).resolve().parents[1] / "benchmarks" / "funnel" / "position_extraction"
sys.path.insert(0, str(POSITION_EXTRACTION))
sys.path.insert(0, str(POSITION_EXTRACTION.parent.parent / "reach"))

from run import _posting_obstacle  # noqa: E402


def _job(title: str, url: str, description: str | None = None) -> ScrapedJob:
    return ScrapedJob(url=url, title=title, company="", location=None,
                      authority_url="", verified=True, description=description)


# --- A3 posting-shape rule -------------------------------------------------

LISTING = "https://www.apple.com/careers/us/"


def test_real_posting_passes_without_description():
    job = _job("Human Factors Engineer-ARC",
               "https://jobs.apple.com/en-us/details/200688368/human-factors-engineer-arc")
    assert _posting_obstacle(job, "https://jobs.apple.com/en-us/search") is None


@pytest.mark.parametrize("title", ["Careers at Apple", "Work at Apple", "Sign in", "Vacancies"])
def test_nav_titles_are_rejected(title):
    job = _job(title, "https://www.apple.com/careers/us/work-at-apple.html")
    assert _posting_obstacle(job, LISTING) == "nav_link"


def test_auth_host_is_rejected():
    job = _job("Recommended jobs", "https://accounts.google.com/ServiceLogin?continue=x")
    assert _posting_obstacle(job, "https://www.google.com/about/careers/") == "nav_link"


def test_off_domain_link_is_rejected():
    job = _job("Senior Engineer", "https://example-partner.com/jobs/123")
    assert _posting_obstacle(job, "https://www.apple.com/careers/us/") == "off_domain"


def test_self_link_is_rejected():
    job = _job("Open positions page", "https://www.apple.com/careers/us/")
    assert _posting_obstacle(job, LISTING) == "self_link"


def test_too_thin_title_is_rejected():
    job = _job("AB", "https://jobs.apple.com/en-us/details/1/x")
    assert _posting_obstacle(job, "https://jobs.apple.com/en-us/search") == "too_thin"


def test_description_is_checked_only_when_fetched():
    job = _job("Engineer", "https://jobs.apple.com/en-us/details/123456/x", description="short")
    # A short fetched description goes through classify_text; without one, the
    # title/URL shape alone decides.
    assert _posting_obstacle(job, "https://jobs.apple.com/en-us/search") in (None, "too_thin")
    no_desc = _job("Engineer", "https://jobs.apple.com/en-us/details/123456/x")
    assert _posting_obstacle(no_desc, "https://jobs.apple.com/en-us/search") is None


# --- production extractor guards -------------------------------------------

def _scrape(html: str, base: str) -> list[ScrapedJob]:
    return CareersPageScraper().scrape_listings(html, base)


def test_extractor_drops_nav_and_auth_links_keeps_postings():
    html = """
    <a href="/careers/us/">Careers at Apple</a>
    <a href="/careers/us/work-at-apple.html">Work at Apple</a>
    <a href="https://accounts.google.com/ServiceLogin?continue=/jobs/x">Sign in</a>
    <a href="/jobs/senior-backend-engineer-123">Senior Backend Engineer</a>
    """
    jobs = _scrape(html, "https://www.apple.com/careers/us/")
    urls = [j.url for j in jobs]
    assert urls == ["https://www.apple.com/jobs/senior-backend-engineer-123"]


def test_extractor_drops_listing_page_self_link():
    html = '<a href="https://acme.example/careers/jobs?page=1">Senior Data Engineer</a>'
    assert _scrape(html, "https://acme.example/careers/jobs?page=1") == []


# --- A3 posting signal (2026-10-13: lenient rule passed section labels) -----

GOOGLE_LISTING = "https://www.google.com/about/careers/applications/jobs/results"
NORDEA_LISTING = "https://www.nordea.com/en/careers/open-jobs"


@pytest.mark.parametrize(
    "title, url, listing",
    [
        ("Benefits", "https://www.nordea.com/en/careers/employee-benefits", NORDEA_LISTING),
        ("Analyst coverage", "https://www.nordea.com/en/investors/analyst-coverage", NORDEA_LISTING),
        ("Tech and AI", "https://www.nordea.com/en/news-insights/tech-and-ai", NORDEA_LISTING),
        ("Google Cloud", "https://www.google.com/about/careers/applications/jobs/cloud", GOOGLE_LISTING),
        ("AI at Google", "https://www.google.com/about/careers/applications/jobs/ai", GOOGLE_LISTING),
        ("YouTube", "https://www.google.com/about/careers/applications/jobs/youtube", GOOGLE_LISTING),
        ("Job search", "https://www.google.com/about/careers/applications/jobs/jobs/results", GOOGLE_LISTING),
    ],
)
def test_section_labels_without_posting_signal_are_rejected(title, url, listing):
    assert _posting_obstacle(_job(title, url), listing) == "no_posting_signal"


def test_placeholder_unknown_title_is_rejected_even_with_posting_id():
    # Google card links with no text come through as "Unknown" (careers_page.py fallback).
    job = _job("Unknown", "https://www.google.com/about/careers/applications/jobs/jobs/results/73697523738256070-prod")
    assert _posting_obstacle(job, GOOGLE_LISTING) == "no_title"


def test_numeric_posting_id_passes_even_with_short_title():
    job = _job("Designer", "https://www.google.com/about/careers/applications/jobs/jobs/results/73697523738256070-prod")
    assert _posting_obstacle(job, GOOGLE_LISTING) is None


def test_role_title_under_job_path_passes_without_id():
    job = _job("Senior Backend Engineer", "https://careers.acme.example/jobs/senior-backend-engineer")
    assert _posting_obstacle(job, "https://careers.acme.example/jobs") is None


# --- entry-resolution stage (entry_run.py) ----------------------------------


def _entry_run():
    import importlib

    return importlib.import_module("entry_run")


def test_entry_norm_ignores_www_scheme_query_and_trailing_slash():
    entry_run = _entry_run()
    assert entry_run._norm("https://www.acme.fi/jobs/?page=2") == entry_run._norm(
        "http://acme.fi/jobs"
    )
    assert entry_run._norm("https://acme.fi/jobs") != entry_run._norm("https://acme.fi/careers")


def test_entry_summary_counts_outcomes_and_excludes_unscored():
    entry_run = _entry_run()
    rows = [
        {"key": "a", "outcome": "matched", "rendered": False, "sitemap": None},
        {"key": "b", "outcome": "different", "rendered": True, "sitemap": None},
        {"key": "c", "outcome": "miss", "rendered": False, "sitemap": "https://x/sitemap.xml"},
        {"key": "d", "outcome": "unscored"},
    ]
    summary = entry_run.summarize(rows)
    assert summary["companies"] == 4
    assert summary["scored"] == 3 and summary["unscored"] == 1
    assert (summary["matched"], summary["different"], summary["miss"]) == (1, 1, 1)
    assert summary["resolved"] == 2
    assert summary["resolution_rate"] == pytest.approx(0.667)
    assert summary["match_rate"] == pytest.approx(0.333)
    assert summary["rendered_retries"] == 1 and summary["sitemaps_read"] == 1
