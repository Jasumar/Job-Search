"""company_discovery snapshot writer: freeze search results and mention pages once.

Live search and live pages drift. Scores must never read them directly, so
this writes every input to a dated folder under
``benchmarks/funnel/data/snapshots/<date>/`` and the scorers read only from
there (docs/plan-benchmarks.md, "Frozen snapshots").

Usage (network required; run explicitly, never from pytest):

    uv run --extra dev python benchmarks/funnel/company_discovery/snapshot.py

Output layout::

    snapshots/<date>/manifest.json            per-input status + sha256
    snapshots/<date>/queries/q<NN>.json       ddgs results for one query
    snapshots/<date>/pages/p<NN>.html         fetched body for one mention page

A failed fetch or search is recorded with ``status`` != "ok" and an empty
body/result list. The scorer counts those as unscored, never as misses.
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from jobsearch_core.ingest.http import PoliteClient
from jobsearch_core.search.client import DdgsSearchClient

FUNNEL_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = FUNNEL_DIR / "data"
SNAPSHOT_ROOT = DATA_DIR / "snapshots"
SEARCH_RESULTS_PER_QUERY = 10
SEARCH_ATTEMPTS = 3
SEARCH_RETRY_SECONDS = 5.0
MIN_BODY_CHARS = 500  # same "suspiciously small = blocked" bar as harness.fetch_pages


def _load(name: str) -> dict:
    return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def snapshot_queries(snap_dir: Path, search: DdgsSearchClient) -> list[dict]:
    rows: list[dict] = []
    queries = _load("queries.json")["queries"]
    (snap_dir / "queries").mkdir(parents=True, exist_ok=True)
    for idx, q in enumerate(queries, start=1):
        key = f"q{idx:02d}"
        try:
            results = []
            # The client retries an empty aggregate once and then raises
            # SearchError rather than answering []; the loop below adds
            # patient retries on top, because a snapshot is frozen forever.
            # A query that never answers is recorded as "error" (unscored),
            # never as a zero-hit query.
            for attempt in range(SEARCH_ATTEMPTS):
                results = search.search(q["query"], num_results=SEARCH_RESULTS_PER_QUERY)
                if results:
                    break
                time.sleep(SEARCH_RETRY_SECONDS)
            status = "ok" if results else "empty"
            payload = [
                {"url": r.url, "title": r.title, "snippet": r.snippet} for r in results
            ]
            error = None
        except Exception as exc:  # noqa: BLE001 — one bad query must not abort the snapshot
            status, payload, error = "error", [], repr(exc)
        body = json.dumps(
            {"query": q["query"], "family": q["family"], "results": payload},
            ensure_ascii=False, indent=1,
        ).encode("utf-8")
        (snap_dir / "queries" / f"{key}.json").write_bytes(body)
        rows.append({
            "key": key, "query": q["query"], "family": q["family"], "status": status,
            "result_count": len(payload), "sha256": _sha256(body), "error": error,
        })
        print(f"[query] {key} {q['query']!r}: {status}, {len(payload)} results", flush=True)
    return rows


def snapshot_pages(snap_dir: Path, http: PoliteClient) -> list[dict]:
    rows: list[dict] = []
    pages = _load("mention_pages.json")["pages"]
    (snap_dir / "pages").mkdir(parents=True, exist_ok=True)
    for idx, p in enumerate(pages, start=1):
        key = f"p{idx:02d}"
        try:
            text = http.fetch(p["url"])
            ok = len(text) > MIN_BODY_CHARS
            status = "ok" if ok else "suspiciously_small"
            error = None
        except Exception as exc:  # noqa: BLE001 — a dead page is a named unscored case
            text, status, error = "", "error", repr(exc)
        body = (text or "").encode("utf-8")
        (snap_dir / "pages" / f"{key}.html").write_bytes(body)
        rows.append({
            "key": key, "url": p["url"], "family": p["family"], "status": status,
            "chars": len(text or ""), "sha256": _sha256(body), "error": error,
        })
        print(f"[page] {key} {p['url']}: {status}, {len(text or '')} chars", flush=True)
    return rows


def main() -> int:
    date = datetime.now(UTC).date().isoformat()
    snap_dir = SNAPSHOT_ROOT / date
    if snap_dir.exists():
        print(f"snapshot already exists: {snap_dir} (delete it to re-freeze)", file=sys.stderr)
        return 2
    snap_dir.mkdir(parents=True)
    search = DdgsSearchClient()
    with PoliteClient() as http:
        query_rows = snapshot_queries(snap_dir, search)
        page_rows = snapshot_pages(snap_dir, http)
    manifest = {
        "snapshot_date": date,
        "taken_at": datetime.now(UTC).isoformat(),
        "search_backend": "ddgs (DdgsSearchClient)",
        "results_per_query": SEARCH_RESULTS_PER_QUERY,
        "queries": query_rows,
        "pages": page_rows,
    }
    (snap_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"wrote {snap_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
