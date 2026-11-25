"""Phase F1: recipe schema, generic interpreter, replay verifier (no LLM, no network).

Properties under test:
- A request is made only to an allowlisted host.
- An empty list is never success; a failed request or unresolvable list path
  raises RecipeError, it does not return [].
- Pagination stops on an empty page or a page with no new URLs.
- verify_postings refuses only an empty replay; count, URL shape, sample titles,
  and reference overlap are reported as signals, never gated (owner, 2026-10-19).
- The Greenhouse and SmartRecruiters recipes reproduce their adapter output
  on the same payload (the F1 validation criterion from plan-scraping.md).
"""

from __future__ import annotations

import json

import pytest
from jobsearch_core.ingest.http import FetchError
from jobsearch_core.ingest.recipe import (
    ApiRecipe,
    DomRecipe,
    FieldMap,
    Pagination,
    RecipeError,
    Verification,
    run_api_recipe,
    run_dom_recipe,
    verify_postings,
)
from jobsearch_core.ingest.scrapers.ats_json import GreenhouseScraper, SmartRecruitersScraper
from jobsearch_core.ingest.scrapers.base import ScrapedJob
from pydantic import TypeAdapter, ValidationError

from tests.test_ats_json import GREENHOUSE_JOB, FakeApiClient

RECIPE_ADAPTER = TypeAdapter(ApiRecipe | DomRecipe)


def _verification(**overrides) -> dict:
    base = {
        "expected_count": 2,
        "sample_titles": ["Backend Engineer"],
        "url_pattern": r"^https://boards\.greenhouse\.io/acme/jobs/\d+$",
    }
    base.update(overrides)
    return base


def _greenhouse_recipe(**overrides) -> dict:
    body = {
        "kind": "api",
        "allowed_hosts": ["boards-api.greenhouse.io"],
        "verification": _verification(),
        "vars": {"token": "acme"},
        "url_template": "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
        "?content=true&paginate=true&page={page}",
        "pagination": {"kind": "page", "param": "page", "start": 1, "step": 1, "max_pages": 20},
        "list_path": ["jobs"],
        "field_map": {
            "url": {"path": ["absolute_url"]},
            "title": {"path": ["title"]},
            "location": {"path": ["location", "name"]},
            "posted_date": {"path": ["first_published"], "transform": "iso_date"},
            "description": {"path": ["content"], "transform": "html_text"},
        },
    }
    body.update(overrides)
    return body


def _greenhouse_pages(jobs: list[dict]) -> FakeApiClient:
    return FakeApiClient({"page=1": {"jobs": jobs, "meta": {"total": len(jobs)}}, "page=2": {"jobs": []}})


# ---- schema -----------------------------------------------------------------


class TestSchema:
    def test_api_and_dom_both_validate_by_kind(self):
        assert isinstance(RECIPE_ADAPTER.validate_python(_greenhouse_recipe()), ApiRecipe)
        dom = {
            "kind": "dom",
            "allowed_hosts": ["careers.example.com"],
            "verification": _verification(url_pattern=r"/job/"),
            "container": "li.job",
            "link": {"selector": "a", "attr": "href"},
            "title": {"selector": "h3"},
        }
        assert isinstance(RECIPE_ADAPTER.validate_python(dom), DomRecipe)

    def test_unknown_kind_is_rejected(self):
        with pytest.raises(ValidationError):
            RECIPE_ADAPTER.validate_python({**_greenhouse_recipe(), "kind": "llm"})

    def test_field_map_needs_exactly_one_url_source(self):
        bad = _greenhouse_recipe()
        bad["field_map"] = {"title": {"path": ["title"]}}
        with pytest.raises(ValidationError, match="exactly one"):
            ApiRecipe.model_validate(bad)

    def test_field_map_url_template_takes_bare_item_keys(self):
        # {item.id} is the detail-recipe syntax: it resolves nothing in a listing
        # item, so every posting would silently lose its URL at replay time.
        bad = _greenhouse_recipe(
            field_map={"url_template": "https://jobs.example.com/{item.id}", "title": {"path": ["title"]}}
        )
        with pytest.raises(ValidationError, match="bare item keys"):
            ApiRecipe.model_validate(bad)

    def test_field_map_url_template_needs_a_literal_https_host(self):
        for template in ("http://jobs.example.com/{id}", "https://{sub}.example.com/{id}"):
            bad = _greenhouse_recipe(
                field_map={"url_template": template, "title": {"path": ["title"]}}
            )
            with pytest.raises(ValidationError):
                ApiRecipe.model_validate(bad)

    def test_field_map_url_template_may_name_a_host_off_the_allowlist(self):
        # A posting lives on the job site, not on the API host the recipe reads.
        recipe = ApiRecipe.model_validate(
            _greenhouse_recipe(
                field_map={
                    "url_template": "https://jobs.example.com/acme/{id}",
                    "title": {"path": ["title"]},
                }
            )
        )
        assert recipe.field_map.url_template == "https://jobs.example.com/acme/{id}"

    def test_pagination_template_must_carry_the_param(self):
        bad = _greenhouse_recipe(url_template="https://boards-api.greenhouse.io/v1/boards/acme/jobs")
        with pytest.raises(ValidationError, match="must contain"):
            ApiRecipe.model_validate(bad)

    def test_offset_pagination_needs_step(self):
        bad = _greenhouse_recipe(pagination={"kind": "offset", "param": "page"})
        with pytest.raises(ValidationError, match="step"):
            ApiRecipe.model_validate(bad)

    def test_dom_link_needs_attr(self):
        with pytest.raises(ValidationError, match="attr"):
            DomRecipe.model_validate(
                {
                    "kind": "dom",
                    "allowed_hosts": ["x.com"],
                    "verification": _verification(url_pattern="x"),
                    "container": "li",
                    "link": {"selector": "a"},
                    "title": {"selector": "a"},
                }
            )

    def test_invalid_url_pattern_is_rejected(self):
        with pytest.raises(ValidationError):
            Verification.model_validate(_verification(url_pattern="("))


# ---- api interpreter --------------------------------------------------------


class TestApiInterpreter:
    def test_refuses_host_outside_allowlist_before_any_request(self):
        # F1a: an off-allowlist host is refused at admission, so no recipe
        # with that host exists to replay. The fetch-time guard is the second
        # line; it is exercised here on a recipe built without validation.
        client = FakeApiClient({"page=1": {"jobs": []}})
        with pytest.raises(ValueError, match="allowed_hosts"):
            ApiRecipe.model_validate(_greenhouse_recipe(allowed_hosts=["example.com"]))
        recipe = ApiRecipe.model_construct(
            **(
                _greenhouse_recipe(allowed_hosts=["example.com"])
                | {
                    "pagination": Pagination.model_validate(_greenhouse_recipe()["pagination"]),
                    "field_map": FieldMap.model_validate(_greenhouse_recipe()["field_map"]),
                    "verification": Verification.model_validate(_greenhouse_recipe()["verification"]),
                }
            )
        )
        with pytest.raises(RecipeError, match="allowed_hosts"):
            run_api_recipe(recipe, client.fetch_json, "https://boards.greenhouse.io/acme")
        assert client.calls == []

    def test_request_failure_raises_not_empty_list(self):
        client = FakeApiClient({"page=1": RuntimeError("503")})
        recipe = ApiRecipe.model_validate(_greenhouse_recipe())
        with pytest.raises(RecipeError, match="request failed"):
            run_api_recipe(recipe, client.fetch_json, "https://boards.greenhouse.io/acme")

    def test_non_json_body_raises(self):
        client = FakeApiClient({"page=1": "<html>blocked</html>"})
        recipe = ApiRecipe.model_validate(_greenhouse_recipe())
        with pytest.raises(RecipeError, match="request failed"):
            run_api_recipe(recipe, client.fetch_json, "https://boards.greenhouse.io/acme")

    def test_transport_failure_keeps_its_attribution(self):
        """Fix 2: the recipe error carries tier/status/content-type, not just a parse error."""
        def fetch(url: str) -> str:
            raise FetchError(
                "non-JSON body from https://api.greenhouse.io/v1/boards/acme/jobs",
                url=url,
                tier="static",
                status=200,
                content_type="text/html",
            )

        recipe = ApiRecipe.model_validate(_greenhouse_recipe())
        with pytest.raises(RecipeError) as excinfo:
            run_api_recipe(recipe, fetch, "https://boards.greenhouse.io/acme")
        message = str(excinfo.value)
        assert "tier=static" in message
        assert "status=200" in message
        assert "content-type=text/html" in message

    def test_list_path_that_does_not_resolve_raises(self):
        client = FakeApiClient({"page=1": {"postings": []}})
        recipe = ApiRecipe.model_validate(_greenhouse_recipe())
        with pytest.raises(RecipeError, match="list_path"):
            run_api_recipe(recipe, client.fetch_json, "https://boards.greenhouse.io/acme")

    def test_pagination_stops_on_empty_page(self):
        jobs = [GREENHOUSE_JOB]
        client = _greenhouse_pages(jobs)
        recipe = ApiRecipe.model_validate(_greenhouse_recipe())
        run_api_recipe(recipe, client.fetch_json, "https://boards.greenhouse.io/acme")
        assert len(client.calls) == 2  # page 1 has jobs, page 2 is empty -> stop

    def test_pagination_stops_when_a_page_adds_no_new_urls(self):
        client = FakeApiClient({"page=1": {"jobs": [GREENHOUSE_JOB]}, "page=2": {"jobs": [GREENHOUSE_JOB]}})
        recipe = ApiRecipe.model_validate(
            _greenhouse_recipe(pagination={"kind": "page", "param": "page", "max_pages": 5})
        )
        jobs = run_api_recipe(recipe, client.fetch_json, "https://boards.greenhouse.io/acme")
        assert len(jobs) == 1
        assert len(client.calls) == 2

    def test_max_pages_caps_requests(self):
        pages = {
            f"page={n}": {"jobs": [{**GREENHOUSE_JOB, "absolute_url": f"https://boards.greenhouse.io/acme/jobs/{n}"}]}
            for n in range(1, 10)
        }
        client = FakeApiClient(pages)
        recipe = ApiRecipe.model_validate(
            _greenhouse_recipe(pagination={"kind": "page", "param": "page", "max_pages": 3})
        )
        run_api_recipe(recipe, client.fetch_json, "https://boards.greenhouse.io/acme")
        assert len(client.calls) == 3

    def test_cursor_pagination_follows_next_token(self):
        # Route by the cursor value: first page has next=abc, second page empty.
        calls: list[str] = []

        def fetch(url: str) -> str:
            calls.append(url)
            if "cursor=abc" in url:
                return json.dumps(
                    {
                        "items": [{**GREENHOUSE_JOB, "absolute_url": "https://boards.greenhouse.io/acme/jobs/2"}],
                        "next": "",
                    }
                )
            return json.dumps({"items": [GREENHOUSE_JOB], "next": "abc"})

        recipe = ApiRecipe.model_validate(
            _greenhouse_recipe(
                url_template="https://boards-api.greenhouse.io/v1/boards/acme/jobs?cursor={cursor}",
                pagination={"kind": "cursor", "param": "cursor", "start": "", "cursor_path": ["next"], "max_pages": 5},
                list_path=["items"],
            )
        )
        jobs = run_api_recipe(recipe, fetch, "https://boards.greenhouse.io/acme")
        assert [j.url for j in jobs] == [
            "https://boards.greenhouse.io/acme/jobs/1001",
            "https://boards.greenhouse.io/acme/jobs/2",
        ]
        assert len(calls) == 2

    def test_relative_url_resolves_against_base(self):
        client = FakeApiClient({"page=1": {"jobs": [{"title": "Dev", "href": "/job/9"}]}, "page=2": {"jobs": []}})
        recipe = ApiRecipe.model_validate(
            _greenhouse_recipe(
                allowed_hosts=["boards-api.greenhouse.io"],
                field_map={"url": {"path": ["href"]}, "title": {"path": ["title"]}},
            )
        )
        jobs = run_api_recipe(recipe, client.fetch_json, "https://careers.example.com/x")
        assert jobs[0].url == "https://careers.example.com/job/9"

    def test_url_template_field_with_literal_vars(self):
        client = FakeApiClient({"offset=0": {"content": [{"id": "77", "name": "Dev"}]}, "offset=100": {"content": []}})
        recipe = ApiRecipe.model_validate(
            {
                "kind": "api",
                "allowed_hosts": ["api.smartrecruiters.com"],
                "verification": _verification(expected_count=1, sample_titles=["Dev"], url_pattern=r"/acme/77$"),
                "vars": {"company": "acme"},
                "url_template": "https://api.smartrecruiters.com/v1/companies/{company}/postings?offset={offset}",
                "pagination": {"kind": "offset", "param": "offset", "start": 0, "step": 100},
                "list_path": ["content"],
                "field_map": {
                    "url_template": "https://jobs.smartrecruiters.com/{company}/{id}",
                    "title": {"path": ["name"]},
                },
            }
        )
        jobs = run_api_recipe(recipe, client.fetch_json, "https://careers.smartrecruiters.com/acme")
        assert jobs[0].url == "https://jobs.smartrecruiters.com/acme/77"


# ---- dom interpreter --------------------------------------------------------

LISTING_HTML = """
<nav><a href="/about">About us</a></nav>
<ul>
  <li class="job"><a href="/job/1"><h3>Data Engineer</h3></a><span class="loc">Helsinki</span></li>
  <li class="job"><a href="/job/2"><h3>Product Manager</h3></a><span class="loc">Oslo</span></li>
  <li class="job"><a href="/job/2"><h3>Product Manager</h3></a><span class="loc">Oslo</span></li>
  <li class="job"><span>no link here</span></li>
</ul>
"""


class TestDomInterpreter:
    def _recipe(self, **overrides) -> DomRecipe:
        body = {
            "kind": "dom",
            "allowed_hosts": ["careers.example.com"],
            "verification": _verification(expected_count=2, url_pattern=r"/job/\d+$"),
            "container": "li.job",
            "link": {"selector": "a", "attr": "href"},
            "title": {"selector": "h3"},
            "location": {"selector": "span.loc"},
        }
        body.update(overrides)
        return DomRecipe.model_validate(body)

    def test_extracts_postings_inside_containers_only(self):
        jobs = run_dom_recipe(self._recipe(), LISTING_HTML, "https://careers.example.com/jobs")
        assert [(j.url, j.title, j.location) for j in jobs] == [
            ("https://careers.example.com/job/1", "Data Engineer", "Helsinki"),
            ("https://careers.example.com/job/2", "Product Manager", "Oslo"),
        ]

    def test_nav_links_are_not_matched_by_a_container_selector(self):
        # The nav <a> is not inside li.job, so it cannot be a posting.
        jobs = run_dom_recipe(self._recipe(), LISTING_HTML, "https://careers.example.com/jobs")
        assert all("/about" not in j.url for j in jobs)

    def test_refuses_page_off_allowlist(self):
        with pytest.raises(RecipeError, match="allowed_hosts"):
            run_dom_recipe(self._recipe(), LISTING_HTML, "https://evil.example.net/jobs")


# ---- verifier ---------------------------------------------------------------


def _job(url: str, title: str = "Backend Engineer") -> ScrapedJob:
    return ScrapedJob(url=url, title=title, company="")


class TestVerify:
    VER = Verification(
        expected_count=2,
        sample_titles=["Backend Engineer"],
        url_pattern=r"^https://boards\.greenhouse\.io/acme/jobs/\d+$",
    )

    def test_empty_list_is_never_success(self):
        result = verify_postings([], self.VER)
        assert not result.ok
        assert "empty" in result.reason

    def test_count_mismatch_is_recorded_not_gated(self):
        result = verify_postings([_job("https://boards.greenhouse.io/acme/jobs/1")], self.VER)
        assert result.ok, result.reason
        assert result.count == 1  # vs expected_count=2: drift info for the caller

    def test_url_outside_pattern_is_counted_not_refused(self):
        postings = [
            _job("https://boards.greenhouse.io/acme/jobs/1"),
            _job("https://boards.greenhouse.io/acme/about"),
        ]
        result = verify_postings(postings, self.VER)
        assert result.ok, result.reason  # one nav link does not discard the posting
        assert result.count == 2
        assert result.off_pattern == 1

    def test_missing_sample_title_is_recorded_not_refused(self):
        postings = [
            _job("https://boards.greenhouse.io/acme/jobs/1", "Other"),
            _job("https://boards.greenhouse.io/acme/jobs/2", "Other"),
        ]
        result = verify_postings(postings, self.VER)
        assert result.ok, result.reason
        assert result.missing_titles == ("Backend Engineer",)

    def test_sample_title_match_is_case_and_whitespace_insensitive(self):
        postings = [
            _job("https://boards.greenhouse.io/acme/jobs/1", "  backend   ENGINEER "),
            _job("https://boards.greenhouse.io/acme/jobs/2", "Other"),
        ]
        result = verify_postings(postings, self.VER)
        assert result.missing_titles == ()

    def test_reference_overlap_is_counted_not_refused(self):
        postings = [
            _job("https://boards.greenhouse.io/acme/jobs/1"),
            _job("https://boards.greenhouse.io/acme/jobs/2"),
        ]
        ref = frozenset({"https://boards.greenhouse.io/acme/jobs/1"})
        result = verify_postings(postings, self.VER, reference_urls=ref)
        assert result.ok, result.reason
        assert result.outside_reference == 1

    def test_verified_when_all_rules_pass(self):
        postings = [
            _job("https://boards.greenhouse.io/acme/jobs/1"),
            _job("https://boards.greenhouse.io/acme/jobs/2", "Frontend Engineer"),
        ]
        assert verify_postings(postings, self.VER).ok


# ---- equivalence with the existing adapters (F1 validation criterion) ------


def _adapter_key(job: ScrapedJob) -> tuple:
    return (job.url, job.title, job.location, job.posted_date, job.description)


class TestAdapterEquivalence:
    def test_greenhouse_recipe_matches_adapter_output(self):
        jobs = [
            GREENHOUSE_JOB,
            {
                **GREENHOUSE_JOB,
                "title": "Frontend Engineer",
                "absolute_url": "https://boards.greenhouse.io/acme/jobs/1002",
            },
        ]
        client = _greenhouse_pages(jobs)
        adapter = GreenhouseScraper().scrape_listings_with_client("", "https://boards.greenhouse.io/acme", client)
        client2 = _greenhouse_pages(jobs)
        recipe = ApiRecipe.model_validate(_greenhouse_recipe())
        replayed = run_api_recipe(recipe, client2.fetch_json, "https://boards.greenhouse.io/acme")
        assert [_adapter_key(j) for j in replayed] == [_adapter_key(j) for j in adapter]
        assert verify_postings(replayed, recipe.verification).ok

    def test_smartrecruiters_recipe_matches_adapter_listing_fields(self):
        listing = {
            "offset": 0,
            "limit": 100,
            "totalFound": 2,
            "content": [
                {
                    "id": "744000146134319",
                    "name": "eCommerce Vendor Management Lead",
                    "releasedDate": "2026-08-28T08:10:53.368Z",
                    "location": {"city": "Helsinki", "country": "fi", "fullLocation": "Helsinki, Finland"},
                    "typeOfEmployment": {"id": "permanent", "label": "Full-time"},
                    "experienceLevel": {"id": "mid_senior_level", "label": "Mid-Senior Level"},
                },
                {
                    "id": "744000146134320",
                    "name": "Backend Developer",
                    "releasedDate": "2026-08-27T08:10:53.368Z",
                    "location": {"city": "Oslo", "country": "no"},
                },
            ],
        }
        detail_routes = {
            "/postings/744000146134319": {"jobAd": {"sections": {}}},
            "/postings/744000146134320": {"jobAd": {"sections": {}}},
        }
        client = FakeApiClient({"offset=0": listing, **detail_routes})
        adapter = SmartRecruitersScraper().scrape_listings_with_client(
            "", "https://careers.smartrecruiters.com/acme", client
        )

        recipe = ApiRecipe.model_validate(
            {
                "kind": "api",
                "allowed_hosts": ["api.smartrecruiters.com"],
                "verification": {
                    "expected_count": 2,
                    "sample_titles": ["Backend Developer"],
                    "url_pattern": r"^https://jobs\.smartrecruiters\.com/acme/\d+$",
                },
                "vars": {"company": "acme"},
                "url_template": "https://api.smartrecruiters.com/v1/companies/{company}/postings?limit=100&offset={offset}",
                "pagination": {"kind": "offset", "param": "offset", "start": 0, "step": 100},
                "list_path": ["content"],
                "field_map": {
                    "url_template": "https://jobs.smartrecruiters.com/{company}/{id}",
                    "title": {"path": ["name"]},
                    "location": {"path": ["location", "fullLocation"], "fallback": {"path": ["location", "city"]}},
                    "posted_date": {"path": ["releasedDate"], "transform": "iso_date"},
                    "employment_type": {"path": ["typeOfEmployment", "label"]},
                    "seniority": {"path": ["experienceLevel", "label"]},
                },
            }
        )
        replay_client = FakeApiClient({"offset=0": listing, "offset=100": {"content": []}})
        replayed = run_api_recipe(recipe, replay_client.fetch_json, "https://careers.smartrecruiters.com/acme")

        # The adapter adds a per-posting description from a detail call; the
        # recipe does not (F1 has no per-item detail fetch). Compare the rest.
        def strip(job: ScrapedJob) -> tuple:
            return (job.url, job.title, job.location, job.posted_date, job.employment_type, job.seniority)

        assert [strip(j) for j in replayed] == [strip(j) for j in adapter]
        assert verify_postings(replayed, recipe.verification).ok


class TestFailureEvidence:
    """A failed replay names the step, URL, path, or field that failed.

    "empty list is never success" cannot distinguish a wrong list path from a
    wrong field map from a different response than the capture, and the author
    only sees the reason string (2026-11-02).
    """

    @staticmethod
    def _run(body: dict, client: FakeApiClient):
        return run_api_recipe(ApiRecipe.model_validate(body), client.fetch_json, "https://boards.greenhouse.io/acme")

    def test_list_path_miss_names_the_observed_keys_and_url(self):
        client = FakeApiClient({"page=1": {"data": {"jobs": [GREENHOUSE_JOB]}}})
        with pytest.raises(RecipeError) as excinfo:
            self._run(_greenhouse_recipe(), client)
        message = str(excinfo.value)
        assert "does not resolve to a list" in message
        assert "['data']" in message
        assert "page=1" in message

    def test_empty_first_page_names_the_request(self):
        client = FakeApiClient({"page=1": {"jobs": []}})
        with pytest.raises(RecipeError) as excinfo:
            self._run(_greenhouse_recipe(), client)
        message = str(excinfo.value)
        assert "empty list" in message
        assert "page=1" in message

    def test_items_without_a_title_name_the_field(self):
        item = {"absolute_url": "https://boards.greenhouse.io/acme/jobs/1"}
        client = FakeApiClient({"page=1": {"jobs": [item, dict(item)]}})
        with pytest.raises(RecipeError) as excinfo:
            self._run(_greenhouse_recipe(), client)
        message = str(excinfo.value)
        assert "2x no title at ['title']" in message

    def test_unresolved_field_map_placeholder_names_it_and_the_item_keys(self):
        body = _greenhouse_recipe()
        body["field_map"] = {
            "url_template": "https://boards.greenhouse.io/acme/jobs/{Id}",
            "title": {"path": ["title"]},
        }
        client = FakeApiClient({"page=1": {"jobs": [{"title": "Backend Engineer"}]}})
        with pytest.raises(RecipeError) as excinfo:
            self._run(body, client)
        message = str(excinfo.value)
        assert "placeholder {Id}" in message
        assert "item keys" in message

    def test_var_value_carrying_braces_cannot_ship_as_a_placeholder(self):
        body = _greenhouse_recipe(vars={"token": "{nope}"})
        client = FakeApiClient({"page=1": {"jobs": [GREENHOUSE_JOB]}})
        with pytest.raises(RecipeError, match="could not be substituted"):
            self._run(body, client)


class TestDomFailureEvidence:
    DOM = {
        "kind": "dom",
        "allowed_hosts": ["careers.example.com"],
        "verification": _verification(url_pattern=r"/job/"),
        "container": "li.job",
        "link": {"selector": "a", "attr": "href"},
        "title": {"selector": "h3"},
    }
    PAGE = "https://careers.example.com/jobs"

    def test_container_matching_nothing_names_the_selector(self):
        recipe = DomRecipe.model_validate(self.DOM)
        with pytest.raises(RecipeError) as excinfo:
            run_dom_recipe(recipe, "<html><body><p>nothing here</p></body></html>", self.PAGE)
        message = str(excinfo.value)
        assert "li.job" in message
        assert "matched no elements" in message
        assert self.PAGE in message

    def test_containers_without_links_report_counts(self):
        recipe = DomRecipe.model_validate(self.DOM)
        html = "<ul><li class='job'><h3>A</h3></li><li class='job'><h3>B</h3></li></ul>"
        with pytest.raises(RecipeError) as excinfo:
            run_dom_recipe(recipe, html, self.PAGE)
        message = str(excinfo.value)
        assert "matched 2 elements" in message
        assert "2 had no link" in message
