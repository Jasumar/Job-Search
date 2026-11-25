"""Runner for benchmark suite entry 3: posting reach rate.

THE USER RUNS THIS. The agent makes it work; the user runs it and reads the
numbers. (Suite convention, and the same rule the listicle entry follows.)

Usage
    uv run --extra dev python benchmarks/reach/run_benchmark.py
    uv run --extra dev python benchmarks/reach/run_benchmark.py --url https://...
    uv run --extra dev python benchmarks/reach/run_benchmark.py --db-audit

``--url`` probes an unlabelled URL and reports the outcome without touching the
labelled case set — the cheap way to check a hard case you just found before
committing a label to it.

What this measures and what it does not
---------------------------------------
The score is transport-only and the aggregate deliberately excludes the
``not_a_posting`` rows (suite convention: never invent a denominator to make a
site scoreable). Two numbers are reported side by side because they answer
different questions:

    reach rate          - of the URLs we try, how many give usable text?
    rendered rate       - how many needed the browser tier?

Rendered rate is the one that says whether Known Gap 7's Playwright escalation
is carrying its weight, and it is invisible in the reach rate: a Playwright
regression that costs us nothing shows up only here.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

BENCH_DIR = Path(__file__).resolve().parent
BENCHMARKS_DIR = BENCH_DIR.parent
# harness.py lives in benchmarks/, next to this entry's own directory, so the
# shared-seam import needs BENCHMARKS_DIR and not the repo root.
sys.path.insert(0, str(BENCH_DIR))
sys.path.insert(0, str(BENCHMARKS_DIR))

from harness import CaseResult, GroundTruth, Report, RunContext, write_report  # noqa: E402
from jobsearch_core.ingest.http import PoliteClient  # noqa: E402
from jobsearch_core.ingest.scrapers.detail import extract_detail_description, fetch_detail  # noqa: E402
from score_reach import (  # noqa: E402
    BLOCKED,
    REACHABLE,
    TOO_THIN,
    TRANSPORT_ERROR,
    UNSCORED_OBSTACLES,
    Outcome,
    classify_text,
    measure,
)

CASES_PATH = BENCH_DIR / "data" / "cases.json"
REPORT_PATH = BENCH_DIR / "data" / "last_report.json"


def run_cases(client: PoliteClient, gt: GroundTruth, *, only: set[str] | None = None) -> Report:
    ctx = RunContext(benchmark="reach_rate", http=client)
    report = Report.new("reach_rate")
    outcomes: list[Outcome] = []

    for row in gt.cases():
        if only and row.key not in only:
            continue
        # The harness carries the GT row verbatim in row.raw; row.label is only
        # the label value. url/note live on the row.
        fields = row.raw if isinstance(row.raw, dict) else {}
        url = fields.get("url")
        if not url:
            report.add(
                CaseResult(
                    key=row.key,
                    predicted=None,
                    label=row.label,
                    scored=False,
                    unscored_reason="case has no url field",
                )
            )
            continue
        outcome = measure(url, client)
        outcomes.append(outcome)

        label = fields.get("label")
        note = str(fields.get("note", ""))
        if label in UNSCORED_OBSTACLES:
            # Labelled by hand, deliberately not predicted by the scorer.
            # Reported so the row cannot silently rot, never scored.
            report.add(
                CaseResult(
                    key=row.key,
                    predicted=outcome.obstacle,
                    label=label,
                    scored=False,
                    unscored_reason="not_a_posting: transport cannot judge this",
                    # Carry labelled_obstacle through. Without it _aggregate
                    # cannot tell this row apart from any other unscored row and
                    # filed both not_a_posting cases under "other".
                    detail={**outcome.as_dict(), "labelled_obstacle": label, "note": note},
                )
            )
            continue

        scored = label in {REACHABLE, outcome.obstacle} or label is not None
        report.add(
            CaseResult(
                key=row.key,
                # Scored against the boolean question, but the obstacle string
                # is kept in the detail so a mislabelled obstacle is visible.
                predicted=outcome.obstacle == REACHABLE,
                label=(label == REACHABLE),
                scored=scored,
                unscored_reason="" if scored else "unlabelled",
                detail={**outcome.as_dict(), "labelled_obstacle": label, "note": note},
            )
        )

    report.stale_labels = gt.staleness()
    report.aggregate = _aggregate(report, ctx)
    return report


def _aggregate(report: Report, ctx: RunContext) -> dict[str, Any]:
    """Summarise from the per-case records, not from the raw outcomes.

    Scoring must follow the LABEL, never the prediction. An earlier version
    filtered on the predicted obstacle, which reported `reach rate 0.706
    (12/17)` on the live run by counting 9 unlabelled cases and 3 `not_a_posting`
    listing pages as if they were posting URLs -- two of those "reachable" rows
    were 483KB and 131KB of careers-page JSON theme config, which inflated the
    number with results that are not postings at all.

    The invariant: a case counts toward the rate only when a hand label says it
    is a posting URL whose reachability this entry is able to judge. Everything
    else is counted, and named, in the unscored bucket so a thin denominator is
    visible rather than silently padded.
    """
    cases = list(report.per_case.items())
    scored = [(k, c) for k, c in cases if c["scored"]]
    reachable = [k for k, c in scored if c["detail"].get("reachable")]
    rendered = [k for k, c in scored if c["detail"].get("tier") == "render"]
    by_obstacle: dict[str, int] = {}
    for _key, case in cases:
        name = str(case["detail"].get("obstacle", "unknown"))
        by_obstacle[name] = by_obstacle.get(name, 0) + 1
    unscored = {
        "unlabelled": [k for k, c in cases if c.get("unscored_reason") == "unlabelled"],
        "not_a_posting": [
            k for k, c in cases if c["detail"].get("labelled_obstacle") in UNSCORED_OBSTACLES
        ],
        "other": [
            k
            for k, c in cases
            if not c["scored"] and c.get("unscored_reason") not in (None, "", "unlabelled")
            and c["detail"].get("labelled_obstacle") not in UNSCORED_OBSTACLES
        ],
    }
    return {
        "cases": len(cases),
        "scored_cases": len(scored),
        "reachable": len(reachable),
        # None rather than a fabricated rate when nothing is scored: a 0/0
        # denominator has no honest numeric answer, and 0.0 would read as
        # "everything failed".
        "reach_rate": round(len(reachable) / len(scored), 3) if scored else None,
        "rendered": len(rendered),
        "rendered_rate": round(len(rendered) / len(scored), 3) if scored else None,
        "unscored_cases": len(cases) - len(scored),
        "unscored": unscored,
        "by_obstacle": by_obstacle,
        "llm_calls": 0,
        "cache_bypassed": True,
        "notes": ctx.notes,
    }


def print_report(report: Report) -> None:
    agg = report.aggregate
    print("\n=== posting reach rate (suite entry 3) ===")
    print(f"ran at {report.ran_at}")
    print(f"cases {agg['cases']}  scored {agg['scored_cases']}  unscored {agg['unscored_cases']}")
    print(f"reach rate    {agg['reach_rate']}  ({agg['reachable']}/{agg['scored_cases']})")
    print(f"rendered rate {agg['rendered_rate']}  ({agg['rendered']}/{agg['scored_cases']})")
    print(f"obstacles     {agg['by_obstacle']}")
    if report.stale_labels:
        print("\nstale labels (re-verify by hand):", report.stale_labels)
    print("\nper case:")
    for key, case in report.per_case.items():
        mark = " " if case["scored"] else "-"
        detail = case["detail"]
        note = f"  <- labelled {detail['labelled_obstacle']}" if detail.get("labelled_obstacle") else ""
        flag = "" if case.get("correct", True) else "   MISMATCH"
        print(f"  {mark} {key:24s} {detail.get('obstacle', '?'):16s} tier={detail.get('tier', '?'):7s} "
              f"len={detail.get('desc_len', 0):6d}{note}{flag}")
        if case.get("unscored_reason"):
            print(f"      ({case['unscored_reason']})")
    if agg["unscored_cases"]:
        print("\nnot counted in the rate (labelled by hand as out of scope, or awaiting a label):")
        for kind, keys in agg["unscored"].items():
            if keys:
                print(f"  {kind:16s} {', '.join(keys)}")
    print("\nTrend instrument, not a gate. Compare against the previous last_report.json in git.")


def probe_single(url: str) -> None:
    client = PoliteClient()
    outcome = measure(url, client)
    print(json.dumps(outcome.as_dict(), ensure_ascii=False, indent=1))


def run_agreement(urls: list[str], *, client: Any) -> list[dict[str, Any]]:
    """§4.1d item 5: run both instruments over the same URLs and compare.

    Production instrument: ``fetch_detail`` exactly as the scrape pipeline calls
    it (transport failures raise, and are reported as ``transport_error``).
    Standalone instrument: the pre-4.1d harness pair — ``client.fetch`` plus
    ``extract_detail_description`` with the exception caught — which is what
    position_extraction's first live run actually measured. Disagreement between the two is
    the drift detector the audit exists for.
    """
    rows: list[dict[str, Any]] = []
    for url in urls:
        # Production: let fetch_detail raise, classify the exception.
        try:
            text = fetch_detail(client, url)
            production = classify_text(text)[1] if text else TOO_THIN
        except Exception as exc:  # noqa: BLE001 - any failure is an outcome
            production = TRANSPORT_ERROR if isinstance(exc, (RuntimeError, ValueError)) else BLOCKED
        # Standalone: catch and extract separately (the old harness shape).
        try:
            html = client.fetch(url)
            standalone = classify_text(extract_detail_description(html) if html else None)[1]
        except Exception:
            standalone = TRANSPORT_ERROR
        rows.append(
            {
                "url": url,
                "production": production,
                "standalone": standalone,
                "agree": production == standalone,
            }
        )
    return rows


def derive_entries(db_path: str | Path) -> list[str]:
    """§4.1d item 2: entry set re-derived from the DB, not hand-maintained.

    The careers page is the unit the pipeline actually visits; the verified
    companies' careers URLs are the entry set. Read-only.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT careers_url FROM companies "
            "WHERE verification_status = 'verified' AND careers_url IS NOT NULL "
            "ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    return [url for (url,) in rows]

def audit_db(db_path: Path, *, limit: int = 0) -> dict[str, Any]:
    """Report stored descriptions that a posting URL should never have yielded.

    Detection and reporting only. This never writes to the database: repairing
    the rows is the user's call, and the repair path lives in the scrape
    pipeline, not in a benchmark tool.
    """
    import sqlite3

    from score_reach import REACHABLE, classify_text

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT id, url, description, raw_company, status FROM jobs "
            "WHERE description IS NOT NULL AND description != '' ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    suspects: list[dict[str, Any]] = []
    for job_id, url, desc, company, status in rows:
        # classify_text already reports binary_document for binary content, so a
        # separate _looks_binary reason here would double-count every hit.
        _, obstacle = classify_text(desc)
        reasons: list[str] = [obstacle] if obstacle != REACHABLE else []
        if reasons:
            suspects.append(
                {
                    "id": job_id,
                    "url": url,
                    "company": company,
                    "status": status,
                    "desc_len": len(desc),
                    "reasons": reasons,
                    "preview": desc[:80].replace("\n", " "),
                }
            )

    if limit:
        suspects = suspects[:limit]
    by_reason: dict[str, int] = {}
    for suspect in suspects:
        for reason in suspect["reasons"]:
            by_reason[reason] = by_reason.get(reason, 0) + 1
    return {"db": str(db_path), "rows_scanned": len(rows), "suspect_count": len(suspects),
            "by_reason": by_reason, "suspects": suspects}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", help="probe one unlabelled URL and exit")
    parser.add_argument(
        "--db-audit",
        action="store_true",
        help="report stored descriptions that no posting URL should have yielded",
    )
    parser.add_argument(
        "--agreement",
        action="store_true",
        help="run both instruments over the labelled cases and report agreement",
    )
    parser.add_argument(
        "--entries",
        action="store_true",
        help="derive the careers-page entry set from the DB's verified companies",
    )
    parser.add_argument("--db", type=Path, default=Path.cwd() / "data" / "jobs.db")
    parser.add_argument("--only", action="append", help="restrict to these case keys (repeatable)")
    parser.add_argument("--limit", type=int, default=0, help="cap reported audit rows")
    args = parser.parse_args(argv)

    if args.db_audit:
        result = audit_db(args.db, limit=args.limit)
        print("\n=== stored-description audit (read-only) ===")
        print(f"db {result['db']}")
        print(f"rows scanned {result['rows_scanned']}  suspects {result['suspect_count']}")
        print(f"by reason {result['by_reason']}")
        for suspect in result["suspects"]:
            print(f"  id={suspect['id']:4d} {suspect['reasons']} {suspect['desc_len']:7d}ch  {suspect['url']}")
            print(f"        {suspect['preview']}")
        print("\nNothing was modified. Repairing these rows is a separate, user-owned decision.")
        return 0

    if args.entries:
        entries = derive_entries(args.db)
        print(f"=== careers-page entry set ({len(entries)} verified companies) ===")
        for url in entries:
            print(f"  {url}")
        return 0

    if args.agreement:
        gt = GroundTruth.load("reach_rate", CASES_PATH)
        urls = [row.raw["url"] for row in gt.cases() if isinstance(row.raw, dict) and row.raw.get("url")]
        client = PoliteClient()
        rows = run_agreement(urls, client=client)
        agree = sum(1 for r in rows if r["agree"])
        print("\n=== instrument agreement (production path vs standalone pair) ===")
        print(f"cases {len(rows)}  agree {agree}  disagree {len(rows) - agree}")
        for row in rows:
            mark = "  " if row["agree"] else "!!"
            print(f"  {mark} {row['production']:16s} vs {row['standalone']:16s}  {row['url']}")
        print("\nDisagreement rows are findings: the two instruments drifted.")
        return 0

    if args.url:
        probe_single(args.url)
        return 0

    gt = GroundTruth.load("reach_rate", CASES_PATH)
    client = PoliteClient()
    report = run_cases(client, gt, only=set(args.only) if args.only else None)
    write_report(REPORT_PATH, report)
    print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
