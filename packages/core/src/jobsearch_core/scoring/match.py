"""Profile-vs-evidence matching (Phase 4.1).

Replaces the legacy five-dimension weighted scorer. The deterministic path is
the default and works fully offline: it reads the persisted structured
``job_profile`` (Phase 3.6d) and the DB-backed candidate evidence (Phase 4.1a)
and produces a single 0..1 match score plus strengths / gaps / blockers.

Invariants enforced here (see docs/master_plan.md, Phase 4.1):

1. **The deterministic score never depends on the LLM.** An optional bounded,
   prompt-cached LLM pass may enrich the *narrative* fields (strengths, gaps,
   blockers, summary); it can never move a job between recommendation groups,
   because grouping is computed before the LLM pass runs.
2. **A hard blocker forces ``skip``** regardless of score. A job whose only
   problem is a low score is a judgement call; a job that needs a work permit
   the candidate cannot obtain is a fact.
3. **Grouping is total and deterministic**: exactly one of ``worth``,
   ``consider``, ``skip`` for every scored job, and stable sort order within a
   group (score desc, then job id asc).
4. **Missing structured facts are not failures.** A job with no ``job_profile``
   is *pending*, never *skip*: it has not been scored, so it must not be
   presented as a rejected recommendation.
5. **No auto-application.** Producing a "worth applying" group never touches
   the tailoring workflow; that stays a separate human-approved action.

The ``jobs.total_score`` and ``jobs.fit_*`` columns hold the result (3.8c added
``fit_components``). The legacy
per-dimension ``scores`` table is not written by this phase and is dropped
later, by the end-of-roadmap cleanup (decided 2026-10-02).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from jobsearch_core.countries import country_matches
from jobsearch_core.llm.cache import load_cached, save_cached
from jobsearch_core.llm.client import LLMClient
from jobsearch_core.log import get_logger
from jobsearch_core.profile_evidence import CandidateEvidence
from jobsearch_core.role_taxonomy import phrase_in_title, role_fit

logger = get_logger(__name__)

WORTH = "worth"
CONSIDER = "consider"
SKIP = "skip"

RECOMMENDATION_ORDER: tuple[str, ...] = (WORTH, CONSIDER, SKIP)

RECOMMENDATION_LABELS: dict[str, str] = {
    WORTH: "Worth Applying",
    CONSIDER: "Consider",
    SKIP: "Skip",
}

# Hard-blocker detection. These are the same authorization / location facts the
# legacy reject gate used, but they are now *evidence against a candidate* on an
# otherwise-applicable job rather than a reason to drop the job at scrape time.
_BLOCKER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "Requires citizenship or work authorization the profile does not show",
        re.compile(
            r"(?i)\b(must be a citizen|must hold a valid .*work permit|"
            r"security clearance|top secret|ts/sci)\b"
        ),
    ),
    (
        "States explicitly that no sponsorship is available",
        re.compile(r"(?i)\b(no visa sponsorship|no sponsorship)\b"),
    ),
)


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


class MatchWeights(BaseModel):
    """Relative weight of each deterministic match component.

    Sums to 1.0 by construction is *not* enforced: the score is normalised by
    the sum of the weights of the components actually compared (MF2). A
    component with nothing to compare is left out, never scored as a perfect
    or a neutral fit.
    """

    required_technologies: float = Field(default=0.35, ge=0)
    experience: float = Field(default=0.25, ge=0)
    location: float = Field(default=0.20, ge=0)
    nice_to_have: float = Field(default=0.10, ge=0)
    languages: float = Field(default=0.10, ge=0)
    # MF4: set once, then checked on the live run (Known Gap 9), not tuned.
    role_fit: float = Field(default=0.30, ge=0)


class MatchConfig(BaseModel):
    """User-tunable match behaviour (Phase 4.1).

    Loaded from ``config/defaults.yaml``; overridable per invocation.
    """

    worth_threshold: float = Field(default=0.70, ge=0, le=1)
    consider_threshold: float = Field(default=0.40, ge=0, le=1)
    max_llm_notes: int = Field(default=10, ge=0)
    weights: MatchWeights = Field(default_factory=MatchWeights)


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------


@dataclass
class MatchResult:
    """One job's deterministic match outcome."""

    job_id: int
    score: float
    recommendation: str
    strengths: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    summary: str = ""
    components: dict[str, float] = field(default_factory=dict)
    # MF4: why Matches hides this job by default (a role mismatch). Not a
    # blocker: /jobs and the stored score keep the job.
    hidden_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "score": round(self.score, 4),
            "recommendation": self.recommendation,
            "strengths": list(self.strengths),
            "gaps": list(self.gaps),
            "blockers": list(self.blockers),
            "summary": self.summary,
            "components": {k: round(v, 4) for k, v in self.components.items()},
            "hidden_reason": self.hidden_reason,
        }


# ---------------------------------------------------------------------------
# reading the persisted profile
# ---------------------------------------------------------------------------


def load_job_profile(job: Any) -> dict[str, Any] | None:
    """Parse the persisted ``job_profile`` JSON of a ``Job`` row.

    Returns ``None`` when no structured profile exists or it cannot be parsed,
    which the caller must treat as *pending*, not as a rejected job.
    """
    raw = getattr(job, "job_profile", None)
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("Job %s has an unparseable job_profile", getattr(job, "id", "?"))
        return None
    if not isinstance(value, dict):
        return None
    return value


def _as_str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


# ---------------------------------------------------------------------------
# evidence projections
# ---------------------------------------------------------------------------


def evidence_skill_bank(evidence: CandidateEvidence) -> set[str]:
    """Every technology the candidate can evidence, normalised lowercase.

    Includes the explicit skills list plus technologies named in experience and
    projects, because Phase 4.1a split those into separate owner fields.
    """
    bank: set[str] = set()
    for item in _as_str_list(evidence.skills):
        bank.add(item.lower())
    for role in evidence.experience:
        for tech in role.technologies:
            bank.add(tech.lower())
    for project in evidence.projects:
        for tech in project.technologies:
            bank.add(tech.lower())
    return bank


def _normalise_tech(value: str) -> str:
    return re.sub(r"[\s.+#-]+", " ", value.strip().lower()).strip()


def tech_match(required: list[str], bank: set[str]) -> tuple[float | None, list[str], list[str]]:
    """Fraction of ``required`` technologies present in the candidate bank.

    ``None`` when the posting names no technology: there is nothing to compare,
    so the component is left out of the score (MF2), not scored as perfect.
    """
    if not required:
        return None, [], []
    normalised_bank = {_normalise_tech(item) for item in bank}
    present: list[str] = []
    missing: list[str] = []
    for tech in required:
        key = _normalise_tech(tech)
        if not key:
            continue
        if key in normalised_bank or any(key in item or item in key for item in normalised_bank):
            present.append(tech)
        else:
            missing.append(tech)
    if not present and not missing:
        return None, [], []
    return len(present) / (len(present) + len(missing)), present, missing


def candidate_years_of_experience(evidence: CandidateEvidence) -> float:
    """Total years across employment entries, ``present`` ending now."""
    now_year = datetime.now(UTC).year
    total = 0.0
    for role in evidence.experience:
        start = _parse_year(role.start)
        if start is None:
            continue
        raw_end = (role.end or "").strip().lower()
        if raw_end in ("", "present", "current", "now"):
            end = now_year
        else:
            parsed = _parse_year(role.end)
            end = parsed if parsed is not None else start
        total += max(0.0, end - start)
    return total


def _parse_year(value: str | None) -> int | None:
    if not value:
        return None
    match = re.search(r"(\d{4})", str(value))
    return int(match.group(1)) if match else None


def _country_matches(job_country: str | None, targets: list[str]) -> bool:
    # MF6: whole names through the alias table, never a substring test.
    return country_matches(job_country, targets)


# ---------------------------------------------------------------------------
# components
# ---------------------------------------------------------------------------


def _required_tech_component(
    profile: dict[str, Any], bank: set[str]
) -> tuple[float | None, list[str], list[str]]:
    return tech_match(_as_str_list(profile.get("required_technologies")), bank)


def _nice_to_have_component(
    profile: dict[str, Any], bank: set[str]
) -> tuple[float | None, list[str], list[str]]:
    return tech_match(_as_str_list(profile.get("nice_to_have_technologies")), bank)


def _experience_component(
    profile: dict[str, Any], evidence: CandidateEvidence
) -> tuple[float | None, list[str], list[str]]:
    """Compare the posting's stated requirement with evidenced years.

    ``required_years_of_experience == 0`` means "not stated": nothing to
    compare, so the component is left out (MF2).
    """
    raw_required = profile.get("required_years_of_experience") or 0
    try:
        required = float(raw_required)
    except (TypeError, ValueError):
        required = 0.0
    have = candidate_years_of_experience(evidence)
    notes: list[str] = []
    if required <= 0:
        return None, notes, []
    if have >= required:
        notes.append(f"Evidences ~{have:.0f} years against {required:.0f} required")
        return 1.0, notes, []
    ratio = have / required if required else 0.0
    return ratio, notes, [f"~{have:.0f} years evidenced against {required:.0f} required"]


def _location_component(
    profile: dict[str, Any], job: Any, evidence: CandidateEvidence
) -> tuple[float, list[str], list[str]]:
    """Location eligibility, never a blocker on its own."""
    remote = str(profile.get("remote_policy") or "unknown").strip().lower()
    sponsorship = str(profile.get("sponsorship") or "not_stated").strip().lower()
    strengths: list[str] = []
    gaps: list[str] = []

    if remote == "remote":
        strengths.append("Fully remote")
        return 1.0, strengths, gaps
    if evidence.visa_required and sponsorship != "mentioned":
        gaps.append("Visa required but the posting does not offer sponsorship")
    if _country_matches(getattr(job, "country", None), evidence.target_countries):
        strengths.append(f"In target country {job.country}")
        return 1.0, strengths, gaps
    if remote == "hybrid":
        gaps.append(f"Hybrid role outside target countries ({remote})")
        return 0.30, strengths, gaps
    if remote == "onsite":
        gaps.append("Onsite role outside target countries")
        return 0.15, strengths, gaps
    gaps.append("Remote policy unstated and not in a target country")
    return 0.40, strengths, gaps


def _language_component(
    profile: dict[str, Any], evidence: CandidateEvidence
) -> tuple[float | None, list[str], list[str]]:
    """Required job languages against evidenced languages."""
    entries = profile.get("languages")
    required: list[str] = []
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, dict) and entry.get("language"):
                required.append(str(entry["language"]))
            elif isinstance(entry, str):
                required.append(entry)
    if not required:
        return None, [], []
    have = {item.language.strip().lower() for item in evidence.languages if item.language}
    strengths: list[str] = []
    missing: list[str] = []
    for language in required:
        key = language.strip().lower()
        (strengths if key in have else missing).append(language)
    total = len(strengths) + len(missing)
    return (len(strengths) / total if total else None), strengths, missing


def detect_blockers(profile: dict[str, Any], job: Any, evidence: CandidateEvidence) -> list[str]:
    """Hard blockers: facts that make the job inapplicable regardless of score."""
    blockers: list[str] = []
    description = (getattr(job, "description", None) or "").strip()
    for label, pattern in _BLOCKER_PATTERNS:
        if pattern.search(description):
            blockers.append(label)
    sponsorship = str(profile.get("sponsorship") or "not_stated").strip().lower()
    if evidence.visa_required and sponsorship != "mentioned":
        blockers.append("Profile requires a visa and the posting offers no sponsorship")
    remote = str(profile.get("remote_policy") or "unknown").strip().lower()
    if remote not in ("remote",) and not _country_matches(
        getattr(job, "country", None), evidence.target_countries
    ):
        country = getattr(job, "country", None)
        blockers.append(
            f"Onsite or hybrid in {country}, which is not a target country"
            if remote in ("onsite", "hybrid")
            else "Location outside target countries and remote policy unstated"
        )
    # Preserve order while removing duplicates.
    seen: set[str] = set()
    unique: list[str] = []
    for blocker in blockers:
        if blocker not in seen:
            seen.add(blocker)
            unique.append(blocker)
    return unique


# ---------------------------------------------------------------------------
# the deterministic scorer
# ---------------------------------------------------------------------------


def score_job_against_evidence(
    job: Any,
    profile: dict[str, Any],
    evidence: CandidateEvidence,
    config: MatchConfig | None = None,
) -> MatchResult:
    """Score one job deterministically. Never calls the LLM.

    ``profile`` is the persisted ``job_profile`` dict; the caller must have
    established that it exists (invariant 4).
    """
    config = config or MatchConfig()
    bank = evidence_skill_bank(evidence)

    required_tech, tech_strengths, tech_gaps = _required_tech_component(profile, bank)
    nice_tech, nice_strengths, nice_gaps = _nice_to_have_component(profile, bank)
    experience, exp_strengths, exp_gaps = _experience_component(profile, evidence)
    location, loc_strengths, loc_gaps = _location_component(profile, job, evidence)
    languages, lang_strengths, lang_gaps = _language_component(profile, evidence)
    title = getattr(job, "raw_title", None) or getattr(job, "normalized_title", None)
    role = role_fit(profile, title, evidence.target_roles, evidence.custom_roles)

    candidates: dict[str, tuple[float | None, float]] = {
        "role_fit": (role.value, config.weights.role_fit),
        "required_technologies": (required_tech, config.weights.required_technologies),
        "experience": (experience, config.weights.experience),
        "location": (location, config.weights.location),
        "nice_to_have": (nice_tech, config.weights.nice_to_have),
        "languages": (languages, config.weights.languages),
    }
    # MF2: a component with nothing to compare is left out of the weighted sum.
    components = {
        name: (value, weight) for name, (value, weight) in candidates.items() if value is not None
    }

    weighted_sum = sum(value * weight for value, weight in components.values())
    total_weight = sum(weight for _, weight in components.values())
    score = weighted_sum / total_weight if total_weight else 0.0
    score = max(0.0, min(1.0, score))

    city = _target_city_strength(job, evidence.target_cities)
    strengths = [
        *([role.strength] if role.strength else []),
        *tech_strengths, *nice_strengths, *exp_strengths, *loc_strengths,
        *([city] if city else []),
        *lang_strengths,
    ]
    gaps = [
        *([role.gap] if role.gap else []),
        *tech_gaps, *nice_gaps, *exp_gaps, *loc_gaps, *lang_gaps,
    ]
    blockers = detect_blockers(profile, job, evidence)

    return MatchResult(
        job_id=getattr(job, "id", 0),
        score=score,
        recommendation=classify(score, blockers, config),
        strengths=strengths,
        gaps=gaps,
        blockers=blockers,
        summary=_build_summary(score, strengths, gaps, blockers, list(components)),
        components={name: value for name, (value, _) in components.items()},
        hidden_reason=role.hide_reason,
    )


def _target_city_strength(job: Any, cities: list[str]) -> str | None:
    """MF5: a target city named in the job location is a strength line only."""
    location = " ".join(
        str(part)
        for part in (getattr(job, "raw_location", None), getattr(job, "normalized_location", None))
        if part
    )
    if not location:
        return None
    for city in cities:
        if city.strip() and phrase_in_title(city, location):
            return f"In your target city {city.strip()}"
    return None


def classify(score: float, blockers: list[str], config: MatchConfig | None = None) -> str:
    """Map a score plus blockers to exactly one recommendation group.

    Blocker-first (invariant 2), then the configured thresholds.
    """
    config = config or MatchConfig()
    if blockers:
        return SKIP
    if score >= config.worth_threshold:
        return WORTH
    if score >= config.consider_threshold:
        return CONSIDER
    return SKIP


COMPONENT_LABELS: dict[str, str] = {
    "role_fit": "role",
    "required_technologies": "required technologies",
    "experience": "experience",
    "location": "location",
    "nice_to_have": "nice-to-have technologies",
    "languages": "languages",
}


def _build_summary(
    score: float,
    strengths: list[str],
    gaps: list[str],
    blockers: list[str],
    compared: list[str] | None = None,
) -> str:
    if blockers:
        head = f"{score:.0%} match — blocked: {blockers[0]}"
    elif strengths and not gaps:
        head = f"{score:.0%} match — {len(strengths)} evidenced strength(s), no stated gaps"
    elif strengths:
        head = f"{score:.0%} match — {len(strengths)} strength(s), {len(gaps)} gap(s)"
    elif gaps:
        head = f"{score:.0%} match — {len(gaps)} gap(s) against the requirements"
    else:
        head = f"{score:.0%} match — no comparable requirements stated"
    if compared is None:
        return head
    # MF2: name what the score was made of, so a high score on one component
    # reads as exactly that.
    names = ", ".join(COMPONENT_LABELS.get(name, name) for name in compared) or "nothing"
    return f"{head}. Compared: {names}."


def group_results(results: list[MatchResult]) -> dict[str, list[MatchResult]]:
    """Group by recommendation, each group sorted by score desc then job id."""
    grouped: dict[str, list[MatchResult]] = {name: [] for name in RECOMMENDATION_ORDER}
    for result in results:
        grouped.setdefault(result.recommendation, []).append(result)
    for entries in grouped.values():
        entries.sort(key=lambda r: (-r.score, r.job_id))
    return grouped


# ---------------------------------------------------------------------------
# optional bounded LLM notes
# ---------------------------------------------------------------------------


class MatchNotesResponse(BaseModel):
    """Narrative only. The score is computed before this and never revised."""

    model_config = {"extra": "ignore"}

    strengths: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    summary: str = ""


MATCH_NOTES_SYSTEM = (
    "You explain, in plain language, why a candidate matches or does not match "
    "a job posting. You never invent requirements: every statement must be "
    "grounded in the structured job profile or the candidate evidence you are "
    "given.\n\n"
    "Return JSON with these keys:\n"
    "- strengths: list of strings\n"
    "- gaps: list of strings\n"
    "- blockers: list of hard blockers (authorization, location)\n"
    "- summary: one sentence"
)


def build_match_notes_prompt(profile: dict[str, Any], evidence_payload: dict[str, Any]) -> str:
    """The notes prompt is built from the sanitised evidence projection.

    Contact details can never reach this prompt: ``evidence_payload`` must be
    :func:`jobsearch_core.profile_evidence.evidence_prompt_payload` output.
    """
    return (
        "Candidate evidence:\n"
        f"{json.dumps(evidence_payload, ensure_ascii=False, indent=2)}\n\n"
        "Structured job profile:\n"
        f"{json.dumps(profile, ensure_ascii=False, indent=2)}"
    )


def explain_match(
    client: LLMClient,
    cache_dir: Path,
    profile: dict[str, Any],
    evidence_payload: dict[str, Any],
) -> dict[str, Any]:
    """One cached LLM call producing narrative fields for a single job.

    Prompt-hash cached, so a re-run with unchanged inputs makes no API call.
    Failures are the caller's to handle: this function propagates.
    """
    user_prompt = build_match_notes_prompt(profile, evidence_payload)
    cached = load_cached(cache_dir, MATCH_NOTES_SYSTEM, user_prompt)
    if cached is not None:
        logger.debug("LLM cache hit: match notes")
        return MatchNotesResponse.model_validate_json(cached).model_dump()

    logger.debug("LLM cache miss: match notes")
    response = client.complete_json(MATCH_NOTES_SYSTEM, user_prompt)
    save_cached(cache_dir, MATCH_NOTES_SYSTEM, user_prompt, response)
    return MatchNotesResponse.model_validate_json(response).model_dump()


def select_jobs_for_notes(results: list[MatchResult]) -> list[MatchResult]:
    """The ``worth`` group first, then ``consider``, never ``skip``.

    Blocked jobs are excluded because an LLM explanation cannot make them
    applicable, so paying for one would spend the bounded budget on advice the
    user will not act on. Ordering is deterministic, so truncating to the caller's
    cap always picks the same jobs.
    """
    grouped = group_results(results)
    return [*grouped[WORTH], *grouped[CONSIDER]]


# ---------------------------------------------------------------------------
# persistence helpers
# ---------------------------------------------------------------------------


def apply_result_to_job(job: Any, result: MatchResult) -> None:
    """Write a result into the reused score / fit columns (no migration).

    Deterministic reasons are written first. A later LLM notes pass may replace
    the narrative fields, never ``total_score``.
    """
    job.total_score = result.score
    job.fit_strengths = json.dumps(result.strengths, ensure_ascii=False)
    job.fit_gaps = json.dumps(result.gaps, ensure_ascii=False)
    job.fit_blockers = json.dumps(result.blockers, ensure_ascii=False)
    job.fit_summary = result.summary
    job.fit_components = json.dumps(
        {name: round(value, 4) for name, value in result.components.items()}
    )
    job.fit_hidden_reason = result.hidden_reason


def match_config_from_defaults(defaults) -> MatchConfig:
    """Build a :class:`MatchConfig` from ``config/defaults.yaml`` values."""
    return MatchConfig(
        worth_threshold=getattr(defaults, "worth_threshold", 0.70),
        consider_threshold=getattr(defaults, "consider_threshold", 0.40),
        max_llm_notes=getattr(defaults, "max_llm_notes", 10),
    )


def results_to_report_data(
    results: list[MatchResult],
    jobs: dict[int, Any] | None = None,
) -> dict[str, Any]:
    """Serializable grouped view for a run summary and the report.

    The shape is the report renderer's entry shape, not the raw
    :meth:`MatchResult.as_dict` shape: title/company/url and the job's
    persisted attributes are merged in so a report written straight from a
    match run reads exactly like one regenerated later from the DB.

    ``jobs`` maps job id to the ``Job`` row. It is optional so the function
    stays usable with results alone (tests, API payloads); without it the
    presentation fields are simply absent and the renderer falls back.
    """
    grouped = group_results(results)
    payload: dict[str, list[dict[str, Any]]] = {}
    for name, entries in grouped.items():
        rows: list[dict[str, Any]] = []
        for result in entries:
            entry = result.as_dict()
            job = (jobs or {}).get(result.job_id)
            if job is not None:
                entry.update(
                    title=getattr(job, "raw_title", None),
                    company=getattr(job, "raw_company", None),
                    url=getattr(job, "url", None),
                    location=getattr(job, "raw_location", None),
                    country=getattr(job, "country", None),
                    remote_policy=getattr(job, "remote_policy", None),
                    seniority=getattr(job, "seniority", None),
                    salary=getattr(job, "salary", None),
                    closing_date=getattr(job, "closing_date", None),
                )
            rows.append(entry)
        payload[name] = rows
    return payload
