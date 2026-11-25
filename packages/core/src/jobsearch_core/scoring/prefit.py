"""Prefit: a free ranking of unprofiled jobs before paid extraction (3.8c M1).

A match run may pay for only ``max_profile_extractions`` job-profile calls, so
it must pick the jobs most likely to fit. Prefit reads only stored columns: no
LLM call, no fetch.

1. **Drop** a job whose country is not a target country and whose text does not
   say remote. The matcher would block it anyway (``detect_blockers``). With no
   target countries set, nothing is dropped.
2. **Rank** the rest. A suspected non-posting (``posting_check``: an article,
   a category page, a press release) sorts after every real-looking job, so no
   extraction budget goes to it while real jobs remain (MQ). It is ranked, never
   dropped or deleted. Then a job with a fit flag sorts after every unflagged
   job (flags are soft before the demo, master_plan §3.8c M0). Then a job whose
   title names a selected role (taxonomy keywords or a typed role, MF3). Then
   higher evidence skill density in title and description: distinct skill
   terms per word, so a long page cannot buy rank by length. Then the older
   job id.

"Unprofiled" includes a job whose stored profile predates MF3 (no
``job_role``): it is re-extracted within the same budget, in the same order.

The drop list is a fact about location only. Fit against the rest of the
profile is the matcher's verdict, never a status.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from jobsearch_core.db.models import Job
from jobsearch_core.ingest.scrapers.posting_check import suspect_reason
from jobsearch_core.profile_evidence import CandidateEvidence
from jobsearch_core.role_taxonomy import custom_role_ids, custom_role_texts, title_keyword_hits
from jobsearch_core.scoring.match import _country_matches, evidence_skill_bank
from jobsearch_core.scoring.profile import profile_is_current

MIN_DENSITY_WORDS = 150

_REMOTE = re.compile(r"\bremote\b", re.IGNORECASE)


@dataclass(frozen=True)
class PrefitEntry:
    job_id: int
    flagged: bool
    role_hits: int
    skill_hits: int
    skill_density: float = 0.0
    suspect: bool = False


@dataclass(frozen=True)
class PrefitResult:
    ranked: list[PrefitEntry]  # best first; unprofiled, passing, not failed
    dropped: int  # unprofiled jobs dropped by the location rule


def _says_remote(job: Any) -> bool:
    if (getattr(job, "remote_policy", None) or "").strip().lower() == "remote":
        return True
    text = " ".join(
        part for part in (job.raw_title, job.raw_location, job.description) if part
    )
    return bool(_REMOTE.search(text))


def _term_pattern(term: str) -> re.Pattern[str]:
    # Whole-token match that also protects "c", "r", "c++" and "c#".
    return re.compile(r"(?<![\w+#.])" + re.escape(term) + r"(?![\w+#])")


def _skill_hits(text: str, patterns: list[re.Pattern[str]]) -> int:
    return sum(1 for pattern in patterns if pattern.search(text))


def skill_density(hits: int, text: str) -> float:
    """Distinct skill terms per word, with a floor on the word count so a very
    short text cannot win on one term."""
    return hits / max(len(text.split()), MIN_DENSITY_WORDS)


def _json_list(raw: str | None) -> list[Any]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    return value if isinstance(value, list) else []


def prefit_unprofiled(session: Session, evidence: CandidateEvidence) -> PrefitResult:
    """Rank every stored job that has a description but no job profile.

    A job whose last profile answer was invalid (``profile_failed_at``) is not
    a candidate: it would fail the same way again.
    """
    selection = list(evidence.target_roles) + custom_role_ids(evidence.custom_roles)
    typed = custom_role_texts(evidence.custom_roles)
    patterns = [_term_pattern(term) for term in sorted(evidence_skill_bank(evidence)) if term]
    rows = (
        session.query(
            Job.id,
            Job.url,
            Job.raw_title,
            Job.raw_location,
            Job.country,
            Job.remote_policy,
            Job.description,
            Job.job_profile,
            Job.fit_flags,
        )
        .filter(
            Job.description.isnot(None),
            Job.description != "",
            Job.profile_failed_at.is_(None),
        )
        .all()
    )

    ranked: list[PrefitEntry] = []
    dropped = 0
    for row in rows:
        if profile_is_current(row.job_profile):
            continue
        if evidence.target_countries and not (
            _country_matches(row.country, evidence.target_countries) or _says_remote(row)
        ):
            dropped += 1
            continue
        text = f"{row.raw_title}\n{row.description}".lower()
        hits = _skill_hits(text, patterns)
        ranked.append(
            PrefitEntry(
                job_id=row.id,
                flagged=bool(_json_list(row.fit_flags)),
                role_hits=title_keyword_hits(row.raw_title, selection, typed),
                skill_hits=hits,
                skill_density=skill_density(hits, text),
                suspect=suspect_reason(row.raw_title, row.url) is not None,
            )
        )
    ranked.sort(
        key=lambda e: (e.suspect, e.flagged, e.role_hits == 0, -e.skill_density, e.job_id)
    )
    return PrefitResult(ranked=ranked, dropped=dropped)
