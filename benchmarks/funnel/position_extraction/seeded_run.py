"""position_extraction, seeded variant: scrape the labelled careers URL directly, no A2 gate.

Usage (from repo root, with the shared venv):

    uv run python benchmarks/funnel/position_extraction/seeded_run.py [--limit N] [--only a,b] [--out PATH]

``run.py`` skips A3/A4 when A2 fails, so a company whose open-positions label is
not linked from the careers page gets no extraction measurement. This runner
seeds the production scrape path with ``careers_url`` only and scores what comes
out, using the same posting rules as ``run.py`` (``_posting_obstacle``).

One crawl per company, not two: the pages are walked by ``crawl_careers_pages``
with a per-page scraper that does not paginate itself, so the page count is
bounded by ``MAX_PAGES`` and the wall-clock by ``COMPANY_BUDGET_S``. Postings
are counted over every crawled page; the stored sample is capped.

Live network. Transport failures are unscored, never failed.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import run as base  # noqa: E402
from jobsearch_core.ingest.http import PoliteClient  # noqa: E402
from jobsearch_core.ingest.scrapers.pagination import crawl_careers_pages  # noqa: E402
from jobsearch_core.ingest.scrapers.pipeline import (  # noqa: E402
    _rendered_html_if_class_d,
    _scrape_via_ats_links,
    _select_and_scrape,
)
from score import FAIL, PASS, STRATA, UNSCORED, rate  # noqa: E402

DEFAULT_OUT = HERE / "reports" / "seeded_extraction_report.json"
MAX_PAGES = 3  # hard page ceiling per company (Google and NVIDIA have thousands of jobs)
COMPANY_BUDGET_S = 120  # wall-clock budget per company, checked between pages
SAMPLE_CAP = base.POSTING_SAMPLE_CAP


class _OutOfBudgetError(Exception):
    pass


def _seeded(company: dict[str, Any], client: PoliteClient) -> dict[str, Any]:
    careers = company["careers_url"]
    deadline = time.monotonic() + COMPANY_BUDGET_S
    collected: list[Any] = []

    def scrape(html: str, url: str) -> list[Any]:
        if time.monotonic() > deadline:
            raise _OutOfBudgetError
        jobs = _select_and_scrape(url, html, client)
        collected.extend(jobs)
        return jobs

    out: dict[str, Any] = {"seed": careers}
    try:
        html = client.fetch(careers)
        html = _rendered_html_if_class_d(careers, html, client, None)
        try:
            crawl = crawl_careers_pages(
                careers, scrape, client=client, max_pages=MAX_PAGES, start_html=html
            )
            pages, stop = crawl.pages_crawled, crawl.stop_reason
        except _OutOfBudgetError:
            pages, stop = None, "time_budget"
        jobs = list(collected)
        via_ats = False
        if not jobs:
            jobs = _scrape_via_ats_links(careers, html, client)
            via_ats = bool(jobs)
    except Exception as exc:  # noqa: BLE001 - transport is a data point
        out.update(status=UNSCORED, reason=f"transport: {type(exc).__name__}: {exc}")
        return out

    # Dedupe across pages by URL, keep order.
    seen: set[str] = set()
    unique = []
    for job in jobs:
        key = (job.url or "").rstrip("/")
        if key not in seen:
            seen.add(key)
            unique.append(job)

    obstacles: dict[str, int] = {}
    real_total = 0
    sample: list[dict[str, str]] = []
    for job in unique:
        obstacle = base._posting_obstacle(job, careers)
        if obstacle is None:
            real_total += 1
        else:
            obstacles[obstacle] = obstacles.get(obstacle, 0) + 1
        if len(sample) < SAMPLE_CAP:
            sample.append(
                {"title": job.title or "", "url": job.url or "", "location": job.location or "",
                 "obstacle": obstacle or "real"}
            )
    out.update(
        extracted_total=len(unique),
        real=real_total,
        obstacles=obstacles,
        pages=pages,
        stop_reason=stop,
        via_ats_link=via_ats,
        posting_list=sample,
    )
    if not unique:
        out.update(status=FAIL, reason="no postings extracted")
    elif real_total < base.MIN_POSTINGS:
        out.update(status=FAIL, reason="no real postings among extracted links")
    else:
        out.update(status=PASS, reason="postings extracted")
    return out


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def block(sub: list[dict[str, Any]]) -> dict[str, Any]:
        scored = [r for r in sub if r["status"] in (PASS, FAIL)]
        passed = [r for r in scored if r["status"] == PASS]
        # Pagination is informational: a second page was reached, or a named stop.
        paged = [r for r in passed if r.get("pages")]
        multi = [r for r in paged if r["pages"] > 1]
        return {
            "companies": len(sub),
            "extraction_rate": rate(len(passed), len(scored)),
            "passed": len(passed),
            "scored": len(scored),
            "unscored": [r["key"] for r in sub if r["status"] == UNSCORED],
            "reached_page_2": len(multi),
            "hit_page_cap": sum(1 for r in sub if r.get("stop_reason") == "page_cap"),
            "hit_time_budget": sum(1 for r in sub if r.get("stop_reason") == "time_budget"),
        }

    return {
        "all": block(rows),
        "by_stratum": {s: block([r for r in rows if r["stratum"] == s]) for s in STRATA
                       if any(r["stratum"] == s for r in rows)},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--only", type=lambda s: {k.strip() for k in s.split(",") if k.strip()}, default=None)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    fixture = json.loads(base.FIXTURE.read_text(encoding="utf-8"))["companies"]
    companies = [c for k, c in fixture.items() if not args.only or k in args.only]
    if args.limit:
        companies = companies[: args.limit]

    client = PoliteClient()
    rows: list[dict[str, Any]] = []
    try:
        for company in companies:
            t0 = time.monotonic()
            row = {"key": base._key(company), "stratum": company["stratum"], **_seeded(company, client)}
            row["seconds"] = round(time.monotonic() - t0, 1)
            rows.append(row)
            print(
                f"{row['key']}: {row['status']} real={row.get('real')}/{row.get('extracted_total')} "
                f"pages={row.get('pages')} stop={row.get('stop_reason')} {row['seconds']}s",
                flush=True,
            )
    finally:
        client.close()

    report = {
        "config": {"max_pages": MAX_PAGES, "company_budget_s": COMPANY_BUDGET_S, "sample_cap": SAMPLE_CAP},
        "summary": _summary(rows),
        "companies": rows,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report["summary"]["all"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
