"""Replay check for XHR captures (Phase 4.3 S2, plan-scraping.md §XHR capture).

Pure decision logic plus one plain HTTP request. A capture is a class-A candidate
only when, replayed with no browser and no captured cookies or tokens, it returns
a job list whose postings all appear in the rendered DOM and whose sample titles
match it. The check decides class A; the capture never does.

Subset rule (owner decision 2026-10-14): every replayed posting URL must appear in
the rendered DOM's postings, and the replayed sample titles must match the DOM's
titles for the same URLs. The API page size need not equal the DOM count.

Writes nothing. Failure reasons are returned, never discarded.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from jobsearch_core.ingest.http import PoliteClient
from jobsearch_core.ingest.scrapers.base import ScrapedJob
from jobsearch_core.ingest.xhr_capture import XhrCapture
from jobsearch_core.log import get_logger

logger = get_logger(__name__)

# Keys a JSON object must carry to count as a posting: a title-like and a URL-like
# field, either by name or by a value that is an absolute or site-relative URL.
_TITLE_KEYS = ("title", "name", "jobTitle", "job_title", "text")
_URL_KEYS = ("url", "positionUrl", "absolute_url", "jobUrl", "job_url", "link", "applyUrl", "href")
_MAX_SAMPLE = 5


@dataclass(frozen=True)
class ReplayResult:
    # None = refused before any request was made (no replay was attempted).
    # False = a replay ran and did not verify (request failed, body not a job list,
    # or postings compared and not matching). True = verified.
    replay_matches: bool | None
    class_a_candidate: bool
    reason: str | None
    replayed_count: int = 0
    matched_count: int = 0
    sample_titles: tuple[str, ...] = field(default_factory=tuple)


def _as_text(value: object) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return None


def _find_postings(payload: object, base_url: str) -> list[ScrapedJob]:
    """Generic job-list finder: every dict in the JSON carrying title and URL fields.

    Walks the whole document, so the array's key name and nesting depth do not
    matter. Nodes are de-duplicated by URL in document order.
    """
    jobs: list[ScrapedJob] = []
    seen: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        title = next((t for t in (_as_text(node.get(k)) for k in _TITLE_KEYS) if t), None)
        raw_url = next((u for u in (_as_text(node.get(k)) for k in _URL_KEYS) if u), None)
        if title and raw_url:
            url = urljoin(base_url, raw_url)
            if url.startswith(("http://", "https://")) and url not in seen:
                seen.add(url)
                jobs.append(ScrapedJob(url=url, title=title, company=""))
        for value in node.values():
            if isinstance(value, (dict, list)):
                walk(value)

    walk(payload)
    return jobs


def parse_job_list(body: str, base_url: str) -> list[ScrapedJob]:
    """Parse a JSON body into postings; empty list when it is not JSON."""
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        return []
    return _find_postings(payload, base_url)


def _title_matches(replayed: str, dom_title: str) -> bool:
    """True when the replayed title opens the DOM title (case-insensitive, whitespace-folded)."""
    norm = " ".join(replayed.split()).casefold()
    dom = " ".join(dom_title.split()).casefold()
    return bool(norm) and dom.startswith(norm)


def _same_site_host(capture_url: str, company_host: str) -> bool:
    host = (urlparse(capture_url).hostname or "").lower()
    base = company_host.lower()
    return host == base or host.endswith("." + base)


def replay_capture(
    capture: XhrCapture,
    *,
    dom_postings: list[ScrapedJob],
    company_host: str,
    fetch_plain: Callable[[str], str],
) -> ReplayResult:
    """Decide whether ``capture`` replays to the rendered DOM's postings.

    ``fetch_plain`` performs one plain GET with no cookies and no browser; it
    may raise. Any raise or non-JSON body fails the replay with a reason.
    Only GET captures are replayed: a POST body is not reproduced here.
    """
    # Refusals before any request: no replay ran, so replay_matches stays None.
    if capture.method.upper() != "GET":
        return _fail(f"method {capture.method} needs a request body; not replayed")
    if capture.status != 200:
        return _fail(f"capture status {capture.status}")
    if not _same_site_host(capture.url, company_host):
        return _fail(f"host {urlparse(capture.url).hostname} is outside the company domain")
    if capture.body_truncated:
        return _fail("capture body truncated; cannot compare a partial job list")

    try:
        body = fetch_plain(capture.url)
    except Exception as exc:  # noqa: BLE001 — every replay failure is recorded, not raised
        return _fail(f"replay request failed: {exc}", compared=True)

    replayed = parse_job_list(body, capture.url)
    if not replayed:
        return _fail("replay body is not a job list (non-JSON or no postings)", compared=True)

    # From here a replay ran; every failure below is a real False.
    dom_by_url = {job.url: job.title for job in dom_postings}
    matched = [job for job in replayed if job.url in dom_by_url]
    if not matched:
        return _fail(
            "no replayed posting appears in the rendered DOM",
            replayed_count=len(replayed),
            compared=True,
        )

    unmatched = [job for job in replayed if job.url not in dom_by_url]
    if unmatched:
        return _fail(
            f"{len(unmatched)} replayed posting(s) absent from the rendered DOM",
            replayed_count=len(replayed),
            matched_count=len(matched),
            compared=True,
        )

    # The rendered DOM appends location and job-ID text to the title, so the replayed
    # title must be the normalized prefix of the DOM title, not an exact match.
    title_mismatch = [
        job for job in matched if not _title_matches(job.title, dom_by_url[job.url])
    ]
    if title_mismatch:
        return _fail(
            f"{len(title_mismatch)} title(s) differ from the rendered DOM",
            replayed_count=len(replayed),
            matched_count=len(matched),
            compared=True,
        )

    return ReplayResult(
        replay_matches=True,
        class_a_candidate=True,
        reason=None,
        replayed_count=len(replayed),
        matched_count=len(matched),
        sample_titles=tuple(job.title for job in matched[:_MAX_SAMPLE]),
    )


def _fail(
    reason: str,
    *,
    replayed_count: int = 0,
    matched_count: int = 0,
    compared: bool = False,
) -> ReplayResult:
    # compared=True means a replay request was made; the verdict is False, not NULL.
    return ReplayResult(
        replay_matches=False if compared else None,
        class_a_candidate=False,
        reason=reason,
        replayed_count=replayed_count,
        matched_count=matched_count,
    )


class PlainFetcher:
    """Cookie-less, browser-free GETs that share one rate-limited ``PoliteClient``.

    The per-host interval lives on the client instance, so one fetcher keeps the
    polite spacing across a whole replay pass. Escalation to the browser is off:
    a replay that needs one is a failed replay. Use as a context manager or call
    ``close``.
    """

    def __init__(self) -> None:
        self._client = PoliteClient()

    def __call__(self, url: str) -> str:
        host = urlparse(url).netloc
        body = self._client._simple_tier("GET", url, host, escalate=False)  # noqa: SLF001
        if body is None:
            raise RuntimeError("simple tier requested escalation")
        return body

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> PlainFetcher:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


__all__: list[str] = ["PlainFetcher", "ReplayResult", "parse_job_list", "replay_capture"]
