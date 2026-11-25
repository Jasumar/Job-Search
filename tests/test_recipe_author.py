"""Phase F3: recipe author (offline; scripted fake LLM, captured fixtures).

Properties under test:
- The LLM never sets trust fields: allowed_hosts and verification are overwritten
  by code, so a body naming another host is still replayed only on the company's
  allowlist.
- A passing trial stores exactly one recipe at status='verified' and resets the
  consecutive failure count.
- A failed trial (bad JSON, admission error, replay error, verification mismatch)
  is recorded as a trial failure, never an exception; its reason is fed back into
  the next prompt.
- Three consecutive failures set recipe_fail, store no recipe, and the next call
  makes zero LLM requests (RecipeBlockedError).
- Transport errors from the LLM propagate and record no trial.
- Empty or self-inconsistent references are refused before any LLM call.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from jobsearch_core.db.models import Company, CompanyRecipe
from jobsearch_core.db.recipe_state import RECIPE_FAIL, RECIPE_OK, RecipeBlockedError
from jobsearch_core.ingest.recipe import run_api_recipe, run_dom_recipe
from jobsearch_core.ingest.recipe_author import (
    Artifact,
    AuthorInput,
    _selector_for,
    author_recipe,
    build_prompt,
    reduce_html,
    reduce_json,
    script_artifacts,
)

RECIPES = Path(__file__).parent / "fixtures" / "recipes"
APPLE_PAGE = "https://jobs.apple.com/en-us/search"
AIRBNB_BASE = "https://boards-api.greenhouse.io/v1/boards/airbnb/jobs"
APPLE_URL_PATTERN = r"^https://jobs\.apple\.com/en-us/details/\d+(-\d+)?/[^/?]+(\?team=[A-Z]+)?$"
AIRBNB_URL_PATTERN = r"^https://careers\.airbnb\.com/positions/\d+\?gh_jid=\d+$"


class ScriptedLLM:
    """Returns queued replies in order and records every (system, user) prompt."""

    model = "test/scripted"

    def __init__(self, replies: list[object]):
        self.replies = list(replies)
        self.prompts: list[tuple[str, str]] = []

    def complete_json(self, system_prompt: str, user_prompt: str) -> str:
        self.prompts.append((system_prompt, user_prompt))
        if not self.replies:
            raise AssertionError("LLM called more times than scripted")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, str) else json.dumps(reply)


def _apple_html() -> str:
    return (RECIPES / "apple_search_page1.html").read_text(encoding="utf-8")


def _apple_body() -> dict:
    body = json.loads((RECIPES / "apple_search.recipe.json").read_text())
    body.pop("verification")
    body.pop("allowed_hosts")
    return body


def _airbnb_payload() -> str:
    return (RECIPES / "airbnb_greenhouse_jobs.json").read_text()


def _airbnb_body() -> dict:
    body = json.loads((RECIPES / "airbnb_greenhouse.recipe.json").read_text())
    body.pop("verification")
    body.pop("allowed_hosts")
    return body


def _apple_input(company_id: int, *, reply_hosts: list[str] | None = None) -> AuthorInput:
    from jobsearch_core.ingest.recipe import DomRecipe

    recipe_body = json.loads((RECIPES / "apple_search.recipe.json").read_text())
    reference = run_dom_recipe(DomRecipe.model_validate(recipe_body), _apple_html(), APPLE_PAGE)
    assert len(reference) == 20
    return AuthorInput(
        company_id=company_id,
        allowed_hosts=["jobs.apple.com"],
        url_pattern=APPLE_URL_PATTERN,
        page_url=APPLE_PAGE,
        reference=reference,
        artifacts=[
            Artifact(
                name="static_html",
                fetch_mode="static",
                url=APPLE_PAGE,
                source=_apple_html(),
                reference=tuple(reference),
            )
        ],
        carried_by="static_html",
    )


def _airbnb_input(company_id: int, fetch_calls: list[str]) -> AuthorInput:
    from jobsearch_core.ingest.recipe import ApiRecipe

    payload = _airbnb_payload()

    def fetch(url: str) -> str:
        fetch_calls.append(url)
        return payload

    recipe = ApiRecipe.model_validate(
        {
            **json.loads((RECIPES / "airbnb_greenhouse.recipe.json").read_text()),
            "verification": {"expected_count": 1, "sample_titles": ["x"], "url_pattern": ".*"},
        }
    )
    reference = run_api_recipe(recipe, fetch, AIRBNB_BASE)
    assert len(reference) == 173
    fetch_calls.clear()
    return AuthorInput(
        company_id=company_id,
        allowed_hosts=["boards-api.greenhouse.io"],
        url_pattern=AIRBNB_URL_PATTERN,
        page_url=AIRBNB_BASE,
        reference=reference,
        artifacts=[
            Artifact(
                name="xhr_json",
                fetch_mode="xhr",
                url=AIRBNB_BASE,
                source=payload,
                reference=tuple(reference),
            )
        ],
        carried_by="xhr_json",
        fetch=fetch,
    )


def _company(session) -> Company:
    company = Company(name="Acme", careers_url=None)
    session.add(company)
    session.commit()
    session.refresh(company)
    return company


# --- evidence reduction -----------------------------------------------------------


def test_reduce_html_lists_link_wrapping_candidates_with_counts():
    out = reduce_html(_apple_html())
    assert "CANDIDATE CONTAINERS" in out
    assert "div[data-core-accordion-button]: 20 uniform" in out
    assert "EXAMPLE div[data-core-accordion-button]:" in out
    assert "/en-us/details/114438158/" in out


def test_reduce_html_drops_scripts_and_keeps_stable_attrs():
    html = (
        "<html><body><script>var secret=1;</script>"
        '<ul><li class="job-row x9f3k2a1"><a href="/details/1/x">Eng</a></li>'
        '<li class="job-row"><a href="/details/2/y">Ops</a></li>'
        '<li class="job-row"><a href="/details/3/z">Sales</a></li></ul></body></html>'
    )
    out = reduce_html(html)
    assert "secret" not in out
    assert "x9f3k2a1" not in out
    assert "li.job-row: 3" in out
    assert 'href="/details/1/x"' in out


def test_selector_anchor_order_is_data_id_class_then_role():
    soup = BeautifulSoup(
        "<div>"
        '<span data-row="1" id="job-list" class="rows"></span>'
        '<span id="job-list-2" class="rows"></span>'
        '<span class="rows"></span>'
        '<span role="list"></span>'
        '<span aria-label="Job openings (12)"></span>'
        "<span></span>"
        "</div>",
        "html.parser",
    )
    assert [_selector_for(tag) for tag in soup.find_all("span")] == [
        "span[data-row]",
        "#job-list-2",
        "span.rows",
        'span[role="list"]',
        'span[aria-label="Job openings (12)"]',
        "span",
    ]


def test_selector_skips_generated_ids_and_unquotable_attributes():
    soup = BeautifulSoup(
        '<div><span id="row-1234"></span><span aria-label=\'Job "open"\'></span></div>',
        "html.parser",
    )
    assert [_selector_for(tag) for tag in soup.find_all("span")] == ["span", "span"]


def test_reduce_html_keeps_datetime_and_itemprop_for_posted_date():
    html = (
        "<html><body><ul>"
        '<li class="job"><a href="/job/1">Eng</a>'
        '<time datetime="2026-10-01" itemprop="datePosted">1 Oct</time></li>'
        "</ul></body></html>"
    )
    out = reduce_html(html)
    assert 'datetime="2026-10-01"' in out
    assert 'itemprop="datePosted"' in out


def test_reduce_html_offers_twenty_candidates_and_two_examples():
    # 21 distinct selectors, each matching three uniform rows.
    rows = "".join(
        f'<div data-k{index}><a href="/j/{index}/{n}">Eng</a></div>'
        for index in range(21)
        for n in range(3)
    )
    out = reduce_html(f"<html><body>{rows}</body></html>")
    candidates = [line for line in out.splitlines() if line.startswith("  ") and ": " in line]
    assert len(candidates) == 20
    assert out.count("EXAMPLE ") == 2


def test_script_artifacts_hand_over_json_ld_only():
    html = (
        "<html><body>"
        '<script type="application/ld+json">'
        '{"@type": "JobPosting", "title": "Eng", "url": "https://jobs.example.com/1"}</script>'
        '<script type="application/ld+json">{not json}</script>'
        '<script type="application/json" id="__NEXT_DATA__">'
        '{"props": {"jobs": [{"id": 1}, {"id": 2}]}}</script>'
        '<script>window.__STATE__ = {"jobs": [{"id": 3}]};</script>'
        "<script>var a = 1;</script>"
        "</body></html>"
    )
    artifacts = script_artifacts(APPLE_PAGE, html)

    assert [artifact.name for artifact in artifacts] == ["json_ld"]
    artifact = artifacts[0]
    assert artifact.fetch_mode == "json_ld"
    assert artifact.url == APPLE_PAGE
    # One artifact holds every valid block; the unparsable one is skipped.
    assert json.loads(artifact.source) == [
        {"@type": "JobPosting", "title": "Eng", "url": "https://jobs.example.com/1"}
    ]
    # Inline page state is still cut, and JSON-LD is not a request to replay.
    assert "__NEXT_DATA__" not in artifact.source and "__STATE__" not in artifact.source
    assert artifact.is_json and not artifact.is_replayable


def test_script_artifacts_are_empty_without_script_bodies():
    assert script_artifacts(APPLE_PAGE, "<html><body><p>none</p></body></html>") == []


def test_reduce_json_lists_pointer_paths_with_samples():
    out = reduce_json(_airbnb_payload())
    assert '["jobs"] is a list of' in out
    assert '["jobs", 0, "absolute_url"]' in out


def test_reduce_json_rejects_non_json():
    with pytest.raises(ValueError):
        reduce_json("<html>")


# --- authoring behaviour ----------------------------------------------------------


def test_first_trial_pass_stores_verified_recipe(session):
    company = _company(session)
    llm = ScriptedLLM([_apple_body()])
    result = author_recipe(session, company, _apple_input(company.id), llm)
    session.commit()

    assert result.status == "verified"
    assert len(result.trials) == 1 and result.trials[0].passed
    rows = session.query(CompanyRecipe).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.status == "verified"
    assert row.scope == "company_careers" and row.company_id == company.id
    assert row.kind == "dom" and row.model_id == "test/scripted"
    stored = json.loads(row.body)
    assert stored["allowed_hosts"] == ["jobs.apple.com"]
    assert stored["verification"]["expected_count"] == 20
    assert company.recipe_state == RECIPE_OK and company.recipe_trials == 0


def test_llm_cannot_widen_allowed_hosts(session):
    company = _company(session)
    body = _apple_body()
    body["allowed_hosts"] = ["evil.example.com"]
    llm = ScriptedLLM([body])
    result = author_recipe(session, company, _apple_input(company.id), llm)
    session.commit()

    assert result.status == "verified"
    stored = json.loads(session.query(CompanyRecipe).one().body)
    assert stored["allowed_hosts"] == ["jobs.apple.com"]


def test_llm_cannot_override_verification(session):
    company = _company(session)
    body = _apple_body()
    body["verification"] = {"expected_count": 1, "sample_titles": ["x"], "url_pattern": ".*"}
    llm = ScriptedLLM([body])
    result = author_recipe(session, company, _apple_input(company.id), llm)
    assert result.status == "verified"
    stored = json.loads(session.query(CompanyRecipe).one().body)
    assert stored["verification"]["expected_count"] == 20


def test_failure_reason_is_fed_back_and_second_trial_passes(session):
    company = _company(session)
    bad = _apple_body()
    bad["container"] = "li.no-such-row"
    llm = ScriptedLLM([bad, _apple_body()])
    result = author_recipe(session, company, _apple_input(company.id), llm)
    session.commit()

    assert result.status == "verified"
    assert [t.passed for t in result.trials] == [False, True]
    # The failing selector is named, so the second prompt can point at it.
    assert result.trials[0].reason and "li.no-such-row" in result.trials[0].reason
    second_user = llm.prompts[1][1]
    assert "Previous attempts failed:" in second_user
    assert "attempt 1: replay failed" in second_user
    assert "li.no-such-row" in second_user
    # The failed attempt keeps the model's raw body: the reason alone cannot be
    # re-diagnosed after the run.
    assert result.trials[0].body and "li.no-such-row" in result.trials[0].body
    assert company.recipe_trials == 0


def test_bad_json_is_a_trial_failure_not_an_exception(session):
    company = _company(session)
    llm = ScriptedLLM(["not json at all", _apple_body()])
    result = author_recipe(session, company, _apple_input(company.id), llm)
    assert result.trials[0].passed is False
    assert "not JSON" in result.trials[0].reason
    assert result.status == "verified"


def test_admission_rejection_is_a_trial_failure(session):
    company = _company(session)
    body = _apple_body()
    body["kind"] = "api"
    llm = ScriptedLLM([body, body, body])
    result = author_recipe(session, company, _apple_input(company.id), llm)
    assert result.status == "failed"
    assert all("admission rejected" in t.reason for t in result.trials)


def test_three_failures_set_recipe_fail_store_nothing_and_block_next_call(session):
    company = _company(session)
    bad = _apple_body()
    bad["container"] = "li.no-such-row"
    llm = ScriptedLLM([bad, bad, bad])
    result = author_recipe(session, company, _apple_input(company.id), llm)
    session.commit()

    assert result.status == "failed"
    assert result.recipe_state == RECIPE_FAIL
    assert len(result.trials) == 3
    assert session.query(CompanyRecipe).count() == 0
    assert company.recipe_state == RECIPE_FAIL and company.recipe_trials == 3

    blocked_llm = ScriptedLLM([])
    with pytest.raises(RecipeBlockedError):
        author_recipe(session, company, _apple_input(company.id), blocked_llm)
    assert blocked_llm.prompts == []


def test_transport_error_propagates_and_records_no_trial(session):
    company = _company(session)
    llm = ScriptedLLM([ConnectionError("network down")])
    with pytest.raises(ConnectionError):
        author_recipe(session, company, _apple_input(company.id), llm)
    assert company.recipe_trials == 0
    assert session.query(CompanyRecipe).count() == 0


def test_empty_reference_is_refused_before_any_llm_call(session):
    company = _company(session)
    inp = _apple_input(company.id)
    empty = AuthorInput(**{**inp.__dict__, "reference": []})
    llm = ScriptedLLM([])
    with pytest.raises(ValueError, match="non-empty"):
        author_recipe(session, company, empty, llm)
    assert llm.prompts == []


def test_api_run_stores_verified_recipe_and_replays_through_fetch(session):
    company = _company(session)
    calls: list[str] = []
    inp = _airbnb_input(company.id, calls)
    llm = ScriptedLLM([_airbnb_body()])
    result = author_recipe(session, company, inp, llm)
    session.commit()

    assert result.status == "verified"
    assert calls, "verification must replay through the injected fetch"
    row = session.query(CompanyRecipe).one()
    assert row.kind == "api"
    assert json.loads(row.body)["verification"]["expected_count"] == 173


def test_company_mismatch_is_refused(session):
    company = _company(session)
    inp = _apple_input(company.id + 99)
    with pytest.raises(ValueError, match="does not match"):
        author_recipe(session, company, inp, ScriptedLLM([]))


def test_prompt_shows_every_artifact_and_lets_the_model_choose_the_kind():
    """F3 parity: the author sees the whole capture, not one pre-classified source."""
    artifacts = [
        Artifact("static_html", "static", APPLE_PAGE, "<html><body>Loading…</body></html>"),
        Artifact("rendered_html", "rendered", APPLE_PAGE, _apple_html(), ()),
        Artifact("xhr_1", "xhr", AIRBNB_BASE, _airbnb_payload(), ()),
    ]
    _, user = build_prompt(
        url_pattern=".*",
        page_url=APPLE_PAGE,
        artifacts=artifacts,
        feedback=[],
    )
    assert "Requested kind" not in user
    assert "Decide the kind" in user
    for artifact in artifacts:
        assert f"- {artifact.name}: " in user
        assert f"--- artifact {artifact.name}" in user
    assert AIRBNB_BASE in user
    # Both schemas are present: the model is never told which one to fill.
    assert '"kind": "api"' in user and '"kind": "dom"' in user


def test_prompt_names_the_carrying_artifact_but_never_the_reference_count_or_titles():
    """The reference is the scoring side; a count and sample titles let the
    author infer the answer (and, in a retry, tune to the grader) instead of
    reading the evidence."""
    inp = _apple_input(1)
    _, user = build_prompt(
        url_pattern=inp.url_pattern,
        page_url=inp.page_url,
        artifacts=inp.artifacts,
        feedback=[],
    )
    assert ", carried the postings" in user
    assert "postings)" not in user
    assert "Sample titles" not in user
    assert f"yields {len(inp.reference)} postings" not in user


def test_author_may_choose_api_when_the_reference_came_from_html(session):
    """The kind is the model's call: a JSON artifact that replays wins over HTML."""
    company = _company(session)
    base = _apple_input(company.id)
    fetch_calls: list[str] = []
    payload = _airbnb_payload()

    def fetch(url: str) -> str:
        fetch_calls.append(url)
        return payload

    inp = AuthorInput(
        company_id=company.id,
        allowed_hosts=["boards-api.greenhouse.io"],
        url_pattern=base.url_pattern,
        page_url=APPLE_PAGE,
        reference=base.reference,
        artifacts=[
            *base.artifacts,
            Artifact("xhr_1", "xhr", AIRBNB_BASE, payload),
        ],
        carried_by="static_html",
        fetch=fetch,
    )
    result = author_recipe(session, company, inp, ScriptedLLM([_airbnb_body()]))
    assert result.status == "verified"
    assert result.kind == "api"
    # The recipe's url_template names the captured XHR URL, so that is the artifact.
    assert result.source_artifact == "xhr_1"
    assert fetch_calls and fetch_calls[0].startswith(AIRBNB_BASE)


def test_author_may_choose_dom_when_the_capture_holds_json(session):
    """JSON in the capture does not force an api recipe: the markup may be enough."""
    company = _company(session)
    base = _apple_input(company.id)
    inp = AuthorInput(
        company_id=company.id,
        allowed_hosts=["jobs.apple.com"],
        url_pattern=base.url_pattern,
        page_url=APPLE_PAGE,
        reference=base.reference,
        artifacts=[*base.artifacts, Artifact("xhr_1", "xhr", AIRBNB_BASE, _airbnb_payload())],
        carried_by="static_html",
        fetch=lambda url: _airbnb_payload(),
    )
    result = author_recipe(session, company, inp, ScriptedLLM([_apple_body()]))
    assert result.status == "verified"
    assert result.kind == "dom"
    assert result.source_artifact == "static_html"


def test_dom_replay_reads_the_artifact_that_carried_the_reference(session):
    """A rendered reference is replayed on the rendered DOM, not the static shell."""
    company = _company(session)
    base = _apple_input(company.id)
    static_shell = "<html><body><div id=root>Loading…</div></body></html>"
    inp = AuthorInput(
        company_id=company.id,
        allowed_hosts=["jobs.apple.com"],
        url_pattern=base.url_pattern,
        page_url=APPLE_PAGE,
        reference=base.reference,
        artifacts=[
            Artifact("static_html", "static", APPLE_PAGE, static_shell),
            Artifact("rendered_html", "rendered", APPLE_PAGE, _apple_html(), tuple(base.reference)),
        ],
        carried_by="rendered_html",
    )
    result = author_recipe(session, company, inp, ScriptedLLM([_apple_body()]))
    assert result.status == "verified"
    assert result.source_artifact == "rendered_html"


def test_reply_kind_is_validated(session):
    company = _company(session)
    inp = _apple_input(company.id)
    body = _apple_body() | {"kind": "js"}
    result = author_recipe(session, company, inp, ScriptedLLM([body, body, body]))
    assert result.status == "failed"
    assert "reply kind must be 'api' or 'dom'" in (result.trials[0].reason or "")


def test_json_capture_without_fetch_is_refused(session):
    """A capture holding JSON means the author may choose api; that needs a fetch."""
    company = _company(session)
    base = _apple_input(company.id)
    inp = AuthorInput(
        company_id=company.id,
        allowed_hosts=["jobs.apple.com"],
        url_pattern=base.url_pattern,
        page_url=APPLE_PAGE,
        reference=base.reference,
        artifacts=[*base.artifacts, Artifact("xhr_1", "xhr", AIRBNB_BASE, _airbnb_payload())],
        carried_by="static_html",
    )
    with pytest.raises(ValueError, match="needs a fetch"):
        author_recipe(session, company, inp, ScriptedLLM([]))


def test_carried_by_must_name_an_artifact(session):
    company = _company(session)
    inp = _apple_input(company.id)
    broken = AuthorInput(
        company_id=inp.company_id,
        allowed_hosts=inp.allowed_hosts,
        url_pattern=inp.url_pattern,
        page_url=inp.page_url,
        reference=inp.reference,
        artifacts=inp.artifacts,
        carried_by="nope",
    )
    with pytest.raises(ValueError, match="carried_by names no artifact"):
        author_recipe(session, company, broken, ScriptedLLM([]))


def test_a_paginated_dom_trial_reads_page_two_through_fetch_html(session):
    """Page one comes from the capture; pages 2+ are live and part of the trial."""
    company = _company(session)
    body = _apple_body()
    body["pagination"] = {"kind": "page", "param": "page", "max_pages": 2}
    inp = _apple_input(company.id)
    calls: list[str] = []

    def fetch_html(url: str) -> str:
        calls.append(url)
        return _apple_html()

    inp = AuthorInput(
        company_id=inp.company_id,
        allowed_hosts=inp.allowed_hosts,
        url_pattern=inp.url_pattern,
        page_url=inp.page_url,
        reference=inp.reference,
        artifacts=inp.artifacts,
        carried_by=inp.carried_by,
        fetch_html=fetch_html,
    )
    result = author_recipe(session, company, inp, ScriptedLLM([body]))
    assert result.status == "verified"
    # Only the second document is fetched: the first is the captured artifact.
    assert calls == [f"{APPLE_PAGE}?page=2"]
    stored = json.loads(session.query(CompanyRecipe).one().body)
    assert stored["pagination"]["kind"] == "page"


def test_a_paginated_dom_trial_without_an_html_fetch_fails_the_trial(session):
    """A body that pages cannot be judged from one captured document."""
    company = _company(session)
    body = _apple_body()
    body["pagination"] = {"kind": "next", "selector": "a.next"}
    result = author_recipe(session, company, _apple_input(company.id), ScriptedLLM([body, body, body]))
    assert result.status == "failed"
    assert "needs an html fetch" in (result.trials[0].reason or "")


def test_dom_schema_tells_the_author_it_can_page():
    _, user = build_prompt(
        url_pattern=".*",
        page_url="https://example.com",
        artifacts=[Artifact("static_html", "static", "https://example.com", "<html></html>")],
        feedback=[],
    )
    assert "It does not paginate" not in user
    assert '"kind": "none|page|offset|next"' in user


def test_api_schema_never_nests_url_template_inside_url():
    """The old schema line read `url_template` as a child of `url`; models copied it."""
    _, user = build_prompt(
        url_pattern=".*",
        page_url="https://example.com",
        artifacts=[Artifact("static_html", "static", "https://example.com", "<html></html>")],
        feedback=[],
    )
    assert '"url": {"path": [...]}  OR  "url_template"' not in user
    assert "never nested inside" in user
