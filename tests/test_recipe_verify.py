"""R3: replay statistics, DOM agreement, the posting-existence gate, stop rule.

Properties under test:
- A statistic is a router: it names a field/parameter to look at and never
  rejects a replay, including the "24 postings all titled Apply" case.
- DOM agreement is a subset check on first-page postings; a rendered page with
  job links and no replayed ones is a navigation failure, not an empty board.
- The gate sees only the replay's own items (worst-looking first), never the
  reference; a "none" without per-item reasons, or about a URL it was not shown,
  is unusable.
- A "none" flags immediately but stops only with an independent agreement.
"""

from __future__ import annotations

import json

import pytest
from jobsearch_core.ingest.recipe import ApiRecipe, DomRecipe, run_api_recipe_report
from jobsearch_core.ingest.recipe_verify import (
    EMPTY_BOARD,
    NAVIGATION_FAILURE,
    UNVERIFIABLE,
    GateError,
    adversarial_selection,
    body_hash,
    build_gate_prompt,
    classify_empty_replay,
    collect_statistics,
    dom_agreement,
    run_posting_gate,
    should_run_gate,
    signals,
    stop_decision,
    structural_empty,
)
from jobsearch_core.ingest.scrapers.base import ScrapedJob

from tests.test_ats_json import FakeApiClient

GATE_MODEL = "test/gate-model"


def job(url: str, title: str = "Backend Engineer", **fields) -> ScrapedJob:
    return ScrapedJob(url=url, title=title, company="Acme", **fields)


def api_recipe(**overrides) -> ApiRecipe:
    body = {
        "kind": "api",
        "allowed_hosts": ["boards-api.greenhouse.io"],
        "verification": {
            "expected_count": 2,
            "sample_titles": ["Backend Engineer"],
            "url_pattern": "https://boards.greenhouse.io/acme/jobs/",
        },
        "vars": {"token": "acme"},
        "url_template": "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?page={page}",
        "pagination": {"kind": "page", "param": "page", "start": 1, "step": 1, "max_pages": 5},
        "list_path": ["jobs"],
        "field_map": {
            "url": {"path": ["absolute_url"]},
            "title": {"path": ["title"]},
            "location": {"path": ["location", "name"]},
        },
    }
    body.update(overrides)
    if body["pagination"].get("kind", "none") == "none":
        # No pager means nothing substitutes {page}: leaving it in the template
        # would send a literal brace to the request URL (and is now rejected).
        body["url_template"] = body["url_template"].replace("&page={page}", "").replace("?page={page}", "")
    return ApiRecipe.model_validate(body)


def dom_recipe(**overrides) -> DomRecipe:
    body = {
        "kind": "dom",
        "allowed_hosts": ["careers.example.com"],
        "verification": {
            "expected_count": 2,
            "sample_titles": ["Role 0"],
            "url_pattern": r"https://careers\.example\.com/job/\d+",
        },
        "container": "li.job",
        "link": {"selector": "a", "attr": "href"},
        "title": {"selector": "h3"},
        "location": {"selector": "span.loc"},
    }
    body.update(overrides)
    return DomRecipe.model_validate(body)


def listing_html(*links: str, extra: str = "") -> str:
    items = "".join(f'<li class="job"><a href="{href}"><h3>Role {i}</h3></a></li>' for i, href in enumerate(links))
    return f'<html><body><ul>{items}{extra}</ul></body></html>'


class FakeCompleter:
    """Stands in for LLMClient: records prompts, replays one canned answer."""

    def __init__(self, answer=None, raw: str | None = None):
        self.model = GATE_MODEL
        self.answer = answer
        self.raw = raw
        self.calls: list[tuple[str, str]] = []

    def complete_json(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        if self.raw is not None:
            return self.raw
        return json.dumps(self.answer)


# --- body hash ---------------------------------------------------------------


def test_body_hash_ignores_key_order_and_whitespace():
    first = {"kind": "dom", "container": "li.job", "title": {"selector": "h3"}}
    second = {"title": {"selector": "h3"}, "container": "li.job", "kind": "dom"}
    assert body_hash(first) == body_hash(second)
    assert body_hash(json.dumps(first, indent=4)) == body_hash(first)


def test_body_hash_changes_with_any_path_or_selector():
    base = {"kind": "dom", "container": "li.job"}
    changed = {"kind": "dom", "container": "li.posting"}
    assert body_hash(base) != body_hash(changed)


def test_body_hash_refuses_a_non_object_body():
    with pytest.raises(ValueError):
        body_hash(["li.job"])


# --- statistics are routers ---------------------------------------------------


def test_statistics_count_fills_and_duplicates_beyond_the_first():
    postings = [
        job("https://x.test/job/1", location="Helsinki"),
        job("https://x.test/job/2", location=""),
        job("https://x.test/job/2", title=" apply "),
        job("https://x.test/job/3", title="Apply"),
    ]
    stats = collect_statistics(postings)
    assert stats.count == 4
    assert stats.fill_rate("location") == 0.25
    assert stats.duplicate_urls == 1
    assert stats.duplicate_titles == 2  # two "Backend Engineer", plus " apply " == "Apply"


def test_fill_rate_is_none_for_an_empty_replay():
    assert collect_statistics([]).fill_rate("location") is None


def test_page_advance_reads_the_requests_after_the_first():
    assert collect_statistics([], page_counts=(2, 3)).page_advance is True
    assert collect_statistics([], page_counts=(2, 0)).page_advance is False
    assert collect_statistics([], page_counts=()).page_advance is False


def test_a_clean_replay_fires_no_signal():
    postings = [job("https://x.test/job/1", location="Helsinki")]
    stats = collect_statistics(postings, page_counts=(1,))
    assert signals(api_recipe(), stats) == ()


def test_duplicate_titles_route_to_the_title_path_but_do_not_reject():
    # The recorded lesson: 24 real postings all titled "Apply" is a router, not a defect.
    postings = [job(f"https://x.test/job/{n}", title="Apply", location="Helsinki") for n in range(24)]
    fired = signals(api_recipe(), collect_statistics(postings, page_counts=(24,)))
    assert [s.name for s in fired] == ["duplicate_titles"]
    assert "title" in fired[0].points_at


def test_an_empty_mapped_field_names_the_field_map_path():
    postings = [job("https://x.test/job/1", location="")]
    fired = signals(api_recipe(), collect_statistics(postings, page_counts=(1,)))
    assert [(s.name, s.points_at) for s in fired] == [
        ("empty_location", "field_map.location: no posting filled this field")
    ]


def test_an_unmapped_field_is_not_a_signal():
    # No location mapping means nothing was expected there, so nothing is missing.
    recipe = api_recipe(field_map={"url": {"path": ["absolute_url"]}, "title": {"path": ["title"]}})
    fired = signals(recipe, collect_statistics([job("https://x.test/job/1")], page_counts=(1,)))
    assert fired == ()


def test_pagination_that_never_advances_names_the_pagination_params():
    postings = [job("https://x.test/job/1", location="Helsinki")]
    fired = signals(api_recipe(), collect_statistics(postings, page_counts=(1, 0)))
    assert [s.name for s in fired] == ["pagination_no_advance"]
    assert "param/step/cursor_path" in fired[0].points_at


def test_no_pagination_configured_never_fires_page_advance():
    postings = [job("https://x.test/job/1", location="Helsinki")]
    recipe = api_recipe(pagination={"kind": "none"})
    assert signals(recipe, collect_statistics(postings, page_counts=(1,))) == ()


def test_detail_failures_route_to_the_detail_template():
    postings = [job("https://x.test/job/1", location="Helsinki")]
    stats = collect_statistics(
        postings, page_counts=(1,), detail_attempted=3, detail_succeeded=1
    )
    detail_recipe = api_recipe(
        field_map={
            "url": {"path": ["absolute_url"]},
            "title": {"path": ["title"]},
            "location": {"path": ["location", "name"]},
        },
        detail={
            "url": "https://boards-api.greenhouse.io/v1/boards/acme/jobs/{item.id}",
            "description_paths": [["content"]],
        },
    )
    fired = signals(detail_recipe, stats)
    assert [s.name for s in fired] == ["detail_failures"]
    assert "detail.url" in fired[0].points_at


def test_dom_location_selector_is_the_router_for_dom_recipes():
    postings = [job("https://careers.example.com/job/1", location="")]
    fired = signals(dom_recipe(), collect_statistics(postings))
    assert [(s.name, s.points_at) for s in fired] == [
        ("empty_location", "location selector: no posting filled this field")
    ]


# --- DOM agreement ------------------------------------------------------------


def test_dom_agreement_matches_replayed_urls_against_the_rendered_page():
    html = listing_html("/job/1", "/job/2", "/job/3")
    postings = [job("https://careers.example.com/job/1"), job("https://careers.example.com/job/2")]
    agreement = dom_agreement(postings, html, "https://careers.example.com/jobs")
    assert agreement is not None
    assert (agreement.replay_count, agreement.rendered_count, agreement.matched) == (2, 3, 2)
    assert agreement.extra == ("https://careers.example.com/job/3",)
    assert agreement.agrees is True


def test_a_replayed_url_the_dom_does_not_show_breaks_agreement():
    html = listing_html("/job/1")
    postings = [job("https://careers.example.com/job/1"), job("https://careers.example.com/job/9")]
    agreement = dom_agreement(postings, html, "https://careers.example.com/jobs")
    assert agreement is not None
    assert agreement.agrees is False
    assert agreement.missing == ("https://careers.example.com/job/9",)


def test_an_empty_replay_has_no_agreement():
    agreement = dom_agreement([], listing_html("/job/1"), "https://careers.example.com/jobs")
    assert agreement is not None
    assert agreement.agrees is False
    assert agreement.matched == 0


def test_no_render_means_no_agreement():
    assert dom_agreement([job("https://careers.example.com/job/1")], None, "https://x") is None


def test_empty_replay_classification_separates_closed_board_from_bad_navigation():
    empty_board = dom_agreement([], listing_html(extra='<p>No open roles right now.</p>'), "https://x/jobs")
    assert classify_empty_replay(empty_board) == EMPTY_BOARD
    navigated = dom_agreement([], listing_html("/job/1"), "https://x/jobs")
    assert classify_empty_replay(navigated) == NAVIGATION_FAILURE
    assert classify_empty_replay(None) == UNVERIFIABLE


# --- adversarial selection and the gate prompt --------------------------------


def test_selection_puts_worst_looking_items_first():
    postings = [
        job("https://careers.example.com/job/1", title="Data Engineer", posted_date="2026-10-01"),
        job("https://careers.example.com/job/2", title="Jobs"),
        job("https://careers.example.com/job/3", title="Backend Engineer"),
        job("https://careers.example.com/job/3", title="Backend Engineer", posted_date="2026-10-02"),
        job("https://careers.example.com/jobs", title="All jobs"),
    ]
    selected = adversarial_selection(postings, "https://careers.example.com/jobs")
    assert [item.why for item in selected] == [
        "duplicate url",
        "duplicate url",
        "page itself",
        "short title",
        "clean anchor",
    ]


def test_selection_respects_the_limit_and_still_includes_clean_anchors():
    postings = [
        job(
            f"https://careers.example.com/job/{n}",
            title=f"Role {n}",
            posted_date="2026-10-01",
        )
        for n in range(30)
    ]
    selected = adversarial_selection(postings, "https://careers.example.com/jobs", limit=5)
    assert len(selected) == 5
    assert all(item.why == "clean anchor" for item in selected)


def test_selection_needs_a_positive_limit():
    with pytest.raises(ValueError):
        adversarial_selection([job("https://x.test/job/1")], "https://x.test/jobs", limit=0)


def test_gate_prompt_shows_items_and_page_but_never_a_reference():
    postings = [job("https://careers.example.com/job/1", title="Data Engineer")]
    items = adversarial_selection(postings, "https://careers.example.com/jobs")
    _, user = build_gate_prompt(dom_recipe(), items, "https://careers.example.com/jobs")
    # The gate is shown the page and the items, never any expected count or
    # reference list: it answers one yes/no question about postings existing.
    assert "expected_count" not in user
    assert "verification" not in user
    assert "https://careers.example.com/jobs" in user
    assert '"Data Engineer"' in user
    assert "container=li.job" in user.replace("'", "")


def test_gate_prompt_includes_the_dom_window_only_for_dom_recipes():
    postings = [job("https://careers.example.com/job/1")]
    items = adversarial_selection(postings, "https://careers.example.com/jobs")
    _, with_dom = build_gate_prompt(
        dom_recipe(), items, "https://careers.example.com/jobs", dom_html=listing_html("/job/1")
    )
    assert "DOM around the containers" in with_dom
    _, api_prompt = build_gate_prompt(api_recipe(), items, "https://careers.example.com/jobs")
    assert "DOM around the containers" not in api_prompt


# --- gate answers --------------------------------------------------------------


def test_gate_postings_verdict_carries_no_analysis():
    postings = [job("https://careers.example.com/job/1")]
    llm = FakeCompleter({"verdict": "postings", "items": []})
    verdict = run_posting_gate(dom_recipe(), postings, "https://careers.example.com/jobs", llm)
    assert verdict.verdict == "postings"
    assert verdict.items == ()
    assert verdict.model_id == GATE_MODEL
    assert len(llm.calls) == 1


def test_gate_none_verdict_keeps_each_item_reason():
    postings = [job("https://careers.example.com/jobs", title="All jobs")]
    llm = FakeCompleter(
        {"verdict": "none", "items": [{"url": "https://careers.example.com/jobs", "reason": "It is the listing page."}]}
    )
    verdict = run_posting_gate(dom_recipe(), postings, "https://careers.example.com/jobs", llm)
    assert verdict.verdict == "none"
    assert verdict.analysis()["items"] == [
        {"url": "https://careers.example.com/jobs", "reason": "It is the listing page."}
    ]


def test_gate_none_without_reasons_is_unusable():
    postings = [job("https://careers.example.com/job/1")]
    llm = FakeCompleter({"verdict": "none", "items": []})
    with pytest.raises(GateError, match="without a per-item reason"):
        run_posting_gate(dom_recipe(), postings, "https://careers.example.com/jobs", llm)


def test_gate_may_not_judge_a_url_it_was_never_shown():
    postings = [job("https://careers.example.com/job/1")]
    llm = FakeCompleter(
        {"verdict": "none", "items": [{"url": "https://careers.example.com/job/999", "reason": "Nav."}]}
    )
    with pytest.raises(GateError, match="not shown"):
        run_posting_gate(dom_recipe(), postings, "https://careers.example.com/jobs", llm)


@pytest.mark.parametrize(
    "raw",
    ["not json", json.dumps({"verdict": "maybe", "items": []}), json.dumps({"verdict": "none", "extra": 1})],
)
def test_gate_rejects_an_unusable_answer(raw):
    postings = [job("https://careers.example.com/job/1")]
    with pytest.raises(GateError):
        run_posting_gate(dom_recipe(), postings, "https://careers.example.com/jobs", FakeCompleter(raw=raw))


def test_gate_refuses_an_empty_replay():
    with pytest.raises(ValueError):
        run_posting_gate(dom_recipe(), [], "https://careers.example.com/jobs", FakeCompleter({"verdict": "none"}))


# --- cadence and the stop rule -------------------------------------------------


def test_gate_runs_on_an_event_not_on_every_run():
    assert should_run_gate(first_accepted=True, signal_fired=False, stop_cleared=False) is True
    assert should_run_gate(first_accepted=False, signal_fired=True, stop_cleared=False) is True
    assert should_run_gate(first_accepted=False, signal_fired=False, stop_cleared=True) is True
    assert should_run_gate(first_accepted=False, signal_fired=False, stop_cleared=False) is False


def test_a_single_none_flags_but_does_not_stop():
    assert stop_decision(verdict="none", structural_empty=False, prior_none=False) is False


@pytest.mark.parametrize("agreement", [{"structural_empty": True}, {"prior_none": True}])
def test_a_none_with_one_agreement_stops(agreement):
    assert stop_decision(verdict="none", **agreement) is True


def test_a_postings_verdict_never_stops():
    assert stop_decision(verdict="postings", structural_empty=True, prior_none=True) is False


def test_structural_emptiness_means_no_item_has_both_url_and_title():
    assert structural_empty([job("", title=""), job("https://x.test/job/1", title="")]) is True
    assert structural_empty([job("https://x.test/job/1")]) is False


# --- the api replay report R3 reads -------------------------------------------


def _detail_recipe() -> ApiRecipe:
    return api_recipe(
        field_map={
            "url": {"path": ["absolute_url"]},
            "title": {"path": ["title"]},
            "location": {"path": ["location", "name"]},
        },
        detail={
            "url": "https://boards-api.greenhouse.io/v1/boards/acme/jobs/{item.id}",
            "description_paths": [["content"]],
        },
    )


def _detail_item(item_id: int) -> dict:
    return {
        "id": item_id,
        "title": f"Engineer {item_id}",
        "absolute_url": f"https://boards.greenhouse.io/acme/jobs/{item_id}",
        "location": {"name": "Helsinki"},
    }


def test_report_records_pages_and_detail_successes():
    client = FakeApiClient(
        {
            "jobs/1001": {"content": "<p>First role.</p>"},
            "jobs/1002": RuntimeError("500"),
            "page=1": {"jobs": [_detail_item(1001), _detail_item(1002)]},
            "page=2": {"jobs": []},
        }
    )
    report = run_api_recipe_report(_detail_recipe(), client.fetch_json, "https://boards.greenhouse.io/acme")
    assert len(report.jobs) == 2
    assert report.page_counts == (2, 0)
    assert report.detail_attempted == 2
    assert report.detail_succeeded == 1
    assert report.jobs[0].description == "First role."
    assert report.jobs[1].description is None
