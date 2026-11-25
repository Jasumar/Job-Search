from __future__ import annotations

import pytest
from jobsearch_core.ingest.http import FetchError, PoliteClient, _is_usable_content

_MEANINGFUL_HTML = (
    "<html><head><title>Acme Careers</title></head><body>"
    "<h1>Open positions</h1><p>We are hiring backend engineers in Helsinki. "
    "Python, Django and cloud platforms are our daily tools.</p>"
    "<p>Join our platform team building the services our customers rely on "
    "every day, with modern tooling and a strong engineering culture.</p>"
    "</body></html>"
)


class FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        text: str = "",
        headers: dict[str, str] | None = None,
        url: str = "",
    ):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}
        self.url = url


class FakeCookie:
    def __init__(self, name: str, value: str, domain: str):
        self.name = name
        self.value = value
        self.domain = domain


class FakeJar:
    def __init__(self, cookies):
        self._cookies = cookies

    def __iter__(self):
        return iter(self._cookies)


class FakeCookies:
    def __init__(self, cookies=None):
        self.jar = FakeJar(cookies or [])
        self.stored: dict[tuple[str, str], str] = {}

    def set(self, name, value, domain="", path="/"):
        self.stored[(domain, name)] = value


class FakeSession:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.requests: list[dict] = []
        self.cookies = FakeCookies()
        self.closed = False

    def request(self, method, url, **kwargs):
        self.requests.append({"method": method, "url": url, **kwargs})
        if not self.responses:
            return FakeResponse(200, _MEANINGFUL_HTML)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.closed = True


class NeverSession(FakeSession):
    def request(self, method, url, **kwargs):
        raise AssertionError("simple tier must not be used for memoized hosts")


def _make_client(responses=None, **kwargs):
    client = PoliteClient(min_interval=0.0, backoff_base=0.0, **kwargs)
    session = FakeSession(responses)
    client._session = session
    return client, session


class TestPoliteClientLadder:
    def test_meaningful_200_returns_without_browser(self, monkeypatch):
        client, session = _make_client([FakeResponse(200, _MEANINGFUL_HTML)])

        def no_browser(url):
            raise AssertionError("browser tier must not run for meaningful content")

        monkeypatch.setattr(client, "_render_with_browser", no_browser)
        assert client.fetch("https://example.com/careers") == _MEANINGFUL_HTML
        assert len(session.requests) == 1
        assert "example.com" not in client._needs_browser

    def test_403_escalates_to_browser_and_memoizes_host(self, monkeypatch):
        client, session = _make_client([FakeResponse(403, "denied")])
        monkeypatch.setattr(client, "_render_with_browser", lambda url: _MEANINGFUL_HTML)

        assert client.fetch("https://example.com/careers") == _MEANINGFUL_HTML
        assert len(session.requests) == 1  # no simple-tier retry on a hard 403
        assert "example.com" in client._needs_browser

    def test_challenge_body_escalates_to_browser(self, monkeypatch):
        challenge = (
            "<html><head><title>Just a moment...</title></head><body>"
            "<script src='/cdn-cgi/challenge-platform/h/b/orchestrate'></script>"
            "</body></html>"
        )
        client, session = _make_client([FakeResponse(200, challenge)])
        monkeypatch.setattr(client, "_render_with_browser", lambda url: _MEANINGFUL_HTML)

        assert client.fetch("https://example.com/careers") == _MEANINGFUL_HTML
        assert len(session.requests) == 1
        assert "example.com" in client._needs_browser

    def test_transient_429_retries_without_escalation(self, monkeypatch):
        client, session = _make_client(
            [FakeResponse(429, "slow down"), FakeResponse(200, _MEANINGFUL_HTML)]
        )

        def no_browser(url):
            raise AssertionError("429 must stay on the retry loop")

        monkeypatch.setattr(client, "_render_with_browser", no_browser)
        assert client.fetch("https://example.com/careers") == _MEANINGFUL_HTML
        assert len(session.requests) == 2

    def test_persistent_5xx_raises_without_escalation(self, monkeypatch):
        responses = [FakeResponse(503, "unavailable")] * 4
        client, session = _make_client(responses, max_retries=3)

        def no_browser(url):
            raise AssertionError("5xx must stay on the retry loop")

        monkeypatch.setattr(client, "_render_with_browser", no_browser)
        with pytest.raises(RuntimeError, match="after 4 attempts"):
            client.fetch("https://example.com/careers")
        assert len(session.requests) == 4

    def test_other_4xx_raises_immediately(self, monkeypatch):
        client, session = _make_client([FakeResponse(404, "not found")])

        def no_browser(url):
            raise AssertionError("404 must not escalate to the browser")

        monkeypatch.setattr(client, "_render_with_browser", no_browser)
        with pytest.raises(RuntimeError, match="HTTP 404"):
            client.fetch("https://example.com/careers")
        assert len(session.requests) == 1

    def test_browser_failure_raises_runtime_error(self, monkeypatch):
        client, _ = _make_client([FakeResponse(403, "denied")])

        def broken_browser(url):
            raise Exception("playwright exploded")

        monkeypatch.setattr(client, "_render_with_browser", broken_browser)
        with pytest.raises(RuntimeError, match="Browser fetch failed"):
            client.fetch("https://example.com/careers")

    def test_browser_unusable_content_raises(self, monkeypatch):
        client, _ = _make_client([FakeResponse(403, "denied")])
        challenge = "<html><head><title>Just a moment...</title></head><body></body></html>"
        monkeypatch.setattr(client, "_render_with_browser", lambda url: challenge)

        with pytest.raises(RuntimeError, match="after browser render"):
            client.fetch("https://example.com/careers")

    def test_memoized_host_skips_simple_tier(self, monkeypatch):
        client, _ = _make_client()
        client._session = NeverSession()
        client._needs_browser.add("example.com")
        monkeypatch.setattr(client, "_render_with_browser", lambda url: _MEANINGFUL_HTML)

        assert client.fetch("https://example.com/careers") == _MEANINGFUL_HTML


class TestFetchStaticFirst:
    def test_memoized_host_still_tries_plain_tier(self, monkeypatch):
        client, session = _make_client([FakeResponse(200, _MEANINGFUL_HTML)])
        client._needs_browser.add("example.com")

        def no_browser(url):
            raise AssertionError("usable static detail page must not render")

        monkeypatch.setattr(client, "_render_with_browser", no_browser)
        assert client.fetch_static_first("https://example.com/jobs/1") == _MEANINGFUL_HTML
        assert len(session.requests) == 1
        assert "example.com" in client._needs_browser  # memo is left alone

    def test_unusable_plain_tier_renders(self, monkeypatch):
        client, session = _make_client([FakeResponse(403, "denied")])
        client._needs_browser.add("example.com")
        monkeypatch.setattr(client, "_render_with_browser", lambda url: _MEANINGFUL_HTML)

        assert client.fetch_static_first("https://example.com/jobs/1") == _MEANINGFUL_HTML
        assert len(session.requests) == 1


class TestFetchJson:
    JSON_HEADERS = {"content-type": "application/json; charset=utf-8"}

    def test_returns_body_text_for_json_response(self, monkeypatch):
        body = '{"results": [{"title": "Backend Engineer"}]}'
        client, session = _make_client([FakeResponse(200, body, self.JSON_HEADERS)])

        def no_browser(url):
            raise AssertionError("JSON fetch must never render")

        monkeypatch.setattr(client, "_render_with_browser", no_browser)
        assert client.fetch_json("https://example.com/api/jobs") == body
        assert len(session.requests) == 1

    def test_rides_through_a_host_memoized_as_browser_required(self, monkeypatch):
        body = '{"results": []}'
        client, _ = _make_client([FakeResponse(200, body, self.JSON_HEADERS)])
        client._needs_browser.add("example.com")

        def no_browser(url):
            raise AssertionError("JSON fetch must never render")

        monkeypatch.setattr(client, "_render_with_browser", no_browser)
        assert client.fetch_json("https://example.com/api/jobs") == body
        assert "example.com" in client._needs_browser  # memo is left alone

    def test_html_body_is_refused_with_tier_and_content_type(self):
        client, _ = _make_client(
            [FakeResponse(200, _MEANINGFUL_HTML, {"content-type": "text/html"})]
        )
        with pytest.raises(FetchError) as excinfo:
            client.fetch_json("https://example.com/api/jobs")
        message = str(excinfo.value)
        assert "non-JSON body" in message
        assert "tier=static" in message
        assert "content-type=text/html" in message
        assert excinfo.value.tier == "static"
        assert excinfo.value.status == 200

    def test_403_without_escalation_reports_status_and_does_not_memoize(self):
        client, session = _make_client(
            [FakeResponse(403, "denied", {"content-type": "text/html"})]
        )
        with pytest.raises(FetchError) as excinfo:
            client.fetch_json("https://example.com/api/jobs")
        assert excinfo.value.status == 403
        assert "HTTP 403" in str(excinfo.value)
        assert len(session.requests) == 1
        assert "example.com" not in client._needs_browser

    def test_rejects_invalid_url(self):
        client = PoliteClient()
        try:
            with pytest.raises(RuntimeError, match="Invalid URL"):
                client.fetch_json("mailto:jobs@example.com")
        finally:
            client.close()


class TestPoliteClientLifecycle:
    def test_close_tears_down_started_browser(self):
        client, session = _make_client()

        class FakeBrowser:
            closed = False

            def close(self):
                self.closed = True

        class FakePlaywright:
            stopped = False

            def stop(self):
                self.stopped = True

        browser, playwright = FakeBrowser(), FakePlaywright()
        client._browser = browser
        client._playwright = playwright
        client.close()
        assert browser.closed
        assert playwright.stopped
        assert session.closed
        assert client._browser is None
        assert client._playwright is None

    def test_close_without_browser_is_safe(self):
        client, session = _make_client()
        client.close()
        assert session.closed

    @pytest.mark.parametrize(
        "url",
        [
            "/info@corp.example",
            "info@corp.example",
            "mailto:info@corp.example",
            "ftp://files.example/jobs/1",
            "",
        ],
    )
    def test_invalid_urls_rejected(self, url):
        client = PoliteClient()
        try:
            with pytest.raises(RuntimeError, match="Invalid URL"):
                client.fetch(url)
        finally:
            client.close()


class TestCookieSharing:
    def test_push_cookies_shares_host_cookies_to_browser_context(self):
        client, session = _make_client()
        session.cookies = FakeCookies([FakeCookie("sid", "abc123", ".example.com")])
        pushed: dict[str, str] = {}

        class FakeContext:
            def add_cookies(self, cookies):
                pushed.update({c["name"]: c["value"] for c in cookies})

        client._push_cookies_to_context(FakeContext(), "https://www.example.com/careers")
        assert pushed == {"sid": "abc123"}

    def test_push_ignores_other_hosts(self):
        client, session = _make_client()
        session.cookies = FakeCookies([FakeCookie("sid", "abc123", ".other.com")])
        pushed: list = []

        class FakeContext:
            def add_cookies(self, cookies):
                pushed.extend(cookies)

        client._push_cookies_to_context(FakeContext(), "https://www.example.com/careers")
        assert pushed == []

    def test_pull_cookies_persists_browser_cookies_to_session(self):
        client, session = _make_client()

        class FakeContext:
            def cookies(self, url):
                return [{"name": "cf_bm", "value": "z", "domain": ".example.com", "path": "/"}]

        client._pull_cookies_from_context(FakeContext(), "https://www.example.com/careers")
        assert session.cookies.stored[(".example.com", "cf_bm")] == "z"


class TestUsableContent:
    def test_json_payload_is_usable_even_when_tiny(self):
        assert _is_usable_content('{"jobs": [], "total": 0}')

    def test_empty_body_is_unusable(self):
        assert not _is_usable_content("")
        assert not _is_usable_content("   ")
        assert not _is_usable_content(None)

    def test_tiny_body_is_unusable(self):
        assert not _is_usable_content("<html><body>hi</body></html>")

    def test_challenge_markers_are_unusable(self):
        assert not _is_usable_content("<html><title>Just a moment...</title></html>")
        assert not _is_usable_content(
            "<html><script src='/cdn-cgi/challenge-platform/h/b'></script><body>x</body></html>"
        )

    def test_noscript_javascript_note_is_not_rejected(self):
        html = (
            "<html><body><noscript>Please enable JavaScript to continue.</noscript>"
            "<p>We are hiring backend engineers with Python and cloud experience. "
            "Our platform team builds services for customers in Helsinki and Stockholm "
            "with modern tooling.</p></body></html>"
        )
        assert _is_usable_content(html)

    def test_html_without_visible_text_is_unusable(self):
        html = "<html><head><script>var a = 1;</script></head><body></body></html>"
        assert not _is_usable_content(html)

    def test_visible_challenge_text_is_unusable(self):
        html = (
            "<html><body><p>Enable JavaScript and cookies to continue. This page "
            "checks your browser before it lets you through to the site content "
            "you requested, so please wait while we verify the request.</p></body></html>"
        )
        assert not _is_usable_content(html)


class TestFetchMemo:
    """RF7: one page is read once per tier within one client's lifetime."""

    def test_repeated_fetch_hits_the_network_once(self):
        client, session = _make_client([FakeResponse(200, _MEANINGFUL_HTML)])
        assert client.fetch("https://example.com/careers") == _MEANINGFUL_HTML
        assert client.fetch("https://example.com/careers") == _MEANINGFUL_HTML
        assert len(session.requests) == 1

    def test_repeated_render_runs_the_browser_once(self, monkeypatch):
        client, _ = _make_client()
        calls: list[str] = []

        def render(url):
            calls.append(url)
            return _MEANINGFUL_HTML

        monkeypatch.setattr(client, "_render_with_browser", render)
        client.fetch_rendered("https://example.com/careers")
        client.fetch_rendered("https://example.com/careers")
        assert calls == ["https://example.com/careers"]

    def test_tiers_do_not_share_an_entry(self, monkeypatch):
        client, session = _make_client([FakeResponse(200, "<html>static</html>" + _MEANINGFUL_HTML)])
        monkeypatch.setattr(client, "_render_with_browser", lambda url: _MEANINGFUL_HTML + "rendered")
        assert client.fetch("https://example.com/careers").startswith("<html>static")
        assert client.fetch_rendered("https://example.com/careers") == _MEANINGFUL_HTML + "rendered"

    def test_failures_are_not_memoized(self, monkeypatch):
        client, _ = _make_client()

        def boom(url):
            raise RuntimeError("render failed")

        monkeypatch.setattr(client, "_render_with_browser", boom)
        for _ in range(2):
            with pytest.raises(RuntimeError):
                client.fetch_rendered("https://example.com/careers")

    def test_detail_fetches_bypass_the_memo(self):
        client, session = _make_client(
            [FakeResponse(200, _MEANINGFUL_HTML), FakeResponse(200, _MEANINGFUL_HTML)]
        )
        client.fetch_static_first("https://example.com/jobs/1")
        client.fetch_static_first("https://example.com/jobs/1")
        assert len(session.requests) == 2

    def test_memo_is_bounded(self):
        client, session = _make_client([FakeResponse(200, _MEANINGFUL_HTML)] * 40)
        for i in range(40):
            client.fetch(f"https://example.com/p{i}")
        assert len(client._memo) <= 32
