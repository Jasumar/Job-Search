"""Tests for the Phase 4.3 website-type classifier (ingest/site_class.py)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from jobsearch_core.ingest import site_class as sc
from jobsearch_core.ingest.xhr_capture import XhrCapture

JOBPOSTING_JSON_LD = """
<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@graph":[
  {"@type":"JobPosting","title":"Engineer"},
  {"@type":["JobPosting"],"title":"Designer"}]}
</script></head><body><p>Careers at Example Oy, we are hiring across teams and
you can read more about working with us in the paragraphs below this text.</p></body></html>
"""

LINKED_LISTINGS = """
<html><body><h1>Careers at Example</h1>
<p>Join our team of engineers and designers working on great products across Finland.</p>
<ul>
<li><a href="/jobs/1">Backend Engineer</a></li>
<li><a href="/jobs/2">Frontend Engineer</a></li>
<li><a href="/jobs/3">Data Analyst</a></li>
<li><a href="/jobs/4">Product Manager</a></li>
</ul></body></html>
"""

GREENHOUSE_EMBED = """
<html><body><p>We are hiring. Explore our open roles and apply below today.</p>
<a href="https://boards.greenhouse.io/example">See open roles</a></body></html>
"""

SPA_SHELL = '<html><body><div id="root"></div><script src="/app.js"></script></body></html>'

BOT_CHECK = (
    "<html><body><p>Checking your browser before you access the site. "
    "Please enable JavaScript and cookies to continue, verify you are human.</p></body></html>"
)


def _top(profile: sc.SiteProfile) -> str:
    return profile.ranked[0].site_class


def test_json_ld_jobposting_is_class_b():
    profile = sc.classify_site("https://example.fi/careers", JOBPOSTING_JSON_LD)
    assert _top(profile) == "B"
    assert profile.resolved_class == "B"
    b = next(cs for cs in profile.ranked if cs.site_class == "B")
    assert b.signals == ("json_ld_jobposting:2",)


def test_linked_job_listings_are_class_c():
    profile = sc.classify_site("https://example.fi/careers", LINKED_LISTINGS)
    assert _top(profile) == "C"
    assert profile.resolved_class == "C"


def test_ats_board_url_is_class_a():
    profile = sc.classify_site("https://boards.greenhouse.io/example", "<html></html>")
    assert _top(profile) == "A"
    a = profile.ranked[0]
    assert "ats_url:greenhouse" in a.signals


def test_ats_link_on_careers_page_is_class_a_with_board_evidence():
    profile = sc.classify_site("https://example.fi/careers", GREENHOUSE_EMBED)
    a = next(cs for cs in profile.ranked if cs.site_class == "A")
    assert a.score == pytest.approx(0.9)
    assert any(sig.startswith("ats_board_links:") for sig in a.signals)


def test_spa_shell_ranks_d_first():
    profile = sc.classify_site("https://example.fi/careers", SPA_SHELL)
    assert _top(profile) == "D"
    assert profile.resolved_class == "D"


def test_bot_check_is_class_e():
    profile = sc.classify_site("https://example.fi/careers", BOT_CHECK)
    assert _top(profile) == "E"


def test_ranking_covers_every_class_most_likely_first():
    profile = sc.classify_site("https://example.fi/careers", LINKED_LISTINGS)
    assert {cs.site_class for cs in profile.ranked} == set(sc.CLASSES)
    scores = [cs.score for cs in profile.ranked]
    assert scores == sorted(scores, reverse=True)


def test_close_scores_are_ambiguous_and_unresolved():
    # Synthetic tie: two classes equal, neither clearly wins.
    ranked, ambiguous, unknown = sc._rank(
        [
            sc.ClassScore("A", 0.0),
            sc.ClassScore("B", 0.8),
            sc.ClassScore("C", 0.78),
            sc.ClassScore("D", 0.0),
            sc.ClassScore("E", 0.0),
        ]
    )
    profile = sc.SiteProfile(url="u", ranked=ranked, ambiguous=ambiguous, unknown=unknown)
    assert profile.ambiguous is True
    assert profile.resolved_class is None


def test_low_scores_are_unknown_and_unresolved():
    ranked, ambiguous, unknown = sc._rank([sc.ClassScore(c, 0.1) for c in sc.CLASSES])
    profile = sc.SiteProfile(url="u", ranked=ranked, ambiguous=ambiguous, unknown=unknown)
    assert profile.unknown is True
    assert profile.resolved_class is None


def test_equal_scores_break_ties_in_chain_order():
    ranked, _, _ = sc._rank([sc.ClassScore(c, 0.5) for c in sc.CLASSES])
    assert [cs.site_class for cs in ranked] == list(sc.CLASSES)


class _FakeGate:
    def __init__(self, answers=None, error: Exception | None = None):
        self._answers = answers
        self._error = error
        self.calls = 0

    def ask(self, state, questions):
        self.calls += 1
        if self._error:
            raise self._error
        return self._answers


def _ambiguous_profile() -> sc.SiteProfile:
    ranked, ambiguous, unknown = sc._rank(
        [sc.ClassScore("A", 0.0), sc.ClassScore("B", 0.8), sc.ClassScore("C", 0.78),
         sc.ClassScore("D", 0.0), sc.ClassScore("E", 0.0)]
    )
    return sc.SiteProfile(url="https://x.fi", ranked=ranked, ambiguous=ambiguous, unknown=unknown)


def _answers(scores: dict[str, float]) -> dict:
    return {f"is_class_{c.lower()}": {"noul": s} for c, s in scores.items()}


def test_jev_skipped_for_clear_profile():
    profile = sc.classify_site("https://example.fi/careers", LINKED_LISTINGS)
    gate = _FakeGate(error=AssertionError("must not be called"))
    resolved = sc.resolve_with_jev(profile, LINKED_LISTINGS, gate)
    assert gate.calls == 0
    assert resolved is profile


def test_jev_resolves_tie_with_clear_winner():
    gate = _FakeGate(answers=_answers({"A": 0.0, "B": 0.1, "C": 0.9, "D": 0.1, "E": 0.0}))
    resolved = sc.resolve_with_jev(_ambiguous_profile(), "page", gate)
    assert gate.calls == 1
    assert resolved.jev_consulted is True
    assert resolved.resolved_class == "C"


def test_jev_failure_keeps_deterministic_profile():
    profile = _ambiguous_profile()
    gate = _FakeGate(error=RuntimeError("HTTP 503"))
    resolved = sc.resolve_with_jev(profile, "page", gate)
    assert resolved is profile
    assert resolved.jev_consulted is False
    assert resolved.resolved_class is None


def test_jev_low_confidence_stays_unresolved():
    gate = _FakeGate(answers=_answers({"A": 0.2, "B": 0.3, "C": 0.35, "D": 0.1, "E": 0.0}))
    resolved = sc.resolve_with_jev(_ambiguous_profile(), "page", gate)
    assert resolved.jev_consulted is True
    assert resolved.resolved_class is None


def test_evidence_roundtrips_as_json_ready_dict():
    profile = sc.classify_site("https://example.fi/careers", JOBPOSTING_JSON_LD)
    evidence = profile.as_evidence()
    assert evidence["ranked"][0]["class"] == "B"
    assert evidence["jev_consulted"] is False


SPA_WITH_CAPTCHA_SCRIPT = (
    "<html><body><div id=\"root\"></div>"
    "<script>window.cfg={captchaSiteKey:'x'};</script></body></html>"
)

RENDERED_LISTINGS = """
<html><body><h1>Careers at Example</h1>
<p>Join our team of engineers and designers working on great products across Finland.</p>
<ul>
<li><a href="/jobs/1">Backend Engineer</a></li>
<li><a href="/jobs/2">Frontend Engineer</a></li>
<li><a href="/jobs/3">Data Analyst</a></li>
<li><a href="/jobs/4">Product Manager</a></li>
</ul></body></html>
"""


def test_block_marker_in_script_text_is_not_class_e():
    # Regression: NVIDIA's static shell carries "captcha" only inside a script,
    # which used to score E 0.9 and hide the rendered listings from the gate.
    profile = sc.classify_site("https://example.fi/careers", SPA_WITH_CAPTCHA_SCRIPT)
    assert _top(profile) == "D"


def test_gate_renders_class_d_and_reprobes_rendered_dom():
    calls = []

    def fake_render(url):
        calls.append(url)
        return RENDERED_LISTINGS

    profile = sc.classify_with_render("https://example.fi/careers", SPA_SHELL, fake_render)
    assert calls == ["https://example.fi/careers"]
    assert _top(profile) == "C"
    assert profile.rendered is True


def test_gate_skips_render_for_plain_listing_page():
    def boom(url):
        raise AssertionError("render must not run for a page already classified C")

    profile = sc.classify_with_render("https://example.fi/careers", LINKED_LISTINGS, boom)
    assert _top(profile) == "C"
    assert profile.rendered is False


def test_gate_render_failure_keeps_static_profile():
    def broken(url):
        raise RuntimeError("chromium missing")

    profile = sc.classify_with_render("https://example.fi/careers", SPA_SHELL, broken)
    assert _top(profile) == "D"
    assert profile.rendered is False


LONG_CONFIG_BLOB_NOT_A_BOT_CHECK = (
    "<html><head><title>Careers at Example</title></head><body><h1>Careers</h1>"
    "<div>" + ('{"recaptchaEnabled": true, "captchaProvider": "x", ' * 40) + "}</div>"
    "<ul><li><a href=\"https://example.fi/jobs/1\">Backend Engineer</a></li></ul>"
    "</body></html>"
)

BOT_CHECK_IN_TITLE = (
    "<html><head><title>Just a moment...</title></head>"
    "<body><p>Checking your browser.</p></body></html>"
)


def test_long_block_with_captcha_word_is_not_class_e():
    profile = sc.classify_site("https://example.fi/careers", LONG_CONFIG_BLOB_NOT_A_BOT_CHECK)
    assert _top(profile) != "E"


def test_bot_check_phrase_in_title_is_class_e():
    profile = sc.classify_site("https://example.fi/careers", BOT_CHECK_IN_TITLE)
    assert _top(profile) == "E"


def test_bare_captcha_word_in_short_block_is_not_class_e():
    html = (
        "<html><body><p>Our careers site uses captcha for spam protection on every "
        "application form, and we review each submitted resume by hand before replying.</p>"
        "</body></html>"
    )
    profile = sc.classify_site("https://example.fi/careers", html)
    assert _top(profile) != "E"


# S3 (plan-scraping.md §XHR capture): an XHR capture that replays to the rendered
# postings is class-A evidence; without one the rendered DOM ranks as before.
NVIDIA_FIXTURES = Path(__file__).parent / "fixtures"
NVIDIA_API = "https://jobs.nvidia.com/api/pcsx/search?domain=nvidia.com&query=&location=&start=0"


def _nvidia_rendered_html() -> str:
    rows = json.loads((NVIDIA_FIXTURES / "nvidia_dom_listings.json").read_text())
    links = "".join(f'<li><a href="{r["url"]}">{r["title"]}</a></li>' for r in rows)
    return f"<html><body><h1>Careers at NVIDIA</h1><ul>{links}</ul></body></html>"


def _nvidia_api_capture(*, method: str = "GET", status: int = 200) -> XhrCapture:
    body = (NVIDIA_FIXTURES / "nvidia_pcsx_search.json").read_text()
    return XhrCapture(
        url=NVIDIA_API,
        method=method,
        status=status,
        resource_type="xhr",
        content_type="application/json",
        request_headers=(),
        request_body=None,
        response_body=body,
        response_bytes=len(body),
        body_truncated=False,
        body_sha256=hashlib.sha256(body.encode()).hexdigest(),
    )


def _render_with_xhr(captures):
    def render(url):
        return _nvidia_rendered_html(), list(captures)

    return render


def _no_plain_render(url):
    raise AssertionError("the xhr render path must be used")


def _plain_api_body(url):
    return (NVIDIA_FIXTURES / "nvidia_pcsx_search.json").read_text()


def test_replayable_capture_on_rendered_page_is_class_a():
    profile = sc.classify_with_render(
        "https://jobs.nvidia.com/careers",
        SPA_SHELL,
        fetch_rendered=_no_plain_render,
        fetch_rendered_with_xhr=_render_with_xhr([_nvidia_api_capture()]),
        fetch_plain=_plain_api_body,
    )
    assert _top(profile) == "A"
    assert profile.resolved_class == "A"
    assert profile.rendered is True
    assert profile.replay_reasons == ()
    a = profile.ranked[0]
    assert a.signals == ("xhr_replay:10/10",)


def test_rendered_page_without_xhr_stays_class_c():
    profile = sc.classify_with_render(
        "https://jobs.example.fi/careers",
        SPA_SHELL,
        fetch_rendered=_no_plain_render,
        fetch_rendered_with_xhr=_render_with_xhr([]),
        fetch_plain=_plain_api_body,
    )
    assert _top(profile) == "C"
    assert profile.resolved_class == "C"
    assert profile.replay_reasons == ("no xhr captures on the rendered page",)


def test_unreplayable_capture_is_not_class_a_and_reason_recorded():
    def broken(url):
        raise RuntimeError("replay blocked")

    profile = sc.classify_with_render(
        "https://jobs.nvidia.com/careers",
        SPA_SHELL,
        fetch_rendered=_no_plain_render,
        fetch_rendered_with_xhr=_render_with_xhr([_nvidia_api_capture(method="POST")]),
        fetch_plain=broken,
    )
    assert _top(profile) != "A"
    assert len(profile.replay_reasons) == 1
    assert "needs a request body" in profile.replay_reasons[0]
    assert profile.as_evidence()["replay_reasons"] == list(profile.replay_reasons)


def test_replay_failure_keeps_rendered_verdict_and_reason():
    def broken(url):
        raise RuntimeError("replay blocked")

    profile = sc.classify_with_render(
        "https://jobs.nvidia.com/careers",
        SPA_SHELL,
        fetch_rendered=_no_plain_render,
        fetch_rendered_with_xhr=_render_with_xhr([_nvidia_api_capture()]),
        fetch_plain=broken,
    )
    assert _top(profile) == "C"
    assert "replay request failed" in profile.replay_reasons[0]
