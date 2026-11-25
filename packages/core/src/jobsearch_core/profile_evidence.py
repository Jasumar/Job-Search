"""Candidate-evidence service layer (Phase 4.1a, decided 2026-09-29).

``data/evidence.yaml`` is retired. The same facts now live in two
single-row tables with exactly one owner each (Phase 4.1a invariant (a)):

- :class:`~jobsearch_core.db.models.UserEvidence` — CV facts: ``education``,
  ``experience``, ``projects``, ``skills``, ``languages``, ``linked_year``,
  and the verbatim ``additional_experience``;
- :class:`~jobsearch_core.db.models.UserProfile` — search preferences
  (``target_countries``, ``role_families``, ``job_preferences``,
  ``visa_required``) and contact details.

This module is the single sanctioned read/write path for both. Reads:

- :func:`load_candidate_evidence` assembles one :class:`CandidateEvidence`
  projection for a caller (scoring, tailoring, the CLI);
- :func:`evidence_prompt_payload` is the one projection that decides what
  is serialized into an LLM prompt. Contact details are excluded by that
  field list, so they stay out of prompts by construction rather than by
  each call site remembering.

Writes:

- :func:`replace_evidence` — full replace in one atomic transaction, the
  only writer of the structured CV facts;
- :func:`store_additional_experience` — the only writer of the
  verbatim field. It stores exactly what the user wrote (invariant (b)).

Nothing here is ever written implicitly by a read or by a score: the
structured evidence proposal produced by the ``profile_parse`` run stays in
the run summary until the user confirms it (invariant (c)).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from jobsearch_core.db.models import UserEvidence, UserProfile
from jobsearch_core.log import get_logger

logger = get_logger(__name__)


class EvidenceEducation(BaseModel):
    degree: str
    field: str = ""
    institution: str
    year: int | None = None
    details: list[str] = Field(default_factory=list)


class EvidenceExperience(BaseModel):
    title: str
    company: str = ""
    start: str = ""
    end: str = ""
    location: str | None = None
    responsibilities: list[str] = Field(default_factory=list)
    technologies: list[str] = Field(default_factory=list)


class EvidenceProject(BaseModel):
    name: str
    url: str = ""
    start: str = ""
    end: str = ""
    role: str = ""
    description: str = ""
    technologies: list[str] = Field(default_factory=list)


class EvidenceLanguage(BaseModel):
    language: str
    proficiency: str = ""


class CandidateEvidence(BaseModel):
    """The read projection every evidence consumer receives (Phase 4.1a).

    Replaces the retired ``config.Evidence`` model. Assembled by
    :func:`load_candidate_evidence` from the two owner tables; never
    constructed by a call site from ad-hoc fields.
    """

    model_config = ConfigDict(extra="ignore")

    # --- CV facts (owner: user_evidence) ---
    education: list[EvidenceEducation] = Field(default_factory=list)
    experience: list[EvidenceExperience] = Field(default_factory=list)
    projects: list[EvidenceProject] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    languages: list[EvidenceLanguage] = Field(default_factory=list)
    linked_year: int | None = None
    # Verbatim user-authored text; never parsed, paraphrased, or summarized.
    additional_experience: str = ""
    # --- search preferences (owner: user_profile) ---
    target_countries: list[str] = Field(default_factory=list)
    target_roles: list[str] = Field(default_factory=list)
    # MF3: typed roles with their taxonomy ids, [{"text", "ids"}].
    custom_roles: list[dict] = Field(default_factory=list)
    # MF5: a strength line only, no score effect.
    target_cities: list[str] = Field(default_factory=list)
    job_preferences: list[str] = Field(default_factory=list)
    remote_only: bool = False
    visa_required: bool = False
    # --- contact details (owner: user_profile) ---
    # Excluded from prompt projection by ``evidence_prompt_payload``.
    name: str = ""
    location: str = ""
    citizenship: str = ""
    linkedin: str = ""
    gitlab: str = ""
    email: str = ""
    phone: str = ""
    evidence_reference: str | None = None

    @property
    def can_score(self) -> bool:
        """MF1: scoring needs at least skills or experience to compare."""
        return bool(self.skills or self.experience)

    @property
    def has_any(self) -> bool:
        """Whether anything worth acting on was stored at all."""
        return bool(
            self.education
            or self.experience
            or self.projects
            or self.skills
            or self.languages
            or self.additional_experience.strip()
            or self.linked_year
        )


# The sanctioned prompt field list (Phase 4.1a, best-effort by decision):
# CV facts + search preferences, never contact details.
PROMPT_FIELDS: tuple[str, ...] = (
    "education",
    "experience",
    "projects",
    "skills",
    "languages",
    "linked_year",
    "additional_experience",
    "target_countries",
    "target_roles",
    "job_preferences",
    "remote_only",
    "visa_required",
)


def evidence_prompt_payload(evidence: CandidateEvidence) -> dict[str, Any]:
    """The only projection of candidate facts into an LLM prompt.

    ``PROMPT_FIELDS`` is the field list; ``additional_experience`` is passed
    through unchanged, so the user's own words reach the model byte-identical.
    """
    dumped = evidence.model_dump()
    return {field: dumped[field] for field in PROMPT_FIELDS}


# --------------------------------------------------------------------------
# structured evidence document (owner: user_evidence)
# --------------------------------------------------------------------------


@dataclass
class EvidenceData:
    """Structured CV facts, the ``PUT``/proposal payload for ``user_evidence``."""

    education: list[dict[str, Any]] = field(default_factory=list)
    experience: list[dict[str, Any]] = field(default_factory=list)
    projects: list[dict[str, Any]] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    languages: list[dict[str, Any]] = field(default_factory=list)
    linked_year: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "education": list(self.education),
            "experience": list(self.experience),
            "projects": list(self.projects),
            "skills": list(self.skills),
            "languages": list(self.languages),
            "linked_year": self.linked_year,
        }


def _decode_list(raw: str | None) -> list[Any]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _decode_str_list(raw: str | None) -> list[str]:
    return [str(item) for item in _decode_list(raw)]


def get_evidence_row(session: Session) -> UserEvidence | None:
    """Return the single evidence row, or ``None`` when none exists yet."""
    return session.query(UserEvidence).order_by(UserEvidence.id).first()


def _data_from_row(row: UserEvidence | None) -> EvidenceData:
    if row is None:
        return EvidenceData()
    return EvidenceData(
        education=[d for d in _decode_list(row.education) if isinstance(d, dict)],
        experience=[d for d in _decode_list(row.experience) if isinstance(d, dict)],
        projects=[d for d in _decode_list(row.projects) if isinstance(d, dict)],
        skills=_decode_str_list(row.skills),
        languages=[d for d in _decode_list(row.languages) if isinstance(d, dict)],
        linked_year=row.linked_year,
    )


def get_evidence(session: Session) -> EvidenceData | None:
    row = get_evidence_row(session)
    return _data_from_row(row) if row is not None else None


def replace_evidence(session: Session, data: EvidenceData) -> EvidenceData:
    """Full-replace the structured CV facts in one atomic transaction.

    Mirrors :func:`jobsearch_core.profile.replace_profile`: the row stays
    single-row, an existing row is updated in place, and a reader never sees
    a half-written document. ``additional_experience`` is untouched — it has
    its own single writer (:func:`store_additional_experience`) so a
    structured-evidence edit can never silently clobber the user's own words.
    """
    row = get_evidence_row(session)
    if row is None:
        row = UserEvidence()
        session.add(row)
    row.education = json.dumps(list(data.education), ensure_ascii=False)
    row.experience = json.dumps(list(data.experience), ensure_ascii=False)
    row.projects = json.dumps(list(data.projects), ensure_ascii=False)
    row.skills = json.dumps(list(data.skills), ensure_ascii=False)
    row.languages = json.dumps(list(data.languages), ensure_ascii=False)
    row.linked_year = data.linked_year
    session.commit()
    session.refresh(row)
    return _data_from_row(row)


def store_additional_experience(session: Session, text: str | None) -> UserEvidence:
    """Persist the verbatim supplementary-experience text.

    The value is stored exactly as written — no ``strip``, no normalization,
    no LLM round-trip (Phase 4.1a invariant (b)). ``None`` means "no update",
    so a CV submission without the optional field never erases what is there.
    """
    row = get_evidence_row(session)
    if row is None:
        row = UserEvidence()
        session.add(row)
    if text is not None:
        row.additional_experience = text
    session.commit()
    session.refresh(row)
    return row


def get_additional_experience(session: Session) -> str:
    row = get_evidence_row(session)
    return row.additional_experience if row is not None else ""


# --------------------------------------------------------------------------
# assembled read projection
# --------------------------------------------------------------------------


def load_candidate_evidence(session: Session) -> CandidateEvidence:
    """Assemble the candidate evidence both owner tables provide.

    Missing rows are normal (first run, or evidence not yet confirmed) and
    yield an empty projection rather than raising: callers already handle
    "no evidence" (deterministic scorers return neutral scores).
    """
    evidence_row = get_evidence_row(session)
    profile_row = (
        session.query(UserProfile).order_by(UserProfile.id).first()
    )

    def _typed(items: list[Any], model: type[BaseModel]) -> list[BaseModel]:
        out: list[BaseModel] = []
        for item in items:
            if isinstance(item, model):
                out.append(item)
            elif isinstance(item, dict):
                try:
                    out.append(model.model_validate(item))
                except Exception:
                    logger.debug("Dropping malformed evidence item: %r", item)
        return out

    data = _data_from_row(evidence_row)
    reference = None
    if evidence_row is not None and evidence_row.updated_at is not None:
        updated = evidence_row.updated_at
        reference = updated.isoformat() if isinstance(updated, datetime) else str(updated)

    if profile_row is None:
        return CandidateEvidence(
            education=_typed(data.education, EvidenceEducation),  # type: ignore[arg-type]
            experience=_typed(data.experience, EvidenceExperience),  # type: ignore[arg-type]
            projects=_typed(data.projects, EvidenceProject),  # type: ignore[arg-type]
            skills=list(data.skills),
            languages=_typed(data.languages, EvidenceLanguage),  # type: ignore[arg-type]
            linked_year=data.linked_year,
            additional_experience=evidence_row.additional_experience if evidence_row else "",
            evidence_reference=reference,
        )

    return CandidateEvidence(
        education=_typed(data.education, EvidenceEducation),  # type: ignore[arg-type]
        experience=_typed(data.experience, EvidenceExperience),  # type: ignore[arg-type]
        projects=_typed(data.projects, EvidenceProject),  # type: ignore[arg-type]
        skills=list(data.skills),
        languages=_typed(data.languages, EvidenceLanguage),  # type: ignore[arg-type]
        linked_year=data.linked_year,
        additional_experience=evidence_row.additional_experience if evidence_row else "",
        target_countries=_decode_str_list(profile_row.target_countries),
        target_roles=_decode_str_list(profile_row.role_families),
        custom_roles=[d for d in _decode_list(profile_row.custom_roles) if isinstance(d, dict)],
        target_cities=_decode_str_list(profile_row.target_cities),
        job_preferences=_decode_str_list(profile_row.job_preferences),
        remote_only=bool(profile_row.remote),
        visa_required=bool(profile_row.visa_required),
        name=profile_row.name,
        location=profile_row.location,
        citizenship=profile_row.citizenship,
        linkedin=profile_row.linkedin,
        gitlab=profile_row.gitlab,
        email=profile_row.email,
        phone=profile_row.phone,
        evidence_reference=reference,
    )


def search_preferences(evidence: CandidateEvidence, source: str) -> dict[str, Any]:
    """Discovery's preference payload, sourced from the DB.

    Replaces the old ``EvidencePreferences.model_dump()`` plus the
    ``evidence_yaml`` source marker.
    """
    return {
        "target_countries": list(evidence.target_countries),
        "target_roles": list(evidence.target_roles),
        "job_preferences": list(evidence.job_preferences),
        "remote_only": evidence.remote_only,
        "visa_required": evidence.visa_required,
        "source": source,
    }
