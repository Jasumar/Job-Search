"""Recipe-authoring benchmark, run 1 (author). Stage B: open-positions page -> postings.

For each company in benchmarks/funnel/data/companies.json:
  1. Capture the open-positions URL: the plain PoliteClient fetch, the rendered
     DOM, every JSON/XHR response the rendered page received, and any JSON the
     existing scraper itself fetched (an ATS board, a Workday endpoint).
  2. Derive the reference from that capture (reference.py): the site's own JSON
     posting list, joined ids, or -- marked low confidence -- a job-shaped link
     cluster. The existing scraper becomes a comparison column, never the
     reference: where the scraper is wrong, a reference taken from it grades the
     recipe against the bug. Nothing is stored, so run 2 deriving its own
     reference measures drift instead of comparing against a frozen list.
  3. Call author_recipe (max 3 LLM trials) against an in-memory DB. The author
     sees the whole capture and chooses the kind itself.
  4. Write benchmarks/recipe_author/snapshots/<date>/<key>.json plus every raw
     artifact, so a replay reads the same source.

A capture whose payloads hold no posting list is recorded as ``capture_gap``: no
reference exists to grade against, and that is a fact about the capture, not a
recipe failure. The user eyeballs the report.

Every company is rendered: parity between the artifacts is the point of the run,
and skipping the render would pre-decide that the static HTML is enough.

Run from the repo root:
    uv run python benchmarks/recipe_author/run_author.py            # all companies
    uv run python benchmarks/recipe_author/run_author.py apple nvidia
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import traceback
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from jobsearch_core.config import Settings
from jobsearch_core.db.models import Base, Company, CompanyRecipe
from jobsearch_core.ingest.http import PoliteClient
from jobsearch_core.ingest.recipe_author import (
    Artifact,
    AuthorInput,
    FetchMode,
    author_recipe,
    script_artifacts,
)
from jobsearch_core.ingest.scrapers.base import ScrapedJob
from jobsearch_core.ingest.scrapers.pipeline import _classify_ats_url, _scrape_ats_board, _select_and_scrape
from jobsearch_core.llm.client import LLMClient
from reference import Reference, build_reference  # bench-local, sits beside this file
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

ROOT = Path(__file__).resolve().parents[2]
COMPANIES = ROOT / "benchmarks" / "funnel" / "data" / "companies.json"
BENCH = Path(__file__).resolve().parent
SNAP_ROOT = BENCH / "snapshots"
SAMPLE = 10


class RecordingClient(PoliteClient):
    """PoliteClient that logs every plain GET made through fetch()."""

    def __init__(self) -> None:
        super().__init__()
        self.log: list[tuple[str, str]] = []

    def fetch(self, url: str, **kwargs):  # type: ignore[override]
        body = super().fetch(url, **kwargs)
        self.log.append((url, body))
        return body


def _is_json(text: str) -> bool:
    return text.lstrip()[:1] in ("{", "[")


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


@dataclass
class Capture:
    """Everything one page load produced: bodies, payloads, and what happened."""

    artifacts: list[Artifact]
    payloads: list[tuple[str, str, FetchMode]]
    notes: dict
    static: str
    rendered: str | None


def capture_page(page_url: str, client: RecordingClient) -> Capture:
    """Load the page three ways: plain GET, rendered DOM, and the JSON it fetched."""
    notes: dict = {"page_url": page_url}
    client.log.clear()
    static = client.fetch(page_url)
    notes["plain_bytes"] = len(static)
    artifacts: list[Artifact] = [Artifact("static_html", "static", page_url, static)]

    rendered: str | None = None
    captures = []
    try:
        try:
            dom, captures = client.fetch_rendered_with_xhr(page_url)
        except Exception as first:  # noqa: BLE001 — one relaunch usually clears it
            notes["render_retry"] = str(first)[:200]
            client.reset_browser()
            dom, captures = client.fetch_rendered_with_xhr(page_url)
        rendered = dom
        notes["rendered_bytes"] = len(dom)
        artifacts.append(Artifact("rendered_html", "rendered", page_url, dom))
    except Exception as exc:  # noqa: BLE001 — a failed render still leaves the static page
        notes["render_error"] = str(exc)[:200]

    payloads: list[tuple[str, str, FetchMode]] = []
    for capture in captures:
        if capture.status >= 400 or not capture.response_body or not _is_json(capture.response_body):
            continue
        payloads.append((capture.url, capture.response_body, "xhr"))
    notes["platform"] = _classify_ats_url(page_url)
    return Capture(artifacts, payloads, notes, static, rendered)


def collect_fetched_payloads(cap: Capture, client: RecordingClient, page_url: str, *, mode: FetchMode) -> None:
    """Add the JSON the scraper itself fetched (an ATS board, a hydrate endpoint).

    Those bodies are the site's own data, so they belong in the capture and in the
    reference; they are not the scraper's reading of it.
    """
    known = {url for url, _, _ in cap.payloads}
    for url, body in client.log:
        if url == page_url or url in known or not _is_json(body):
            continue
        known.add(url)
        cap.payloads.append((url, body, mode))
    # Stable names: XHR responses arrive in whatever order the page fires them, so
    # json_N is assigned by URL, not by arrival.
    cap.payloads.sort(key=lambda item: (item[2], item[0]))
    cap.artifacts = [a for a in cap.artifacts if a.fetch_mode in ("static", "rendered")]
    for number, (url, body, fetch_mode) in enumerate(cap.payloads, start=1):
        cap.artifacts.append(Artifact(f"json_{number}", fetch_mode, url, body))


def scraper_column(page_url: str, cap: Capture, client: RecordingClient) -> tuple[list[ScrapedJob], dict]:
    """What the existing scraper path reads off this capture, as a comparison column.

    It is no longer the reference: where the scraper is wrong (Google 2026-10-25:
    26 navigation links) a reference taken from it grades recipes against the bug.
    """
    platform = cap.notes.get("platform")
    path = f"ats:{platform}" if platform else "scraper"
    try:
        if platform:
            jobs = _scrape_ats_board(platform, page_url, cap.static, client)
        else:
            jobs = _select_and_scrape(page_url, cap.static, client)
        source = "static_html"
        if not jobs and cap.rendered is not None:
            if platform:
                jobs = _scrape_ats_board(platform, page_url, cap.rendered, client)
            else:
                jobs = _select_and_scrape(page_url, cap.rendered, client)
            source = "rendered_html"
    except Exception as exc:  # noqa: BLE001 — a broken scraper must not stop the run
        return [], {"path": path, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    return jobs, {"path": path, "source": source, "count": len(jobs)}


def build_input(
    company_id: int,
    company_name: str,
    page_url: str,
    cap: Capture,
    ref: Reference,
    client: RecordingClient,
) -> AuthorInput:
    """AuthorInput for one company, built from the capture and its derived reference."""
    jobs = [ScrapedJob(url=p.url, title=p.title, company=company_name) for p in ref.postings]
    carried_by = ref.carried_by or "static_html"
    if carried_by not in {artifact.name for artifact in cap.artifacts}:
        carried_by = cap.artifacts[0].name
    artifacts = [
        replace(a, reference=tuple(jobs)) if a.name == carried_by else a for a in cap.artifacts
    ]

    # Postings can sit on more than one host (an ATS board reached through the
    # careers site, a mirror). The pattern must cover every reference host, or the
    # reference fails its own pattern and no authoring run happens at all.
    origins = sorted({f"{urlparse(p.url).scheme}://{urlparse(p.url).netloc}" for p in ref.postings})
    url_pattern = "^(?:" + "|".join(re.escape(o + "/") for o in origins) + ").+"
    allowed = sorted(
        {_host(page_url)} | {_host(p.url) for p in ref.postings} | {_host(u) for u, _, _ in cap.payloads}
    )
    return AuthorInput(
        company_id=company_id,
        allowed_hosts=allowed,
        url_pattern=url_pattern,
        page_url=page_url,
        reference=jobs,
        artifacts=artifacts,
        carried_by=carried_by,
        # The author's api replays fetch JSON: a rendered body is never a valid
        # answer to a JSON request, and fetch_json says so with the tier named.
        fetch=client.fetch_json,
    )


def job_row(job: ScrapedJob) -> dict:
    return {"title": job.title, "url": job.url, "location": job.location}


def _write_atomic(path: Path, text: str) -> None:
    """Write via a temp file and rename, so an interrupted run leaves no half file.

    A truncated snapshot is unreadable JSON and the company's whole capture is
    lost; a rename either lands or does not (2026-11-02).
    """
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _artifact_ext(fetch_mode: str) -> str:
    return "html" if fetch_mode in ("static", "rendered") else "json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("keys", nargs="*")
    parser.add_argument("--model", default=None)
    args = parser.parse_args()

    companies = json.loads(COMPANIES.read_text())["companies"]
    keys = args.keys or list(companies)
    unknown = [k for k in keys if k not in companies]
    if unknown:
        print(f"unknown company keys: {unknown}", file=sys.stderr)
        return 2

    settings = Settings()
    if not settings.openrouter_api_key:
        print("OPENROUTER_API_KEY is not set", file=sys.stderr)
        return 2
    model = args.model or settings.analysis_model
    llm = LLMClient(
        api_key=settings.openrouter_api_key,
        model=model,
        base_url=settings.llm_base_url,
        min_interval_seconds=settings.llm_min_interval_seconds,
        max_retries=settings.llm_max_retries,
    )

    run_date = date.today().isoformat()
    out_dir = SNAP_ROOT / run_date
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"run date {run_date}, model {model}, companies {len(keys)}")

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session: Session = sessionmaker(bind=engine)()
    client = RecordingClient()

    for key in keys:
        entry = companies[key]
        page_url = entry["open_positions_url"]
        snap: dict = {
            "key": key,
            "name": entry["name"],
            "stratum": entry["stratum"],
            "family": entry["family"],
            "open_positions_kind_label": entry["open_positions_kind"],
            "run_date": run_date,
            "model": model,
            "status": None,
        }
        print(f"\n== {key}: {page_url}")
        try:
            company = Company(name=entry["name"], careers_url=entry.get("careers_url"))
            session.add(company)
            session.flush()
            cap = capture_page(page_url, client)
            notes = cap.notes
            snap["notes"] = notes
            scraper_jobs, scraper_notes = scraper_column(page_url, cap, client)
            collect_fetched_payloads(
                cap,
                client,
                page_url,
                mode="ats_board" if notes.get("platform") else "xhr",
            )
            # Script bodies stay cut, except the page's JSON-LD, which is handed
            # over as its own artifact (evidence only — never an api source).
            # Added after collect_fetched_payloads, which rebuilds the artifact
            # list from the fetched payloads alone.
            cap.artifacts.extend(script_artifacts(page_url, cap.rendered or cap.static))
            ref = build_reference(cap.artifacts)
            snap["reference"] = {
                "count": ref.count,
                "method": ref.method,
                "confidence": ref.confidence,
                "carried_by": ref.carried_by,
                "by_artifact": ref.by_artifact,
                "notes": list(ref.notes),
                "sample": ref.sample(SAMPLE),
                # Stored in full so run 2 can diff two derived references; it is a
                # record of this capture, not an expectation for the next one.
                "urls": sorted(ref.urls()),
            }
            snap["artifacts"] = []
            for artifact in cap.artifacts:
                fname = f"{key}.{artifact.name}.{_artifact_ext(artifact.fetch_mode)}"
                _write_atomic(out_dir / fname, artifact.source)
                snap["artifacts"].append(
                    {
                        "name": artifact.name,
                        "fetch_mode": artifact.fetch_mode,
                        "url": artifact.url,
                        "bytes": len(artifact.source),
                        "file": fname,
                    }
                )
            ref_urls = ref.urls()
            scraper_urls = {job.url for job in scraper_jobs}
            snap["scraper"] = {
                **scraper_notes,
                "in_reference": len(scraper_urls & ref_urls),
                "outside_reference": len(scraper_urls - ref_urls),
            }
            print(
                f"  reference={ref.count} ({ref.method}/{ref.confidence}) "
                f"carried_by={ref.carried_by} artifacts={len(cap.artifacts)} "
                f"scraper={scraper_notes.get('count')}"
            )
            if ref.count == 0:
                snap["status"] = "capture_gap"
                print("  capture gap: no posting list in the capture, nothing to grade")
            else:
                inp = build_input(company.id, entry["name"], page_url, cap, ref, client)
                result = author_recipe(session, company, inp, llm)
                session.commit()
                snap["status"] = result.status
                snap["recipe_state"] = result.recipe_state
                snap["kind"] = result.kind
                snap["source_artifact"] = result.source_artifact
                snap["trials"] = [
                    {"number": t.number, "passed": t.passed, "reason": t.reason, "body": t.body}
                    for t in result.trials
                ]
                if result.recipe_id is not None:
                    row = session.get(CompanyRecipe, result.recipe_id)
                    if row is not None:
                        snap["recipe_body"] = json.loads(row.body)
                print(f"  status={result.status} kind={result.kind} trials={len(result.trials)}")
        except Exception as exc:  # noqa: BLE001 — record and continue
            session.rollback()
            snap["status"] = "error"
            snap["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
            snap["traceback_tail"] = traceback.format_exc()[-800:]
            print(f"  error: {snap['error']}")
        _write_atomic(out_dir / f"{key}.json", json.dumps(snap, indent=2, ensure_ascii=False))

    print(f"\nsnapshots written to {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
