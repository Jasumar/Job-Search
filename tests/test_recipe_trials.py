"""Phase F1a: recipe attempt counter on companies (no LLM, no network).

Properties under test:
- Three consecutive failed trials set recipe_state to recipe_fail; fewer do not.
- A passing trial resets the consecutive count.
- A recipe_fail company refuses further trials (RecipeBlockedError) and its state
  and count are left unchanged.
- clear_recipe_fail (manual rerun) returns the company to ok with count 0.
"""

from __future__ import annotations

import pytest
from jobsearch_core.db.models import Company
from jobsearch_core.db.recipe_state import (
    RECIPE_FAIL,
    RECIPE_OK,
    RECIPE_TRIAL_CAP,
    RecipeBlockedError,
    clear_recipe_fail,
    record_trial,
)


def _company(session) -> Company:
    company = Company(name="Acme", careers_url=None)
    session.add(company)
    session.commit()
    session.refresh(company)
    return company


def test_cap_is_three():
    assert RECIPE_TRIAL_CAP == 3


def test_two_failures_stay_ok(session):
    company = _company(session)
    assert record_trial(session, company, passed=False) == RECIPE_OK
    assert record_trial(session, company, passed=False) == RECIPE_OK
    assert company.recipe_trials == 2


def test_third_consecutive_failure_sets_recipe_fail(session):
    company = _company(session)
    for _ in range(2):
        record_trial(session, company, passed=False)
    assert record_trial(session, company, passed=False) == RECIPE_FAIL
    session.commit()
    session.refresh(company)
    assert company.recipe_state == RECIPE_FAIL
    assert company.recipe_trials == 3


def test_pass_resets_consecutive_count(session):
    company = _company(session)
    record_trial(session, company, passed=False)
    record_trial(session, company, passed=False)
    record_trial(session, company, passed=True)
    assert company.recipe_trials == 0
    # Two more failures after the reset are still below the cap.
    record_trial(session, company, passed=False)
    assert record_trial(session, company, passed=False) == RECIPE_OK


def test_recipe_fail_blocks_trials_and_leaves_state_unchanged(session):
    company = _company(session)
    for _ in range(RECIPE_TRIAL_CAP):
        record_trial(session, company, passed=False)
    for passed in (True, False):
        with pytest.raises(RecipeBlockedError):
            record_trial(session, company, passed=passed)
    assert company.recipe_state == RECIPE_FAIL
    assert company.recipe_trials == RECIPE_TRIAL_CAP


def test_manual_rerun_clears_recipe_fail(session):
    company = _company(session)
    for _ in range(RECIPE_TRIAL_CAP):
        record_trial(session, company, passed=False)
    clear_recipe_fail(session, company)
    session.commit()
    session.refresh(company)
    assert company.recipe_state == RECIPE_OK
    assert company.recipe_trials == 0
    assert record_trial(session, company, passed=False) == RECIPE_OK
