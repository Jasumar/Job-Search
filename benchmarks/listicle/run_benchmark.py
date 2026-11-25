"""Listicle extraction benchmark (Phase: listicle-extraction benchmark, decided 2026-09-21).

Measures the extractor ONLY: given fetched page text, how many ground-truth
companies does the mining path pull out? Fetching, routing, and
discovery integration are out of scope.

NOT part of the default pytest suite (live LLM + network). Run explicitly:

    uv run --extra dev --extra browser python benchmarks/listicle/run_benchmark.py

Visibility requirement: the runner logs every step — fetch status per site,
raw extracted names per site, per-site recall, matched/unmatched entity lists,
noise counts, and the aggregate score — and writes a full JSON report.

Matching semantics: entity-level, one-to-one fuzzy name matching with a legal-
form collision guard (a suffix like Oy/GmbH is often the only distinguisher
between two similarly named companies, so it is NEVER stripped and names with
differing legal forms can never match). Residual mismatches are printed for
human eyeball, never auto-scored as misses without review.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
DATA_DIR = BENCH_DIR / "data"
REPORT_PATH = DATA_DIR / "last_report.json"

# Per-site sampling plan (user decision 2026-09-22): do not require the full
# list for huge sites — sample head + spread through the middle so we can see
# that the miner actually reads the whole page, plus report full-set recall
# separately when the miner returns enough names to measure it.
SAMPLE_PLAN: dict[str, int] = {
    "estonia_fintech": 7,          # full list (7 entries)
    "finland_startups": 10,        # only 10 visible above the sign-up wall
    "world_largest_tech": 30,      # head 10 + spread sample of the ~100 named
    "japan_tech": 30,              # head 10 + spread sample of the ~100 named
    "eu_largest_revenue": 30,      # head 10 + spread sample of the 99 named
}

# Similarity threshold for name matching (RapidFuzz if available, else
# difflib.SequenceMatcher.ratio). Matched pairs below this are rejected.
MATCH_THRESHOLD = 0.85

# Mining cap used for the benchmark: raised above the production 15 so we can
# measure true recall on the "gold mine" pages, and above the largest ground
# truth list (100) so the cap can never be what limits recall — a truncated
# extraction used to be indistinguishable from a page the miner could not read,
# which is exactly how the class-2 site was misdiagnosed once already. The
# production cap question (kill / soften the hard cap) is a separate outcome of
# this phase.
BENCH_MAX_COMPANIES = 200

# Legal-form tokens (I1 invariant): a suffix is often the ONLY distinguisher
# between two similarly named entities (e.g. "Foo Oy" vs "Foo GmbH"). Names are
# never stripped of these; instead a pair whose legal forms differ can never
# match, even at high similarity.
LEGAL_FORMS = (
    "oyj", "oy", "ab", "as", "aps", "gmbh", "ag", "nv", "bv", "sa", "spa",
    "srl", "sarl", "plc", "ltd", "limited", "llc", "inc", "corp",
    "corporation", "kk", "k k", "pte", "pty",
)


def normalize(name: str) -> str:
    """Casefold + punctuation/whitespace normalization. NEVER strips legal forms."""
    s = name.casefold().strip()
    s = re.sub(r"\(.*?\)", " ", s)  # drop parentheticals: "Alphabet (Google)" -> "alphabet"
    s = s.replace("&", " and ")
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    return re.sub(r"\s+", " ", s).strip()


def legal_form(norm_name: str) -> str | None:
    """Legal-form token of a normalized name, or None."""
    last = norm_name.rsplit(" ", 1)[-1]
    return last if last in LEGAL_FORMS else None


def _ratio(a: str, b: str) -> float:
    try:
        from rapidfuzz import fuzz
        return float(fuzz.ratio(a, b)) / 100.0
    except ImportError:
        from difflib import SequenceMatcher
        return SequenceMatcher(None, a, b).ratio()


def _near_misses(
    gt_norm: list[tuple[str, str]],
    ex_norm: list[tuple[str, str]],
    used_gt: set[int],
    used_ex: set[int],
) -> list[dict]:
    """Best below-threshold candidate per unmatched ground-truth name.

    Two invariants (I5, I3 — see the call site):
      I5: ``best_candidate`` is the ORIGINAL extracted spelling, never the
          normalized one. A reviewer cannot act on "amazon com".
      I3: a single extracted name may be the candidate for at most ONE
          ground-truth name. Without exclusivity a single extraction is
          offered against every unmatched GT name, and the reported
          ``similarity`` reads as if it belonged to each of them.
    """
    near: list[dict] = []
    claimed: set[int] = set()
    for gi, (gname, g) in enumerate(gt_norm):
        if gi in used_gt:
            continue
        gform = legal_form(g)
        best, best_ei = 0.0, None
        for ei, (_, e) in enumerate(ex_norm):
            if ei in used_ex or ei in claimed or legal_form(e) != gform:
                continue
            r = _ratio(g, e)
            if r > best:
                best, best_ei = r, ei
        if best_ei is not None:
            claimed.add(best_ei)
            near.append(
                {
                    "ground_truth": gname,
                    "best_candidate": ex_norm[best_ei][0],
                    "similarity": round(best, 3),
                }
            )
    return near


def match_names(
    gt_list: list[str], extracted: list[str], threshold: float = MATCH_THRESHOLD
) -> dict:
    """One-to-one greedy matching by name similarity with a legal-form collision guard.

    Returns per-name pairs, matched/unmatched indices, and similarity detail.
    Two names are match candidates only if (a) their legal forms are equal
    (both None, or the same token) and (b) their similarity >= threshold.
    """
    gt_norm = [(n, normalize(n)) for n in gt_list]
    ex_norm = [(n, normalize(n)) for n in extracted]
    pairs: list[tuple[int, int, float]] = []
    for gi, (_, g) in enumerate(gt_norm):
        gform = legal_form(g)
        for ei, (_, e) in enumerate(ex_norm):
            eform = legal_form(e)
            if gform != eform:
                continue
            r = _ratio(g, e)
            if r >= threshold:
                pairs.append((gi, ei, r))
    # greedy: highest similarity first, each gt/ex used once
    pairs.sort(key=lambda p: -p[2])
    used_gt: set[int] = set()
    used_ex: set[int] = set()
    matched: list[dict] = []
    for gi, ei, r in pairs:
        if gi in used_gt or ei in used_ex:
            continue
        used_gt.add(gi)
        used_ex.add(ei)
        matched.append({
            "ground_truth": gt_list[gi],
            "extracted": extracted[ei],
            "similarity": round(r, 3),
        })
    # Near-misses: the best remaining candidate per unmatched ground-truth name
    # (I4 — reported with its score for human review, never counted as found).
    # Two invariants the report must hold for the THRESHOLD eyeball to be usable:
    #   I5 — the candidate is the ORIGINAL extracted spelling, not the normalized
    #        one ("amazon com" is unreadable; "Amazon.com" is).
    #   I3 — the scan is one-to-one exclusive: one extracted name may be offered
    #        against at most one ground-truth name. Otherwise a single extraction
    #        is repeated against dozens of GT names and the list says nothing
    #        about which pairing the score belongs to.
    near = _near_misses(gt_norm, ex_norm, used_gt, used_ex)

    return {
        "matched": matched,
        "misses": [gt_list[i] for i in range(len(gt_list)) if i not in used_gt],
        "noise": [extracted[i] for i in range(len(extracted)) if i not in used_ex],
        "near_misses": near,
    }


def load_ground_truth() -> dict[str, list[str]]:
    return json.loads((DATA_DIR / "ground_truth.json").read_text(encoding="utf-8"))


def build_sample(gt: list[str], take: int) -> list[str]:
    """Head `take//2` + evenly spread rest (no duplicates)."""
    if take >= len(gt):
        return list(gt)
    head = gt[: take // 2]
    rest = gt[take // 2 :]
    step = max(1, len(rest) // max(1, take - len(head)))
    spread = rest[::step][: take - len(head)]
    return head + spread


def fetch_pages(sites: dict[str, str], http, verbose: bool = True) -> dict[str, str]:
    """Fetch each site once through the polite client.

    The client is passed in (not created here) so :func:`mine` can reuse the
    same session — and its browser and cookie state — for the client-render
    retry. Rendering spins up Chromium, so it must not be paid twice.
    """
    pages: dict[str, str] = {}
    for key, url in sites.items():
        if verbose:
            print(f"[fetch] {key}: {url}", flush=True)
        t0 = time.monotonic()
        try:
            body = http.fetch(url)
        except Exception as exc:  # noqa: BLE001 — benchmark must not abort on one site
            print(f"[fetch] {key}: FAILED ({exc!r}) after {time.monotonic() - t0:.1f}s", flush=True)
            pages[key] = ""
            continue
        ok = len(body) > 500
        print(f"[fetch] {key}: {len(body)} chars in {time.monotonic() - t0:.1f}s "
              f"({'ok' if ok else 'SUSPICIOUSLY SMALL — likely blocked'})", flush=True)
        pages[key] = body if ok else ""
    return pages


def mine(
    pages: dict[str, str], sites: dict[str, str], http, verbose: bool = True
) -> tuple[dict[str, list[str]], dict[str, bool]]:
    """Mine every fetched page; returns (extracted names, rendered sites).

    ``rendered`` records which sites needed the browser tier — class-3
    client-rendered sources whose list never appears in the served HTML. A site
    in that map was scored from a rendered DOM, so its recall is not
    comparable to a plain-HTML site's without that context.
    """
    from jobsearch_core.config import Settings
    from jobsearch_core.llm.client import LLMClient
    from jobsearch_core.llm.decisions import DecisionsClient
    from jobsearch_core.search.discover import mine_listicle_with_render

    settings = Settings()
    client = LLMClient(
        api_key=settings.openrouter_api_key,
        model=settings.analysis_model,
        base_url=settings.llm_base_url,
        min_interval_seconds=settings.llm_min_interval_seconds,
        max_retries=settings.llm_max_retries,
    )
    # Noul gate: without it mining falls back to head truncation at 20K chars,
    # which scored 0% recall on the first run. The same gate produces the
    # class-3 "names absent from the HTML" signal that triggers a render.
    gate = DecisionsClient(
        api_key=settings.openrouter_api_key,
        model=settings.typesafe_model,
        max_retries=settings.llm_max_retries,
    ) if settings.openrouter_api_key else None
    extracted: dict[str, list[str]] = {}
    rendered: dict[str, bool] = {}
    for key, body in pages.items():
        if not body:
            extracted[key] = []
            rendered[key] = False
            continue
        if verbose:
            print(f"[mine] {key}: calling analysis model (cap={BENCH_MAX_COMPANIES})...", flush=True)
        t0 = time.monotonic()
        mined, was_rendered = mine_listicle_with_render(
            sites.get(key, key), body, client, settings.llm_cache_dir, http,
            max_companies=BENCH_MAX_COMPANIES, gate=gate,
        )
        names = [item.company_name for item in mined]
        suffix = " [client-rendered]" if was_rendered else ""
        print(f"[mine] {key}: {len(names)} companies in {time.monotonic() - t0:.1f}s{suffix}", flush=True)
        extracted[key] = names
        rendered[key] = was_rendered
    return extracted, rendered


def score(
    gt: dict[str, list[str]],
    extracted: dict[str, list[str]],
    rendered: dict[str, bool] | None = None,
    verbose: bool = True,
) -> tuple[dict, float]:
    report: dict = {}
    total_hits, total_gt, total_sim = 0, 0, 0.0
    for key, gt_list in gt.items():
        sample = build_sample(gt_list, SAMPLE_PLAN.get(key, len(gt_list)))
        res = match_names(sample, extracted.get(key, []))
        hits, denom = len(res["matched"]), len(sample)
        mean_sim = (
            sum(m["similarity"] for m in res["matched"]) / hits if hits else 0.0
        )
        total_hits += hits
        total_gt += denom
        total_sim += sum(m["similarity"] for m in res["matched"])
        recall = hits / denom if denom else 0.0
        report[key] = {
            "sample_size": denom,
            "full_gt_size": len(gt_list),
            "extracted_count": len(extracted.get(key, [])),
            # True means the extraction stopped at the mining cap, so recall is
            # capped by the benchmark itself and must not be read as page recall.
            "cap_reached": len(extracted.get(key, [])) >= BENCH_MAX_COMPANIES,
            # True = the list came from a headless render, not the served HTML,
            # so this site's recall was not measured on the plain fetch.
            "client_rendered": bool((rendered or {}).get(key)),
            "recall_sample": round(recall, 3),
            "mean_similarity": round(mean_sim, 3),
            "matched": res["matched"],
            "misses": res["misses"],
            "noise_count": len(res["noise"]),
            "noise_examples": res["noise"][:15],
            "near_misses": res["near_misses"],
        }
        if verbose:
            print(f"\n=== {key} ===")
            rendered_note = " [client-rendered]" if (rendered or {}).get(key) else ""
            print(f"  sample {denom} of {len(gt_list)} ground-truth; "
                  f"extracted {len(extracted.get(key, []))}{rendered_note}")
            print(f"  recall@{MATCH_THRESHOLD} {hits}/{denom} = {recall:.0%}  mean_similarity {mean_sim:.3f}")
            print(f"  misses: {res['misses']}")
            print(f"  noise (extracted but unmatched): {len(res['noise'])}; examples: {res['noise'][:8]}")
            for nm in res["near_misses"][:10]:
                print(f"  near-miss: {nm['ground_truth']} ~ {nm['best_candidate']} ({nm['similarity']:.3f})")
    aggregate = total_hits / total_gt if total_gt else 0.0
    agg_mean = total_sim / total_hits if total_hits else 0.0
    print(f"\nAGGREGATE RECALL@{MATCH_THRESHOLD}: {total_hits}/{total_gt} = {aggregate:.0%}  "
          f"(bar: 80%)  mean_similarity {agg_mean:.3f}")
    return report, aggregate


def main() -> int:
    sites = json.loads((DATA_DIR / "sites.json").read_text(encoding="utf-8"))
    gt = load_ground_truth()
    verbose = "--quiet" not in sys.argv
    print(f"Listicle benchmark: {len(sites)} sites, mining cap {BENCH_MAX_COMPANIES}\n", flush=True)
    from jobsearch_core.ingest.http import PoliteClient

    # One client for the whole run: the browser tier and cookie jar are shared
    # between the initial fetch and the class-3 render retry.
    with PoliteClient() as http:
        pages = fetch_pages(sites, http, verbose)
        print(flush=True)
        extracted, rendered = mine(pages, sites, http, verbose)
    print(flush=True)
    report, aggregate = score(gt, extracted, rendered, verbose)
    payload = {
        "sites": sites,
        "bench_max_companies": BENCH_MAX_COMPANIES,
        "aggregate_recall": round(aggregate, 3),
        "per_site": report,
        "client_rendered_sites": sorted(k for k, v in rendered.items() if v),
        "extracted_raw": extracted,
    }
    REPORT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nFull report written to {REPORT_PATH}")
    return 0 if aggregate >= 0.8 else 1


if __name__ == "__main__":
    sys.exit(main())
