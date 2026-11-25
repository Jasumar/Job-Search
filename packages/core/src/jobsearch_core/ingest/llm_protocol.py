"""The slice of ``LLMClient`` the recipe author and the posting gate use.

Lives in its own module so the scrape pipeline can type an LLM parameter
without importing ``recipe_author`` (which imports ``recipe``, which imports
the scrapers package — a cycle).
"""

from __future__ import annotations

from typing import Protocol


class JsonCompleter(Protocol):
    """A model that answers with a JSON object string."""

    model: str

    def complete_json(self, system_prompt: str, user_prompt: str) -> str: ...


__all__ = ["JsonCompleter"]
