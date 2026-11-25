"""One-time import of ``data/evidence.yaml`` into the DB (Phase 4.1a step 2).

Phase 4.1a retires ``data/evidence.yaml``. Before it is deleted, its contents
must land in the DB so day-one scores are not empty and no CV fact is lost.
The import is an **explicit** CLI command (``jobsearch import-evidence``),
never a startup step: a schema migration that silently reads a file and
writes user data is not auditable, and a failure would leave the database in
a state nobody asked for.

Split by ownership (Phase 4.1a invariant (a) — one owner per field):

- ``education`` / ``experience`` / ``projects`` / ``skills`` / ``languages``
  -> ``user_evidence`` (:func:`jobsearch_core.profile_evidence.replace_evidence`);
- ``profile.*`` contact details, ``preferences.target_roles`` /
  ``target_countries`` -> ``user_profile``.

Deliberately **not** imported: ``preferences.remote_only`` (that flag has no
own column; ``user_profile.remote`` is the work-arrangement choice and the
two are not the same concept), and ``education[].year`` as ``linked_year``
(``linked_year`` is meant to be a LinkedIn-derived graduation year, and
guessing it from education would be fabricating a fact). Both are left for
the user to fill in through the UI.

The YAML is read through the legacy :class:`jobsearch_core.config.Evidence`
model so unknown or slightly-off keys degrade to defaults instead of raising
mid-import.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from jobsearch_core.config import load_yaml
from jobsearch_core.db.models import UserProfile
from jobsearch_core.log import get_logger
from jobsearch_core.profile_evidence import (
    EvidenceData,
    EvidenceEducation,
    EvidenceExperience,
    EvidenceLanguage,
    EvidenceProject,
    replace_evidence,
)

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Legacy YAML shape.
#
# ``EvidencePreferences`` / ``EvidenceProfile`` are the *reader* for the retired
# ``data/evidence.yaml`` and are deliberately tolerant: unknown or slightly-off
# keys in a hand-edited file degrade to defaults rather than aborting a
# one-time migration. The per-item CV-fact models are shared with the runtime
# (``profile_evidence``) so an imported value and a freshly parsed value have
# the same type.
# ---------------------------------------------------------------------------


class EvidencePreferences(BaseModel):
    target_roles: list[str] = Field(default_factory=list)
    target_countries: list[str] = Field(default_factory=list)
    remote_only: bool = False
    visa_required: bool = False


class EvidenceProfile(BaseModel):
    name: str = ""
    location: str = ""
    citizenship: str = ""
    linkedin: str = ""
    github: str = ""
    email: str = ""
    phone: str = ""


class Evidence(BaseModel):
    profile: EvidenceProfile = Field(default_factory=EvidenceProfile)
    education: list[EvidenceEducation] = Field(default_factory=list)
    experience: list[EvidenceExperience] = Field(default_factory=list)
    projects: list[EvidenceProject] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    languages: list[EvidenceLanguage] = Field(default_factory=list)
    preferences: EvidencePreferences = Field(default_factory=EvidencePreferences)


def _as_dicts(items: list[Any]) -> list[dict[str, Any]]:
    return [item.model_dump() for item in items]


def import_evidence_yaml(session: Session, path: Path) -> dict[str, Any]:
    """Import a legacy ``evidence.yaml`` into ``user_evidence`` + ``user_profile``.

    Returns a small report of what was written. Both writes happen in this
    session; the caller commits once at the end, so a failure on the second
    write cannot leave the CV facts persisted without the profile fields.

    The existing rows are **replaced**, not merged: this is a migration of
    the hand-written bank, not an incremental update. A second run therefore
    produces the same state as the first (idempotent).
    """
    data = load_yaml(path)
    legacy = Evidence(**data)

    written = replace_evidence(
        session,
        EvidenceData(
            education=_as_dicts(legacy.education),
            experience=_as_dicts(legacy.experience),
            projects=_as_dicts(legacy.projects),
            skills=list(legacy.skills),
            languages=_as_dicts(legacy.languages),
            linked_year=None,
        ),
    )

    profile_row = session.query(UserProfile).order_by(UserProfile.id).first()
    if profile_row is None:
        profile_row = UserProfile()
        session.add(profile_row)
    contact_written: list[str] = []
    for source, target in (
        ("name", "name"),
        ("location", "location"),
        ("citizenship", "citizenship"),
        ("linkedin", "linkedin"),
        ("github", "gitlab"),
        ("email", "email"),
        ("phone", "phone"),
    ):
        value = getattr(legacy.profile, source, "")
        if value:
            setattr(profile_row, target, value)
            contact_written.append(target)
    profile_row.visa_required = bool(legacy.preferences.visa_required)

    # Only fill search preferences the wizard has not already decided. The
    # profile row is the owner; an empty list means "not chosen yet".
    if not json.loads(profile_row.role_families or "[]"):
        profile_row.role_families = json.dumps(
            list(legacy.preferences.target_roles), ensure_ascii=False
        )
    if not json.loads(profile_row.target_countries or "[]"):
        profile_row.target_countries = json.dumps(
            list(legacy.preferences.target_countries), ensure_ascii=False
        )

    session.commit()
    session.refresh(profile_row)

    return {
        "source": str(path),
        "user_evidence": written.to_dict(),
        "user_profile_contact_fields": contact_written,
        "visa_required": bool(profile_row.visa_required),
        "role_families": legacy.preferences.target_roles,
        "target_countries": legacy.preferences.target_countries,
        "skipped": ["preferences.remote_only", "linked_year"],
    }
