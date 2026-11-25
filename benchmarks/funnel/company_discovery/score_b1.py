"""company_discovery1 scorer: does a discovery query surface its expected companies?

Reads ONLY the frozen snapshot (never live search). For each query in
``data/queries.json`` it checks the frozen top-N results for each expected
company, using the company's alias list.

Matching is a heuristic: a company is "found" when one of its aliases appears
as a whole word in a result's URL, title, or snippet. Short names ("Bolt",
"Apple") can false-match generic pages, which is why the per-query detail is
written to the report for review and is never a pass without a look.

Rates follow the suite convention: unscored cases (snapshot query empty or
errored) are excluded from the denominator and reported beside the rate; an
empty denominator returns None, not 0.0.

Usage:
    uv run python benchmarks/funnel/company_discovery/score_b1.py [--snapshot 2026-10-09]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

FUNNEL_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = FUNNEL_DIR / "data"
SNAPSHOT_ROOT = DATA_DIR / "snapshots"
REPORT_PATH = FUNNEL_DIR / "company_discovery" / "last_report_b1.json"

COMPANIES_FILE = DATA_DIR / "companies.json"
TOP_N = 10


def load_aliases() -> dict[str, list[str]]:
    """Alias list per company id, read from companies.json (single source)."""
    companies = json.loads(COMPANIES_FILE.read_text(encoding="utf-8"))["companies"]
    return {cid: list(c["aliases"]) for cid, c in companies.items()}


ALIASES: dict[str, list[str]] = load_aliases()


@dataclass
class Bucket:
    scored: int = 0
    found: int = 0
    unscored: int = 0

    @property
    def rate(self) -> float | None:
        return self.found / self.scored if self.scored else None


def _pattern(alias: str) -> re.Pattern[str]:
    return re.compile(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", re.IGNORECASE)


def found_in_results(results: list[dict], company_id: str) -> bool:
    pats = [_pattern(a) for a in ALIASES[company_id]]
    for r in results[:TOP_N]:
        blob = " ".join(str(r.get(k, "")) for k in ("url", "title", "snippet"))
        if any(p.search(blob) for p in pats):
            return True
    return False


def score(snapshot_dir: Path) -> dict:
    manifest = json.loads((snapshot_dir / "manifest.json").read_text(encoding="utf-8"))
    queries = json.loads((DATA_DIR / "queries.json").read_text(encoding="utf-8"))["queries"]
    meta_by_key = {f"q{i:02d}": m for i, m in enumerate(manifest["queries"], start=1)}
    rows = []
    buckets: dict[str, Bucket] = {}
    total = Bucket()
    for idx, q in enumerate(queries, start=1):
        key = f"q{idx:02d}"
        snap = json.loads((snapshot_dir / "queries" / f"{key}.json").read_text(encoding="utf-8"))
        ok = meta_by_key[key]["status"] == "ok"
        found: dict[str, bool | None] = {}
        fam = buckets.setdefault(str(q["family"]), Bucket())
        for c in q["expected"]:
            if not ok:
                found[c] = None
                fam.unscored += 1
                total.unscored += 1
                continue
            hit = found_in_results(snap["results"], c)
            found[c] = hit
            for b in (fam, total):
                b.scored += 1
                b.found += int(hit)
        rows.append({
            "key": key, "family": q["family"], "query": q["query"],
            "status": meta_by_key[key]["status"], "expected": q["expected"], "found": found,
        })
    return {
        "snapshot": snapshot_dir.name,
        "top_n": TOP_N,
        "by_family": {k: {"scored": b.scored, "found": b.found, "unscored": b.unscored,
                          "rate": b.rate} for k, b in sorted(buckets.items())},
        "total": {"scored": total.scored, "found": total.found, "unscored": total.unscored,
                  "rate": total.rate},
        "queries": rows,
    }


def _fmt(rate: float | None) -> str:
    return "n/a" if rate is None else f"{rate:.0%}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", default=None)
    args = ap.parse_args()
    snap_dir = SNAPSHOT_ROOT / args.snapshot if args.snapshot else max(SNAPSHOT_ROOT.iterdir())
    report = score(snap_dir)
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    for fam, b in report["by_family"].items():
        print(f"family {fam}: {b['found']}/{b['scored']} = {_fmt(b['rate'])}  (unscored {b['unscored']})")
    t = report["total"]
    print(f"total: {t['found']}/{t['scored']} = {_fmt(t['rate'])}  (unscored {t['unscored']})")
    print(f"report: {REPORT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
