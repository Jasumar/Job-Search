"""company_discovery B2 scorer: mention page -> company names, offline.

Reads ONLY the frozen snapshot pages (never live). Each page goes through the
production ``name_collect`` reference path, ``listing.name_reference``, which is
what decides whether a source has a list to replay. The discovery miner
(``mine_page_text``) is a different path and is not measured here.

Two recall measures per page:

- ``labelled`` — the fixture's ``expected`` company ids, found when one alias
  appears as a whole word in an extracted name. Subset labels: a page with a
  100-row ranking is checked on its 3-6 fixture companies only.
- ``full`` — only for pages whose complete name list was labelled in
  ``benchmarks/listicle/data/ground_truth.json`` (matched by URL through
  ``sites.json``). Uses the listicle matcher (one-to-one, legal-form guard).

Unscored pages (the snapshot fetch failed or the body is empty) are listed
beside the rates and never count as misses. An empty denominator gives None.
``extraction route`` is the artifact that carried the list; ``reason`` is the
reference note when no names came out.

Usage:
    uv run python benchmarks/funnel/company_discovery/score_b2.py [--snapshot 2026-10-09]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path

HERE = Path(__file__).resolve().parent
FUNNEL_DIR = HERE.parent
REPO_ROOT = FUNNEL_DIR.parent.parent
sys.path.insert(0, str(REPO_ROOT / "benchmarks" / "listicle"))

import run_benchmark  # noqa: E402
from jobsearch_core.ingest.listing import name_reference  # noqa: E402
from jobsearch_core.ingest.recipe_author import Artifact  # noqa: E402
from run_benchmark import match_names  # noqa: E402

try:
    from rapidfuzz import fuzz as _fuzz
except ImportError:
    _fuzz = None


def _ratio(a: str, b: str) -> float:
    """The listicle ratio, with the import tried once (it re-imports on every call)."""
    if _fuzz is not None:
        return float(_fuzz.ratio(a, b)) / 100.0
    return SequenceMatcher(None, a, b).ratio()


run_benchmark._ratio = _ratio

DATA_DIR = FUNNEL_DIR / "data"
SNAPSHOT_ROOT = DATA_DIR / "snapshots"
REPORT_PATH = HERE / "last_report_b2.json"
LISTICLE_DATA = REPO_ROOT / "benchmarks" / "listicle" / "data"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _alias_found(alias: str, names: list[str]) -> bool:
    pattern = re.compile(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", re.IGNORECASE)
    return any(pattern.search(name) for name in names)


def full_labels() -> dict[str, list[str]]:
    """Complete name lists by page URL, from the folded listicle fixture."""
    sites = _load(LISTICLE_DATA / "sites.json")
    truth = _load(LISTICLE_DATA / "ground_truth.json")
    return {url: truth[key] for key, url in sites.items() if key in truth}


def score_page(
    page: dict, html: str, aliases: dict[str, list[str]], full: list[str] | None
) -> dict:
    """One page's row. Pure: no I/O."""
    reference = name_reference([Artifact("static_html", "static", page["url"], html)])
    names = [job.title for job in reference.names]
    expected = page["expected"]
    found = [cid for cid in expected if any(_alias_found(a, names) for a in aliases[cid])]
    row: dict = {
        "url": page["url"],
        "family": page["family"],
        "extracted": len(names),
        "route": reference.carried_by,
        "reason": None if names else reference.note,
        "labelled_expected": len(expected),
        "labelled_found": len(found),
        "labelled_missed": [cid for cid in expected if cid not in found],
        "full_expected": None,
        "full_found": None,
        "sample": names[:8],
    }
    if full:
        matched = match_names(full, names)
        row["full_expected"] = len(full)
        row["full_found"] = len(matched["matched"])
    return row


def _rate(found: int, scored: int) -> float | None:
    return found / scored if scored else None


def run(snapshot: str) -> dict:
    snap_dir = SNAPSHOT_ROOT / snapshot
    manifest = _load(snap_dir / "manifest.json")
    status = {entry["url"]: entry for entry in manifest["pages"]}
    aliases = {
        cid: list(c["aliases"]) for cid, c in _load(DATA_DIR / "companies.json")["companies"].items()
    }
    full_by_url = full_labels()
    rows: list[dict] = []
    unscored: list[dict] = []
    for page in _load(DATA_DIR / "mention_pages.json")["pages"]:
        entry = status.get(page["url"])
        body = ""
        if entry and entry["status"] == "ok":
            body = (snap_dir / "pages" / f"{entry['key']}.html").read_text(encoding="utf-8")
        if not body.strip():
            unscored.append({"url": page["url"], "reason": "snapshot body missing or empty"})
            continue
        rows.append(score_page(page, body, aliases, full_by_url.get(page["url"])))
    labelled = sum(r["labelled_expected"] for r in rows)
    labelled_found = sum(r["labelled_found"] for r in rows)
    return {
        "snapshot": snapshot,
        "pages_scored": len(rows),
        "pages_with_names": sum(1 for r in rows if r["extracted"]),
        "labelled_recall": _rate(labelled_found, labelled),
        "labelled_found": labelled_found,
        "labelled_expected": labelled,
        "unscored": unscored,
        "rows": rows,
    }


def print_report(report: dict) -> None:
    print(f"B2 snapshot {report['snapshot']}: {report['pages_scored']} pages scored")
    for row in report["rows"]:
        full = (
            f"  full {row['full_found']}/{row['full_expected']}"
            if row["full_expected"] is not None
            else ""
        )
        print(
            f"  f{row['family']} {row['extracted']:>4} names  "
            f"labelled {row['labelled_found']}/{row['labelled_expected']}{full}  {row['url']}"
        )
        if row["reason"]:
            print(f"      reason: {row['reason']}")
    for item in report["unscored"]:
        print(f"  unscored: {item['url']} ({item['reason']})")
    rate = report["labelled_recall"]
    print(
        f"labelled recall: {report['labelled_found']}/{report['labelled_expected']}"
        + (f" ({rate:.0%})" if rate is not None else " (n/a)")
        + f"; pages with names {report['pages_with_names']}/{report['pages_scored']}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--snapshot", default="2026-10-09")
    args = parser.parse_args()
    report = run(args.snapshot)
    REPORT_PATH.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print_report(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
