"""Plain-text extraction from raw HTML (leaf module, no package-level imports).

Lives under ``ingest`` rather than ``search`` so ``ingest/site_class.py`` can use
it without importing ``jobsearch_core.search``: that package's ``__init__``
imports ``discover``, which imports ``scrapers/pipeline``, which imports
``site_class`` — a cycle. ``search/chunking.py`` re-exports ``html_to_text``.
"""

from __future__ import annotations

import re

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


def html_to_text(raw: str) -> str:
    """Strip code bodies, tags, and whitespace so chunks carry real content."""
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", _CODE_BODY_RE.sub(" ", raw))).strip()
