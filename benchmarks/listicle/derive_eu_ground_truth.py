"""Derive the ``eu_largest_revenue`` ground truth from the tracked copied content.

Ground truth is never re-derived from live pages — the miner must not be scored
against text the harness fetched for it in a shape the miner never saw. It is
read out of the hand-copied page snapshot committed at the repo root
(``listicle_benchmark_copied_content.txt``), which is the source the class-2
site was added from. Wikipedia's ranking renders as one TSV-ish row per rank
(``rank<TAB>name<TAB>industry<TAB>revenue<TAB>country``); names the source lists
twice (Crédit Agricole appears at rank 26 and 88) collapse to their first
occurrence, because the one-to-one matcher can never match one ground-truth
name twice.

Usage: uv run python benchmarks/listicle/derive_eu_ground_truth.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
DATA_DIR = BENCH_DIR / "data"
COPIED_CONTENT = BENCH_DIR.parent.parent / "listicle_benchmark_copied_content.txt"

SITE_KEY = "eu_largest_revenue"
SOURCE_URL = "https://en.wikipedia.org/wiki/List_of_largest_companies_in_Europe_by_revenue"

_RANK_ROW_RE = re.compile(r"^\s*\d+\t([^\t]+)\t")
_PAGE_HEADER_RE = re.compile(r":\s*https?://")


def derive_names(text: str, url: str) -> list[str]:
    """Company names of the rank rows in the copied page whose URL line matches."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if url in line), None)
    if start is None:
        raise SystemExit(f"source URL not found in copied content: {url}")
    names: list[str] = []
    for line in lines[start + 1 :]:
        if _PAGE_HEADER_RE.search(line):
            break  # next copied page
        match = _RANK_ROW_RE.match(line)
        if not match:
            continue
        name = match.group(1).strip()
        if name and name not in names:
            names.append(name)
    return names


def main() -> int:
    names = derive_names(COPIED_CONTENT.read_text(encoding="utf-8"), SOURCE_URL)
    if not names:
        raise SystemExit("no rank rows derived — copied content shape changed?")
    path = DATA_DIR / "ground_truth.json"
    ground_truth = json.loads(path.read_text(encoding="utf-8"))
    ground_truth[SITE_KEY] = names
    path.write_text(
        json.dumps(ground_truth, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    print(f"{SITE_KEY}: {len(names)} ground-truth names -> {path}")
    print(", ".join(names))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
