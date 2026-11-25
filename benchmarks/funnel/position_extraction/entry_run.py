"""Entry-resolution runner: careers_url -> resolved entry, scored against the label.

The funnel's ``run.py`` scores the *labelled* open-positions URL: A3 asks whether
the extractor reads a page someone already found. This runner measures the step
before it — the 4.0d descent (``entry_resolution.resolve_entry``) seeded with
``careers_url`` only, i.e. the question `careers_search` actually asks: given
the page discovery accepted, does the descent land on the listing?

Usage (from repo root, with the shared venv):

    uv run python benchmarks/funnel/position_extraction/entry_run.py [--limit N] [--only k1,k2]

Live network: every run fetches the labelled URLs and whatever the descent
chooses. Three outcomes per company:

- ``matched`` — the entry equals the fixture's labelled open-positions URL
  (path compared, trailing slash and query ignored);
- ``different`` — a page that yields postings, but not the labelled one (often a
  locale variant or a redirect target; not a failure by itself);
- ``miss`` — nothing within the bounds yields postings.

Each resolved row also carries ``real``: postings on the chosen entry that pass
the shared posting check (``posting_check``), the ES measure.

Transport failures are ``unscored``, never failed, so they never move a rate.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
FUNNEL_DIR = HERE.parent
REPO_ROOT = FUNNEL_DIR.parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO_ROOT / "benchmarks" / "reach"))

from jobsearch_core.ingest.http import PoliteClient  # noqa: E402
from jobsearch_core.ingest.scrapers.pipeline import (  # noqa: E402
    resolve_scraping_entry,
    scrape_company,
)
from jobsearch_core.ingest.scrapers.posting_check import posting_obstacle  # noqa: E402

FIXTURE = FUNNEL_DIR / "data" / "companies.json"
DEFAULT_OUT = HERE / "reports" / "entry_resolution_report.json"


def _norm(url: str) -> str:
    """Comparable form: host + path, no trailing slash, no query, no www."""
    parts = urlsplit(url)
    host = (parts.hostname or "").removeprefix("www.")
    path = parts.path.rstrip("/")
    return f"{host}{path}"


def _real_on(name: str, url: str, client: PoliteClient) -> int | None:
    """Postings on ``url`` that pass the shared posting check (the ES ``real`` column).

    Scored the same way for every run, whatever the resolver counted, so the
    ES0 baseline and later runs compare. ``None`` when the page cannot be read.
    """
    try:
        jobs = scrape_company(name, url, client=client, fetch_descriptions=False, verified=True)
    except Exception:  # noqa: BLE001 - the entry was already chosen; this is a measurement
        return None
    return sum(1 for job in jobs if posting_obstacle(job, url) is None)


def _one(company: dict[str, Any], client: PoliteClient) -> dict[str, Any]:
    careers = company["careers_url"]
    label = company.get("open_positions_url") or careers
    row: dict[str, Any] = {
        "key": company["name"],
        "stratum": company["stratum"],
        "country": company.get("country"),
        "careers_url": careers,
        "label": label,
    }
    try:
        html = client.fetch(careers)
    except Exception as exc:  # noqa: BLE001 - transport is a data point
        row.update(outcome="unscored", reason=f"transport: {type(exc).__name__}: {exc}")
        return row

    report = resolve_scraping_entry(careers, client=client, page_html=html)
    entry = report.resolution
    row["reason"] = report.reason
    row["entry"] = entry.url if entry else None
    row["hops"] = entry.hops if entry else None
    row["postings"] = entry.postings if entry else 0
    row["rendered"] = report.rendered
    row["sitemap"] = report.sitemap
    row["landing_raw"] = getattr(report, "landing_raw", None)
    row["tried"] = [
        {
            "url": tried.url,
            "score": tried.score,
            "outcome": tried.outcome,
            "raw": getattr(tried, "raw", None),
            "real": getattr(tried, "real", None),
        }
        for tried in report.tried
    ]
    row["candidates"] = [
        {"url": candidate.url, "score": candidate.score, "text": candidate.link_text}
        for candidate in report.candidates
    ]

    if entry is None:
        row["outcome"] = "miss"
        row["label_postings"] = None
        row["real"] = 0
        return row
    row["real"] = _real_on(company["name"], entry.url, client)
    if _norm(entry.url) == _norm(label):
        row["outcome"] = "matched"
        row["label_postings"] = entry.postings
        return row

    row["outcome"] = "different"
    # Quality, not just agreement: a "different" entry that lists as many jobs
    # as the hand label is not a worse answer, and one that lists fewer is the
    # failure this runner exists to see.
    try:
        labelled = scrape_company(
            company["name"], label, client=client, fetch_descriptions=False, verified=True
        )
        # Zero is not a comparable count: an ATS board the pipeline refuses to
        # scrape directly (the label of several rows) would otherwise make every
        # entry look better than the label.
        row["label_postings"] = len(labelled) or None
        if not labelled:
            row["label_note"] = "the labelled page yielded nothing to this pipeline"
    except Exception as exc:  # noqa: BLE001 - the label side is not the subject
        row["label_postings"] = None
        row["label_error"] = f"{type(exc).__name__}: {exc}"
    return row


def run(limit: int | None = None, only: set[str] | None = None) -> list[dict[str, Any]]:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))["companies"]
    companies = [c for k, c in fixture.items() if not only or k in only]
    if limit:
        companies = companies[:limit]
    client = PoliteClient()
    rows: list[dict[str, Any]] = []
    try:
        for company in companies:
            row = _one(company, client)
            rows.append(row)
            print(f"{row['key']:28} {row['outcome']:10} {row.get('entry') or row.get('reason')}",
                  flush=True)
    finally:
        client.close()
    return rows


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    scored = [row for row in rows if row["outcome"] != "unscored"]
    resolved = [row for row in scored if row["outcome"] in ("matched", "different")]
    counts = {name: sum(1 for row in scored if row["outcome"] == name)
              for name in ("matched", "different", "miss")}
    return {
        "companies": len(rows),
        "scored": len(scored),
        "unscored": len(rows) - len(scored),
        **counts,
        "resolved": counts["matched"] + counts["different"],
        "resolution_rate": round((counts["matched"] + counts["different"]) / len(scored), 3)
        if scored
        else None,
        "match_rate": round(counts["matched"] / len(scored), 3) if scored else None,
        "rendered_retries": sum(1 for row in rows if row.get("rendered")),
        "sitemaps_read": sum(1 for row in rows if row.get("sitemap")),
        # How often the resolved page lists at least as many postings as the
        # hand label: the descent's own quality bar, independent of agreement.
        "beats_or_matches_label": sum(
            1
            for row in resolved
            if row.get("label_postings") is not None
            and row["postings"] >= row["label_postings"]
        ),
        # ES: resolved entries that carry at least one real posting.
        "entries_with_real": sum(1 for row in resolved if (row.get("real") or 0) >= 1),
        "comparable": sum(1 for row in resolved if row.get("label_postings") is not None),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--only",
        type=lambda s: {k.strip() for k in s.split(",") if k.strip()},
        default=None,
        help="comma-separated fixture keys, e.g. nvidia,apple,google,nordea,kone",
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    rows = run(args.limit, args.only)
    summary = summarize(rows)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps({"summary": summary, "companies": rows}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
