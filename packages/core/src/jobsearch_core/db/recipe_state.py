"""Recipe attempt tracking on ``companies`` (Phase F1a, plan-scraping.md §Recipe authoring).

State, not time (owner decision, 2026-10-18): after ``RECIPE_TRIAL_CAP``
consecutive failed trials a company's ``recipe_state`` becomes ``recipe_fail``.
A company in ``recipe_fail`` is blocked from further trials until
``clear_recipe_fail`` (the manual rerun) resets it.

Invariants:

- A passing trial resets ``recipe_trials`` to 0 (the count is consecutive).
- A trial is never recorded against a ``recipe_fail`` company; the caller gets
  ``RecipeBlockedError`` instead, so a blocked company cannot silently run again.

Writes nothing but the two ``companies`` columns, and flushes; the caller commits.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from jobsearch_core.db.models import Company

RECIPE_TRIAL_CAP = 3
RECIPE_OK = "ok"
RECIPE_FAIL = "recipe_fail"


class RecipeBlockedError(Exception):
    """A trial was requested for a company whose recipe is in ``recipe_fail``."""


def ensure_trials_allowed(company: Company) -> None:
    if company.recipe_state == RECIPE_FAIL:
        raise RecipeBlockedError(
            f"company {company.id} recipe is recipe_fail after {company.recipe_trials} "
            "failed trials; a manual rerun must clear it"
        )


def record_trial(session: Session, company: Company, *, passed: bool) -> str:
    """Record one recipe trial outcome and return the resulting ``recipe_state``."""
    ensure_trials_allowed(company)
    if passed:
        company.recipe_trials = 0
    else:
        company.recipe_trials += 1
        if company.recipe_trials >= RECIPE_TRIAL_CAP:
            company.recipe_state = RECIPE_FAIL
    session.flush()
    return company.recipe_state


def clear_recipe_fail(session: Session, company: Company) -> None:
    """Manual rerun: return the company to ``ok`` with a fresh trial count."""
    company.recipe_state = RECIPE_OK
    company.recipe_trials = 0
    session.flush()
