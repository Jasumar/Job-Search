from __future__ import annotations

import json
import threading
import time
from collections import OrderedDict, defaultdict
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from curl_cffi.requests.exceptions import RequestException

from jobsearch_core.config import Settings
from jobsearch_core.ingest.xhr_capture import XhrCapture, build_capture
from jobsearch_core.log import get_logger

# Playwright is an optional extra (`jobsearch_core[browser]`). Import at module
# top, never inside the render path: that path runs on the job-run-worker thread
# (AGENTS.md §Threads and imports). A missing install is reported at render time.
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

logger = get_logger(__name__)

_DEFAULT_TIMEOUT = 30.0
_DEFAULT_MIN_INTERVAL = 2.0
_DEFAULT_MAX_RETRIES = 3
_DEFAULT_BACKOFF_BASE = 1.0
_DEFAULT_IMPERSONATE = "chrome"
# Pages the per-client fetch memo keeps (RF7). Bounded because listing pages can
# be large; detail fetches never enter the memo.
_FETCH_MEMO_SIZE = 32

# Tokens found in proxy/WAF challenge bodies (script URLs, page titles). Checked
# against the raw response body so challenge pages are caught before parsing.
_RAW_CHALLENGE_MARKERS = (
    "cf-chl",
    "cf_chl_opt",
    "challenge-platform",
    "cf-browser-verification",
    "checkcookie",
    "_incapsula_resource",
    "ddos-guard",
    "just a moment",
    "checking your browser",
    "attention required",
)

# Tokens only counted once scripts/styles are stripped, so a page that merely
# mentions JavaScript inside <noscript> is not falsely rejected.
_VISIBLE_CHALLENGE_MARKERS = (
    "enable javascript and cookies",
    "verify you are human",
    "verify that you are not a robot",
    "are you a robot",
)

_MIN_PLAIN_LENGTH = 200
_MIN_VISIBLE_TEXT = 100
_MAX_SCROLL_SECONDS = 15.0


def _is_usable_content(text: str | None) -> bool:
    """Decide whether a response body is real page/API content.

    Challenge pages, WAF block pages, and empty or text-free bodies are
    unusable. JSON payloads are always usable when non-empty so ATS API
    endpoints are never misclassified. The heuristic is what triggers
    Playwright escalation, so it runs on every tier's result.
    """
    if not text or not text.strip():
        return False
    stripped = text.strip()
    lower = stripped.lower()
    if any(marker in lower for marker in _RAW_CHALLENGE_MARKERS):
        return False
    if stripped[0] in "{[":
        return True
    if len(stripped) < _MIN_PLAIN_LENGTH:
        return False
    if "<html" in lower or "<!doctype" in lower or "<body" in lower:
        soup = BeautifulSoup(stripped, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        visible = soup.get_text(" ", strip=True)
        if len(visible) < _MIN_VISIBLE_TEXT:
            return False
        visible_lower = visible.lower()
        return not any(marker in visible_lower for marker in _VISIBLE_CHALLENGE_MARKERS)
    return True


class FetchError(RuntimeError):
    """A fetch failed, carrying the facts needed to say *which tier* refused it.

    Callers that only need the message keep using it as a ``RuntimeError``; the
    structured fields exist so a replay failure can be attributed to a status, a
    content type, a wrong final URL, or the wrong tier instead of surfacing as a
    bare parse error (2026-10-25: a JSON endpoint memoized browser-required made
    every api-recipe trial die inside ``json.loads``).
    """

    def __init__(
        self,
        message: str,
        *,
        url: str = "",
        tier: str = "static",
        status: int | None = None,
        content_type: str | None = None,
        final_url: str | None = None,
        body_head: str | None = None,
    ) -> None:
        self.url = url
        self.tier = tier
        self.status = status
        self.content_type = content_type
        self.final_url = final_url
        self.body_head = body_head
        super().__init__(f"{message} ({self.facts()})")

    def facts(self) -> str:
        parts = [f"tier={self.tier}"]
        if self.status is not None:
            parts.append(f"status={self.status}")
        if self.content_type:
            parts.append(f"content-type={self.content_type}")
        if self.final_url and self.final_url != self.url:
            parts.append(f"final_url={self.final_url}")
        if self.body_head:
            parts.append(f"body={self.body_head!r}")
        return ", ".join(parts)


def _body_head(text: str | None) -> str | None:
    """A short, single-line prefix of the body for error messages."""
    if not text:
        return None
    collapsed = " ".join(text.split())
    return collapsed[:120] + ("…" if len(collapsed) > 120 else "")


def _response_meta(response: Any, url: str) -> dict[str, Any]:
    """Status/content-type/final-URL facts from a transport response."""
    headers = getattr(response, "headers", None) or {}
    content_type = None
    try:
        content_type = headers.get("content-type") or headers.get("Content-Type")
    except AttributeError:
        content_type = None
    return {
        "status": getattr(response, "status_code", None),
        "content_type": str(content_type) if content_type else None,
        "final_url": str(getattr(response, "url", "") or url),
    }


class PoliteClient:
    """Polite fetcher with a browser-grade base transport.

    Every request is sent through curl_cffi impersonating a real Chrome
    TLS/HTTP2 fingerprint, which defeats TLS-fingerprint bot checks on both
    HTML pages and JSON endpoints. When a response is unusable (hard 403 or a
    body failing the meaningful-content heuristic on any status), the fetch
    escalates once to a headless Chromium render (Playwright) that shares the
    simple tier's cookies. Transient failures (timeouts, connection errors,
    429, 5xx) stay on the per-host rate-limited retry/backoff loop and never
    escalate. No CAPTCHA solving is attempted.

    The ``host -> needs browser`` memo is in-memory and per-client only: a host
    whose simple-tier fetch failed once skips the simple tier on later fetches.

    ``fetch`` and ``fetch_rendered`` also keep a small per-client page memo keyed
    by (tier, URL), so one run reads a page once per tier (RF7). A client lives
    for one run. Failures are not memoized, and detail fetches
    (``fetch_static_first``) bypass the memo.
    """

    def __init__(
        self,
        user_agent: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
        min_interval: float = _DEFAULT_MIN_INTERVAL,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        backoff_base: float = _DEFAULT_BACKOFF_BASE,
        impersonate: str = _DEFAULT_IMPERSONATE,
        browser_channel: str | None = None,
    ):
        self._session = self._create_session(impersonate, user_agent)
        self._timeout = timeout
        self._min_interval = min_interval
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._impersonate = impersonate
        self._requested_browser_channel = browser_channel
        self._last_request: dict[str, float] = defaultdict(float)
        self._needs_browser: set[str] = set()
        self._memo: OrderedDict[tuple[str, str], str] = OrderedDict()
        self._memo_lock = threading.Lock()
        self._playwright: Any = None
        self._browser: Any = None

    @staticmethod
    def _create_session(impersonate: str, user_agent: str | None) -> Any:
        # Lazy import: keeps module import cheap and testable without the transport.
        from curl_cffi import requests as curl_requests

        kwargs: dict[str, Any] = {"impersonate": impersonate, "allow_redirects": True}
        if user_agent:
            kwargs["headers"] = {"User-Agent": user_agent}
        return curl_requests.Session(**kwargs)

    def _wait_for_host(self, host: str) -> None:
        elapsed = time.monotonic() - self._last_request[host]
        if elapsed < self._min_interval:
            wait = self._min_interval - elapsed
            logger.debug("Rate-limiting %s: sleeping %.2fs", host, wait)
            time.sleep(wait)
        self._last_request[host] = time.monotonic()

    def _memo_get(self, tier: str, url: str) -> str | None:
        with self._memo_lock:
            body = self._memo.get((tier, url))
            if body is not None:
                self._memo.move_to_end((tier, url))
            return body

    def _memo_put(self, tier: str, url: str, body: str) -> None:
        with self._memo_lock:
            self._memo[(tier, url)] = body
            self._memo.move_to_end((tier, url))
            while len(self._memo) > _FETCH_MEMO_SIZE:
                self._memo.popitem(last=False)

    def fetch(self, url: str, **kwargs: Any) -> str:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise RuntimeError(f"Invalid URL for fetching: {url}")
        host = parsed.netloc
        # A call with extra request options is a different request; skip the memo.
        memoize = not kwargs
        if memoize:
            cached = self._memo_get("static", url)
            if cached is not None:
                return cached
        if host in self._needs_browser:
            logger.debug("Host %s is memoized browser-required; rendering directly", host)
            body = self._fetch_via_browser(url)
        else:
            result = self._simple_tier("GET", url, host, escalate=True, **kwargs)
            body = self._fetch_via_browser(url) if result is None else result
        if memoize:
            self._memo_put("static", url, body)
        return body

    def fetch_static_first(self, url: str, **kwargs: Any) -> str:
        """Like ``fetch``, but ignores the host's browser memo.

        A listing page can need a render while the host's detail pages are
        plain server HTML. The memo is a listing fact, so a detail fetch tries
        the plain tier first and renders only when that tier is unusable (RF6).
        A plain tier that fails still memoizes the host, as in ``fetch``.
        """
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise RuntimeError(f"Invalid URL for fetching: {url}")
        result = self._simple_tier(
            "GET", url, parsed.netloc, escalate=True, **kwargs
        )
        if result is None:
            return self._fetch_via_browser(url)
        return result

    def fetch_rendered(self, url: str) -> str:
        """Fetch ``url`` through the headless-browser tier unconditionally.

        ``fetch`` only renders when the plain tier looks *broken* (403, WAF
        challenge, empty body). A client-rendered page is not broken: it
        answers 200 with perfectly usable HTML that simply does not contain the
        data yet. Callers that can tell the data is missing — e.g. list mining,
        which knows whether the page holds any company names — use this to ask
        for the rendered DOM instead of re-fetching and re-inspecting.
        """
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise RuntimeError(f"Invalid URL for fetching: {url}")
        # Remember the host so later plain fetches skip a tier that already
        # proved insufficient for this site.
        self._needs_browser.add(parsed.netloc)
        cached = self._memo_get("rendered", url)
        if cached is not None:
            return cached
        body = self._fetch_via_browser(url)
        self._memo_put("rendered", url, body)
        return body

    def fetch_rendered_with_xhr(self, url: str) -> tuple[str, list[XhrCapture]]:
        """Like ``fetch_rendered``, also returning the page's JSON/XHR responses.

        Observation only: the captures are the responses the rendered page
        already received. Nothing is requested in addition to the page load.
        """
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise RuntimeError(f"Invalid URL for fetching: {url}")
        self._needs_browser.add(parsed.netloc)
        captures: list[XhrCapture] = []
        html = self._fetch_via_browser(url, captures)
        # The captures cannot be replayed from the memo, but the DOM can.
        self._memo_put("rendered", url, html)
        return html, captures

    def fetch_json(self, url: str) -> str:
        """Fetch a URL whose response must be JSON; the browser tier is never used.

        Rendering cannot satisfy a JSON caller: page.content() wraps the body in
        an HTML document, so a rendered answer is always the wrong artifact. Instead
        of escalating and letting the caller's ``json.loads`` fail with a syntax
        error, this asks only the TLS tier and reports what that tier actually got.
        """
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise RuntimeError(f"Invalid URL for fetching: {url}")
        meta: dict[str, Any] = {}
        body = self._simple_tier(
            "GET", url, parsed.netloc, escalate=False, meta=meta
        )
        if body is None:  # pragma: no cover - escalate=False never returns None
            raise RuntimeError(f"No JSON body for {url}")
        try:
            json.loads(body)
        except ValueError as exc:
            raise FetchError(
                f"JSON request to {url} got a non-JSON body",
                url=url,
                tier="static",
                status=meta.get("status"),
                content_type=meta.get("content_type"),
                final_url=meta.get("final_url"),
                body_head=_body_head(body),
            ) from exc
        return body

    def _simple_tier(
        self,
        method: str,
        url: str,
        host: str,
        *,
        escalate: bool,
        meta: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> str | None:
        """Run the retry/backoff loop on the TLS-impersonated client.

        Returns the body text on success, or None when the caller should
        escalate to the browser tier. Raises ``FetchError`` when escalation is
        not allowed or the retries are exhausted. ``meta``, when given, receives
        the last response's status/content-type/final URL so a caller can report
        what the tier actually returned.
        """
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                self._wait_for_host(host)
                logger.debug("Fetching %s (attempt %d/%d)", url, attempt + 1, self._max_retries + 1)
                response = self._session.request(
                    method, url, timeout=self._timeout, **kwargs
                )
                status = response.status_code
            except RequestException as exc:
                last_exc = exc
                logger.warning("Attempt %d failed for %s: %s", attempt + 1, url, exc)
                if attempt < self._max_retries:
                    wait = self._backoff_base * (2**attempt)
                    logger.info("Backing off %.1fs before retry", wait)
                    time.sleep(wait)
                continue

            logger.debug("Fetched %s — %s", url, status)
            facts = _response_meta(response, url)
            if meta is not None:
                meta.update(facts, body=_body_head(response.text))
            if status == 403:
                logger.info("HTTP 403 for %s — escalating to browser tier", url)
                if escalate:
                    self._needs_browser.add(host)
                    return None
                raise FetchError(f"HTTP 403 for {url}", url=url, **facts)
            if status == 429 or 500 <= status < 600:
                last_exc = FetchError(f"HTTP {status} for {url}", url=url, **facts)
                if attempt < self._max_retries:
                    wait = self._backoff_base * (2**attempt)
                    logger.info("Backing off %.1fs before retry (HTTP %d)", wait, status)
                    time.sleep(wait)
                continue
            if 400 <= status < 500:
                raise FetchError(f"HTTP {status} for {url}", url=url, **facts)
            text = response.text
            if _is_usable_content(text):
                return text
            logger.info(
                "Unusable content from %s (HTTP %d) — escalating to browser tier", url, status
            )
            if escalate:
                self._needs_browser.add(host)
                return None
            raise FetchError(
                f"Unusable content from {url}",
                url=url,
                body_head=_body_head(text),
                **facts,
            )

        raise FetchError(
            f"Failed to fetch {url} after {self._max_retries + 1} attempts",
            url=url,
            status=getattr(last_exc, "status", None),
            content_type=getattr(last_exc, "content_type", None),
            final_url=getattr(last_exc, "final_url", None),
            body_head=getattr(last_exc, "body_head", None),
        ) from last_exc

    def _fetch_via_browser(self, url: str, captures: list[XhrCapture] | None = None) -> str:
        try:
            text = self._render_with_browser(url) if captures is None else self._render_with_browser(url, captures)
        except RuntimeError:
            raise
        except Exception as exc:
            raise FetchError(
                f"Browser fetch failed for {url}: {exc}", url=url, tier="rendered"
            ) from exc
        if not _is_usable_content(text):
            raise FetchError(
                f"Unusable content from {url} after browser render",
                url=url,
                tier="rendered",
                body_head=_body_head(text),
            )
        return text

    def _render_with_browser(self, url: str, captures: list[XhrCapture] | None = None) -> str:
        if sync_playwright is None:
            raise RuntimeError(
                "Playwright is required for browser-grade fetching. "
                "Install it with: pip install 'jobsearch_core[browser]' && playwright install chromium"
            )

        browser = self._get_browser(sync_playwright)
        context = browser.new_context()
        try:
            self._push_cookies_to_context(context, url)
            page = context.new_page()
            if captures is not None:
                page.on("response", lambda response: _record_response(response, captures))
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=self._timeout * 1000)
                self._scroll_page(page)
                text = page.content()
                self._pull_cookies_from_context(context, url)
                return text
            finally:
                page.close()
        finally:
            context.close()

    def _get_browser(self, sync_playwright: Any) -> Any:
        if self._browser is None:
            channel = self._requested_browser_channel or self._resolve_browser_channel()
            logger.info("Starting headless Chromium (channel=%s)", channel)
            self._playwright = sync_playwright().start()
            launch_kwargs: dict[str, Any] = {"headless": True}
            if channel and channel != "chromium":
                launch_kwargs["channel"] = channel
            self._browser = self._playwright.chromium.launch(**launch_kwargs)
        return self._browser

    @staticmethod
    def _resolve_browser_channel() -> str:
        return Settings().browser_channel

    def _scroll_page(self, page: Any) -> None:
        """Scroll through the page so lazy-loaded listings load, bounded in time."""
        deadline = time.monotonic() + _MAX_SCROLL_SECONDS
        last_height = -1
        stable_rounds = 0
        while time.monotonic() < deadline:
            height = page.evaluate("document.body ? document.body.scrollHeight : 0")
            if height == last_height:
                stable_rounds += 1
                if stable_rounds >= 2:
                    break
            else:
                stable_rounds = 0
            last_height = height
            page.evaluate("window.scrollBy(0, window.innerHeight * 2)")
            page.wait_for_timeout(400)
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            logger.debug("networkidle not reached for %s; continuing", page.url)

    def _push_cookies_to_context(self, context: Any, url: str) -> None:
        """Best-effort cookie share so a list-page session carries into the render."""
        try:
            host = urlparse(url).netloc
            cookies: list[dict[str, str]] = []
            jar = getattr(self._session.cookies, "jar", None)
            if jar is not None:
                for cookie in jar:
                    domain = (cookie.domain or "").lstrip(".")
                    if not cookie.value or not domain or not host.endswith(domain):
                        continue
                    cookies.append({"name": cookie.name, "value": cookie.value, "url": url})
            else:
                cookies = [
                    {"name": name, "value": value, "url": url}
                    for name, value in self._session.cookies.items()
                ]
            if cookies:
                context.add_cookies(cookies)
        except Exception as exc:
            logger.debug("Cookie push to browser skipped: %s", exc)

    def _pull_cookies_from_context(self, context: Any, url: str) -> None:
        """Persist the browser session's cookies back into the simple tier."""
        try:
            host = urlparse(url).netloc
            for cookie in context.cookies(url):
                self._session.cookies.set(
                    cookie["name"],
                    cookie["value"],
                    domain=cookie.get("domain") or host,
                    path=cookie.get("path") or "/",
                )
        except Exception as exc:
            logger.debug("Cookie pull from browser skipped: %s", exc)

    def reset_browser(self) -> None:
        """Drop the browser and its Playwright driver, keeping the HTTP session.

        A driver that wedged mid-render (Playwright occasionally fails a sync call
        with 'Sync API inside the asyncio loop' after a heavy page) stays wedged for
        every later render, so recovery means relaunching it, not retrying in place.
        """
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                logger.debug("Browser close failed", exc_info=True)
            self._browser = None
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                logger.debug("Playwright stop failed", exc_info=True)
            self._playwright = None

    def close(self) -> None:
        self.reset_browser()
        self._session.close()

    def __enter__(self) -> PoliteClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


def _record_response(response: Any, captures: list[XhrCapture]) -> None:
    """Playwright response listener: append a capture for JSON/XHR responses.

    Any failure to read one response is logged and skipped; it must never break
    the page render that the listener observes.
    """
    try:
        request = response.request
        capture = build_capture(
            url=response.url,
            method=request.method,
            status=response.status,
            resource_type=request.resource_type,
            response_headers=response.headers,
            request_headers=request.headers,
            request_body=request.post_data,
            read_body=lambda: response.body(),
        )
    except Exception as exc:  # noqa: BLE001 — observation must not break the render
        logger.debug("XHR capture skipped for %s: %s", getattr(response, "url", "?"), exc)
        return
    if capture is not None:
        captures.append(capture)

