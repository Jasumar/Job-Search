"""XHR capture records for the browser tier (Phase 4.3, plan-scraping.md §XHR capture).

Observation only: the rendered page's own requests are the only requests made.
This module turns a Playwright response into a plain ``XhrCapture`` value. It
does no network I/O and imports no browser library, so it is unit-testable.

Rules (plan-scraping.md §XHR capture):
  * record only resource type ``fetch``/``xhr`` or a JSON content type;
  * ``Cookie`` and ``Authorization`` are dropped before storage, never stored;
  * the response body is kept up to ``BODY_CAP_BYTES``; ``response_bytes`` is
    the full size, and ``body_truncated`` says whether the sample is partial;
  * ``body_sha256`` is the hash of the *full* body, before the cap is
    applied (R1): two runs of the same URL can be compared by body identity
    without storing the body.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass

BODY_CAP_BYTES = 256_000
_CAPTURE_RESOURCE_TYPES = frozenset({"fetch", "xhr"})
_REDACTED_HEADERS = frozenset({"cookie", "authorization"})


@dataclass(frozen=True)
class XhrCapture:
    url: str
    method: str
    status: int
    resource_type: str
    content_type: str
    request_headers: tuple[tuple[str, str], ...]
    request_body: str | None
    response_body: str | None
    response_bytes: int
    body_truncated: bool
    # SHA-256 of the full response bytes (before BODY_CAP_BYTES truncation).
    # Empty only for a capture built by hand (tests); ``build_capture`` sets it.
    body_sha256: str = ""


def is_capture_candidate(resource_type: str, content_type: str) -> bool:
    """True for a fetch/xhr request, or any response declaring a JSON body."""
    if resource_type in _CAPTURE_RESOURCE_TYPES:
        return True
    return "json" in content_type.lower()


def redact_headers(headers: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    """Request headers as sorted (name, value) pairs, minus credential headers."""
    return tuple(
        sorted(
            (name.lower(), value)
            for name, value in headers.items()
            if name.lower() not in _REDACTED_HEADERS
        )
    )


def build_capture(
    *,
    url: str,
    method: str,
    status: int,
    resource_type: str,
    response_headers: Mapping[str, str],
    request_headers: Mapping[str, str],
    request_body: str | None,
    read_body: Callable[[], bytes],
) -> XhrCapture | None:
    """Build a capture, or None when the response is not a capture candidate.

    ``read_body`` is called only for candidates, so a page's images and scripts
    are never read into memory.
    """
    content_type = response_headers.get("content-type", "")
    if not is_capture_candidate(resource_type, content_type):
        return None
    body = read_body()
    truncated = len(body) > BODY_CAP_BYTES
    sample = body[:BODY_CAP_BYTES].decode("utf-8", errors="replace")
    return XhrCapture(
        body_sha256=hashlib.sha256(body).hexdigest(),
        url=url,
        method=method,
        status=status,
        resource_type=resource_type,
        content_type=content_type,
        request_headers=redact_headers(request_headers),
        request_body=request_body,
        response_body=sample,
        response_bytes=len(body),
        body_truncated=truncated,
    )
