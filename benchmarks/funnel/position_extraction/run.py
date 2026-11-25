"""position_extraction runner: company fixture -> A2 -> A3 -> A4 outcomes, scored by score.py.

Usage (from repo root, with the shared venv):

    uv run python benchmarks/funnel/position_extraction/run.py [--limit N] [--out PATH]

Live network: every run fetches the labelled URLs. A1 (company -> careers URL
discovery) is not measured here; the labelled careers URL seeds the run and A1
is reported as unscored. Login walls and transport failures are unscored, not
failed, so they never move a rate.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
FUNNEL_DIR = HERE.parent
REPO_ROOT = FUNNEL_DIR.parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO_ROOT / "benchmarks" / "reach"))

from jobsearch_core.ingest import site_class  # noqa: E402
from jobsearch_core.ingest.http import PoliteClient  # noqa: E402
from jobsearch_core.ingest.scrapers.pagination import crawl_careers_pages  # noqa: E402
from jobsearch_core.ingest.scrapers.pipeline import (  # noqa: E402
    _classify_ats_url,
    _find_ats_board_links,
    scrape_company,
)
from jobsearch_core.ingest.scrapers.posting_check import (  # noqa: E402
    posting_obstacle as core_posting_obstacle,
)
from score import (  # noqa: E402
    FAIL,
    PASS,
    UNSCORED,
    CompanyResult,
    StageOutcome,
    summarize,
)
from score_reach import classify_text  # noqa: E402

FIXTURE = FUNNEL_DIR / "data" / "companies.json"
DEFAULT_OUT = HERE / "reports" / "position_extraction_report.json"

# Rough posting-count band for A3. Companies sit inside it or outside; the band
# is a sanity check, not a gate.
MIN_POSTINGS = 1


def _key(company: dict[str, Any]) -> str:
    return company["name"]


def _a2(company: dict[str, Any], client: PoliteClient) -> StageOutcome:
    """Careers page reaches the labelled open-positions URL (or its ATS link)."""
    label = company.get("open_positions_url")
    careers = company["careers_url"]
    if not label:
        # The careers page is itself the listing.
        return StageOutcome(PASS, "careers page is the listing", {"label": careers})
    try:
        careers_html = client.fetch(careers)
    except Exception as exc:  # noqa: BLE001 - transport is a data point
        return StageOutcome(UNSCORED, f"transport: {type(exc).__name__}: {exc}")
    # An ATS board counts only when reached through a link on the careers page
    # (master_plan §Employer Pages Over ATS Boards).
    board_links = {url.rstrip("/") for _, url in _find_ats_board_links(careers_html, careers)}
    label_key = label.rstrip("/")
    if _classify_ats_url(label):
        if label_key in board_links:
            return StageOutcome(PASS, "ATS board linked from careers page", {"label": label})
        return StageOutcome(FAIL, "ATS board not linked from careers page", {"label": label})
    if label_key == careers.rstrip("/"):
        return StageOutcome(PASS, "labelled listing is the careers page", {"label": label})
    # Employer-owned listing: reached if the careers page links to it.
    from urllib.parse import urljoin

    from bs4 import BeautifulSoup

    soup = BeautifulSoup(careers_html, "html.parser")
    hrefs = {urljoin(careers, a["href"]).rstrip("/") for a in soup.find_all("a", href=True)}
    if label_key in hrefs or label_key.split("?")[0] in {h.split("?")[0] for h in hrefs}:
        return StageOutcome(PASS, "listing linked from careers page", {"label": label})
    return StageOutcome(FAIL, "labelled listing not linked from careers page", {"label": label})


# Benchmark-only sample size: A3 scores the first N extracted postings per company
# so the report stays small and the rate is sample-based. Production is unchanged.
POSTING_SAMPLE_CAP = 20


def _posting_obstacle(job: Any, listing: str) -> str | None:
    """Why ``job`` is not a real posting, or None if it looks like one.

    The shape rule is core's (``posting_check``), shared with the entry
    resolver. The benchmark adds its own independent body-text check
    (``classify_text``) when a description was fetched.
    """
    obstacle = core_posting_obstacle(job, listing)
    if obstacle is not None:
        return obstacle
    if job.description:
        reachable, text_obstacle = classify_text(job.description)
        if not reachable:
            return text_obstacle
    return None


def _site_class(listing: str, client: PoliteClient) -> str:
    """Class the production gate picks for the listing URL (read-only observation).

    Mirrors ``pipeline._rendered_html_if_class_d``: the same classifier and
    render entry points, so the label is the class the scrape path dispatches on.
    Ambiguous or unknown profiles are reported as such, not forced to a class.
    """
    try:
        html = client.fetch(listing)
    except Exception:  # noqa: BLE001 - transport is reported by A2/A3, not here
        return "unreachable"
    try:
        profile = site_class.classify_with_render(
            listing,
            html,
            client.fetch_rendered,
            fetch_rendered_with_xhr=client.fetch_rendered_with_xhr,
        )
    except Exception:  # noqa: BLE001 - the gate falls back to the static HTML in production
        profile = site_class.classify_site(listing, html)
    if profile.resolved_class is None:
        return "ambiguous" if profile.ambiguous else "unknown"
    return profile.resolved_class


def _a3(company: dict[str, Any], client: PoliteClient) -> StageOutcome:
    """Open-positions page yields real postings: title, on-domain URL, not nav."""
    careers = company["careers_url"]
    listing = company.get("open_positions_url") or careers
    try:
        # Share the run's client: a second PoliteClient starts a second sync
        # Playwright in the same thread, which fails.
        jobs = scrape_company(
            _key(company), listing, client=client, verified=True, fetch_descriptions=False
        )
    except Exception as exc:  # noqa: BLE001
        return StageOutcome(UNSCORED, f"transport: {type(exc).__name__}: {exc}")
    if not jobs:
        return StageOutcome(FAIL, "no postings extracted", {"postings": 0, "posting_list": []})
    extracted_total = len(jobs)
    jobs = jobs[:POSTING_SAMPLE_CAP]
    obstacles: dict[str, int] = {}
    real = 0
    posting_list: list[dict[str, str]] = []
    for job in jobs:
        obstacle = _posting_obstacle(job, listing)
        if obstacle is None:
            real += 1
        else:
            obstacles[obstacle] = obstacles.get(obstacle, 0) + 1
        posting_list.append(
            {"title": job.title or "", "url": job.url or "", "location": job.location or "",
             "obstacle": obstacle or "real"}
        )
    detail = {
        "postings": len(jobs), "extracted_total": extracted_total, "real": real,
        "obstacles": obstacles, "posting_list": posting_list,
    }
    if real < MIN_POSTINGS:
        return StageOutcome(FAIL, "no real postings among extracted links", detail)
    return StageOutcome(PASS, "postings extracted", detail)


def _a4(company: dict[str, Any], client: PoliteClient) -> StageOutcome:
    """Pagination: the next page is reached, or a clean stop is reported."""
    start = company.get("open_positions_url") or company["careers_url"]
    try:
        result = crawl_careers_pages(
            start,
            lambda html, url: scrape_company(
                _key(company), url, client=client, verified=True, fetch_descriptions=False
            ),
            client=client,
            max_pages=3,
        )
    except Exception as exc:  # noqa: BLE001
        return StageOutcome(UNSCORED, f"transport: {type(exc).__name__}: {exc}")
    pages = result.pages_crawled
    reached_next = pages > 1
    detail = {"pages": pages, "stop_reason": result.stop_reason}
    if reached_next:
        return StageOutcome(PASS, "next page reached", detail)
    if result.stop_reason and result.stop_reason != "no_signal":
        return StageOutcome(PASS, f"clean stop: {result.stop_reason}", detail)
    return StageOutcome(FAIL, "no next page and no clean stop reported", detail)


def run(limit: int | None = None, only: set[str] | None = None) -> list[CompanyResult]:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))["companies"]
    companies = [c for k, c in fixture.items() if not only or k in only]
    if limit:
        companies = companies[:limit]
    client = PoliteClient()
    results: list[CompanyResult] = []
    try:
        for company in companies:
            res = CompanyResult(key=_key(company), stratum=company["stratum"])
            res.site_class = _site_class(company.get("open_positions_url") or company["careers_url"], client)
            res.outcomes["A2"] = _a2(company, client)
            res.outcomes["A3"] = _a3(company, client) if res.status("A2") == PASS else StageOutcome(FAIL, "A2 failed")
            res.outcomes["A4"] = _a4(company, client) if res.status("A3") == PASS else StageOutcome(FAIL, "A3 failed")
            results.append(res)
            print(f"{res.key} [{res.site_class}]: {[res.status(s) for s in ('A2', 'A3', 'A4')]}", flush=True)
    finally:
        client.close()
    return results


def _dump(results: list[CompanyResult]) -> None:
    """Print each company's extracted postings for a visual check against the site."""
    for res in results:
        a3 = res.outcomes.get("A3")
        postings = (a3.detail.get("posting_list") if a3 else None) or []
        print(f"\n=== {res.key} [{res.site_class}] A3={res.status('A3')} postings={len(postings)}")
        for p in postings:
            print(f"  [{p['obstacle']}] {p['title']} | {p['location']} | {p['url']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--only", type=lambda s: {k.strip() for k in s.split(",") if k.strip()}, default=None,
        help="comma-separated fixture keys, e.g. nvidia,apple,google,nordea,kone",
    )
    parser.add_argument("--dump", action="store_true", help="print each extracted posting")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    results = run(args.limit, args.only)
    if args.dump:
        _dump(results)
    report = {
        "summary": summarize(results),
        "companies": [
            {
                "key": r.key,
                "stratum": r.stratum,
                "site_class": r.site_class,
                "stages": {s: {"status": o.status, "reason": o.reason, **o.detail} for s, o in r.outcomes.items()},
            }
            for r in results
        ],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report["summary"]["all"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
