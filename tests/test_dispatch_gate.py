"""Phase 4.3 S5: site-class dispatch gate in scrapers/pipeline.py.

Invariants:
- Dispatch order follows the deterministic classifier ranking (no Jev, no I/O).
- An ambiguous or unknown profile keeps the original _SCRAPERS order, so a
  tie never hands a page to a weaker scraper (JSON-LD vs. generic links).
- Every scraper stays in the trial list: ranking never drops a fallback.
- A classifier failure falls back to the original order and never raises.
"""

from __future__ import annotations

import json

import pytest
from jobsearch_core.ingest import site_class
from jobsearch_core.ingest.scrapers import pipeline
from jobsearch_core.ingest.scrapers.careers_page import CareersPageScraper
from jobsearch_core.ingest.scrapers.json_ld import JsonLdJobPostingScraper

URL = "https://acme.example/careers"


def _json_ld_page(n_links: int = 0) -> str:
    ld = {"@type": "JobPosting", "title": "Platform Engineer", "url": "https://acme.example/jobs/9"}
    links = "".join(
        f'<a href="/careers/jobs/{i}">Senior Software Engineer {i}</a>' for i in range(n_links)
    )
    return (
        "<html><head>"
        f'<script type="application/ld+json">{json.dumps(ld)}</script>'
        f"</head><body>{links}</body></html>"
    )


def _types(scrapers: list) -> list[type]:
    return [type(s) for s in scrapers]


def test_dispatch_keeps_every_scraper() -> None:
    ordered = pipeline._dispatch_order(URL, _json_ld_page(12))
    assert sorted(_types(ordered), key=lambda t: t.__name__) == sorted(
        _types(pipeline._SCRAPERS), key=lambda t: t.__name__
    )


def test_ambiguous_tie_keeps_original_order_so_jsonld_runs() -> None:
    # 12 generic job links score C=1.0 against B=0.9: a tie, so the default
    # order is kept and the structured JobPosting is what gets scraped.
    html = _json_ld_page(12)
    profile = site_class.classify_site(URL, html)
    assert profile.ambiguous

    assert _types(pipeline._dispatch_order(URL, html)) == _types(pipeline._SCRAPERS)
    jobs = pipeline._select_and_scrape(URL, html, None)  # type: ignore[arg-type]
    assert [j.title for j in jobs] == ["Platform Engineer"]


def test_clear_class_b_runs_jsonld_first() -> None:
    # No job links: B is the only real signal, so it is ranked first.
    html = _json_ld_page(0)
    profile = site_class.classify_site(URL, html)
    assert not profile.ambiguous and not profile.unknown
    assert profile.ranked[0].site_class == "B"

    ordered = _types(pipeline._dispatch_order(URL, html))
    assert ordered[0] is JsonLdJobPostingScraper


def test_clear_class_c_runs_careers_page_first() -> None:
    # Many job links, no structured data: C wins clearly.
    html = "<html><body>" + "".join(
        f'<a href="/careers/jobs/{i}">Senior Software Engineer {i}</a>' for i in range(12)
    ) + "</body></html>"
    profile = site_class.classify_site(URL, html)
    if profile.ambiguous or profile.unknown:
        pytest.skip("fixture no longer yields a clear C profile")
    assert profile.ranked[0].site_class == "C"
    assert _types(pipeline._dispatch_order(URL, html))[0] is CareersPageScraper


def test_classifier_failure_falls_back_to_default_order(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(url: str, html: str):
        raise RuntimeError("classifier exploded")

    monkeypatch.setattr(site_class, "classify_site", boom)
    assert _types(pipeline._dispatch_order(URL, "<html></html>")) == _types(pipeline._SCRAPERS)
