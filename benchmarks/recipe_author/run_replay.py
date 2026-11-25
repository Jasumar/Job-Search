"""Recipe-authoring benchmark, run 2 (replay).

Replays the recipe stored by run 1 on a *fresh capture* of the same open-positions
URL, and grades it against the reference derived from that same fresh capture.
Nothing is frozen: a stored URL list would rot the day a company posts a job, so
run 2 derives its own reference and reports day-1 -> day-2 movement as drift.

R3 adds three things on top of the grade, all outside the verification gate:
per-recipe statistics (routers, never judges), first-page-to-rendered-DOM
agreement, and the posting-existence gate. The gate is one model call per
source per event (first accepted replay, a firing statistic, or a cleared stop),
not per run: ``--no-gate`` replays with zero LLM calls.

    uv run python benchmarks/recipe_author/run_replay.py 2026-10-25         # replay run 1, today's capture
    uv run python benchmarks/recipe_author/run_replay.py 2026-10-25 apple   # one company
    uv run python benchmarks/recipe_author/run_replay.py 2026-10-25 --no-gate
    uv run python benchmarks/recipe_author/run_replay.py 2026-10-25 --db data/jobsearch.db
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from jobsearch_core.config import Settings
from jobsearch_core.db.recipe_flags import record_flag
from jobsearch_core.db.session import get_engine, init_db
from jobsearch_core.ingest.recipe import (
    ApiRecipe,
    DomRecipe,
    RecipeError,
    parse_body,
    run_api_recipe_report,
    run_dom_recipe,
    verify_postings,
)
from jobsearch_core.ingest.recipe_verify import (
    GATE_SAMPLE_LIMIT,
    NAVIGATION_FAILURE,
    GateError,
    body_hash,
    classify_empty_replay,
    collect_statistics,
    dom_agreement,
    run_posting_gate,
    should_run_gate,
    signals,
    stop_decision,
    structural_empty,
)
from jobsearch_core.llm.client import LLMClient
from reference import build_reference  # type: ignore[import-not-found]
from run_author import (  # type: ignore[import-not-found]
    SAMPLE,
    SNAP_ROOT,
    Capture,
    RecordingClient,
    capture_page,
    collect_fetched_payloads,
    job_row,
)
from sqlalchemy.orm import Session


def _write_atomic(path: Path, text: str) -> None:
    """Write via a temp file and rename, so an interrupted run leaves no half file."""
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _captured_fetch(capture: Capture, client: RecordingClient):
    """Fetch for api replays: the captured body when we have it, else a live fetch."""
    by_url = {a.url: a.source for a in capture.artifacts if a.fetch_mode in ("xhr", "ats_board")}

    def fetch(url: str) -> str:
        body = by_url.get(url)
        if body is None:
            return client.fetch_json(url)
        json.loads(body)  # same contract as fetch_json: only JSON text goes through
        return body

    return fetch


def replay_one(
    snap: dict,
    client: RecordingClient,
    *,
    llm: LLMClient | None = None,
    prior_none: bool = False,
    prior_flagged: bool = False,
    session: Session | None = None,
) -> dict:
    url = snap["notes"]["page_url"]
    out: dict = {
        "key": snap["key"],
        "author_run_date": snap["run_date"],
        "replay_run_date": date.today().isoformat(),
        "page_url": url,
        "stored_kind": snap["recipe_body"]["kind"],
        "status": None,
    }
    body = snap["recipe_body"]
    recipe = parse_body(body, body["kind"])
    # The capture the author worked from decides which artifact a dom recipe reads.
    source_artifact = snap.get("source_artifact")
    artifact = next((a for a in snap.get("artifacts", []) if a["name"] == source_artifact), None)
    rendered_first = bool(artifact and artifact["fetch_mode"] == "rendered")
    out["source_artifact"] = source_artifact
    out["source_fetch_mode"] = artifact["fetch_mode"] if artifact else None

    capture = capture_page(url, client)
    collect_fetched_payloads(capture, client, url, mode="xhr")
    ref = build_reference(capture.artifacts)
    out["reference"] = {
        "count": ref.count,
        "method": ref.method,
        "confidence": ref.confidence,
        "carried_by": ref.carried_by,
        "notes": list(ref.notes),
    }
    if ref.count == 0:
        out["status"] = "capture_gap"
        return out

    api_report = None
    html_used: str | None = capture.static
    try:
        if isinstance(recipe, ApiRecipe):
            api_report = run_api_recipe_report(recipe, _captured_fetch(capture, client), url)
            postings = api_report.jobs
        else:
            # run_dom_recipe raises when a document lacks the container, so an
            # empty match on one document is the signal to try the other; the
            # first failure is kept in case neither document matches.
            order = [capture.rendered, capture.static] if rendered_first else [capture.static, capture.rendered]
            first_error: RecipeError | None = None
            for html in [h for h in order if h is not None]:
                try:
                    postings = run_dom_recipe(recipe, html, url)
                except RecipeError as exc:
                    first_error = first_error or exc
                    continue
                html_used = html
                out["used_render"] = html is capture.rendered
                break
            else:
                if first_error is not None:
                    raise first_error
                postings = []
    except RecipeError as exc:
        out["status"] = "replay_error"
        out["error"] = str(exc)[:300]
        return out
    except Exception as exc:  # noqa: BLE001
        out["status"] = "replay_error"
        out["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        return out

    out["body_sha256"] = body_hash(body)
    out["count"] = len(postings)
    out["sample"] = [job_row(j) for j in postings[:SAMPLE]]
    # R3: first-page-to-rendered-DOM agreement, on every replay that produced
    # postings or nothing. An empty replay is not automatically a failure.
    agreement = dom_agreement(postings, capture.rendered, url)
    if agreement is not None:
        out["dom_agreement"] = {
            "replay": agreement.replay_count,
            "rendered": agreement.rendered_count,
            "matched": agreement.matched,
            "missing": len(agreement.missing),
            "extra": len(agreement.extra),
            "agrees": agreement.agrees,
        }
    if not postings:
        # Open question 6, resolved: the rendered DOM says whether the board is
        # closed or the recipe failed to navigate. Only the latter stops.
        out["status"] = classify_empty_replay(agreement)
        out["stop"] = out["status"] == NAVIGATION_FAILURE
        return out

    ref_urls = ref.urls()
    got = {job.url for job in postings}
    check = verify_postings(postings, recipe.verification, reference_urls=ref_urls)
    # Signals, not gates: the replay only has to be non-empty. Navigation links,
    # button-label titles and count drift are reported for scoring (owner,
    # 2026-10-19).
    out["verify_on_fresh_capture"] = {
        "ok": check.ok,
        "reason": check.reason,
        "count": check.count,
        "off_pattern": check.off_pattern,
        "missing_titles": list(check.missing_titles),
        "outside_reference": check.outside_reference,
    }
    out["vs_fresh_reference"] = {
        "reference": len(ref_urls),
        "matched": len(got & ref_urls),
        "unmatched": len(got - ref_urls),
        "missing": len(ref_urls - got),
    }
    out["status"] = "ok"

    # R3 statistics: routers that say where to look next, never a verdict.
    stats = collect_statistics(
        postings,
        page_counts=api_report.page_counts if api_report else (),
        detail_attempted=api_report.detail_attempted if api_report else 0,
        detail_succeeded=api_report.detail_succeeded if api_report else 0,
    )
    out["statistics"] = {
        "count": stats.count,
        "fill": {
            name: stats.fill_rate(name)
            for name in ("location", "posted_date", "employment_type", "seniority", "description")
        },
        "duplicate_urls": stats.duplicate_urls,
        "duplicate_titles": stats.duplicate_titles,
        "page_counts": list(stats.page_counts),
        "page_advance": stats.page_advance,
        "detail": [stats.detail_succeeded, stats.detail_attempted],
        # Why the per-item requests failed, when they did: a "detail 3/12"
        # without a cause cannot be acted on after the run (2026-11-02).
        "detail_error": api_report.detail_error if api_report else None,
    }
    fired = signals(recipe, stats)
    out["signals"] = [
        {"name": signal.name, "value": signal.value, "points_at": signal.points_at}
        for signal in fired
    ]

    # R3 posting-existence gate. One call per source per event, not per run: the
    # first accepted replay for a body, or a statistic that fired after an
    # earlier answer existed. The stop additionally needs an agreement, so a
    # single "none" only flags.
    gate_event = should_run_gate(
        first_accepted=not prior_flagged,
        signal_fired=bool(fired),
        stop_cleared=False,  # clearing a stop is an author action (R5)
    )
    out["gate_event"] = gate_event
    if llm is not None and gate_event:
        try:
            verdict = run_posting_gate(
                recipe,
                postings,
                url,
                llm,
                # The rendered page is the best DOM evidence even when the replay
                # read the static one: it is what a browser would have shown.
                dom_html=(capture.rendered or html_used) if isinstance(recipe, DomRecipe) else None,
                limit=GATE_SAMPLE_LIMIT,
            )
        except GateError as exc:
            # The gate fails open: an unusable answer never stops a replay.
            out["gate"] = {"verdict": "error", "error": str(exc)[:300]}
        else:
            stopped = stop_decision(
                verdict=verdict.verdict,
                structural_empty=structural_empty(postings),
                prior_none=prior_none,
            )
            out["gate"] = {
                "verdict": verdict.verdict,
                "model_id": verdict.model_id,
                "items": [{"url": i.url, "reason": i.reason} for i in verdict.items],
                "stopped": stopped,
            }
            out["stop"] = stopped
            if session is not None:
                record_flag(
                    session,
                    body_sha256=out["body_sha256"],
                    verdict=verdict.verdict,
                    analysis=verdict.analysis(),
                    stopped=stopped,
                    model_id=verdict.model_id,
                    page_url=url,
                )

    # Drift compares two *derived* references: what run 1's capture held versus what
    # today's capture holds. It is a fact about the site, not a failure of the recipe.
    run1_urls = set(snap.get("reference", {}).get("urls", [])) or {
        row["url"] for row in snap.get("reference", {}).get("sample", []) if row.get("url")
    }
    if run1_urls:
        out["reference_drift"] = {
            "run1": len(run1_urls),
            "today": len(ref_urls),
            "gone": sorted(run1_urls - ref_urls)[:SAMPLE],
            "new": sorted(ref_urls - run1_urls)[:SAMPLE],
            "gone_count": len(run1_urls - ref_urls),
            "new_count": len(ref_urls - run1_urls),
        }
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("author_date")
    parser.add_argument("keys", nargs="*")
    parser.add_argument(
        "--no-gate",
        action="store_true",
        help="skip the posting-existence gate (run 2 then makes zero LLM calls)",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="SQLite path to store gate flags in (omitted: the flag stays in the replay file)",
    )
    args = parser.parse_args()

    src = SNAP_ROOT / args.author_date
    if not src.is_dir():
        print(f"no snapshots at {src}", file=sys.stderr)
        return 2
    run_date = date.today().isoformat()
    out_dir = SNAP_ROOT / run_date
    out_dir.mkdir(parents=True, exist_ok=True)
    client = RecordingClient()

    llm = None
    if not args.no_gate:
        settings = Settings()
        if not settings.openrouter_api_key:
            print("OPENROUTER_API_KEY is not set (use --no-gate to replay without the gate)", file=sys.stderr)
            return 2
        llm = LLMClient(
            api_key=settings.openrouter_api_key,
            model=settings.analysis_model,
            base_url=settings.llm_base_url,
            min_interval_seconds=settings.llm_min_interval_seconds,
            max_retries=settings.llm_max_retries,
        )
        print(f"gate model {llm.model}")

    session = None
    if args.db:
        # The flags table is part of the schema, so the DB must already be migrated.
        init_db(f"sqlite:///{Path(args.db).expanduser()}")
        session = Session(get_engine())

    # A repeat "none" for the same body is the independent agreement a stop needs;
    # any prior answer for the body means the gate already had its first say.
    prior_none: dict[str, bool] = {}
    prior_flagged: set[str] = set()
    for earlier in sorted(SNAP_ROOT.glob("*/*.replay.json")):
        try:
            row = json.loads(earlier.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        digest = row.get("body_sha256")
        if not digest:
            continue
        verdict = (row.get("gate") or {}).get("verdict")
        if verdict in ("postings", "none"):
            prior_flagged.add(digest)
        if verdict == "none":
            prior_none[digest] = True
    # One snapshot per company: the artifact bodies sit beside it as <key>.<name>.json
    files = sorted(p for p in src.glob("*.json") if p.name.count(".") == 1)
    for path in files:
        snap = json.loads(path.read_text())
        if args.keys and snap["key"] not in args.keys:
            continue
        if not snap.get("recipe_body"):
            print(f"{snap['key']}: no stored recipe (author status {snap['status']}), skipped")
            continue
        digest = body_hash(snap["recipe_body"])
        result = replay_one(
            snap,
            client,
            llm=llm,
            prior_none=prior_none.get(digest, False),
            prior_flagged=digest in prior_flagged,
            session=session,
        )
        _write_atomic(out_dir / f"{snap['key']}.replay.json", json.dumps(result, indent=2, ensure_ascii=False))
        vs = result.get("vs_fresh_reference", {})
        gate = (result.get("gate") or {}).get("verdict")
        print(
            f"{snap['key']}: {result['status']} count={result.get('count')} "
            f"matched={vs.get('matched')}/{vs.get('reference')} gate={gate or '-'} "
            f"signals={len(result.get('signals') or [])} stop={result.get('stop', False)}"
        )
    if session is not None:
        session.commit()
        session.close()
    print(f"replay written to {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
