"""User-profile service layer (Phase 3.8f backend, decided 2026-09-11).

The ``user_profile`` table is single-row by convention. All writes go through
:func:`replace_profile` (one atomic full-replace transaction) so a reader
never sees partial state; reads go through :func:`load_profile` /
:func:`get_profile_record`.

The API layer (Phase 3.8f endpoints) validates and serializes; this module
owns the DB contract:

- ``skills`` / ``job_preferences`` / ``notes`` / ``target_countries`` /
  ``target_cities`` are JSON string lists (non-categorical, decided
  2026-09-11);
- the profile never carries the raw CV — ``POST /profile/cv`` writes it at
  parse time via :func:`store_raw_cv` (part of the ``profile_parse`` run);
- discovery snapshots the profile once at claim/execution start
  (:func:`profile_snapshot`), so mid-run edits never affect an in-flight run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from jobsearch_core.countries import canonical_country
from jobsearch_core.db.models import UserProfile
from jobsearch_core.log import get_logger
from jobsearch_core.role_taxonomy import (
    clean_custom_roles,
    custom_role_ids,
    discovery_families,
    normalize_selection,
)

logger = get_logger(__name__)

_LIST_FIELDS = ("skills", "job_preferences", "notes", "target_countries", "target_cities", "role_families")
_BOOL_FIELDS = ("full_time", "part_time", "onsite", "hybrid", "remote")
_TEXT_FIELDS = ("name", "education", "experience")

# Phase 4.1a: contact details + work-eligibility moved here from
# ``data/evidence.yaml``. Part of the profile full-replace (the wizard's
# profile form is their only editor), so they are covered by the same
# atomic transaction as the rest of the profile.
_CONTACT_FIELDS = (
    "location",
    "citizenship",
    "linkedin",
    "gitlab",
    "email",
    "phone",
)
_CONTACT_BOOL_FIELDS = ("visa_required",)

@dataclass
class ProfileSnapshot:
    """Immutable per-run copy of the profile's discovery-relevant fields."""

    countries: list[str] = field(default_factory=list)
    preferences: list[str] = field(default_factory=list)
    # tech.yaml discovery families derived from the selected technology roles
    # (MF3: discovery stays tech-scoped; non-technology roles add nothing).
    role_families: list[str] = field(default_factory=list)
    reference: str | None = None  # updated_at at claim time (observability)


@dataclass
class ProfileData:
    """Structured profile payload (the wizard's PUT body, validated upstream)."""

    name: str = ""
    education: str = ""
    experience: str = ""
    skills: list[str] = field(default_factory=list)
    job_preferences: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    target_countries: list[str] = field(default_factory=list)
    target_cities: list[str] = field(default_factory=list)
    role_families: list[str] = field(default_factory=list)
    # MF3: typed roles, each {"text", "ids"} (ids from config/role_taxonomy.yaml).
    custom_roles: list[dict] = field(default_factory=list)
    full_time: bool = False
    part_time: bool = False
    onsite: bool = False
    hybrid: bool = False
    remote: bool = False
    location: str = ""
    citizenship: str = ""
    linkedin: str = ""
    gitlab: str = ""
    email: str = ""
    phone: str = ""
    visa_required: bool = False

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "education": self.education,
            "experience": self.experience,
            "skills": list(self.skills),
            "job_preferences": list(self.job_preferences),
            "notes": list(self.notes),
            "target_countries": list(self.target_countries),
            "target_cities": list(self.target_cities),
            "role_families": list(self.role_families),
            "custom_roles": [dict(entry) for entry in self.custom_roles],
            "full_time": self.full_time,
            "part_time": self.part_time,
            "onsite": self.onsite,
            "hybrid": self.hybrid,
            "remote": self.remote,
            "location": self.location,
            "citizenship": self.citizenship,
            "linkedin": self.linkedin,
            "gitlab": self.gitlab,
            "email": self.email,
            "phone": self.phone,
            "visa_required": self.visa_required,
        }


def _decode_list(raw: str) -> list[str]:
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [str(item) for item in data] if isinstance(data, list) else []


def _decode_dicts(raw: str | None) -> list[dict]:
    try:
        data = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def profile_to_data(profile: UserProfile) -> ProfileData:
    return ProfileData(
        name=profile.name,
        education=profile.education,
        experience=profile.experience,
        skills=_decode_list(profile.skills),
        job_preferences=_decode_list(profile.job_preferences),
        notes=_decode_list(profile.notes),
        target_countries=_decode_list(profile.target_countries),
        target_cities=_decode_list(profile.target_cities),
        role_families=_decode_list(profile.role_families),
        custom_roles=_decode_dicts(profile.custom_roles),
        full_time=profile.full_time,
        part_time=profile.part_time,
        onsite=profile.onsite,
        hybrid=profile.hybrid,
        remote=profile.remote,
        location=profile.location,
        citizenship=profile.citizenship,
        linkedin=profile.linkedin,
        gitlab=profile.gitlab,
        email=profile.email,
        phone=profile.phone,
        visa_required=profile.visa_required,
    )


def get_profile_row(session: Session) -> UserProfile | None:
    """Return the single profile row, or ``None`` when none exists yet."""
    return session.query(UserProfile).order_by(UserProfile.id).first()


def get_profile(session: Session) -> ProfileData | None:
    row = get_profile_row(session)
    return profile_to_data(row) if row is not None else None


def replace_profile(session: Session, data: ProfileData) -> ProfileData:
    """Full-replace the structured profile in one atomic transaction.

    Never writes the raw CV (that is the parse route's job). The row stays
    single-row: an existing row is updated in place, the first save inserts.
    """
    existing = get_profile_row(session)
    if existing is None:
        existing = UserProfile()
        session.add(existing)
    existing.name = data.name
    existing.education = data.education
    existing.experience = data.experience
    existing.skills = json.dumps(list(data.skills), ensure_ascii=False)
    existing.job_preferences = json.dumps(list(data.job_preferences), ensure_ascii=False)
    existing.notes = json.dumps(list(data.notes), ensure_ascii=False)
    existing.target_countries = json.dumps(list(data.target_countries), ensure_ascii=False)
    existing.target_cities = json.dumps(list(data.target_cities), ensure_ascii=False)
    # MF3: only taxonomy ids are stored; old chip ids are translated.
    existing.role_families = json.dumps(normalize_selection(list(data.role_families)), ensure_ascii=False)
    existing.custom_roles = json.dumps(clean_custom_roles(list(data.custom_roles)), ensure_ascii=False)
    existing.full_time = bool(data.full_time)
    existing.part_time = bool(data.part_time)
    existing.onsite = bool(data.onsite)
    existing.hybrid = bool(data.hybrid)
    existing.remote = bool(data.remote)
    existing.location = data.location
    existing.citizenship = data.citizenship
    existing.linkedin = data.linkedin
    existing.gitlab = data.gitlab
    existing.email = data.email
    existing.phone = data.phone
    existing.visa_required = bool(data.visa_required)
    session.commit()
    session.refresh(existing)
    return profile_to_data(existing)


def store_raw_cv(
    session: Session,
    *,
    cv_text: str,
    description: str | None,
) -> UserProfile:
    """Persist the raw CV text and description at parse time.

    Called by the ``profile_parse`` run before the LLM call. This is the only
    writer of ``cv_text``/``description`` — ``PUT /profile`` never touches
    them (decided 2026-09-11). Creates the profile row if none exists so the
    raw CV is not lost before the first confirmed ``PUT``.
    """
    row = get_profile_row(session)
    if row is None:
        row = UserProfile()
        session.add(row)
    row.cv_text = cv_text
    if description is not None:
        row.description = description
    session.commit()
    session.refresh(row)
    return row


def profile_snapshot(session: Session) -> ProfileSnapshot:
    """Snapshot the profile for one discovery run (Phase 3.8f read timing).

    Discovery works from this snapshot for the whole run: mid-run profile
    edits never affect an in-flight run. ``countries`` keeps the DB-first
    selection (validated against config/countries.yaml); ``preferences`` is the
    JSON string list usable directly as prompt/filter input.
    """
    row = get_profile_row(session)
    if row is None:
        return ProfileSnapshot()
    countries: list[str] = []
    for raw in _decode_list(row.target_countries):
        known = canonical_country(raw)
        if known and known not in countries:
            countries.append(known)
    reference = row.updated_at.isoformat() if isinstance(row.updated_at, datetime) else str(row.updated_at)
    selection = _decode_list(row.role_families) + custom_role_ids(_decode_dicts(row.custom_roles))
    return ProfileSnapshot(
        countries=countries,
        preferences=_decode_list(row.job_preferences),
        role_families=discovery_families(selection),
        reference=reference,
    )


def has_profile(session: Session) -> bool:
    """Whether a profile row exists with any structured content."""
    row = get_profile_row(session)
    if row is None:
        return False
    data = profile_to_data(row)
    return bool(
        data.name
        or data.education
        or data.experience
        or data.skills
        or data.job_preferences
        or data.notes
        or data.target_countries
        or data.target_cities
    )
