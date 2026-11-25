"""Tests for XHR capture recording (plan-scraping.md §XHR capture, S1)."""

from __future__ import annotations

import hashlib

from jobsearch_core.ingest.http import _record_response
from jobsearch_core.ingest.xhr_capture import (
    BODY_CAP_BYTES,
    build_capture,
    is_capture_candidate,
    redact_headers,
)


def _build(**overrides):
    kwargs = dict(
        url="https://jobs.example.com/api/search",
        method="POST",
        status=200,
        resource_type="fetch",
        response_headers={"content-type": "application/json"},
        request_headers={"Cookie": "sid=secret", "Authorization": "Bearer x", "Accept": "application/json"},
        request_body='{"q": ""}',
        read_body=lambda: b'{"jobs": []}',
    )
    kwargs.update(overrides)
    return build_capture(**kwargs)


def test_fetch_json_response_is_captured_with_credentials_redacted():
    capture = _build()
    assert capture is not None
    assert capture.method == "POST"
    assert capture.response_body == '{"jobs": []}'
    assert capture.response_bytes == len(b'{"jobs": []}')
    assert not capture.body_truncated
    names = {name for name, _ in capture.request_headers}
    assert "cookie" not in names and "authorization" not in names
    assert ("accept", "application/json") in capture.request_headers


def test_image_and_script_responses_are_skipped_without_reading_body():
    reads = []

    def read_body():
        reads.append(1)
        return b"x"

    assert _build(resource_type="image", response_headers={"content-type": "image/png"}, read_body=read_body) is None
    assert reads == []


def test_json_content_type_is_candidate_even_for_document_type():
    assert is_capture_candidate("document", "application/json; charset=utf-8")
    assert not is_capture_candidate("document", "text/html")


def test_body_over_cap_is_truncated_but_size_is_full():
    big = b"a" * (BODY_CAP_BYTES + 10)
    capture = _build(read_body=lambda: big)
    assert capture is not None
    assert capture.body_truncated
    assert capture.response_bytes == BODY_CAP_BYTES + 10
    assert len(capture.response_body) == BODY_CAP_BYTES


def test_body_hash_covers_the_full_body_not_the_sample():
    """R1: a truncated sample still identifies its body by hash."""
    big = b"a" * (BODY_CAP_BYTES + 10)
    capture = _build(read_body=lambda: big)
    assert capture is not None
    assert capture.body_sha256 == hashlib.sha256(big).hexdigest()
    assert capture.body_sha256 != hashlib.sha256(big[:BODY_CAP_BYTES]).hexdigest()


def test_same_body_hashes_equal_and_different_bodies_differ():
    first = _build(read_body=lambda: b'{"jobs": [1, 2]}')
    same = _build(read_body=lambda: b'{"jobs": [1, 2]}')
    other = _build(read_body=lambda: b'{"jobs": [1, 3]}')
    assert first is not None and same is not None and other is not None
    assert first.body_sha256 == same.body_sha256
    assert first.body_sha256 != other.body_sha256


def test_redact_headers_is_case_insensitive_and_sorted():
    assert redact_headers({"X-B": "2", "COOKIE": "a=b", "x-a": "1"}) == (("x-a", "1"), ("x-b", "2"))


class _FakeRequest:
    def __init__(self, method, resource_type, headers, post_data=None):
        self.method = method
        self.resource_type = resource_type
        self.headers = headers
        self.post_data = post_data


class _FakeResponse:
    def __init__(self, url, status, headers, request, body):
        self.url = url
        self.status = status
        self.headers = headers
        self.request = request
        self._body = body

    def body(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def test_listener_records_xhr_and_survives_body_read_failure():
    captures: list = []
    req = _FakeRequest("GET", "xhr", {"Cookie": "c=1"})
    _record_response(
        _FakeResponse("https://x.test/a", 200, {"content-type": "application/json"}, req, b"[]"),
        captures,
    )
    _record_response(
        _FakeResponse("https://x.test/b", 200, {"content-type": "application/json"}, req, RuntimeError("gone")),
        captures,
    )
    assert [c.url for c in captures] == ["https://x.test/a"]
    assert captures[0].request_headers == ()
