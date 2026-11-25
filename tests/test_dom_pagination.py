"""R5b demo slice: a ``dom`` recipe that pages.

Properties pinned here:
- A paginated ``dom`` body is admitted by the same strict schema and by
  ``parse_body``; each kind rejects the fields it does not use.
- ``run_dom_recipe_report`` fetches page one only when nothing was handed to it
  as ``first_html``, then follows the recipe's own pager (a query parameter or a
  selector-chosen anchor) and never guesses a next page from the markup.
- The loop's guards: a page budget, a repeated URL, a page that adds no unseen
  posting, a later page that cannot be read or fetched, and a next link off
  ``allowed_hosts``. The first page is the only one that can fail the replay.
- ``first_page_jobs`` is the first document's postings only, because DOM
  agreement compares one rendered page and pages 2+ are not on it.
"""

from __future__ import annotations

import pytest
from jobsearch_core.ingest.recipe import (
    DOM_STOP_REASONS,
    DomPagination,
    DomRecipe,
    RecipeError,
    parse_body,
    run_dom_recipe,
    run_dom_recipe_report,
)
from pydantic import ValidationError

PAGE = "https://acme.example/careers"


def _verification() -> dict:
    return {
        "expected_count": 3,
        "sample_titles": ["Role 1"],
        "url_pattern": r"^https://acme\.example/jobs/\d+$",
    }


def _body(pagination: dict | None = None) -> dict:
    body = {
        "kind": "dom",
        "allowed_hosts": ["acme.example"],
        "verification": _verification(),
        "container": "li.job",
        "link": {"selector": "a", "attr": "href"},
        "title": {"selector": "h3"},
    }
    if pagination is not None:
        body["pagination"] = pagination
    return body


def _recipe(pagination: dict | None = None) -> DomRecipe:
    return DomRecipe.model_validate(_body(pagination))


def _listing(ids: list[int], next_href: str | None = None) -> str:
    rows = "".join(
        f'<li class="job"><a href="/jobs/{i}"><h3>Role {i}</h3></a></li>' for i in ids
    )
    nxt = f'<a class="next-page" href="{next_href}">Next</a>' if next_href else ""
    return f"<html><body><ul>{rows}</ul>{nxt}</body></html>"


class Pages:
    """A fetch that serves one document per URL and records what was asked for."""

    def __init__(self, pages: dict[str, str], fail_on: set[str] | None = None):
        self.pages = pages
        self.fail_on = fail_on or set()
        self.calls: list[str] = []

    def __call__(self, url: str) -> str:
        self.calls.append(url)
        if url in self.fail_on:
            raise RuntimeError(f"fetch failed: {url}")
        if url not in self.pages:
            raise AssertionError(f"unexpected fetch: {url}")
        return self.pages[url]


class TestDomPaginationSchema:
    def test_default_is_single_page(self):
        recipe = _recipe()
        assert recipe.pagination.kind == "none"
        assert recipe.pagination.max_pages == 10

    def test_page_kind_needs_a_param(self):
        with pytest.raises(ValidationError, match="needs a param"):
            DomPagination(kind="page")

    def test_offset_kind_needs_a_positive_step(self):
        with pytest.raises(ValidationError, match="needs a positive step"):
            DomPagination(kind="offset", param="start")
        with pytest.raises(ValidationError, match="step must be >= 1"):
            DomPagination(kind="offset", param="start", step=0)

    def test_next_kind_needs_a_selector(self):
        with pytest.raises(ValidationError, match="needs a selector"):
            DomPagination(kind="next")

    def test_none_kind_takes_no_paging_fields(self):
        with pytest.raises(ValidationError, match="takes no param"):
            DomPagination(kind="none", param="page")

    def test_next_kind_takes_no_param_or_step(self):
        with pytest.raises(ValidationError, match="not a param"):
            DomPagination(kind="next", selector="a.next", param="page")

    def test_page_budget_is_capped_at_twenty_documents(self):
        with pytest.raises(ValidationError, match="less than or equal to 20"):
            DomPagination(kind="page", param="page", max_pages=21)

    def test_a_paginated_dom_body_is_admitted(self):
        body = _body({"kind": "page", "param": "page", "max_pages": 3})
        recipe = parse_body(body, "dom")
        assert isinstance(recipe, DomRecipe)
        assert recipe.pagination.kind == "page"
        assert recipe.pagination.max_pages == 3


class TestParameterPagination:
    def test_reads_two_pages_and_dedupes(self):
        recipe = _recipe({"kind": "page", "param": "page", "max_pages": 5})
        # The first request is the listing URL as authored; paging starts after it.
        pages = Pages(
            {
                PAGE: _listing([1, 2]),
                f"{PAGE}?page=2": _listing([2, 3]),
                f"{PAGE}?page=3": "<html><body><p>No results</p></body></html>",
            }
        )
        report = run_dom_recipe_report(recipe, pages, PAGE)
        assert [job.url for job in report.jobs] == [
            "https://acme.example/jobs/1",
            "https://acme.example/jobs/2",
            "https://acme.example/jobs/3",
        ]
        assert report.page_counts == (2, 1)
        assert report.page_advance
        # The third document matched no container: the loop stops and keeps the rest.
        assert report.stop_reason == "empty_page"
        assert pages.calls == [PAGE, f"{PAGE}?page=2", f"{PAGE}?page=3"]

    def test_first_html_is_not_fetched_again(self):
        recipe = _recipe({"kind": "page", "param": "page", "max_pages": 2})
        pages = Pages({f"{PAGE}?page=2": _listing([3])})
        report = run_dom_recipe_report(recipe, pages, PAGE, first_html=_listing([1, 2]))
        assert pages.calls == [f"{PAGE}?page=2"]
        assert report.page_counts == (2, 1)

    def test_existing_value_is_replaced_not_appended(self):
        recipe = _recipe({"kind": "page", "param": "page", "max_pages": 2})
        url = "https://acme.example/careers?department=eng&page=1"
        pages = Pages(
            {
                "https://acme.example/careers?department=eng&page=2": _listing([3]),
            }
        )
        report = run_dom_recipe_report(recipe, pages, url, first_html=_listing([1, 2]))
        assert pages.calls == ["https://acme.example/careers?department=eng&page=2"]
        assert report.page_counts == (2, 1)

    def test_a_literal_placeholder_is_substituted(self):
        recipe = _recipe({"kind": "page", "param": "page", "max_pages": 2})
        url = "https://acme.example/careers/page/{page}"
        pages = Pages(
            {
                "https://acme.example/careers/page/1": _listing([1]),
                "https://acme.example/careers/page/2": _listing([2]),
            }
        )
        report = run_dom_recipe_report(recipe, pages, url)
        assert pages.calls == [
            "https://acme.example/careers/page/1",
            "https://acme.example/careers/page/2",
        ]
        assert report.page_counts == (1, 1)

    def test_offset_advances_by_the_step(self):
        recipe = _recipe({"kind": "offset", "param": "from", "start": 0, "step": 2, "max_pages": 2})
        pages = Pages(
            {
                PAGE: _listing([1, 2]),
                f"{PAGE}?from=2": _listing([3]),
            }
        )
        report = run_dom_recipe_report(recipe, pages, PAGE)
        assert pages.calls == [PAGE, f"{PAGE}?from=2"]
        assert report.page_counts == (2, 1)

    def test_page_that_adds_nothing_stops_the_loop(self):
        recipe = _recipe({"kind": "page", "param": "page", "max_pages": 5})
        pages = Pages(
            {
                PAGE: _listing([1, 2]),
                f"{PAGE}?page=2": _listing([1, 2]),
            }
        )
        report = run_dom_recipe_report(recipe, pages, PAGE)
        assert report.stop_reason == "no_new_jobs"
        assert report.page_counts == (2, 0)

    def test_page_budget_stops_the_loop(self):
        recipe = _recipe({"kind": "page", "param": "page", "max_pages": 1})
        pages = Pages({PAGE: _listing([1, 2])})
        report = run_dom_recipe_report(recipe, pages, PAGE)
        assert report.stop_reason == "page_cap"
        assert report.page_counts == (2,)

    def test_a_repeated_page_url_stops_the_loop(self):
        recipe = _recipe({"kind": "page", "param": "page", "max_pages": 5})
        # The pager number is ignored by the site, so page 2 is page 1 again.
        report = run_dom_recipe_report(recipe, lambda url: _listing([1, 2]), PAGE)
        assert report.stop_reason == "no_new_jobs"


class TestNextLinkPagination:
    def test_follows_the_selected_anchor(self):
        recipe = _recipe({"kind": "next", "selector": "a.next-page", "max_pages": 5})
        pages = Pages(
            {
                f"{PAGE}?p=2": _listing([3], next_href="?p=3"),
                f"{PAGE}?p=3": _listing([4]),
            }
        )
        report = run_dom_recipe_report(
            recipe, pages, PAGE, first_html=_listing([1, 2], next_href="?p=2")
        )
        assert report.page_counts == (2, 1, 1)
        assert report.stop_reason == "no_signal"

    def test_absent_anchor_is_a_clean_stop(self):
        recipe = _recipe({"kind": "next", "selector": "a.next-page", "max_pages": 5})
        report = run_dom_recipe_report(recipe, Pages({}), PAGE, first_html=_listing([1, 2]))
        assert report.stop_reason == "no_signal"
        assert report.page_counts == (2,)

    def test_offsite_next_link_is_reported_not_fetched(self):
        recipe = _recipe({"kind": "next", "selector": "a.next-page", "max_pages": 5})
        pages = Pages({})
        report = run_dom_recipe_report(
            recipe,
            pages,
            PAGE,
            first_html=_listing([1, 2], next_href="https://boards.example.net/acme?page=2"),
        )
        assert report.stop_reason == "offsite"
        assert pages.calls == []
        assert report.page_counts == (2,)

    def test_a_self_link_is_not_followed_twice(self):
        recipe = _recipe({"kind": "next", "selector": "a.next-page", "max_pages": 5})
        pages = Pages({})
        report = run_dom_recipe_report(recipe, pages, PAGE, first_html=_listing([1, 2], next_href=PAGE))
        assert report.stop_reason == "repeat"
        assert pages.calls == []

    def test_javascript_href_is_no_signal(self):
        recipe = _recipe({"kind": "next", "selector": "a.next-page", "max_pages": 5})
        report = run_dom_recipe_report(
            recipe, Pages({}), PAGE, first_html=_listing([1, 2], next_href="javascript:void(0)")
        )
        assert report.stop_reason == "no_signal"


class TestFailureHandling:
    def test_a_later_page_that_fails_keeps_what_was_collected(self):
        recipe = _recipe({"kind": "page", "param": "page", "max_pages": 5})
        pages = Pages(
            {PAGE: _listing([1, 2])},
            fail_on={f"{PAGE}?page=2"},
        )
        report = run_dom_recipe_report(recipe, pages, PAGE)
        assert report.stop_reason == "fetch_error"
        assert len(report.jobs) == 2

    def test_the_first_page_failure_fails_the_replay(self):
        recipe = _recipe({"kind": "page", "param": "page", "max_pages": 5})
        pages = Pages({}, fail_on={PAGE})
        with pytest.raises(RecipeError, match="request failed"):
            run_dom_recipe_report(recipe, pages, PAGE)

    def test_the_first_page_with_no_container_fails_the_replay(self):
        recipe = _recipe({"kind": "page", "param": "page", "max_pages": 5})
        pages = Pages({PAGE: "<html><body><p>Nothing</p></body></html>"})
        with pytest.raises(RecipeError, match="matched no elements"):
            run_dom_recipe_report(recipe, pages, PAGE)


class TestSinglePageRecipe:
    def test_none_kind_fetches_once_and_never_pages(self):
        recipe = _recipe()
        pages = Pages({PAGE: _listing([1, 2])})
        report = run_dom_recipe_report(recipe, pages, PAGE)
        assert pages.calls == [PAGE]
        assert report.stop_reason == "single_page"
        assert report.page_counts == (2,)
        assert [job.url for job in report.first_page_jobs] == [job.url for job in report.jobs]

    def test_first_page_jobs_excludes_later_pages(self):
        recipe = _recipe({"kind": "page", "param": "page", "max_pages": 2})
        pages = Pages({PAGE: _listing([1, 2]), f"{PAGE}?page=2": _listing([3])})
        report = run_dom_recipe_report(recipe, pages, PAGE)
        assert [job.url for job in report.first_page_jobs] == [
            "https://acme.example/jobs/1",
            "https://acme.example/jobs/2",
        ]

    def test_stop_reasons_are_the_declared_vocabulary(self):
        assert run_dom_recipe_report(_recipe(), Pages({PAGE: _listing([1])}), PAGE).stop_reason in DOM_STOP_REASONS

    def test_run_dom_recipe_still_reads_one_document(self):
        recipe = _recipe({"kind": "page", "param": "page", "max_pages": 2})
        assert len(run_dom_recipe(recipe, _listing([1, 2]), PAGE)) == 2
