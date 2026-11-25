"""CV-text parsing into proposed profile fields (Phase 3.8f backend).

Decided 2026-09-11 ("LLM proposes, form decides"): the ``profile_parse`` run
sends the raw CV text to the analysis LLM, which proposes structured profile
fields. The proposal lands **only** in the run's ``summary`` — it is never
written to ``user_profile`` or ``user_evidence``. The wizard reads it from
``GET /runs/{id}`` and the owner confirms/edits it; Save writes it through
``PUT /profile`` and ``PUT /profile/evidence`` (MF1).

The evidence proposal has the :class:`~jobsearch_core.profile_evidence.EvidenceData`
shape, so a confirmed proposal is stored as it is.

MF3: suggested roles are ``config/role_taxonomy.yaml`` ids, and a typed role is
mapped to taxonomy ids by :func:`map_custom_role` (one cached call per role).
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from jobsearch_core.llm.cache import load_cached, save_cached
from jobsearch_core.llm.client import LLMClient
from jobsearch_core.log import get_logger
from jobsearch_core.profile_evidence import (
    EvidenceEducation,
    EvidenceExperience,
    EvidenceLanguage,
    EvidenceProject,
)
from jobsearch_core.role_taxonomy import load_taxonomy, normalize_selection, prompt_vocabulary

logger = get_logger(__name__)


def known_role_families() -> list[str]:
    """Every selectable taxonomy id: groups, then roles (MF3)."""
    taxonomy = load_taxonomy()
    return [group.id for group in taxonomy.groups] + list(taxonomy.roles)


class ProposedEvidence(BaseModel):
    """LLM-proposed ``user_evidence`` fields (never auto-saved).

    Structured CV facts in the ``EvidenceData`` shape. ``additional_experience``
    is deliberately absent: it is user-authored free text that must never be
    LLM-parsed, paraphrased, or generated (4.1a invariant b).
    """

    model_config = ConfigDict(extra="ignore")

    skills: list[str] = Field(default_factory=list)
    education: list[dict] = Field(default_factory=list)
    experience: list[dict] = Field(default_factory=list)
    projects: list[dict] = Field(default_factory=list)
    languages: list[dict] = Field(default_factory=list)
    linked_year: int | None = None


class ProposedProfile(BaseModel):
    """LLM-proposed structured profile fields (never persisted as truth)."""

    model_config = ConfigDict(extra="ignore")

    name: str = ""
    education: str = ""
    experience: str = ""
    skills: list[str] = Field(default_factory=list)
    job_preferences: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    suggested_role_families: list[str] = Field(default_factory=list)


PARSE_CV_SYSTEM = (
    "You read a candidate's CV and propose structured profile fields as JSON. "
    "Only record what the CV actually states; never invent experience, skills, "
    "or technologies. Fields the CV does not support are left empty.\n\n"
    "Return JSON with these keys:\n"
    "- name: string (the candidate's name, empty if not stated)\n"
    "- education: short free-text summary of education (degree, field, institution)\n"
    "- experience: short free-text summary of relevant work experience\n"
    "- skills: list of plain strings (technologies and skills named in the CV)\n"
    "- job_preferences: list of plain strings (what the candidate says they are "
    "looking for: role types, domains, work arrangements)\n"
    "- notes: list of plain strings (other considerations worth keeping, e.g. "
    "locations, eligibility constraints stated in the CV)\n"
    "- suggested_role_families: list of role ids that fit the candidate's "
    "background and what they are looking for, chosen ONLY from this list "
    "(use a full \"group/role\" id; use a bare group id only when every role in "
    "the group fits). Empty if none fit.\n"
    "{families}\n"
    "- evidence: object with these keys (structured CV facts; empty lists when "
    "the CV does not state them):\n"
    "  - skills: list of strings (every technology, tool and skill the CV names)\n"
    "  - experience: list of objects with keys title, company, start, end "
    "(year or YYYY-MM; end \"present\" for a current job), technologies (list "
    "of strings used in that job), responsibilities (list of short strings)\n"
    "  - education: list of objects with keys degree, field, institution, year "
    "(integer or null)\n"
    "  - projects: list of objects with keys name, description, technologies\n"
    "  - languages: list of objects with keys language, proficiency (as stated, "
    "e.g. native, fluent, B2)\n"
    "  - linked_year: integer year the CV is linked to, or null"
)


def _parse_system_prompt() -> str:
    return PARSE_CV_SYSTEM.replace("{families}", prompt_vocabulary())


def filter_role_families(names: list[str]) -> list[str]:
    """Keep only taxonomy ids (old chip ids are translated), deduped.

    The LLM may hallucinate ids; validation against config/role_taxonomy.yaml
    keeps only real ones.
    """
    return normalize_selection([n.strip().lower() for n in names if isinstance(n, str)])


def _user_prompt(cv_text: str, description: str | None) -> str:
    user_prompt = f"CV:\n\n{cv_text}"
    if description:
        user_prompt += f"\n\nWhat the candidate is looking for:\n\n{description}"
    return user_prompt


def propose_profile_from_cv(
    client: LLMClient,
    cache_dir,
    cv_text: str,
    description: str | None = None,
) -> dict:
    """Propose structured profile fields from CV text (prompt-hash cached)."""
    user_prompt = _user_prompt(cv_text, description)
    system_prompt = _parse_system_prompt()
    cached = load_cached(cache_dir, system_prompt, user_prompt)
    if cached is not None:
        logger.debug("LLM cache hit: CV profile parsing")
        return _validated_proposal(cached)

    logger.debug("LLM cache miss: CV profile parsing")
    response = client.complete_json(system_prompt, user_prompt)
    proposal = _validated_proposal(response)
    save_cached(cache_dir, system_prompt, user_prompt, response)
    return proposal


def _validated_proposal(response: str | dict) -> dict:
    """Parse the LLM response and force role ids into the taxonomy."""
    if isinstance(response, dict):
        proposal = ProposedProfile.model_validate(response).model_dump()
    else:
        proposal = ProposedProfile.model_validate_json(response).model_dump()
    proposal["suggested_role_families"] = filter_role_families(
        proposal["suggested_role_families"]
    )
    return proposal


def _clean_items(items: list[Any], model: type[BaseModel]) -> list[dict]:
    """Keep the items that fit the evidence model, in its shape."""
    out: list[dict] = []
    for item in items:
        if isinstance(item, str) and model is EvidenceLanguage:
            item = {"language": item}
        if not isinstance(item, dict):
            continue
        try:
            out.append(model.model_validate(item).model_dump())
        except ValidationError:
            logger.debug("Dropping malformed proposed evidence item: %r", item)
    return out


def _str_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    out: list[str] = []
    for value in values:
        text = str(value).strip()
        if text and text.lower() not in {item.lower() for item in out}:
            out.append(text)
    return out


def normalize_evidence(raw: dict) -> dict:
    """Coerce a proposal into the ``EvidenceData`` shape; drop what does not fit.

    Experience entries must have a title, education entries a degree and an
    institution (the ``EvidenceExperience`` / ``EvidenceEducation`` contract).
    """
    experience = []
    for item in raw.get("experience") or []:
        if isinstance(item, dict):
            item = dict(item)
            # Older prompt shape: role/period.
            if not item.get("title") and item.get("role"):
                item["title"] = item["role"]
            item["technologies"] = _str_list(item.get("technologies"))
            item["responsibilities"] = _str_list(item.get("responsibilities"))
            for key in ("start", "end", "company"):
                if item.get(key) is None:
                    item[key] = ""
                else:
                    item[key] = str(item[key])
        experience.append(item)
    education = []
    for item in raw.get("education") or []:
        if isinstance(item, dict):
            item = dict(item)
            if item.get("field") is None:
                item["field"] = ""
            year = item.get("year")
            if year is not None and not isinstance(year, int):
                try:
                    item["year"] = int(str(year)[:4])
                except ValueError:
                    item["year"] = None
        education.append(item)
    projects = []
    for item in raw.get("projects") or []:
        if isinstance(item, dict):
            item = dict(item)
            item["technologies"] = _str_list(item.get("technologies"))
        projects.append(item)
    languages = []
    for item in raw.get("languages") or []:
        if isinstance(item, dict) and item.get("proficiency") is None:
            item = {**item, "proficiency": ""}
        languages.append(item)
    linked_year = raw.get("linked_year")
    return {
        "skills": _str_list(raw.get("skills")),
        "experience": _clean_items(experience, EvidenceExperience),
        "education": _clean_items(education, EvidenceEducation),
        "projects": _clean_items(projects, EvidenceProject),
        "languages": _clean_items(languages, EvidenceLanguage),
        "linked_year": linked_year if isinstance(linked_year, int) else None,
    }


def _validated_evidence(response: str | dict) -> dict:
    """Extract the ``evidence`` sub-object of a parse response.

    Kept separate from the profile proposal so a malformed/absent evidence
    block degrades to empty rather than failing the whole proposal. When the
    evidence block names no skills, the top-level ``skills`` list is used: it
    is the same CV's skills.
    """
    if isinstance(response, str):
        try:
            parsed = json.loads(response)
        except json.JSONDecodeError:
            logger.debug("CV parse response was not valid JSON; evidence empty")
            return ProposedEvidence().model_dump()
    else:
        parsed = response
    if not isinstance(parsed, dict):
        return ProposedEvidence().model_dump()
    raw = parsed.get("evidence") if isinstance(parsed.get("evidence"), dict) else {}
    evidence = normalize_evidence(raw)
    if not evidence["skills"]:
        evidence["skills"] = _str_list(parsed.get("skills"))
    return ProposedEvidence.model_validate(evidence).model_dump()


def propose_evidence_from_cv(
    client: LLMClient,
    cache_dir,
    cv_text: str,
    description: str | None = None,
) -> dict:
    """Propose structured ``user_evidence`` fields from CV text.

    Same cache entry as the profile proposal (identical prompts), so this
    costs no extra LLM call.
    """
    user_prompt = _user_prompt(cv_text, description)
    system_prompt = _parse_system_prompt()
    cached = load_cached(cache_dir, system_prompt, user_prompt)
    if cached is None:
        # The profile proposal owns the write; this must not race it.
        return ProposedEvidence().model_dump()
    return _validated_evidence(cached)


# --------------------------------------------------------------------------
# typed roles (MF3)
# --------------------------------------------------------------------------


MAP_ROLE_SYSTEM = (
    "You map a job role a person typed to ids in a fixed role list. Choose the "
    "ids whose work the typed role does; a role can belong to more than one "
    "group (\"solutions consultant\" is both technology/solutions-engineer and "
    "sales/pre-sales). Use full \"group/role\" ids. Choose nothing rather than a "
    "loose fit.\n\n"
    "Role list:\n{families}\n\n"
    "Return JSON: {\"ids\": [string, ...]}"
)


class _RoleMapResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ids: list[str] = Field(default_factory=list)


def map_custom_role(client: LLMClient, cache_dir, text: str) -> list[str]:
    """Taxonomy ids for one typed role; one prompt-cached call per role text."""
    system_prompt = MAP_ROLE_SYSTEM.replace("{families}", prompt_vocabulary())
    user_prompt = f"Typed role: {text.strip()}"
    cached = load_cached(cache_dir, system_prompt, user_prompt)
    if cached is not None:
        try:
            return normalize_selection(_RoleMapResponse.model_validate_json(cached).ids)
        except ValidationError:
            logger.debug("LLM cache entry invalid: role mapping; re-asking")
    response = client.complete_json(system_prompt, user_prompt)
    ids = normalize_selection(_RoleMapResponse.model_validate_json(response).ids)
    save_cached(cache_dir, system_prompt, user_prompt, response)
    return ids
