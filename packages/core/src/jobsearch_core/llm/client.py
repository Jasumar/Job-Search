from __future__ import annotations

import time

from openrouter import OpenRouter
from openrouter.components.chatformatjsonobjectconfig import ChatFormatJSONObjectConfig
from openrouter.components.chatsystemmessage import ChatSystemMessage
from openrouter.components.chatusermessage import ChatUserMessage
from openrouter.errors import TooManyRequestsResponseError

from jobsearch_core.log import get_logger

logger = get_logger(__name__)


class LLMClient:
    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o-mini",
        base_url: str | None = None,
        min_interval_seconds: float = 2.0,
        max_retries: int = 2,
    ):
        kwargs: dict = {"api_key": api_key}
        if base_url:
            kwargs["server_url"] = base_url
        self._client = OpenRouter(**kwargs)
        self.model = model
        self.min_interval_seconds = max(0.0, min_interval_seconds)
        self.max_retries = max(0, max_retries)
        self._last_request_at: float | None = None

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        response = self._send(
            system_prompt,
            user_prompt,
        )
        raw = response.choices[0].message.content
        if raw is None:
            return ""
        if isinstance(raw, str):
            return raw
        return ""

    def complete_json(self, system_prompt: str, user_prompt: str) -> str:
        response = self._send(
            system_prompt,
            user_prompt,
            response_format=ChatFormatJSONObjectConfig(type="json_object"),
        )
        raw = response.choices[0].message.content
        if raw is None:
            return "{}"
        if isinstance(raw, str):
            return raw
        return "{}"

    def _send(self, system_prompt: str, user_prompt: str, **kwargs):
        messages = [
            ChatSystemMessage(content=system_prompt, role="system"),
            ChatUserMessage(content=user_prompt, role="user"),
        ]
        for attempt in range(self.max_retries + 1):
            self._wait_for_rate_limit()
            try:
                logger.debug("OpenRouter request model=%s attempt=%d", self.model, attempt + 1)
                response = self._client.chat.send(
                    model=self.model,
                    messages=messages,
                    temperature=0.0,
                    **kwargs,
                )
                self._last_request_at = time.monotonic()
                logger.debug("OpenRouter request completed model=%s", self.model)
                return response
            except TooManyRequestsResponseError:
                if attempt >= self.max_retries:
                    raise
                delay = 2**attempt
                logger.warning("OpenRouter rate limited; retrying in %ss", delay)
                time.sleep(delay)

    def _wait_for_rate_limit(self) -> None:
        if self._last_request_at is None:
            return
        elapsed = time.monotonic() - self._last_request_at
        remaining = self.min_interval_seconds - elapsed
        if remaining > 0:
            logger.debug("Waiting %.2fs for OpenRouter rate limit", remaining)
            time.sleep(remaining)
