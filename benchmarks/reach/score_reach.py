"""Scorer for benchmark suite entry 3: posting reach rate.

Question: given a posting URL, does usable posting text arrive at all?

This is the transport-only half of the posting pipeline. Entry 4 (attribute
extraction) takes the text this produces and asks whether the right fields come
out. Scored as one number they could not tell a Playwright escalation
regression from schema drift, so the seam is drawn here: this module never
looks at *which* fields came out, only at whether usable text arrived and, if
it did not, what stopped it.

No LLM anywhere in this file. That is what makes it the cheapest entry in the
suite to label and the only one whose numbers can be trusted without a model in
the loop — and it is why the suite's "bypass the LLM cache or report hit rate"
convention does not apply to this entry. There is no cache to bypass. The cost
is all transport, so the numbers move only when the fetch path changes.

Independence from the code under test
-------------------------------------
The obvious way to write this is to ask the production helpers
(``_is_usable_content`` and friends) whether the response was usable. That
would be circular: it would define "reachable" as "the thing under test agrees
with itself", so a regression in those markers could not show up as a bad
number — it would silently redefine the target instead. So the markers below
are written independently, deliberately not imported from
``jobsearch_core.ingest.http``. When the two disagree, that disagreement is
itself the finding.

What is deliberately out of scope
---------------------------------
A page that is not a posting at all — a careers landing page, an FAQ page —
arrives with plenty of text and is *not* a transport failure. Detecting it
needs semantics, not transport, and semantically it belongs to discovery and
to suite entry 5. Those cases are therefore labelled by hand with the
obstacle ``not_a_posting`` and reported, never scored by this classifier.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from jobsearch_core.ingest.http import PoliteClient
from jobsearch_core.ingest.scrapers.detail import fetch_detail  # noqa: E402
from jobsearch_core.ingest.textguards import (
    is_login_wall,  # noqa: E402
)

# Below this a response is too thin to be a posting body. 200 is the same floor
# the transport layer uses for a plain page, chosen so the two numbers are
# comparable when reading a report side by side.
MIN_USABLE_CHARS = 200

# Independently written, not shared with ingest.http. See the module docstring.
_WALL_MARKERS = (
    "just a moment",
    "checking your browser",
    "attention required",
    "access denied",
    "unusual traffic",
    "ddos-guard",
    "verify you are human",
    "are you a robot",
    "not a robot",
    "enable javascript and cookies",
    "request unsuccessful. incapsula",
    "cloudflare",
)

# A login wall REPLACES the posting; an incidental login link (a "report this
# job" widget, an account upsell) sits inside a complete posting. Marker
# presence cannot tell those apart on its own — the real Built In postings in
# the live DB carry "Please log in or sign up to report this job" alongside
# 4–15KB of perfectly good description.
#
# Calibrated 2026-10-02 against the live DB: the two genuine walls
# (jobs.siemens.com) are 763 chars, the eight incidental-widget pages are
# 4844–15152 chars. 2000 sits in the empty gap with wide margin on both sides.
#
# The invariant: a page this short cannot also carry a real posting body, so
# if a login marker is present there is no posting behind it to reach. If a page
# is long enough to hold a posting, a login marker inside it describes the
# widget, not the page. Note this makes the wall test deliberately blind to
# long pages — a site that serves a login page padded to 8KB would read as
# reachable. That is a known, accepted limit of a length-based test, recorded
# here rather than hidden.
# LOGIN_WALL_MAX_CHARS and the login markers are imported from
# jobsearch_core.ingest.textguards — single calibrated definition, shared with
# the production detail extractor so the two layers cannot drift.

_BINARY_PREFIXES = ("%PDF", "PK\x03\x04", "\x89PNG", "GIF8", "\xff\xd8\xff")

# Outcome vocabulary. These strings are the report's contract, so they are
# stable identifiers rather than prose.
REACHABLE = "reachable"
BLOCKED = "blocked"
LOGIN_WALL = "login_wall"
BINARY_DOCUMENT = "binary_document"
TOO_THIN = "too_thin"
TRANSPORT_ERROR = "transport_error"

#: Obstacles a label may carry that the scorer deliberately never predicts.
UNSCORED_OBSTACLES = frozenset({"not_a_posting"})


@dataclass
class Outcome:
    """What happened to one URL."""

    url: str
    reachable: bool
    obstacle: str
    tier: str
    desc_len: int = 0
    memoized_at_start: bool = False
    error: str = ""
    preview: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "reachable": self.reachable,
            "obstacle": self.obstacle,
            "tier": self.tier,
            "desc_len": self.desc_len,
            "memoized_at_start": self.memoized_at_start,
            "preview": self.preview,
            **({"error": self.error} if self.error else {}),
            **self.detail,
        }


def classify_text(text: str | None) -> tuple[bool, str]:
    """Decide whether ``text`` is usable posting text, and if not, why.

    Returns ``(reachable, obstacle)``. Pure text inspection, no network, no LLM.
    """
    if text is None:
        return False, TOO_THIN
    stripped = text.strip()
    if not stripped:
        return False, TOO_THIN
    if _looks_binary(stripped):
        return False, BINARY_DOCUMENT
    lower = stripped.lower()
    # A wall replaces the posting, so the text IS the wall. An incidental login
    # link inside a complete posting must not flip the verdict. Length is the
    # discriminator; see LOGIN_wall_MAX_CHARS.
    if is_login_wall(stripped):
        return False, LOGIN_WALL
    if any(marker in lower for marker in _WALL_MARKERS):
        return False, BLOCKED
    if len(stripped) < MIN_USABLE_CHARS:
        return False, TOO_THIN
    return True, REACHABLE


def _looks_binary(text: str) -> bool:
    """True for a binary payload that arrived where text was expected.

    The DuckDuckGo privacy-PDF row in the live DB is the case this exists for:
    82KB of ``%PDF-1.7`` bytes was stored as a job description because it was
    long enough to clear every length check.
    """
    if text.startswith(_BINARY_PREFIXES):
        return True
    # Control characters other than tab/newline/carriage-return, which is what
    # compressed PDF/PNG streams look like once decoded as latin-1.
    control = sum(1 for ch in text[:2000] if ord(ch) < 32 and ch not in "\t\n\r")
    return control / max(len(text[:2000]), 1) > 0.01


def _host(url: str) -> str:
    match = re.match(r"^[a-z]+://([^/?#]+)", url, re.IGNORECASE)
    return match.group(1).lower() if match else ""


def measure(url: str, client: PoliteClient, *, preview_chars: int = 140) -> Outcome:
    """Run one URL through the production posting-detail path and classify it.

    Calls the production ``fetch_detail`` exactly as the scrape pipeline does,
    so the number describes the product rather than a benchmark-only code path.
    Since the 4.1d defect-1 fix, ``fetch_detail`` propagates transport failures
    instead of collapsing them into None, so catching here is catching the
    production exception — the benchmark and production now share the entire
    detail path. The tier is read back off the client afterwards:
    ``PoliteClient`` escalates internally and does not report which tier
    answered.
    """
    host = _host(url)
    memoized = host in getattr(client, "_needs_browser", set())

    tier = "render" if memoized else "simple"
    try:
        text = fetch_detail(client, url)
    except Exception as exc:  # noqa: BLE001 - a transport failure is a data point
        return Outcome(
            url=url,
            reachable=False,
            obstacle=TRANSPORT_ERROR,
            tier=tier,
            memoized_at_start=memoized,
            error=f"{type(exc).__name__}: {exc}",
        )

    # PoliteClient escalates internally without reporting which tier answered;
    # a host landing in _needs_browser during the fetch is the observable signal.
    if host in getattr(client, "_needs_browser", set()):
        tier = "render"

    # Extraction failure is unambiguously "we got a page, it had no posting".
    reachable, obstacle = classify_text(text)
    return Outcome(
        url=url,
        reachable=reachable,
        obstacle=obstacle,
        tier=tier,
        desc_len=len(text) if text else 0,
        memoized_at_start=memoized,
        preview=(text or "")[:preview_chars].replace("\n", " "),
    )
