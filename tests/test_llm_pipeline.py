"""Rate limiting for the LLM client.

The Phase 4.1 match phase keeps `LLMClient` for the optional, bounded notes
pass, so its pacing and retry behaviour is still load-bearing. The retired
legacy response models (`FitResponse`, `RequirementsResponse`) had their own
tests here; those are gone with `scoring/llm.py`.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from jobsearch_core.llm.client import LLMClient


def _response(content: str | None):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


def test_first_request_does_not_sleep():
    client = LLMClient(api_key="test", min_interval_seconds=5.0)
    with patch.object(client, "_client") as raw:
        raw.chat.send.return_value = _response("ok")
        with patch("jobsearch_core.llm.client.time.sleep") as sleep:
            client.complete("system", "user")
    assert not sleep.called


def test_second_request_sleeps_for_rate_limit():
    client = LLMClient(api_key="test", min_interval_seconds=5.0)
    with patch.object(client, "_client") as raw:
        raw.chat.send.return_value = _response("ok")
        with patch("jobsearch_core.llm.client.time.sleep") as sleep:
            client.complete("system", "user")
            client.complete("system", "user")
    assert sleep.called


def test_complete_returns_response_text():
    client = LLMClient(api_key="test", min_interval_seconds=0.0)
    with patch.object(client, "_client") as raw:
        raw.chat.send.return_value = _response("hello")
        assert client.complete("system", "user") == "hello"


def test_complete_returns_empty_string_on_null_content():
    client = LLMClient(api_key="test", min_interval_seconds=0.0)
    with patch.object(client, "_client") as raw:
        raw.chat.send.return_value = _response(None)
        assert client.complete("system", "user") == ""


def test_complete_json_returns_json_object():
    client = LLMClient(api_key="test", min_interval_seconds=0.0)
    with patch.object(client, "_client") as raw:
        raw.chat.send.return_value = _response('{"a": 1}')
        assert client.complete_json("system", "user") == '{"a": 1}'
    assert raw.chat.send.call_args.kwargs["response_format"] is not None
