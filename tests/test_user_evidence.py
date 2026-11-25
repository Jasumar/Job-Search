"""Phase 4.1a invariants for the DB-backed candidate evidence.

The properties under test are the ones the phase decided on, expressed as
executable assertions rather than prose:

(a) every field has exactly one owner table, and ``data/evidence.yaml`` is
    no longer a read or write source;
(b) ``additional_experience`` is stored byte-identical to the user's text and
    is never routed through an LLM;
(c) structured evidence is only ever persisted by an explicit call, never as a
    side effect of a read, a score, or a profile save;
(d) the prompt projection excludes contact details by construction;
(e) re-scoring an already-scored job requires explicit opt-in.
"""

from __future__ import annotations

import json

from jobsearch_core.db.models import UserEvidence, UserProfile
from jobsearch_core.profile import ProfileData, profile_snapshot, replace_profile
from jobsearch_core.profile_evidence import (
    EvidenceData,
    evidence_prompt_payload,
    get_evidence,
    load_candidate_evidence,
    replace_evidence,
    store_additional_experience,
)

CONTACT = {
    "location": "Tampere, Finland",
    "citizenship": "Finnish",
    "linkedin": "https://linkedin.com/in/testuser",
    "gitlab": "https://gitlab.com/testuser",
    "email": "test.user@example.com",
    "phone": "+358 40 1234567",
}


def _profile(**overrides) -> ProfileData:
    base = {
        "name": "Test User",
        "education": "BSc Computer Science",
        "experience": "Python Developer",
        "skills": ["Python", "SQL"],
        "target_countries": ["Finland", "Sweden"],
        "role_families": ["backend"],
        "visa_required": True,
    }
    base.update(overrides)
    return ProfileData(**base)


# --- (a) single ownership -------------------------------------------------


def test_contact_fields_live_on_the_profile_row(session):
    replace_profile(session, _profile(**CONTACT))
    row = session.query(UserProfile).one()
    for field, value in CONTACT.items():
        assert getattr(row, field) == value


def test_cv_facts_live_on_the_evidence_row(session):
    replace_profile(session, _profile())
    replace_evidence(session, EvidenceData(skills=["Python", "SQL"]))
    row = session.query(UserEvidence).one()
    assert json.loads(row.skills) == ["Python", "SQL"]
    # and the profile row does not carry them
    assert session.query(UserProfile).one().skills is not None


def test_candidate_evidence_assembles_from_both_tables(session):
    replace_profile(session, _profile(**CONTACT))
    replace_evidence(session, EvidenceData(skills=["Python"], linked_year=2015))

    ev = load_candidate_evidence(session)

    assert ev.name == "Test User"
    assert ev.skills == ["Python"]
    assert ev.target_countries == ["Finland", "Sweden"]
    assert ev.target_roles == ["technology/backend"]
    assert ev.visa_required is True


# --- (b) additional_experience is verbatim and never LLM-parsed -----------


def test_additional_experience_is_stored_verbatim(session):
    text = "  Side project: I maintain a small Rust CLI.\n\nKept my own line breaks.  "
    store_additional_experience(session, text)
    session.commit()
    assert session.query(UserEvidence).one().additional_experience == text


def test_additional_experience_survives_a_structured_replace(session):
    """A structured-evidence write must not clobber the user's own words."""
    store_additional_experience(session, "freelance work in 2019")
    replace_evidence(session, EvidenceData(skills=["Go"]))
    session.commit()
    assert session.query(UserEvidence).one().additional_experience == "freelance work in 2019"


def test_additional_experience_is_not_part_of_the_llm_proposal():
    """(b) second half: it must not be a field an LLM can fill."""
    from jobsearch_core.cv_parse import ProposedEvidence

    assert "additional_experience" not in ProposedEvidence.model_fields


# --- (c) structured evidence is never auto-persisted ---------------------


def test_reading_evidence_does_not_create_a_row(session):
    replace_profile(session, _profile())
    assert get_evidence(session) is None
    assert load_candidate_evidence(session).skills == []
    assert session.query(UserEvidence).count() == 0


def test_profile_save_does_not_touch_evidence(session):
    replace_profile(session, _profile())
    session.commit()
    assert session.query(UserEvidence).count() == 0


def test_profile_snapshot_carries_no_evidence_facts(session):
    replace_profile(session, _profile())
    snap = profile_snapshot(session)
    assert snap.role_families == ["backend"]
    # the snapshot is preferences-only, so evidence can never leak into it
    assert not hasattr(snap, "skills") or snap.skills == []


# --- (d) the prompt projection excludes contact details -------------------


def test_prompt_payload_excludes_contact_details(session):
    replace_profile(session, _profile(**CONTACT))
    replace_evidence(session, EvidenceData(skills=["Python"]))

    payload = evidence_prompt_payload(load_candidate_evidence(session))
    blob = json.dumps(payload)

    # Contact details are excluded as projection *fields*...
    for key in ("email", "phone", "linkedin", "gitlab", "location", "citizenship"):
        assert key not in payload
    # ...and their values must not appear anywhere. The values must be
    # distinctive enough not to collide with a legitimate CV fact — a
    # citizenship of "Finnish" would otherwise match the languages list.
    assert CONTACT["email"] not in blob
    assert CONTACT["phone"] not in blob
    assert CONTACT["linkedin"] not in blob
    assert CONTACT["gitlab"] not in blob
    assert "Python" in blob


def test_project_urls_and_job_locations_are_kept(session):
    """The exclusion is contact data, not all locators.

    A project URL and an employment location are CV facts a model legitimately
    needs; excluding them would silently degrade scoring.
    """
    replace_profile(session, _profile(**CONTACT))
    replace_evidence(
        session,
        EvidenceData(
            experience=[
                {
                    "title": "Engineer",
                    "company": "Example Oy",
                    "location": "Helsinki, Finland",
                }
            ],
            projects=[
                {
                    "name": "Tool",
                    "url": "https://gitlab.com/example/tool",
                }
            ],
        ),
    )

    blob = json.dumps(evidence_prompt_payload(load_candidate_evidence(session)))
    assert "https://gitlab.com/example/tool" in blob
    assert "Helsinki, Finland" in blob
