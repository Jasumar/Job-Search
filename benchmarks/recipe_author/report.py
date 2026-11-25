"""Build benchmarks/recipe_author/report-<author-date>.md from the snapshots.

Reads snapshots/<author-date>/ (run 1) and, when present, the replay files
snapshots/<replay-date>/<key>.replay.json (run 2). The report is for eyeballing:
no labels are set here.

    uv run python benchmarks/recipe_author/report.py 2026-10-19
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import date

from run_author import BENCH, SNAP_ROOT  # type: ignore[import-not-found]

SHOW_TITLES = 5


def shape(body: dict) -> str:
    if body.get("kind") == "api":
        return (
            f"api url_template={body.get('url_template')} pagination={body.get('pagination', {}).get('kind')} "
            f"list_path={body.get('list_path')}"
        )
    return (
        f"dom container={body.get('container')!r} link={body.get('link')!r} "
        f"title={body.get('title')!r} location={body.get('location')!r}"
    )


def fmt_titles(rows: list[dict]) -> str:
    return "; ".join((r.get("title") or "")[:60] for r in rows[:SHOW_TITLES]) or "-"


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: report.py <author-date>", file=sys.stderr)
        return 2
    author_date = sys.argv[1]
    src = SNAP_ROOT / author_date
    if not src.is_dir():
        print(f"no snapshots at {src}", file=sys.stderr)
        return 2
    run_date = date.today().isoformat()
    snaps = [
        json.loads(p.read_text())
        for p in sorted(src.glob("*.json"))
        # One snapshot per company: artifact files are named <key>.<artifact>.json
        if p.name.count(".") == 1
    ]
    replays = {}
    rdir = SNAP_ROOT / run_date
    if rdir.is_dir():
        for p in rdir.glob("*.replay.json"):
            replays[p.name.removesuffix(".replay.json")] = json.loads(p.read_text())

    status = Counter(s["status"] for s in snaps)
    lines: list[str] = []
    lines.append(f"# Recipe-authoring benchmark, stage B (authored {author_date})")
    lines.append("")
    lines.append(f"Report generated {run_date}. Replay date(s): {run_date} for replays shown below.")
    lines.append(
        "The reference is derived from each capture (site payload / id join / link cluster), "
        "never from the scraper being graded. The user eyeballs."
    )
    lines.append("")
    lines.append(f"Authoring outcomes: {dict(status)}")
    if replays:
        rstat = Counter(r["status"] for r in replays.values())
        lines.append(f"Replay outcomes ({run_date}): {dict(rstat)}")
    lines.append("")

    for s in snaps:
        key = s["key"]
        notes = s.get("notes", {})
        ref = s.get("reference") or {}
        lines.append(f"## {key} ({s['name']}, {s['stratum']}, family {s['family']})")
        lines.append(f"- URL: {notes.get('page_url')}")
        artifacts = s.get("artifacts") or []
        artifact_label = "none"
        for a in artifacts:
            if a["name"] == s.get("source_artifact"):
                artifact_label = f"{a['fetch_mode']} ({a['name']})"
        capture = ", ".join(f"{a['name']}={a['fetch_mode']}" for a in artifacts) or "none"
        scraper = s.get("scraper") or {}
        lines.append(
            f"- Label kind: {s['open_positions_kind_label']}; scraper path: {scraper.get('path')}; "
            f"chosen kind: {s.get('kind')}"
        )
        lines.append(
            f"- Capture: {capture}; reference carried by {ref.get('carried_by') or 'none'}; "
            f"authored against {artifact_label}"
        )
        if notes.get("render_error"):
            lines.append(f"- Render failed: {notes['render_error']}"[:220])
        state = f" (recipe_state {s['recipe_state']})" if s.get("recipe_state") else ""
        lines.append(f"- Authoring: {s['status']}{state}")
        if s.get("error"):
            lines.append(f"- Error: {s['error']}")
        if s["status"] != "capture_gap" and s.get("trials"):
            for t in s["trials"]:
                verdict = "PASS" if t["passed"] else "FAIL"
                lines.append(f"  - trial {t['number']}: {verdict} {(t.get('reason') or '')[:160]}")
        if s["status"] != "capture_gap" and s.get("recipe_body"):
            lines.append(f"- Recipe: {shape(s['recipe_body'])}")
            expected = (s["recipe_body"].get("verification") or {}).get("expected_count")
            lines.append(f"- Expected count from reference: {expected}")
        if s["status"] == "capture_gap":
            lines.append(
                f"- CAPTURE GAP: no posting list in the capture ({ref.get('method')}); "
                "nothing was graded -- not a recipe failure"
            )
            for note in (ref.get("notes") or [])[:3]:
                lines.append(f"  - {note}"[:200])
        else:
            lines.append(
                f"- Reference (run 1): {ref.get('count')} postings, "
                f"method {ref.get('method') or '-'}/{ref.get('confidence') or '-'}, "
                f"by artifact {ref.get('by_artifact') or '-'}"
            )
            if ref.get("sample"):
                lines.append(f"- Reference titles: {fmt_titles(ref['sample'])}")
            # Reference notes explain a small denominator (pages it could not resolve).
            for note in (ref.get("notes") or [])[:2]:
                lines.append(f"  - reference note: {note}"[:200])
            if scraper:
                lines.append(
                    f"- Scraper column: {scraper.get('count')} postings "
                    f"({scraper.get('in_reference')} in reference, "
                    f"{scraper.get('outside_reference')} outside)"
                )
        rp = replays.get(key)
        if rp:
            lines.append(
                # A hand-written or half-written replay file must not cost the run's report.
                f"- Replay ({rp.get('replay_run_date') or '-'}): {rp.get('status')}, count {rp.get('count')}"
            )
            vs = rp.get("vs_fresh_reference")
            if vs:
                lines.append(
                    f"  - vs fresh reference: {vs['matched']}/{vs['reference']} matched, "
                    f"{vs['unmatched']} outside, {vs['missing']} missing"
                )
            drift = rp.get("reference_drift")
            if drift:
                lines.append(
                    f"  - Reference drift: run1 {drift['run1']} -> today {drift['today']} "
                    f"({drift['gone_count']} gone, {drift['new_count']} new)"
                )
            if rp.get("error"):
                lines.append(f"  - Replay error: {rp['error']}")
            if rp.get("sample"):
                lines.append(f"  - Replay titles: {fmt_titles(rp['sample'])}")
            if rp.get("verify_on_fresh_capture") is not None:
                v = rp["verify_on_fresh_capture"]
                verdict = "ok" if v["ok"] else f"FAIL: {v.get('reason') or 'empty replay'}"
                verdict += f" (off-pattern {v.get('off_pattern')}, outside {v.get('outside_reference')})"
                lines.append(f"  - Verify on fresh fetch: {verdict}")
            dom = rp.get("dom_agreement")
            if dom:
                lines.append(
                    f"  - First page vs rendered DOM: {dom['matched']}/{dom['replay']} replayed "
                    f"in DOM, {dom['missing']} missing, {dom['rendered']} DOM links"
                )
            st = rp.get("statistics")
            if st:
                fill = ", ".join(
                    f"{name} {rate:.2f}" for name, rate in (st.get("fill") or {}).items() if rate is not None
                )
                lines.append(
                    f"  - Statistics: dups {st['duplicate_urls']} url / {st['duplicate_titles']} title, "
                    f"pages {st['page_counts']} advance={st['page_advance']}, "
                    f"detail {st['detail'][0]}/{st['detail'][1]}"
                )
                if fill:
                    lines.append(f"    fill rates: {fill}")
            for signal_row in (rp.get("signals") or [])[:4]:
                lines.append(f"  - Signal {signal_row['name']}={signal_row['value']}: {signal_row['points_at']}")
            gate = rp.get("gate")
            if gate:
                if gate.get("verdict") == "error":
                    lines.append(f"  - Gate: ERROR {gate.get('error')}")
                else:
                    head = f"  - Gate ({gate.get('model_id')}): {gate['verdict']}"
                    if gate.get("stopped"):
                        head += " -> STOP"
                    elif gate["verdict"] == "none":
                        head += " -> flagged, continuing (no agreement yet)"
                    lines.append(head)
                    for item in gate.get("items") or []:
                        lines.append(f"    - {item['url']}: {item['reason']}"[:240])
            elif rp.get("gate_event") is False:
                # Cadence, not a failure: the body already has an answer and
                # nothing fired since, so this run spent no model call.
                lines.append("  - Gate: skipped (existing answer, no event)")
        lines.append("")

    out = BENCH / f"report-{author_date}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
