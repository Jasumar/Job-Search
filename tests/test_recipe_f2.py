"""Phase F2: hand-written recipes replayed against real captures (no LLM, no network).

Properties under test:
- The Airbnb (Greenhouse board) recipe, replayed on the captured board payload,
  yields all 173 postings, passes verify_postings, and matches the Greenhouse
  adapter field-for-field on the same payload.
- The Apple search recipe, replayed on the captured search page, yields the 20
  listing rows and passes verify_postings.
- Both recipes refuse to answer when the payload or page is wrong (empty list is
  never success).

Captures live in tests/fixtures/recipes/ (live fetch, 2026-10-17). The Apple
recipe covers page 1 only; the page-2 probe is the existing crawler's job, not
this recipe's.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jobsearch_core.ingest.recipe import (
    ApiRecipe,
    DomRecipe,
    RecipeError,
    run_api_recipe,
    run_dom_recipe,
    verify_postings,
)
from jobsearch_core.ingest.scrapers.ats_json import GreenhouseScraper
from pydantic import TypeAdapter

from tests.test_ats_json import FakeApiClient

RECIPES = Path(__file__).parent / "fixtures" / "recipes"
RECIPE_ADAPTER = TypeAdapter(ApiRecipe | DomRecipe)


def _airbnb_recipe() -> ApiRecipe:
    body = json.loads((RECIPES / "airbnb_greenhouse.recipe.json").read_text())
    recipe = RECIPE_ADAPTER.validate_python(body)
    assert isinstance(recipe, ApiRecipe)
    return recipe


def _apple_recipe() -> DomRecipe:
    body = json.loads((RECIPES / "apple_search.recipe.json").read_text())
    recipe = RECIPE_ADAPTER.validate_python(body)
    assert isinstance(recipe, DomRecipe)
    return recipe


def _airbnb_payload() -> str:
    return (RECIPES / "airbnb_greenhouse_jobs.json").read_text()


def _apple_html() -> str:
    return (RECIPES / "apple_search_page1.html").read_text(encoding="utf-8")


def _airbnb_fetch(calls: list[str]):
    """Serve the captured board payload for every page; page 2 repeats page 1,
    which is what the live API returns and exercises the no-new-URLs stop rule."""
    payload = _airbnb_payload()

    def fetch(url: str) -> str:
        calls.append(url)
        return payload

    return fetch


def _adapter_key(job) -> tuple:
    return (job.url, job.title, job.location, job.posted_date, job.description)


class TestAirbnbGreenhouseRecipe:
    def test_replay_yields_full_board_and_verifies(self):
        recipe = _airbnb_recipe()
        calls: list[str] = []
        jobs = run_api_recipe(recipe, _airbnb_fetch(calls), "https://careers.airbnb.com/positions/")
        assert len(jobs) == 173
        assert verify_postings(jobs, recipe.verification).ok

    def test_pagination_stops_on_repeat_page(self):
        # Page 2 repeats page 1 (no new URLs), so the recipe stops after two requests.
        recipe = _airbnb_recipe()
        calls: list[str] = []
        run_api_recipe(recipe, _airbnb_fetch(calls), "https://careers.airbnb.com/positions/")
        assert len(calls) == 2
        assert "page=1" in calls[0] and "page=2" in calls[1]

    def test_matches_greenhouse_adapter_output(self):
        # The adapter reads the same board. Compared fields: url, title, location,
        # posted_date, description. The adapter stops after one page because
        # meta.total is 173; the recipe requests a second page and stops on a repeat.
        recipe = _airbnb_recipe()
        replayed = run_api_recipe(recipe, _airbnb_fetch([]), "https://careers.airbnb.com/positions/")
        client = FakeApiClient({"page=1": _airbnb_payload()})
        adapter = GreenhouseScraper().scrape_listings_with_client(
            "", "https://boards.greenhouse.io/airbnb", client
        )
        assert [_adapter_key(j) for j in replayed] == [_adapter_key(j) for j in adapter]

    def test_empty_board_is_refused_not_success(self):
        recipe = _airbnb_recipe()
        # An empty first page is now refused at the request that returned it, so
        # the author is told which URL came back empty instead of "no postings".
        with pytest.raises(RecipeError, match="resolved to an empty list"):
            run_api_recipe(
                recipe,
                lambda url: json.dumps({"jobs": [], "meta": {"total": 0}}),
                "https://careers.airbnb.com/positions/",
            )
        assert not verify_postings([], recipe.verification).ok

    def test_non_board_payload_raises(self):
        recipe = _airbnb_recipe()
        try:
            run_api_recipe(recipe, lambda url: "<html>blocked</html>", "https://careers.airbnb.com/positions/")
        except RecipeError:
            return
        raise AssertionError("expected RecipeError on non-JSON body")


class TestAppleSearchRecipe:
    def test_replay_yields_listing_rows_and_verifies(self):
        recipe = _apple_recipe()
        jobs = run_dom_recipe(recipe, _apple_html(), "https://jobs.apple.com/en-us/search")
        assert len(jobs) == 20
        assert verify_postings(jobs, recipe.verification).ok

    def test_each_posting_carries_location(self):
        recipe = _apple_recipe()
        jobs = run_dom_recipe(recipe, _apple_html(), "https://jobs.apple.com/en-us/search")
        assert all(j.location == "Various Locations within United States" for j in jobs)

    def test_page_without_listing_rows_is_refused(self):
        recipe = _apple_recipe()
        with pytest.raises(RecipeError, match="matched no elements"):
            run_dom_recipe(recipe, "<html><body><p>No results</p></body></html>", "https://jobs.apple.com/en-us/search")
        assert not verify_postings([], recipe.verification).ok

    def test_refuses_page_off_allowlist(self):
        recipe = _apple_recipe()
        try:
            run_dom_recipe(recipe, _apple_html(), "https://www.apple.com/careers/us/")
        except RecipeError:
            return
        raise AssertionError("expected RecipeError for host outside allowed_hosts")
