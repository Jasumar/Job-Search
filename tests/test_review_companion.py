from __future__ import annotations

import json
import socket
from urllib.request import urlopen

from jobsearch_core.review_companion import ReviewCompanion


def _get(url: str) -> dict:
    with urlopen(url, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def test_review_companion_serves_state_and_advances(tmp_path):
    companion = ReviewCompanion(open_browser=False)
    assert companion.start() is True
    try:
        base = f"http://127.0.0.1:{companion._port}"
        assert companion.base_url == base + "/"

        companion.set_current("https://example.com/careers", label="Example", index=1, total=3)
        state = _get(f"{base}/state")
        assert state["status"] == "active"
        assert state["url"] == "https://example.com/careers"
        assert state["label"] == "Example"
        assert state["index"] == 1
        assert state["total"] == 3

        companion.set_current("https://next.example/careers", label="Next", index=2, total=3)
        state = _get(f"{base}/state")
        assert state["url"] == "https://next.example/careers"
        assert state["index"] == 2
    finally:
        companion.close()


def test_review_companion_marks_complete(tmp_path):
    companion = ReviewCompanion(open_browser=False)
    assert companion.start() is True
    try:
        companion.set_current("https://example.com/careers", label="Example", index=1, total=1)
        companion.mark_complete()
        state = _get(f"http://127.0.0.1:{companion._port}/state")
        assert state["status"] == "complete"
        assert state["url"] is None
    finally:
        companion.close()


def test_review_companion_serves_control_page(tmp_path):
    companion = ReviewCompanion(open_browser=False)
    assert companion.start() is True
    try:
        with urlopen(f"http://127.0.0.1:{companion._port}/", timeout=5) as response:
            html = response.read().decode("utf-8")
        assert response.status == 200
        assert "Review companion" in html
        assert "/state" in html
    finally:
        companion.close()


def test_review_companion_degrades_when_port_unavailable():
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]
    try:
        companion = ReviewCompanion(port=port, open_browser=False)
        assert companion.start() is False
        assert companion.running is False
    finally:
        blocker.close()
