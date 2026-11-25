"""Chunk selection for listicle mining.

Raw listicle HTML is long and its list content often sits far beyond the
first chunk, so a single fixed-cap prompt starves the miner (0% recall on
the first benchmark run). Strategy: strip tags to clean text, chunk at a
fixed size, use the Jev Noul gate ("does this chunk contain company
names?") to keep only content-bearing chunks, and concatenate them for
mining. The gate is never a hard dependency — any failure mines all chunks.
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

from jobsearch_core.ingest.html_text import html_to_text as html_to_text  # re-export: public API
from jobsearch_core.llm.decisions import DecisionsClient
from jobsearch_core.log import get_logger

logger = get_logger(__name__)

CHUNK_SIZE = 20000
NOUL_KEEP_THRESHOLD = 0.2
TOP_K_ALWAYS_KEEP = 1

# Gated-content guard (listicle mining). Signal-based by design: a page that
# merely links to a login page is not restricted, and mining must not become a
# deliberate premium-content harvester. Only subtrees the page itself *labels*
# as restricted, and the upsell phrasing itself, are removed.
_RESTRICTED_ATTR_RE = re.compile(
    r"members?-only|signin|sign-in|login|log-in|auth-required|"
    r"requires?-auth|paywall|pay-wall|premium|locked|subscriber",
    re.IGNORECASE,
)
# Marketing phrasing that marks the surrounding block as gated. The sentence is
# a call to action, not content, so removing just the sentence preserves
# whatever public text sits next to it.
_RESTRICTED_PHRASE_RE = re.compile(
    r"(?:log|sign)\s*in\s+to\s+(?:access|view|see|read|unlock)\b[^.\n]{0,80}"
    r"|(?:create|make)\s+(?:a|an)\s+(?:free\s+)?account\s+to\s+(?:access|view|see|read|unlock)\b[^.\n]{0,80}"
    r"|subscribe\s+to\s+(?:view|see|read|unlock|access)\b[^.\n]{0,80}"
    r"|this\s+(?:content|article|list|page)\s+is\s+(?:for\s+)?(?:members?|subscribers?)\b[^.\n]{0,80}"
    r"|unlock\s+(?:the\s+)?(?:full|rest\s+of\s+(?:the\s+)?)?(?:list|article|content)\b[^.\n]{0,80}",
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
# Script and style bodies are not page content: they inflate the mining prompt
# with minified JS/CSS (a 165 KB page can be most-script), they push the real
# list past the prompt cap, and they read as noise to the chunk gate. Drop the
# bodies before tag stripping. The pattern also matches an unterminated
# ``<script`` (truncated fetch) by letting the body run to the end of the text.
_CODE_BODY_RE = re.compile(
    r"<(script|style)\b[^>]*>.*?(?:</\1\s*>|\Z)",
    re.IGNORECASE | re.DOTALL,
)

_NOUL_QUESTION = {
    "has_company_names": {
        "type": "noul",
        "instructions": (
            "Does `page_text` contain the names of companies presented as "
            "subjects of the content (e.g. profiled, ranked or listed as "
            "businesses), as opposed to only incidental mentions in generic "
            "prose or boilerplate?"
        ),
    }
}


def _is_restricted_tag(tag: object) -> bool:
    """Whether this element's own attributes label its subtree as restricted."""
    attrs = getattr(tag, "attrs", None) or {}
    for key, value in attrs.items():
        if key not in {"class", "id"} and not str(key).startswith("data-"):
            continue
        # ``class`` is a list, ``id`` a string, ``data-*`` may be either; join
        # the multi-value form so a hyphenated word cannot be split by the
        # space and hide itself from the pattern.
        joined = " ".join(value) if isinstance(value, list) else str(value)
        if _RESTRICTED_ATTR_RE.search(" ".join(joined.split())):
            return True
    return False


def strip_gated_content(raw: str) -> str:
    """Remove subtrees and sentences the page itself marks as restricted.

    Deliberate harvesting of premium content must not become a pipeline
    feature, so gated material is dropped before it ever reaches the miner.
    The guard is *signal-based*, not blanket:

      * A subtree is dropped when its own ``class``/``id``/``data-*`` value
        carries a restriction word (``members-only``, ``paywall``,
        ``auth-required``, …). A page whose navigation merely *links* to
        ``/login`` is untouched — only a labelled container is removed, so
        public content next to a login link survives.
      * Gated *phrasing* ("login to access all …", "unlock the full list") is
        removed as whole sentences wherever it appears. That is a call to
        action, never list content, and sentence-level removal keeps the
        public prose around it.

    Parsing failures must not lose the page: on any error the input is
    returned stripped of code bodies only, so mining continues on the ungated
    text rather than nothing.
    """
    if not raw:
        return ""
    try:
        soup = BeautifulSoup(raw, "html.parser")
    except Exception as exc:  # noqa: BLE001 — a parse failure must not lose the page
        logger.warning("Gated-content parse failed; mining ungated text: %s", exc)
        return _WS_RE.sub(" ", _TAG_RE.sub(" ", _CODE_BODY_RE.sub(" ", raw))).strip()

    dropped = 0
    for tag in soup.find_all(True):
        if _is_restricted_tag(tag):
            tag.decompose()
            dropped += 1
    if dropped:
        logger.info("Gated-content guard removed %d labelled container(s)", dropped)
    text = _CODE_BODY_RE.sub(" ", str(soup))
    text = _WS_RE.sub(" ", _TAG_RE.sub(" ", text)).strip()
    return _RESTRICTED_PHRASE_RE.sub(" ", text).strip()


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE) -> list[str]:
    return [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]


def names_signal(scores: list[float | None], keep_threshold: float = NOUL_KEEP_THRESHOLD) -> bool | None:
    """Whether any chunk plausibly contains the company names we came for.

    ``False`` means the gate looked at every chunk and none of them carried
    company names — the classic signature of a client-rendered page whose data
    never arrived in the HTML. ``True`` when at least one chunk cleared the
    threshold. ``None`` when the gate could not answer at all (every call
    failed), which is *not* evidence of a missing list: mining proceeds
    unchanged and no render is triggered.
    """
    ranked = [s for s in scores if s is not None]
    if not ranked:
        return None
    return any(s >= keep_threshold for s in ranked)


def select_chunks(
    page_text: str,
    gate: DecisionsClient,
    chunk_size: int = CHUNK_SIZE,
    keep_threshold: float = NOUL_KEEP_THRESHOLD,
    top_k: int = TOP_K_ALWAYS_KEEP,
) -> tuple[list[str], list[float | None]]:
    """Pick the chunks worth mining.

    Returns ``(kept_chunks, noul_scores)`` where scores align with all
    chunks (None placeholders on gated failures). Raises if every Noul
    call fails — callers then fall back to ungated mining.
    """
    chunks = chunk_text(strip_gated_content(page_text), chunk_size)
    if len(chunks) <= 1:
        return chunks, [1.0] if chunks else []
    scores: list[float | None] = []
    any_success = False
    for chunk in chunks:
        try:
            answers = gate.ask({"page_text": chunk}, _NOUL_QUESTION)
            noul = float(answers["has_company_names"]["noul"])
            scores.append(noul)
            any_success = True
        except Exception as exc:  # noqa: BLE001 — gate must never crash mining
            logger.warning("Noul gate failed on a chunk: %s", exc)
            scores.append(None)
    if not any_success:
        raise RuntimeError("all Noul gate calls failed")
    order = sorted(
        (i for i, s in enumerate(scores) if s is not None),
        key=lambda i: scores[i] or 0.0,
        reverse=True,
    )
    keep = {
        i for i, s in enumerate(scores) if s is not None and s >= keep_threshold
    }
    keep.update(order[:top_k])
    kept = [chunks[i] for i in sorted(keep)]
    logger.info(
        "Chunk gate: %d/%d chunks kept (noul>=%.2f): %s",
        len(kept), len(chunks), keep_threshold,
        [f"{s:.2f}" if s is not None else "err" for s in scores],
    )
    return kept, scores
