"""Tests for the S2 replay check (plan-scraping.md §XHR capture, Phase 4.3 S2).

Fixtures are real NVIDIA data: tests/fixtures/nvidia_pcsx_search.json (the captured
/api/pcsx/search page, 10 positions of 2694) and nvidia_dom_listings.json (the 20
postings the rendered DOM shows). No network: fetch_plain is a fake.
"""

from __future__ import annotations

import json
from pathlib import Path

from jobsearch_core.ingest.replay import parse_job_list, replay_capture
from jobsearch_core.ingest.scrapers.base import ScrapedJob
from jobsearch_core.ingest.xhr_capture import XhrCapture

FIXTURES = Path(__file__).parent / "fixtures"
NVIDIA_API = "https://jobs.nvidia.com/api/pcsx/search?domain=nvidia.com&query=&location=&start=0"
NVIDIA_HOST = "jobs.nvidia.com"


def _dom_postings() -> list[ScrapedJob]:
    rows = json.loads((FIXTURES / "nvidia_dom_listings.json").read_text())
    return [ScrapedJob(url=r["url"], title=r["title"], company="") for r in rows]


def _api_body() -> str:
    return (FIXTURES / "nvidia_pcsx_search.json").read_text()


def _capture(url: str = NVIDIA_API, *, method: str = "GET", status: int = 200, truncated: bool = False) -> XhrCapture:
    return XhrCapture(
        url=url,
        method=method,
        status=status,
        resource_type="xhr",
        content_type="application/json",
        request_headers=(),
        request_body=None,
        response_body=_api_body(),
        response_bytes=len(_api_body()),
        body_truncated=truncated,
    )


def _run(capture: XhrCapture, fetch):
    return replay_capture(
        capture,
        dom_postings=_dom_postings(),
        company_host=NVIDIA_HOST,
        fetch_plain=fetch,
    )


def test_nvidia_capture_replays_to_dom_and_is_class_a_candidate():
    result = _run(_capture(), lambda url: _api_body())
    assert result.replay_matches is True
    assert result.class_a_candidate is True
    assert result.reason is None
    assert result.replayed_count == 10
    assert result.matched_count == 10
    assert len(result.sample_titles) == 5


def test_parse_job_list_extracts_nvidia_positions_with_absolute_urls():
    jobs = parse_job_list(_api_body(), NVIDIA_API)
    assert len(jobs) == 10
    assert jobs[0].url == "https://jobs.nvidia.com/careers/job/893382452913"
    assert jobs[0].title == "Senior Software and System Architect"


def test_non_json_replay_stays_not_a_candidate_with_reason():
    result = _run(_capture(), lambda url: "<html><body>Please sign in</body></html>")
    assert result.replay_matches is False
    assert result.class_a_candidate is False
    assert "not a job list" in result.reason


def test_replay_needing_token_is_rejected_when_plain_fetch_raises():
    def needs_token(url):
        raise RuntimeError("HTTP 401 for " + url)

    result = _run(_capture(), needs_token)
    assert result.class_a_candidate is False
    assert "HTTP 401" in result.reason


def test_replay_count_differing_from_dom_is_not_a_candidate():
    # A replay whose postings are not on the rendered page (e.g. a different
    # region's list) must stay non-A, with the count of unmatched postings recorded.
    other = json.loads(_api_body())
    for position in other["data"]["positions"]:
        position["positionUrl"] = "/careers/job/" + str(position["id"] + 1)
    result = _run(_capture(), lambda url: json.dumps(other))
    assert result.class_a_candidate is False
    assert result.reason == "no replayed posting appears in the rendered DOM"
    assert result.replayed_count == 10 and result.matched_count == 0


def test_title_mismatch_is_not_a_candidate():
    other = json.loads(_api_body())
    other["data"]["positions"][0]["name"] = "Completely Different Role"
    result = _run(_capture(), lambda url: json.dumps(other))
    assert result.class_a_candidate is False
    assert "differ from the rendered DOM" in result.reason


def test_capture_outside_company_domain_is_never_replayed():
    calls = []

    def fetch(url):
        calls.append(url)
        return _api_body()

    result = _run(_capture("https://evil.example.com/api/jobs"), fetch)
    assert result.class_a_candidate is False
    assert "outside the company domain" in result.reason
    assert calls == []


def test_post_capture_is_not_replayed():
    calls = []
    result = _run(_capture(method="POST"), lambda url: calls.append(url) or _api_body())
    assert result.class_a_candidate is False
    assert "needs a request body" in result.reason
    assert calls == []


def test_truncated_capture_is_not_replayed():
    result = _run(_capture(truncated=True), lambda url: _api_body())
    assert result.class_a_candidate is False
    assert "truncated" in result.reason


def test_non_200_capture_is_not_replayed():
    result = _run(_capture(status=403), lambda url: _api_body())
    assert result.class_a_candidate is False
    assert "status 403" in result.reason
