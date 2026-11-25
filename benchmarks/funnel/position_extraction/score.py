"""position_extraction scoring: stage outcomes -> rates and first-failing-stage attribution.

Pure logic, no network. The live probes live in ``run.py``; this module only
turns their per-company outcomes into the report shape:

- Each stage is a rate over the previous stage (``A2`` is over all companies;
  ``A3`` is over companies that passed ``A2``; ``A4`` over those that passed
  ``A3``), plus an end-to-end rate over companies scored at every stage.
- Unscored outcomes (transport failure, login wall) are excluded from every
  rate and counted beside it. ``None``, not ``0.0``, on an empty denominator.
- Each company is attributed to the first stage that failed, so a wrong link
  (A2) and a wrong extractor (A3) show up as different drop-offs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

PASS = "pass"
FAIL = "fail"
UNSCORED = "unscored"

#: Stages after A1, in funnel order. A1 is reported as unscored until the
#: company -> careers discovery step is wired (see the buildreport).
STAGES: tuple[str, ...] = ("A2", "A3", "A4")
STRATA: tuple[str, ...] = ("large", "medium", "startup", "fi_sme")


@dataclass
class StageOutcome:
    status: str  # PASS | FAIL | UNSCORED
    reason: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class CompanyResult:
    key: str
    stratum: str
    site_class: str = "unclassified"
    outcomes: dict[str, StageOutcome] = field(default_factory=dict)

    def status(self, stage: str) -> str | None:
        outcome = self.outcomes.get(stage)
        return outcome.status if outcome else None


def rate(passed: int, scored: int) -> float | None:
    """Pass rate over scored cases; ``None`` when nothing was scored."""
    if scored == 0:
        return None
    return passed / scored


def attribute(result: CompanyResult) -> str:
    """Name the first failing stage, ``unscored`` if blocked, else ``pass``."""
    for stage in STAGES:
        status = result.status(stage)
        if status == FAIL:
            return f"fail:{stage}"
        if status == UNSCORED:
            return f"unscored:{stage}"
    return "pass"


def _stage_rate(results: list[CompanyResult], stage: str) -> dict[str, Any]:
    index = STAGES.index(stage)
    if index == 0:
        # A2 is measured over every company.
        eligible = results
    else:
        # Each later stage is measured only over companies that passed the
        # stage before it.
        previous = STAGES[index - 1]
        eligible = [r for r in results if r.status(previous) == PASS]
    scored = [r for r in eligible if r.status(stage) in (PASS, FAIL)]
    passed = [r for r in scored if r.status(stage) == PASS]
    unscored = [r for r in eligible if r.status(stage) == UNSCORED]
    return {
        "rate": rate(len(passed), len(scored)),
        "passed": len(passed),
        "scored": len(scored),
        "unscored": len(unscored),
        "unscored_keys": sorted(r.key for r in unscored),
        "eligible": len(eligible),
    }


def _end_to_end(results: list[CompanyResult]) -> dict[str, Any]:
    # Scored end-to-end: no stage blocked. A company that failed a stage is
    # scored (as a failure); one blocked by an unscored outcome is not.
    blocked = [r for r in results if any(r.status(s) == UNSCORED for s in STAGES)]
    scored = [r for r in results if r not in blocked]
    passed = [r for r in scored if all(r.status(s) == PASS for s in STAGES)]
    return {
        "rate": rate(len(passed), len(scored)),
        "passed": len(passed),
        "scored": len(scored),
        "unscored": len(blocked),
        "unscored_keys": sorted(r.key for r in blocked),
    }


def summarize(results: list[CompanyResult]) -> dict[str, Any]:
    """Aggregate per stratum and overall, in the shared report shape."""
    groups: dict[str, list[CompanyResult]] = {"all": results}
    for stratum in STRATA:
        groups[stratum] = [r for r in results if r.stratum == stratum]
    summary: dict[str, Any] = {}
    for name, members in groups.items():
        block: dict[str, Any] = {"companies": len(members)}
        for stage in STAGES:
            block[stage] = _stage_rate(members, stage)
        block["end_to_end"] = _end_to_end(members)
        block["attribution"] = {
            key: sum(1 for r in members if attribute(r) == key)
            for key in sorted({attribute(r) for r in members})
        }
        summary[name] = block
    return summary
