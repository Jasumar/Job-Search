"""OpenRouter Decisions API caller (Jev / System One models).

Deliberately separate from :class:`~jobsearch_core.llm.client.LLMClient`:
Jev answers typed questions over a state via ``POST /api/alpha/decisions``
and must not go through the chat-completions path.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from jobsearch_core.log import get_logger

logger = get_logger(__name__)

_DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"


class DecisionsClient:
    """Minimal typed-questions caller for Jev models via OpenRouter."""

    def __init__(self, api_key: str, model: str = "typesafe/jev-1.13", max_retries: int = 2):
        if not api_key:
            raise ValueError("DecisionsClient requires an OpenRouter API key")
        self._api_key = api_key
        self.model = model
        self.max_retries = max(0, max_retries)

    def ask(self, state: dict | str, questions: dict) -> dict:
        """Return the ``answers`` object for the given state and questions.

        Raises on transport/HTTP failure after retries; callers decide on
        fallback behavior (the gate must never be a hard dependency).
        """
        body = json.dumps(
            {"model": self.model, "state": state, "questions": questions}
        ).encode()
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            if attempt:
                delay = 2 ** (attempt - 1)
                logger.warning("Decisions API retrying in %ss", delay)
                time.sleep(delay)
            try:
                return self._send(body)
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                logger.warning("Decisions API call failed: %s", exc)
        raise RuntimeError(f"Decisions API failed after retries: {last_error}")

    def _send(self, body: bytes) -> dict:
        req = urllib.request.Request(
            _DECISIONS_URL,
            data=body,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.load(resp)
        if "answers" not in payload:
            raise ValueError(f"Decisions API response missing answers: {payload!r}")
        return payload["answers"]


def load_api_key_from_env_file(env_path: str = ".env") -> str:
    """Read ``OPENROUTER_API_KEY`` from a dotenv file (spaces tolerated)."""
    try:
        with open(env_path, encoding="utf-8") as fh:
            for line in fh:
                key, _, value = line.strip().partition("=")
                if key.strip() == "OPENROUTER_API_KEY":
                    return value.strip().strip("'\"")
    except OSError:
        pass
    return ""
