"""Per-item detail requests on the generic API recipe interpreter (ingest/recipe.py).

Properties under test:
- A detail request that fails keeps the job with description None; the list
  replay is not failed by it.
- A detail host off the allowlist is refused at admission, before any request.
- A successful detail request fills the description from its paths.
"""

from __future__ import annotations

import pytest
from jobsearch_core.ingest.recipe import ApiRecipe, RecipeError, run_api_recipe

from tests.test_ats_json import FakeApiClient
from tests.test_recipe import _greenhouse_recipe

BASE = "https://boards.smartrecruiters.example/acme"


def _detail_recipe(**overrides) -> dict:
    body = _greenhouse_recipe() | {
        "allowed_hosts": ["api.smartrecruiters.example"],
        "url_template": "https://api.smartrecruiters.example/v1/postings?limit=100&offset={offset}",
        "pagination": {"kind": "offset", "param": "offset", "start": 0, "step": 100, "max_pages": 1},
        "list_path": ["content"],
        "field_map": {
            "title": {"path": ["name"]},
            "url_template": "https://jobs.example/{id}",
        },
        "detail": {
            "url": "https://api.smartrecruiters.example/v1/postings/{item.id}",
            "description_paths": [["jobAd", "sections", "jobDescription", "text"]],
        },
    }
    return body | overrides


def _summary(job_id: str = "p1") -> dict:
    return {"id": job_id, "name": "Data Analyst"}


def test_detail_failure_leaves_description_none_but_keeps_job():
    recipe = ApiRecipe.model_validate(_detail_recipe())
    client = FakeApiClient(
        {
            "offset=0": {"content": [_summary()], "totalFound": 1},
            "/postings/p1": RuntimeError("detail blocked"),
        }
    )
    jobs = run_api_recipe(recipe, client.fetch, BASE)
    assert len(jobs) == 1
    assert jobs[0].title == "Data Analyst"
    assert jobs[0].description is None


def test_successful_detail_fills_description():
    recipe = ApiRecipe.model_validate(_detail_recipe())
    detail = {"jobAd": {"sections": {"jobDescription": {"text": "<p>Build things.</p>"}}}}
    client = FakeApiClient(
        {
            "offset=0": {"content": [_summary()], "totalFound": 1},
            "/postings/p1": detail,
        }
    )
    jobs = run_api_recipe(recipe, client.fetch, BASE)
    assert jobs[0].description == "Build things."


def test_off_allowlist_detail_host_is_refused_at_admission():
    body = _detail_recipe(detail={"url": "https://evil.example/{item.id}", "description_paths": [["x"]]})
    with pytest.raises(ValueError, match="detail url host"):
        ApiRecipe.model_validate(body)


def test_detail_request_never_leaves_allowed_hosts():
    recipe = ApiRecipe.model_validate(_detail_recipe())
    client = FakeApiClient({"offset=0": {"content": [_summary()], "totalFound": 1}, "/postings/p1": {}})
    run_api_recipe(recipe, client.fetch, BASE)
    assert all("api.smartrecruiters.example" in call for call in client.calls)


def test_list_request_failure_still_raises_not_empty():
    recipe = ApiRecipe.model_validate(_detail_recipe())
    client = FakeApiClient({"offset=0": RuntimeError("503")})
    with pytest.raises(RecipeError, match="request failed"):
        run_api_recipe(recipe, client.fetch, BASE)


def test_smartrecruiters_recipe_reproduces_adapter_output():
    """The generic recipe matches the hand-written SmartRecruiters adapter on the same capture.

    Compared fields: title, url, location, posted_date, employment_type,
    seniority, description. The recipe reads the same JSON paths the adapter does.
    """
    from jobsearch_core.ingest.scrapers.ats_json import SmartRecruitersScraper

    summary = {
        "id": "p1",
        "name": "Data Analyst",
        "releasedDate": "2026-05-01T09:00:00Z",
        "location": {"fullLocation": "Riga, Latvia"},
        "typeOfEmployment": {"label": "Full-time"},
        "experienceLevel": {"label": "Mid"},
    }
    detail = {
        "jobAd": {
            "sections": {
                "jobDescription": {"text": "<p>Analyse data.</p>"},
                "qualifications": {"text": "<p>SQL.</p>"},
            }
        }
    }
    routes = {"/postings/p1": detail, "postings?limit": {"content": [summary], "totalFound": 1}}
    recipe = ApiRecipe.model_validate(
        {
            "kind": "api",
            "allowed_hosts": ["api.smartrecruiters.com"],
            "verification": {
                "expected_count": 1,
                "url_pattern": r"^https://jobs\.smartrecruiters\.com/acme/[^/]+$",
                "sample_titles": ["Data Analyst"],
            },
            "vars": {"company": "acme"},
            "url_template": (
                "https://api.smartrecruiters.com/v1/companies/{company}/postings?limit=100&offset={offset}"
            ),
            "pagination": {"kind": "offset", "param": "offset", "start": 0, "step": 100, "max_pages": 1},
            "list_path": ["content"],
            "field_map": {
                "url_template": "https://jobs.smartrecruiters.com/acme/{id}",
                "title": {"path": ["name"]},
                "location": {"path": ["location", "fullLocation"], "fallback": {"path": ["location", "city"]}},
                "posted_date": {"path": ["releasedDate"], "transform": "iso_date"},
                "employment_type": {"path": ["typeOfEmployment", "label"]},
                "seniority": {"path": ["experienceLevel", "label"]},
            },
            "detail": {
                "url": "https://api.smartrecruiters.com/v1/companies/{vars.company}/postings/{item.id}",
                "description_paths": [
                    ["jobAd", "sections", "jobDescription", "text"],
                    ["jobAd", "sections", "qualifications", "text"],
                ],
            },
        }
    )
    client = FakeApiClient(routes)
    replayed = run_api_recipe(recipe, client.fetch, "https://jobs.smartrecruiters.com/acme")
    adapter_jobs = SmartRecruitersScraper().scrape_listings_with_client(
        "", "https://jobs.smartrecruiters.com/acme", FakeApiClient(routes)
    )

    def fields(jobs):
        return [
            (j.title, j.url, j.location, j.posted_date, j.employment_type, j.seniority, j.description)
            for j in jobs
        ]

    assert fields(replayed) == fields(adapter_jobs)
    assert replayed[0].employment_type == "Full-time"
    assert replayed[0].seniority == "Mid"
