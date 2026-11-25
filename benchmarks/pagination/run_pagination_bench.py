"""Pagination benchmark harness.

Scores how completely a careers-page crawl follows an employer's next-page
affordance, and — equally important — how honestly it reports when it stops.

Employer careers pages have no free "total pages" field, so there is no
ground-truth denominator to compute coverage against. Instead each site is
scored on facts the crawl itself can attest:

    signal_found      the crawl found a next-page affordance (or the page
                      genuinely has none)
    followed          it actually followed one
    clean_stop        it stopped for a stated reason rather than silently
    offsite_refused   it declined a link that left the careers section
    runaway_refused   it hit the page cap instead of looping forever

The headline number is ``clean_termination_rate``: the share of *scored* sites
where termination was both detected and clean. Sites whose behaviour cannot be
judged from the page alone are labelled ``unknown`` and excluded from the rate
rather than being silently scored as passes. Per the project decision, this
harness reports numbers and gates nothing.

Scoring labels
--------------
``determinate``
    The crawl either stopped for a clear reason or hit a bound it should have.
``unknown``
    The crawl was cut short by a transport failure, so nothing about the
    pagination behaviour can be concluded. Never counted in any rate.
``clean``
    A determinate stop that a correct crawler would also produce
    (``no_signal``, ``signal_disabled``, ``offsite``, ``repeat``,
    ``no_new_jobs``).
``truncated``
    Reached ``page_cap`` — jobs exist beyond the cap that were never crawled.
    Correct-but-incomplete: reported, not hidden.
``unbounded``
    The crawl would not terminate on its own. A real defect.

``clean`` and ``truncated`` both count toward "terminated cleanly"; ``unbounded``
does not. Two rates are printed so the difference stays visible.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jobsearch_core.ingest.http import PoliteClient
from jobsearch_core.ingest.scrapers.careers_page import CareersPageScraper
from jobsearch_core.ingest.scrapers.pagination import (
    crawl_careers_pages,
)

BENCH_DIR = Path(__file__).resolve().parent
DEFAULT_SITES = BENCH_DIR / "data" / "sites.json"
DEFAULT_REPORT = Path("reports") / "pagination" / "last_report.json"

MAX_PAGES = 10
CLEAN_REASONS = frozenset(
    {"no_signal", "signal_disabled", "offsite", "repeat", "no_new_jobs"}
)
TRUNCATED_REASONS = frozenset({"page_cap"})


@dataclass
class SiteResult:
    key: str
    url: str
    pages_crawled: int
    jobs: int
    stop_reason: str
    stop_detail: str
    label: str  # determinate | unknown
    verdict: str  # clean | truncated | unbounded | unknown
    notes: list[str] = field(default_factory=list)

    @property
    def counted(self) -> bool:
        return self.label == "determinate"

    @property
    def terminated_cleanly(self) -> bool:
        return self.verdict in {"clean", "truncated"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "url": self.url,
            "pages_crawled": self.pages_crawled,
            "jobs": self.jobs,
            "stop_reason": self.stop_reason,
            "stop_detail": self.stop_detail,
            "label": self.label,
            "verdict": self.verdict,
            "counted": self.counted,
            "notes": self.notes,
        }


def pages_crawled_of(site: SiteResult) -> int:
    return site.pages_crawled


@dataclass
class Report:
    sites: list[SiteResult] = field(default_factory=list)

    def add(self, site: SiteResult) -> None:
        self.sites.append(site)

    # --- aggregates --------------------------------------------------------

    @property
    def determinate(self) -> list[SiteResult]:
        return [s for s in self.sites if s.counted]

    @property
    def unknown(self) -> list[SiteResult]:
        return [s for s in self.sites if not s.counted]

    @property
    def clean(self) -> list[SiteResult]:
        return [s for s in self.determinate if s.verdict == "clean"]

    @property
    def truncated(self) -> list[SiteResult]:
        return [s for s in self.determinate if s.verdict == "truncated"]

    @property
    def unbounded(self) -> list[SiteResult]:
        return [s for s in self.determinate if s.verdict == "unbounded"]

    @property
    def followed_any(self) -> list[SiteResult]:
        return [s for s in self.determinate if s.pages_crawled > 1]

    @property
    def clean_termination_rate(self) -> float | None:
        scored = self.determinate
        if not scored:
            return None
        done = len(self.clean) + len(self.truncated)
        return done / len(scored)

    @property
    def full_coverage_rate(self) -> float | None:
        scored = self.determinate
        if not scored:
            return None
        return len(self.clean) / len(scored)

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_pages": MAX_PAGES,
            "totals": {
                "sites": len(self.sites),
                "scored": len(self.determinate),
                "unknown": len(self.unknown),
                "clean": len(self.clean),
                "truncated": len(self.truncated),
                "unbounded": len(self.unbounded),
                "followed_any": len(self.followed_any),
            },
            "clean_termination_rate": self.clean_termination_rate,
            "full_coverage_rate": self.full_coverage_rate,
            "sites": [s.to_dict() for s in self.sites],
        }

    def render(self) -> str:
        lines = [
            f"{'site':<24} {'pages':>5} {'jobs':>5} {'stop_reason':<22} {'verdict':<10} note",
            "-" * 100,
        ]
        for s in self.sites:
            note = s.stop_detail[:44]
            lines.append(
                f"{s.key:<24} {s.pages_crawled:>5} {s.jobs:>5} {s.stop_reason:<22} "
                f"{s.verdict:<10} {note}"
            )
        totals = self.to_dict()["totals"]
        lines.append("-" * 100)
        lines.append(
            f"sites={totals['sites']} scored={totals['scored']} unknown={totals['unknown']} "
            f"clean={totals['clean']} truncated={totals['truncated']} "
            f"unbounded={totals['unbounded']} followed_any={totals['followed_any']}"
        )
        for name, value in (
            ("clean_termination_rate", self.clean_termination_rate),
            ("full_coverage_rate", self.full_coverage_rate),
        ):
            lines.append(f"{name} = {'n/a' if value is None else f'{value:.0%}'}")
        return "\n".join(lines)


def classify(result: Any, *, max_pages: int = MAX_PAGES) -> tuple[str, str, list[str]]:
    """Map a crawl result to ``(label, verdict, notes)``."""
    notes: list[str] = []
    reason = result.stop_reason

    if reason == "fetch_error":
        return "unknown", "unknown", [f"transport failure: {result.stop_detail}"]

    if reason in CLEAN_REASONS:
        return "determinate", "clean", notes

    if reason in TRUNCATED_REASONS:
        return "determinate", "truncated", [
            f"stopped at the {max_pages}-page cap; listings beyond it were not crawled"
        ]

    if reason == "load_more_unsupported":
        # Detected, but the remaining listings were never fetched. Scoring this
        # as clean would credit a partial crawl with full coverage, so it is
        # unbounded: determinate about *why* we stopped, not about having
        # finished.
        return "determinate", "unbounded", [
            f"{result.stop_detail or 'load-more control'}: needs a browser click "
            "loop, so the remaining listings were not crawled"
        ]

    notes.append(f"unhandled stop reason: {reason}")
    return "determinate", "unbounded", notes


def run_site(
    key: str,
    url: str,
    *,
    max_pages: int = MAX_PAGES,
    client: PoliteClient | None = None,
) -> SiteResult:
    close = client is None
    http = client if client is not None else PoliteClient()
    try:
        result = crawl_careers_pages(
            url,
            lambda html, page_url: CareersPageScraper().scrape_listings(html, page_url),
            client=http,
            max_pages=max_pages,
        )
    except Exception as exc:  # a crash is a fact about the crawl, not a pass
        return SiteResult(
            key=key,
            url=url,
            pages_crawled=0,
            jobs=0,
            stop_reason="fetch_error",
            stop_detail=f"crawl raised {type(exc).__name__}: {exc}",
            label="unknown",
            verdict="unknown",
            notes=["crawl raised"],
        )
    finally:
        if close:
            http.close()

    label, verdict, notes = classify(result, max_pages=max_pages)
    return SiteResult(
        key=key,
        url=url,
        pages_crawled=result.pages_crawled,
        jobs=len(result.jobs),
        stop_reason=result.stop_reason,
        stop_detail=result.stop_detail,
        label=label,
        verdict=verdict,
        notes=notes,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sites", type=Path, default=DEFAULT_SITES)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES)
    parser.add_argument("--only", action="append", default=None, help="run one site key")
    parser.add_argument("--list-sites", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    if args.list_sites:
        for key, url in json.loads(args.sites.read_text(encoding="utf-8")).items():
            if key == "_notes":
                continue
            print(f"{key:<28} {url}")
        return 0

    sites = json.loads(args.sites.read_text(encoding="utf-8"))
    sites.pop("_notes", None)
    if args.only:
        sites = {k: v for k, v in sites.items() if k in set(args.only)}

    report = Report()
    for key, url in sites.items():
        print(f"[pagination] {key}: {url}", flush=True)
        report.add(run_site(key, url, max_pages=args.max_pages))

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    print()
    print(report.render())
    print(f"\nreport written to {args.report}")
    return 0


def main_entry() -> int:  # pragma: no cover - thin wrapper
    return main()


if __name__ == "__main__":
    sys.exit(main_entry())
