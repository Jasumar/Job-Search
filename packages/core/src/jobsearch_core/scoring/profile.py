"""Extraction-first job profiles (Phase 3.6d).

One LLM extraction call per job at collect time, persisted as a JSON
``job_profile``. All user-fit scoring is deferred to Phase 4.1; this module
only extracts neutral, structured facts from the job description.
"""

from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from jobsearch_core.llm.cache import load_cached, save_cached
from jobsearch_core.llm.client import LLMClient
from jobsearch_core.log import get_logger
from jobsearch_core.role_taxonomy import OTHER, load_taxonomy, prompt_vocabulary

logger = get_logger(__name__)


class InvalidProfileResponseError(ValueError):
    """The model answered, but the answer is not a valid profile.

    The same description gives the same answer again, so the caller marks the job
    instead of retrying it on every run (RF8). Transport errors are not this.
    """


class JobProfileResponse(BaseModel):
    """Structured neutral profile extracted from a job description."""

    model_config = ConfigDict(extra="ignore")

    required_technologies: list[str] = Field(default_factory=list)
    nice_to_have_technologies: list[str] = Field(default_factory=list)
    seniority: str = "unknown"
    required_years_of_experience: int = Field(default=0, ge=0)
    required_education: str | None = None
    languages: list[dict] = Field(default_factory=list)  # {language, level}
    remote_policy: str = "unknown"
    work_locations: list[str] = Field(default_factory=list)
    sponsorship: str = "not_stated"
    salary: str | None = None
    employment_type: str | None = None
    domain: str | None = None
    closing_date: str | None = None
    summary: str = ""
    # MF3: the job's function from config/role_taxonomy.yaml (a fact about the
    # job, not the candidate). "other" when no taxonomy role fits.
    job_group: str = OTHER
    job_role: str = OTHER


EXTRACT_PROFILE_SYSTEM = (
    "You extract structured facts from a job posting into JSON. "
    "Only record what the posting explicitly states; never invent requirements. "
    "Use \"unknown\"/\"not_stated\" when a field is not mentioned.\n\n"
    "Return JSON with these keys:\n"
    "- required_technologies: list of strings (must-have technologies)\n"
    "- nice_to_have_technologies: list of strings (nice-to-have / bonus)\n"
    "- seniority: string (e.g. junior / mid / senior / lead / unknown)\n"
    "- required_years_of_experience: integer (0 if not stated)\n"
    "- required_education: string or null\n"
    "- languages: list of objects {language, level} with required level "
    "(e.g. Finnish B2); empty if none required. Consider the posting's country.\n"
    "- remote_policy: string (remote / hybrid / onsite / unknown)\n"
    "- work_locations: list of strings (cities/countries mentioned)\n"
    "- sponsorship: string (mentioned / not_stated) for work-authorization or visa sponsorship\n"
    "- salary: string or null (as stated)\n"
    "- employment_type: string or null (permanent / contract / fixed-term ...)\n"
    "- domain: string or null (the employer's domain/industry)\n"
    "- closing_date: string or null (application deadline as stated)\n"
    "- summary: short neutral one-sentence summary of the role\n"
    "- job_group: the group id of the job's function from the role list below, "
    "or \"other\"\n"
    "- job_role: the full \"group/role\" id of the job's function from the role "
    "list below, judged from the title and the duties; \"other\" when none fits\n\n"
    "Role list:\n"
)


def _extract_system_prompt() -> str:
    # The role list is part of the system prompt, so a taxonomy change changes
    # the cache key and the next match run re-extracts (MF3).
    return EXTRACT_PROFILE_SYSTEM + prompt_vocabulary()


def normalize_job_role(profile: dict) -> dict:
    """Force ``job_group`` / ``job_role`` into the taxonomy, else "other"."""
    taxonomy = load_taxonomy()
    role = str(profile.get("job_role") or "").strip().lower()
    group = str(profile.get("job_group") or "").strip().lower()
    if role in taxonomy.roles:
        profile["job_role"] = role
        profile["job_group"] = taxonomy.roles[role].group
        return profile
    profile["job_role"] = OTHER
    profile["job_group"] = group if group in taxonomy.group_ids else OTHER
    return profile


def profile_is_current(raw: str | None) -> bool:
    """Whether a stored ``job_profile`` exists and carries the MF3 role fields.

    A profile extracted before MF3 has no ``job_role``; the match run
    re-extracts it within ``max_profile_extractions`` (prefit order).
    """
    if not raw:
        return False
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return False
    return isinstance(value, dict) and "job_role" in value


def extract_job_profile(
    client: LLMClient, cache_dir, description: str, country: str | None = None
) -> dict:
    """Extract the structured job profile, prompt-hash cached like other calls.

    ``country`` biases language-extraction (e.g. Finnish/Swedish for FI roles)
    but only when explicitly stated in the posting.
    """
    user_prompt = f"Job description:\n\n{description}"
    if country:
        user_prompt += f"\n\n(Job country: {country})"
    system_prompt = _extract_system_prompt()
    cached = load_cached(cache_dir, system_prompt, user_prompt)
    if cached is not None:
        try:
            profile = normalize_job_role(JobProfileResponse.model_validate_json(cached).model_dump())
        except ValidationError:
            # An invalid answer cached by an older build must not repeat forever.
            logger.debug("LLM cache entry invalid: job profile extraction; re-asking")
        else:
            logger.debug("LLM cache hit: job profile extraction")
            return profile

    logger.debug("LLM cache miss: job profile extraction")
    response = client.complete_json(system_prompt, user_prompt)
    # Validate before caching (RF8): an invalid answer is never cached.
    try:
        profile = normalize_job_role(JobProfileResponse.model_validate_json(response).model_dump())
    except ValidationError as exc:
        raise InvalidProfileResponseError(str(exc)) from exc
    save_cached(cache_dir, system_prompt, user_prompt, response)
    return profile


def populate_flattened_from_profile(job, profile: dict) -> None:
    """Fill the legacy flattened columns from the profile so downstream
    consumers (report, dry-run scoring) keep working unchanged."""
    languages = [
        f"{entry.get('language', '')} {entry.get('level', '')}".strip()
        for entry in profile.get("languages", [])
        if entry.get("language")
    ]
    if languages:
        job.languages_required = json.dumps(languages, ensure_ascii=False)
    if profile.get("remote_policy") and profile["remote_policy"] != "unknown":
        job.remote_policy = profile["remote_policy"]
    if profile.get("seniority") and profile["seniority"] != "unknown":
        job.seniority = profile["seniority"]
    if profile.get("employment_type"):
        job.employment_type = profile["employment_type"]
    if profile.get("salary"):
        job.salary = profile["salary"]
    if profile.get("closing_date"):
        job.closing_date = profile["closing_date"]
    job.sponsorship = profile.get("sponsorship", "not_stated")
