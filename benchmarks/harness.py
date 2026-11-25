"""Shared benchmark harness plumbing (suite convention: one harness, many scorers).

Decided 2026-10-02 with the suite direction: a new benchmark is the existing
runner with a different scorer and a different ground-truth format, not a
bespoke script. Entry 1 (listicle extraction) and entry 3 (posting reach rate)
are the first two users of this seam.

What lives here, and why it is not per-benchmark:

- **Ground truth load + freshness.** Every GT row carries a "last verified"
  date, and a stale label fabricates a regression that costs a debugging
  session to disprove. The check is shared so no benchmark can forget it.
- **Run context** (model, cache dir, HTTP client) so a scorer never has to
  know how the fetch was done.
- **Report writing** with a run timestamp, so a number can always be traced
  back to when it was produced.

What deliberately stays per-benchmark: the scorer, the GT *format*, and the
report's per-case detail. Labelling cost differs per entry, and company_discovery's
signal-based pagination GT is a completely different shape from a binary
reachable/unreachable label.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# Labels older than this are reported as stale. Not a gate: a stale label is a
# reason to re-verify by hand, not a reason to discard a measurement.
STALE_AFTER_DAYS = 90


class BenchmarkError(Exception):
    """Raised for a malformed benchmark input the user must fix by hand."""


@dataclass
class GroundTruthRow:
    """One hand-labelled case.

    ``label`` is deliberately untyped: entry 3 is binary (reachable or not),
    entry 1 is a name list, and company_discovery's pagination entry is signal-based. The
    harness must not force one benchmark's label shape onto another's.
    """

    key: str
    label: Any
    last_verified: str | None = None
    note: str = ""
    #: The row exactly as it appears in the GT file. Scorers that need per-case
    #: fields beyond the label (entry 3 needs each case's ``url``) read them from
    #: here rather than having the harness guess a schema — the suite's GT
    #: formats differ per entry by design, so this stays untyped.
    raw: Any = None

    @property
    def verified_at(self) -> datetime | None:
        if not self.last_verified:
            return None
        try:
            return datetime.fromisoformat(self.last_verified).replace(tzinfo=UTC)
        except ValueError as exc:
            raise BenchmarkError(
                f"Ground-truth row {self.key!r} has an unparseable "
                f"last_verified {self.last_verified!r}; expected YYYY-MM-DD"
            ) from exc

    def is_stale(self, *, today: datetime | None = None, max_age_days: int = STALE_AFTER_DAYS) -> bool:
        """True when the label is absent, unparseable-free but old, or undated.

        An undated row counts as stale: the suite rule is that *every* GT row
        carries a last-verified date, so a missing date is a labeling gap to
        close rather than a row to trust silently.
        """
        now = today or datetime.now(UTC)
        verified = self.verified_at
        if verified is None:
            return True
        return now - verified > timedelta(days=max_age_days)


def _parse_verified(key: str, value: Any) -> str | None:
    """Validate a last-verified date eagerly, at load time.

    Validating here rather than lazily in ``verified_at`` means a typo in the GT
    file fails immediately with the offending row named, instead of crashing
    partway through a long benchmark run with a bare ValueError from deep inside
    the staleness pass.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise BenchmarkError(
            f"Ground-truth row {key!r} has a non-string last_verified: {value!r}"
        )
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise BenchmarkError(
            f"Ground-truth row {key!r} has last_verified {value!r}; expected YYYY-MM-DD"
        ) from exc
    return value


@dataclass
class GroundTruth:
    """A benchmark's hand-labelled cases, keyed by case id."""

    name: str
    rows: dict[str, GroundTruthRow] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, name: str, raw: dict[str, Any]) -> GroundTruth:
        rows: dict[str, GroundTruthRow] = {}
        for key, value in raw.items():
            if isinstance(value, dict):
                rows[key] = GroundTruthRow(
                    key=key,
                    label=value.get("label"),
                    last_verified=_parse_verified(key, value.get("last_verified")),
                    note=str(value.get("note", "")),
                    raw=value,
                )
            else:
                # Bare-list GT (the listicle entry's format) — no dates to carry.
                rows[key] = GroundTruthRow(key=key, label=value, raw=value)
        return cls(name=name, rows=rows)

    @classmethod
    def load(cls, name: str, path: Path) -> GroundTruth:
        if not path.exists():
            raise BenchmarkError(f"Ground truth file not found: {path}")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise BenchmarkError(f"Ground truth file {path} is not valid JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise BenchmarkError(f"Ground truth file {path} must contain a JSON object")
        return cls.from_dict(name, raw)

    def __len__(self) -> int:
        return len(self.rows)

    def get(self, key: str) -> GroundTruthRow | None:
        return self.rows.get(key)

    def cases(self) -> list[GroundTruthRow]:
        return list(self.rows.values())

    def staleness(self, *, today: datetime | None = None) -> list[str]:
        """Keys whose label has not been verified within ``STALE_AFTER_DAYS``."""
        now = today or datetime.now(UTC)
        stale: list[str] = []
        for row in self.rows.values():
            verified = row.verified_at
            if verified is None or now - verified > timedelta(days=STALE_AFTER_DAYS):
                stale.append(row.key)
        return stale


@dataclass
class RunContext:
    """Shared per-run state so no scorer has to know how a fetch was done."""

    benchmark: str
    http: Any = None
    llm_client: Any = None
    settings: Any = None
    notes: dict[str, Any] = field(default_factory=dict)

    def record(self, key: str, value: Any) -> None:
        self.notes[key] = value


@dataclass
class CaseResult:
    """One case's outcome. ``label``/``predicted`` stay untyped per entry."""

    key: str
    predicted: Any
    label: Any = None
    detail: dict[str, Any] = field(default_factory=dict)
    scored: bool = True
    unscored_reason: str = ""

    @property
    def correct(self) -> bool | None:
        if not self.scored or self.label is None:
            return None
        return bool(self.predicted == self.label)


@dataclass
class Report:
    """A benchmark report plus the provenance needed to trust it later."""

    benchmark: str
    ran_at: str
    per_case: dict[str, dict[str, Any]] = field(default_factory=dict)
    aggregate: dict[str, Any] = field(default_factory=dict)
    stale_labels: list[str] = field(default_factory=list)

    @staticmethod
    def new(benchmark: str) -> Report:
        return Report(benchmark=benchmark, ran_at=datetime.now(UTC).isoformat())

    def add(self, result: CaseResult) -> None:
        entry: dict[str, Any] = {
            "predicted": result.predicted,
            "scored": result.scored,
            "detail": result.detail,
        }
        if result.label is not None:
            entry["label"] = result.label
        if result.unscored_reason:
            entry["unscored_reason"] = result.unscored_reason
        if result.correct is not None:
            entry["correct"] = result.correct
        self.per_case[result.key] = entry

    def to_json(self) -> dict[str, Any]:
        return {
            "benchmark": self.benchmark,
            "ran_at": self.ran_at,
            "stale_labels": self.stale_labels,
            "aggregate": self.aggregate,
            "per_case": self.per_case,
        }


def write_report(path: Path, report: Report) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.to_json(), ensure_ascii=False, indent=1), encoding="utf-8")
