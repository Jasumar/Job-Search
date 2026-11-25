"""F3 smoke run: LLM recipe author on the Airbnb (api) and Apple (dom) fixtures.

Not wired into any scrape path. Uses an in-memory SQLite database, the captured
fixtures in tests/fixtures/recipes/, and one real OpenRouter model. Prints each
trial's outcome and the stored recipe (if any). Makes at most 3 LLM calls per
company (RECIPE_TRIAL_CAP).

Run from the repo root:
    uv run python benchmarks/recipe_author_smoke.py            # both companies
    uv run python benchmarks/recipe_author_smoke.py airbnb     # one company
    uv run python benchmarks/recipe_author_smoke.py --model X  # override model
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from jobsearch_core.config import Settings
from jobsearch_core.db.models import Base, Company, CompanyRecipe
from jobsearch_core.ingest.recipe import ApiRecipe, DomRecipe, run_api_recipe, run_dom_recipe
from jobsearch_core.ingest.recipe_author import Artifact, AuthorInput, author_recipe
from jobsearch_core.llm.client import LLMClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

FIX = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "recipes"

AIRBNB_BASE = "https://boards-api.greenhouse.io/v1/boards/airbnb/jobs"
AIRBNB_PATTERN = r"^https://careers\.airbnb\.com/positions/\d+\?gh_jid=\d+$"
APPLE_PAGE = "https://jobs.apple.com/en-us/search"
APPLE_PATTERN = r"^https://jobs\.apple\.com/en-us/details/\d+(-\d+)?/[^/?]+(\?team=[A-Z]+)?$"


def airbnb_input(company_id: int) -> AuthorInput:
    payload = (FIX / "airbnb_greenhouse_jobs.json").read_text()
    # Replay uses the captured payload (offline). Each page request returns the same
    # capture, so the replay stops after a repeat page; reference count is 173.
    def fetch(url: str) -> str:
        return payload

    seed = ApiRecipe.model_validate(
        {
            **json.loads((FIX / "airbnb_greenhouse.recipe.json").read_text()),
            "verification": {"expected_count": 1, "sample_titles": ["x"], "url_pattern": ".*"},
        }
    )
    reference = run_api_recipe(seed, fetch, AIRBNB_BASE)
    return AuthorInput(
        company_id=company_id,
        allowed_hosts=["boards-api.greenhouse.io"],
        url_pattern=AIRBNB_PATTERN,
        page_url=AIRBNB_BASE,
        reference=reference,
        artifacts=[Artifact("json_1", "xhr", AIRBNB_BASE, payload, tuple(reference))],
        carried_by="json_1",
        fetch=fetch,
    )


def apple_input(company_id: int) -> AuthorInput:
    html = (FIX / "apple_search_page1.html").read_text(encoding="utf-8")
    seed = DomRecipe.model_validate(json.loads((FIX / "apple_search.recipe.json").read_text()))
    reference = run_dom_recipe(seed, html, APPLE_PAGE)
    return AuthorInput(
        company_id=company_id,
        allowed_hosts=["jobs.apple.com"],
        url_pattern=APPLE_PATTERN,
        page_url=APPLE_PAGE,
        reference=reference,
        artifacts=[Artifact("static_html", "static", APPLE_PAGE, html, tuple(reference))],
        carried_by="static_html",
    )


TARGETS = {
    "airbnb": ("Airbnb", airbnb_input),
    "apple": ("Apple Inc.", apple_input),
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("targets", nargs="*", default=list(TARGETS))
    parser.add_argument("--model", default=None)
    args = parser.parse_args()

    settings = Settings()
    if not settings.openrouter_api_key:
        print("OPENROUTER_API_KEY is not set", file=sys.stderr)
        return 2
    model = args.model or settings.analysis_model
    llm = LLMClient(
        api_key=settings.openrouter_api_key,
        model=model,
        base_url=settings.llm_base_url,
        min_interval_seconds=settings.llm_min_interval_seconds,
        max_retries=settings.llm_max_retries,
    )
    print(f"model: {model}")

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session: Session = sessionmaker(bind=engine)()

    for key in args.targets:
        name, build = TARGETS[key]
        company = Company(name=name, careers_url=None)
        session.add(company)
        session.flush()
        inp = build(company.id)
        print(f"\n== {name}: reference {len(inp.reference)} postings, self-chosen kind")
        print(f"  capture: carried_by={inp.carried_by} artifacts={[(a.name, a.fetch_mode) for a in inp.artifacts]}")
        result = author_recipe(session, company, inp, llm)
        session.commit()
        print(
            f"status: {result.status}  kind: {result.kind}  "
            f"artifact: {result.source_artifact}  recipe_state: {result.recipe_state}"
        )
        for t in result.trials:
            print(f"  trial {t.number}: {'PASS' if t.passed else 'FAIL'}" + (f" - {t.reason}" if t.reason else ""))
        if result.recipe_id is not None:
            row = session.get(CompanyRecipe, result.recipe_id)
            print("stored body:")
            print(json.dumps(json.loads(row.body), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
