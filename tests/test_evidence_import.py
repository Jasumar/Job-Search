"""One-time ``evidence.yaml`` → DB migration (Phase 4.1a).

The YAML is hand-written and gitignored, so it only exists in the user's real
checkout. These tests use a throwaway copy to prove the migration is lossless
for the fields that moved and idempotent when re-run.
"""

from __future__ import annotations

from jobsearch_core.db.models import UserEvidence, UserProfile
from jobsearch_core.evidence_import import import_evidence_yaml

LEGACY = """
profile:
  name: Test User
  location: Tampere, Finland
  citizenship: Finnish
  linkedin: https://linkedin.com/in/testuser
  github: https://github.com/testuser
  email: test.user@example.com
  phone: "+358401234567"
preferences:
  target_roles: [backend, data]
  target_countries: [Finland, Sweden]
  remote_only: false
  visa_required: true
education:
  - degree: BSc
    field: Computer Science
    institution: University of Tampere
    year: 2020
experience:
  - title: Python Developer
    company: Example Oy
    start: "2020-06"
    end: present
    responsibilities: [Built internal tools]
    technologies: [Python, SQL]
skills: [Python, SQL, Docker]
languages:
  - language: Finnish
    proficiency: native
"""


def _write(tmp_path) -> object:
    p = tmp_path / "evidence.yaml"
    p.write_text(LEGACY, encoding="utf-8")
    return p


def test_import_moves_every_field_to_its_owner_table(session, tmp_path):
    report = import_evidence_yaml(session, _write(tmp_path))

    ev = session.query(UserEvidence).one()
    assert "Python" in ev.skills
    assert "Docker" in ev.skills
    assert len(LEGACY) > 0 and report["user_evidence"]["skills"] == ["Python", "SQL", "Docker"]

    profile = session.query(UserProfile).one()
    # contact details went to the profile row, not the evidence row
    assert profile.location == "Tampere, Finland"
    assert profile.citizenship == "Finnish"
    assert profile.email == "test.user@example.com"
    assert profile.phone == "+358401234567"
    assert profile.gitlab == "https://github.com/testuser"
    assert profile.visa_required is True
    # preferences too
    assert profile.role_families == '["backend", "data"]'
    assert profile.target_countries == '["Finland", "Sweden"]'


def test_import_is_idempotent(session, tmp_path):
    path = _write(tmp_path)
    import_evidence_yaml(session, path)
    import_evidence_yaml(session, path)
    assert session.query(UserEvidence).count() == 1
    assert session.query(UserProfile).count() == 1


def test_import_does_not_overwrite_wizard_choices(session, tmp_path):
    """A wizard choice already made in the UI wins over the legacy YAML."""
    from jobsearch_core.profile import ProfileData, replace_profile

    replace_profile(session, ProfileData(role_families=["ai"], target_countries=["Iceland"]))
    session.commit()

    import_evidence_yaml(session, _write(tmp_path))

    profile = session.query(UserProfile).one()
    assert profile.role_families == '["technology/ml-engineer"]'
    assert profile.target_countries == '["Iceland"]'


def test_imported_evidence_is_readable_by_the_projection(session, tmp_path):
    from jobsearch_core.profile_evidence import load_candidate_evidence

    import_evidence_yaml(session, _write(tmp_path))
    ev = load_candidate_evidence(session)

    assert ev.name == "Test User"
    assert ev.skills == ["Python", "SQL", "Docker"]
    assert ev.target_countries == ["Finland", "Sweden"]
    assert ev.target_roles == ["backend", "data"]
