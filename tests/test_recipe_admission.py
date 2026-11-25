"""Phase F1a: strict write-time admission of recipe bodies (no LLM, no network).

Properties under test:
- A body whose ``kind`` differs from the ``company_recipes.kind`` column is
  rejected before insert.
- An unknown key anywhere in the body is rejected.
- A host not on the allowlist, a templated host, or a non-https URL is rejected.
- A placeholder must be one the interpreter substitutes: a declared var or the
  pagination param.
- Admission is pure: it performs no HTTP request.
"""

from __future__ import annotations

import copy

import pytest
from jobsearch_core.ingest.recipe import parse_body

from tests.test_recipe import _greenhouse_recipe, _verification


def _api() -> dict:
    return _greenhouse_recipe()


def test_valid_api_body_is_admitted():
    assert parse_body(_api(), "api").kind == "api"


def test_valid_body_accepts_json_text():
    import json

    assert parse_body(json.dumps(_api()), "api").kind == "api"


@pytest.mark.parametrize(
    "mutate",
    [
        # kind/column mismatch: body says dom, column says api
        lambda r: r | {"kind": "dom"},
        # unknown top-level key
        lambda r: r | {"extra": 1},
        # unknown key inside a nested model
        lambda r: r | {"pagination": {"kind": "page", "param": "page", "bogus": 2}},
        # request host off the allowlist
        lambda r: r | {"allowed_hosts": ["evil.example"]},
        # templated host
        lambda r: r | {"url_template": "https://{vars.token}.example/x?page={page}"},
        # http, not https
        lambda r: r | {"url_template": "http://boards-api.greenhouse.io/x?page={page}"},
        # unknown placeholder root
        lambda r: r | {"url_template": "https://boards-api.greenhouse.io/x?page={page}&q={nope.x}"},
        # allowlisted host list entry is not a bare lowercase hostname
        lambda r: r | {"allowed_hosts": ["Boards-API.greenhouse.io"]},
        # verification pattern does not compile
        lambda r: r | {"verification": _verification(url_pattern="(")},
    ],
)
def test_strict_admission_rejects(mutate):
    body = mutate(copy.deepcopy(_api()))
    with pytest.raises(ValueError):
        parse_body(body, "api")


def test_dom_body_is_rejected_when_column_says_api():
    dom = {
        "kind": "dom",
        "allowed_hosts": ["jobs.apple.com"],
        "verification": _verification(),
        "container": "li",
        "link": {"selector": "a", "attr": "href"},
        "title": {"selector": "h3"},
    }
    with pytest.raises(ValueError, match="does not match column kind"):
        parse_body(dom, "api")

def test_nested_url_template_error_names_the_right_placement():
    """Fix 4: the rejection says where url_template belongs, not just "extra key"."""
    body = _api()
    body["field_map"] = {
        "url": {"path": ["absolute_url"], "url_template": "https://x/{id}"},
        "title": {"path": ["title"]},
    }
    with pytest.raises(ValueError) as excinfo:
        parse_body(body, "api")
    message = str(excinfo.value)
    assert "field_map.url.url_template" in message
    assert "direct child of field_map" in message


def test_max_pages_over_cap_explains_the_budget():
    """Fix 4: an over-cap max_pages names the cap and the intended alternative."""
    body = _api()
    body["pagination"] = {"kind": "page", "param": "page", "max_pages": 100}
    with pytest.raises(ValueError) as excinfo:
        parse_body(body, "api")
    message = str(excinfo.value)
    assert "max_pages" in message
    assert "api caps at 50" in message
    assert "pagination" in message


def test_a_dom_max_pages_over_cap_names_the_dom_budget():
    """The dom cap is 20, not the api 50: the hint must not promise the wrong one."""
    body = {
        "kind": "dom",
        "allowed_hosts": ["acme.example"],
        "verification": _verification(url_pattern=r"/jobs/\d+$"),
        "container": "li.job",
        "link": {"selector": "a", "attr": "href"},
        "title": {"selector": "h3"},
        "pagination": {"kind": "next", "selector": "a.next", "max_pages": 40},
    }
    with pytest.raises(ValueError) as excinfo:
        parse_body(body, "dom")
    message = str(excinfo.value)
    assert "max_pages" in message
    assert "dom at 20" in message


def test_exactly_one_url_error_points_at_both_accepted_forms():
    body = _api()
    body["field_map"] = {"title": {"path": ["title"]}}
    with pytest.raises(ValueError) as excinfo:
        parse_body(body, "api")
    assert "field_map" in str(excinfo.value)
    assert "url_template" in str(excinfo.value)


def test_pagination_param_may_be_any_name_the_body_resolves():
    """A pager named ``start`` is legal: the interpreter substitutes any param name."""
    body = _api()
    body["url_template"] = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?start={start}"
    body["pagination"] = {"kind": "offset", "param": "start", "start": 0, "step": 25, "max_pages": 5}
    recipe = parse_body(body, "api")
    assert recipe.pagination.param == "start"


def test_placeholder_the_interpreter_cannot_substitute_is_rejected():
    """``{page}`` with param ``offset`` would ship as a literal brace."""
    body = _api()
    body["url_template"] = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?offset={offset}&page={page}"
    body["pagination"] = {"kind": "offset", "param": "offset", "start": 0, "step": 25, "max_pages": 5}
    with pytest.raises(ValueError) as excinfo:
        parse_body(body, "api")
    message = str(excinfo.value)
    assert "unknown placeholder {page}" in message
    # The message must name what this body can resolve, not a fixed vocabulary.
    assert "offset" in message and "token" in message


def test_invented_placeholder_still_fails_before_any_request():
    """Samsung-style ``{compCd}`` must not reach a live fetch as a literal brace."""
    body = _api()
    body["url_template"] = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?page={page}&compCd={compCd}"
    with pytest.raises(ValueError, match="unknown placeholder \\{compCd\\}"):
        parse_body(body, "api")
