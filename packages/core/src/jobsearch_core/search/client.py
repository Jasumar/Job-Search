from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from subprocess import CalledProcessError
from typing import Protocol

import httpx

from jobsearch_core.config import Settings
from jobsearch_core.log import get_logger

try:  # optional dependency: only the ddgs backend needs it
    from ddgs import DDGS
except ImportError:  # pragma: no cover - exercised only without the dev extra
    DDGS = None  # type: ignore[assignment]

logger = get_logger(__name__)


@dataclass
class SearchResult:
    url: str
    title: str
    snippet: str = ""


#: A search produced no answer at all. Two causes, kept apart so a log line and
#: a row read differently: no engine answered (block, throttle, soft-error page)
#: and every engine answered but nothing matched.
SEARCH_FAILURE_NO_ENGINE = "no engine answered"
SEARCH_FAILURE_NOTHING_MATCHED = "nothing matched"


class SearchError(RuntimeError):
    """The query itself failed; it is not evidence about a company.

    Raised by a client that cannot promise its empty list is a real answer.
    ``reason`` names which failure (:data:`SEARCH_FAILURE_NO_ENGINE` or
    :data:`SEARCH_FAILURE_NOTHING_MATCHED`), and the message carries the query,
    so a caller can put both on the row that hit it (master_plan P4).
    """

    def __init__(self, query: str, reason: str, detail: str = "") -> None:
        self.query = query
        self.reason = reason
        self.detail = detail
        message = f"search failed ({reason}) for {query!r}"
        super().__init__(f"{message}: {detail}" if detail else message)


#: The backend itself was unreachable or errored for this query (retries spent).
SEARCH_FAILURE_BACKEND = "backend unavailable"


class SearchRefusedError(RuntimeError):
    """The backend refused every request, not just this query.

    A dead key or spent quota fails identically for the next company, so a run
    pauses on it at once with the remaining rows still queued (master_plan RF4).
    Deliberately not a :class:`SearchError`: that one counts toward a streak.
    """


class SearchClient(Protocol):
    def search(self, query: str, num_results: int = 10) -> list[SearchResult]:
        ...

    def close(self) -> None:
        ...


class SearXNGSearchClient:
    def __init__(self, base_url: str = "http://localhost:8888"):
        self._client = httpx.Client(base_url=base_url, timeout=15.0)

    def search(self, query: str, num_results: int = 10) -> list[SearchResult]:
        try:
            resp = self._client.get(
                "/search",
                params={"q": query, "format": "json", "number_of_results": num_results},
            )
            resp.raise_for_status()
            data = resp.json()
        except (httpx.RequestError, ValueError, KeyError) as exc:
            logger.warning("SearXNG search failed for %r: %s", query, exc)
            raise SearchError(query, SEARCH_FAILURE_NO_ENGINE, f"{type(exc).__name__}: {exc}") from exc

        results: list[SearchResult] = []
        for item in data.get("results", [])[:num_results]:
            results.append(
                SearchResult(
                    url=item.get("url", ""),
                    title=item.get("title", ""),
                    snippet=item.get("content", ""),
                )
            )
        if not results and data.get("unresponsive_engines"):
            # Same rule as ddgs (master_plan P4): an empty aggregate over engines
            # that did not answer is a search failure, not a company miss.
            engines = ", ".join(
                str(engine[0] if isinstance(engine, list) and engine else engine)
                for engine in data["unresponsive_engines"]
            )
            raise SearchError(query, SEARCH_FAILURE_NO_ENGINE, f"unresponsive engines: {engines}")
        return results

    def health(self) -> bool:
        try:
            resp = self._client.get("/healthz")
            return resp.status_code == 200 and resp.text.strip() == "OK"
        except Exception:
            return False

    def close(self) -> None:
        self._client.close()


def _find_repo_root() -> Path:
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        if (ancestor / "docker" / "searxng" / "docker-compose.yml").exists():
            return ancestor
    raise FileNotFoundError("Cannot locate repo root (no docker/searxng/docker-compose.yml found)")


def _compose_cmd() -> list[str]:
    compose_file = _find_repo_root() / "docker" / "searxng" / "docker-compose.yml"
    return ["docker", "compose", "-f", str(compose_file)]


def _ensure_searxng(
    client: SearXNGSearchClient,
    max_wait: float = 20.0,
    poll_interval: float = 1.0,
) -> None:
    if client.health():
        return

    cmd = [*_compose_cmd(), "up", "-d"]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Could not start SearXNG: {exc}. Is Docker installed and on PATH?"
        ) from exc
    except CalledProcessError as exc:
        raise RuntimeError(
            f"Could not start SearXNG:\n{exc.stderr or exc.stdout}"
        ) from exc

    elapsed = 0.0
    while elapsed < max_wait:
        time.sleep(poll_interval)
        elapsed += poll_interval
        if client.health():
            return

    raise RuntimeError(
        "SearXNG started but health check did not pass within "
        f"{max_wait}s. Try manually: {' '.join([*_compose_cmd(), 'up', '-d'])}"
    )


DDGS_TEXT_BACKENDS = "brave,mojeek,duckduckgo,yahoo"
#: Above the library default of 5 s: ``ddgs.py:198`` discards the in-flight
#: engine's results once that deadline passes, so a slow-but-successful engine
#: reads as an empty answer.
DDGS_TIMEOUT_SECONDS = 15.0
#: One retry after this pause, because the soft-error page clears within seconds.
DDGS_RETRY_PAUSE_SECONDS = 3.0
DDGS_MAX_ATTEMPTS = 2
#: ddgs's own message for "every engine answered and nothing matched". Any other
#: exception text means an engine was blocked or served a soft-error page.
_DDGS_EMPTY_MESSAGE = "No results found."

BRAVE_SEARCH_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
BRAVE_MAX_COUNT = 20
# 429 is the plan's rate limit and 5xx is Brave's side; both are worth one more
# try after a short pause. 401/402/403 are answered, not retried: the key or its
# quota is the problem and repeating the call wastes the run.
_BRAVE_RETRYABLE = frozenset({429, 500, 502, 503, 504})
_BRAVE_REFUSED = frozenset({401, 402, 403})
#: Ceiling for a Retry-After pause. A stuck run is worse than a lost sweep.
_BRAVE_MAX_PAUSE_SECONDS = 30.0


def _retry_after_seconds(resp: httpx.Response) -> float:
    """Seconds Brave asked us to wait, or 0 when it did not say."""
    raw = resp.headers.get("retry-after", "").strip()
    try:
        return max(0.0, min(float(raw), _BRAVE_MAX_PAUSE_SECONDS))
    except ValueError:
        return 0.0


class BraveSearchClient:
    """Paid Brave Search API backend (owner decision 2026-11-09).

    Why this backend replaces ddgs: the free engine set answers 429/403/202
    challenge pages from the dev machine and ddgs collapses every one of them
    into "No results found.", so a blocked sweep reads like a country with no
    careers pages (master_plan P4). Brave returns a real status code and — the
    requirement that rules out plain-text/AI search APIs — parses the operator
    syntax the query builders emit (``site:``, quoted phrases, ``OR``,
    ``-term``). A backend that treated those as ordinary words would degrade
    every query silently instead of failing.

    The failure contract differs from :class:`DdgsSearchClient` on purpose: a
    200 with no web results is an empty answer (``[]``), while a missing key, a
    refused key, spent quota or an unreachable endpoint raises. Returning ``[]``
    for those would reproduce the bug this backend exists to fix.
    """

    def __init__(self, api_key: str | None = None, *, timeout: float = 15.0, max_retries: int = 1) -> None:
        key = api_key if api_key is not None else Settings().brave_search_key
        if not key:
            raise RuntimeError(
                "BRAVE_SEARCH_KEY is not set; the brave search backend needs the key in .env"
            )
        self._max_retries = max_retries
        self._client = httpx.Client(
            timeout=timeout,
            headers={"Accept": "application/json", "X-Subscription-Token": key},
        )

    def search(self, query: str, num_results: int = 10) -> list[SearchResult]:
        params = {
            "q": query,
            "count": max(1, min(num_results, BRAVE_MAX_COUNT)),
            # Snippets arrive clean; decoration would wrap matches in <strong>.
            "text_decorations": "false",
        }
        last_error = "no attempt made"
        pause = 0.0
        for attempt in range(self._max_retries + 1):
            if attempt:
                # A 429 carries Retry-After; obey it when it is longer than the
                # plain attempt backoff, otherwise the retry lands inside the
                # same rate window and burns the second attempt too.
                time.sleep(max(float(attempt), pause))
            try:
                resp = self._client.get(BRAVE_SEARCH_ENDPOINT, params=params)
            except httpx.RequestError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                continue
            if resp.status_code in _BRAVE_REFUSED:
                raise SearchRefusedError(
                    f"Brave search refused the request (HTTP {resp.status_code}): "
                    f"{resp.text[:200]}. Check BRAVE_SEARCH_KEY and the plan's quota."
                )
            if resp.status_code in _BRAVE_RETRYABLE:
                last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                pause = _retry_after_seconds(resp)
                continue
            if resp.status_code >= 400:
                # 422 is Brave's answer to a malformed query and also to an
                # invalid subscription token; the body names which, so it is
                # surfaced rather than flattened to an httpx traceback.
                raise SearchError(
                    query,
                    SEARCH_FAILURE_BACKEND,
                    f"Brave search failed (HTTP {resp.status_code}): {resp.text[:300]}",
                )
            return self._parse(resp.json(), num_results)
        raise SearchError(
            query,
            SEARCH_FAILURE_BACKEND,
            f"Brave search failed after {self._max_retries + 1} attempt(s): {last_error}",
        )

    @staticmethod
    def _parse(payload: dict, num_results: int) -> list[SearchResult]:
        results: list[SearchResult] = []
        for item in (payload.get("web") or {}).get("results") or []:
            url = item.get("url") or ""
            if not url:
                continue
            results.append(
                SearchResult(
                    url=url,
                    title=item.get("title") or "",
                    snippet=item.get("description") or "",
                )
            )
            if len(results) >= num_results:
                break
        return results

    def close(self) -> None:
        self._client.close()


class DdgsSearchClient:
    """Zero-setup search backend pinning a resilient ddgs engine set.

    Constructs a new DDGS instance per attempt to avoid session issues. ddgs 9.x
    defaults to backend="auto", which fans every query out to all nine text
    engines (yandex, grokipedia, wikipedia, google, ...), re-introducing engine
    noise and 429s, so the set is pinned via DDGS_TEXT_BACKENDS.

    The failure contract (master_plan P4): a non-200 engine response is dropped
    silently (``base.py:65-70``) and a soft-error page is a 200 whose SERP does
    not match the xpath, so ddgs collapses a block, a throttle and a genuine
    empty into one exception (``ddgs.py:218-223``). An empty aggregate is
    therefore never returned as ``[]``: the query is retried once, and a still
    empty answer raises :class:`SearchError` with its reason and the query
    text. That keeps a blocked sweep from reading like a country with no
    careers pages.

    Rate-limits aggressively — use narrow (country + role) queries to avoid
    429 errors on full sweeps.
    Phase 4.3: an explicit inter-query delay (``min_interval_seconds``) keeps
    full discovery sweeps (up to 15 baseline queries + variants) under the
    engines' rate limits.
    """

    #: Minimum seconds between consecutive ddgs requests (Known Gap 9 politeness).
    min_interval_seconds: float = 2.0

    def __init__(
        self,
        min_interval_seconds: float | None = None,
        *,
        timeout: float = DDGS_TIMEOUT_SECONDS,
        max_attempts: int = DDGS_MAX_ATTEMPTS,
        retry_pause_seconds: float = DDGS_RETRY_PAUSE_SECONDS,
    ) -> None:
        if min_interval_seconds is not None:
            self.min_interval_seconds = min_interval_seconds
        self.timeout = timeout
        self.max_attempts = max(1, max_attempts)
        self.retry_pause_seconds = retry_pause_seconds
        self._last_request_monotonic: float | None = None

    def _wait_for_interval(self) -> None:
        now = time.monotonic()
        if self._last_request_monotonic is not None:
            elapsed = now - self._last_request_monotonic
            remaining = self.min_interval_seconds - elapsed
            if remaining > 0:
                time.sleep(remaining)
        self._last_request_monotonic = time.monotonic()

    def _search_once(self, query: str, num_results: int) -> tuple[list[dict], str]:
        """One ddgs call; returns the raw results and the failure detail.

        The detail is empty when no engine raised: ddgs then answered "no
        results", which it cannot tell apart from a soft-error page it did not
        parse.
        """
        if DDGS is None:  # pragma: no cover - install shape, not a query failure
            raise RuntimeError(
                "ddgs is not installed; the ddgs backend needs the dev extra "
                "(`uv run --extra dev`) — use the brave backend otherwise"
            )
        self._wait_for_interval()
        ddgs = DDGS(timeout=self.timeout)
        try:
            raw = list(ddgs.text(query, max_results=num_results, backend=DDGS_TEXT_BACKENDS))
        except Exception as exc:  # noqa: BLE001 - one attempt, classified below
            return [], f"{type(exc).__name__}: {exc}"
        return raw, ""

    def search(self, query: str, num_results: int = 10) -> list[SearchResult]:
        raw: list[dict] = []
        detail = ""
        for attempt in range(1, self.max_attempts + 1):
            if attempt > 1:
                time.sleep(self.retry_pause_seconds)
            raw, detail = self._search_once(query, num_results)
            if raw:
                break

        if not raw:
            reason = _ddgs_failure_reason(detail)
            logger.warning(
                "ddgs search %s for %r after %d attempt(s)%s",
                reason,
                query,
                self.max_attempts,
                f": {detail}" if detail else "",
            )
            raise SearchError(query, reason, detail)

        results: list[SearchResult] = []
        for item in raw[:num_results]:
            results.append(
                SearchResult(
                    url=item.get("href", ""),
                    title=item.get("title", ""),
                    snippet=item.get("body", ""),
                )
            )
        return results

    def close(self) -> None:
        """ddgs opens no long-lived client; present so the protocol is satisfied."""


def _ddgs_failure_reason(detail: str) -> str:
    """Which failure an empty aggregate is, as far as ddgs reports it.

    No detail (ddgs's own "No results found.") means the engines answered and
    nothing matched on this query; any other exception text is a block or a
    soft-error page.
    """
    if not detail or detail.endswith(_DDGS_EMPTY_MESSAGE):
        return SEARCH_FAILURE_NOTHING_MATCHED
    return SEARCH_FAILURE_NO_ENGINE


def build_search_client(backend: str, *, api_key: str | None = None) -> SearchClient:
    """The one place a backend name becomes a client.

    Each caller used to carry its own ``if backend == "searxng" … else ddgs``,
    so an unknown name silently ran ddgs. An unknown name is now an error.
    """
    if backend == "brave":
        return BraveSearchClient(api_key)
    if backend == "searxng":
        return SearXNGSearchClient()
    if backend == "ddgs":
        return DdgsSearchClient()
    raise ValueError(
        f"Unknown search backend: {backend!r} (expected 'brave', 'searxng' or 'ddgs')"
    )
