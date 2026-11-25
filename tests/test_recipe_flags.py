"""Storage for gate answers (R3).

Properties under test: a flag is keyed by the exact body hash (so a re-authored
body never inherits an analysis), rows are a history rather than an upsert, the
stop rule reads "answered none before" per body, and a malformed key or verdict
is refused before it can be mistaken for evidence.
"""

from __future__ import annotations

import json

import pytest
from jobsearch_core.db.models import RecipeFlag
from jobsearch_core.db.recipe_flags import (
    flags_for_body,
    has_prior_none,
    latest_flag,
    record_flag,
    stopped_bodies,
)

SHA_A = "a" * 64
SHA_B = "b" * 64


def test_record_flag_stores_the_verdict_and_analysis(session):
    flag = record_flag(
        session,
        body_sha256=SHA_A,
        verdict="none",
        analysis={"verdict": "none", "items": [{"url": "https://x.test/jobs", "reason": "section link"}]},
        model_id="test/model",
        page_url="https://x.test/jobs",
        stopped=True,
    )
    session.commit()
    assert flag.id is not None
    assert flag.stopped is True
    assert json.loads(flag.analysis)["items"][0]["reason"] == "section link"


def test_history_keeps_every_gate_event_for_a_body(session):
    record_flag(session, body_sha256=SHA_A, verdict="none", analysis={"verdict": "none"})
    record_flag(session, body_sha256=SHA_A, verdict="postings", analysis={"verdict": "postings"})
    session.commit()
    assert [f.verdict for f in flags_for_body(session, SHA_A)] == ["none", "postings"]
    latest = latest_flag(session, SHA_A)
    assert latest is not None and latest.verdict == "postings"


def test_a_different_body_does_not_inherit_the_analysis(session):
    record_flag(session, body_sha256=SHA_A, verdict="none", analysis={"verdict": "none"})
    session.commit()
    assert flags_for_body(session, SHA_B) == []
    assert latest_flag(session, SHA_B) is None
    assert has_prior_none(session, SHA_B) is False


def test_prior_none_is_per_body_not_global(session):
    record_flag(session, body_sha256=SHA_A, verdict="none", analysis={"verdict": "none"})
    record_flag(session, body_sha256=SHA_B, verdict="postings", analysis={"verdict": "postings"})
    session.commit()
    assert has_prior_none(session, SHA_A) is True
    assert has_prior_none(session, SHA_B) is False


def test_stopped_bodies_lists_only_stopped_flags(session):
    record_flag(session, body_sha256=SHA_A, verdict="none", analysis={}, stopped=True)
    record_flag(session, body_sha256=SHA_B, verdict="none", analysis={}, stopped=False)
    session.commit()
    assert list(stopped_bodies(session)) == [SHA_A]


@pytest.mark.parametrize("bad", ["", "A" * 64, "a" * 63, "g" * 64, SHA_A + " "])
def test_a_key_that_is_not_a_body_hash_is_refused(session, bad):
    with pytest.raises(ValueError):
        record_flag(session, body_sha256=bad, verdict="none", analysis={})


def test_an_unknown_verdict_is_refused(session):
    with pytest.raises(ValueError):
        record_flag(session, body_sha256=SHA_A, verdict="maybe", analysis={})


def test_recipe_id_is_optional_provenance(session):
    flag = record_flag(session, body_sha256=SHA_A, verdict="postings", analysis={}, recipe_id=None)
    session.commit()
    assert isinstance(flag, RecipeFlag)
    assert flag.recipe_id is None
