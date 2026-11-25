"""Tests for the class-B JSON-LD JobPosting scraper (plan-scraping.md S5)."""

from __future__ import annotations

import json

from jobsearch_core.ingest.scrapers.json_ld import JsonLdJobPostingScraper
from jobsearch_core.ingest.scrapers.pipeline import _select_and_scrape


def _page(payload: object, extra_html: str = "") -> str:
    return (
        "<html><head>"
        f'<script type="application/ld+json">{json.dumps(payload)}</script>'
        f"</head><body>{extra_html}</body></html>"
    )


def test_matches_requires_jobposting_node():
    scraper = JsonLdJobPostingScraper()
    assert scraper.matches("https://acme.example/careers", _page({"@type": "Organization"})) is False
    assert scraper.matches("https://acme.example/careers", _page({"@type": "JobPosting", "title": "x"})) is True
    assert scraper.matches("https://acme.example/careers", None) is False


def test_extracts_postings_from_graph_and_lists():
    payload = {
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "WebPage", "name": "Careers"},
            {
                "@type": "JobPosting",
                "title": "Data Engineer",
                "url": "https://acme.example/jobs/1",
                "datePosted": "2026-10-01",
                "employmentType": "FULL_TIME",
                "jobLocation": {"@type": "Place", "address": {"addressLocality": "Helsinki", "addressCountry": "FI"}},
                "description": "<p>Build <b>pipelines</b>.</p>",
            },
        ],
    }
    extra = {"@type": ["JobPosting"], "name": "QA Lead", "@id": "/jobs/2"}
    html = _page(payload) + f'<script type="application/ld+json">{json.dumps([extra])}</script>'
    jobs = JsonLdJobPostingScraper().scrape_listings(html, "https://acme.example/careers")

    assert [j.title for j in jobs] == ["Data Engineer", "QA Lead"]
    first = jobs[0]
    assert first.url == "https://acme.example/jobs/1"
    assert first.location == "Helsinki, FI"
    assert first.posted_date == "2026-10-01"
    assert first.employment_type == "FULL_TIME"
    assert first.description == "Build pipelines ."
    assert first.verified is True
    assert jobs[1].url == "https://acme.example/jobs/2"


def test_skips_postings_without_title_or_url_and_duplicates():
    payload = [
        {"@type": "JobPosting", "title": "No URL"},
        {"@type": "JobPosting", "url": "https://acme.example/jobs/3"},
        {"@type": "JobPosting", "title": "Kept", "url": "https://acme.example/jobs/4"},
        {"@type": "JobPosting", "title": "Kept again", "url": "https://acme.example/jobs/4"},
        {"@type": "JobPosting", "title": "Non-http", "url": "javascript:void(0)"},
    ]
    jobs = JsonLdJobPostingScraper().scrape_listings(_page(payload), "https://acme.example/careers")
    assert [j.title for j in jobs] == ["Kept"]


def test_malformed_json_block_is_ignored():
    html = '<script type="application/ld+json">{not json</script>'
    scraper = JsonLdJobPostingScraper()
    assert scraper.matches("https://acme.example", html) is False
    assert scraper.scrape_listings(html, "https://acme.example") == []


def test_pipeline_prefers_json_ld_over_generic_links():
    payload = {"@type": "JobPosting", "title": "Platform Engineer", "url": "https://acme.example/jobs/9"}
    html = _page(payload, extra_html='<a href="/jobs/other">Other role</a>')
    jobs = _select_and_scrape("https://acme.example/careers", html, client=None)  # type: ignore[arg-type]
    assert [j.title for j in jobs] == ["Platform Engineer"]
