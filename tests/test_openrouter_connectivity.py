from __future__ import annotations

import os

import pytest
from dotenv import load_dotenv
from jobsearch_core.config import Settings
from jobsearch_core.llm.client import LLMClient
from openrouter.errors import TooManyRequestsResponseError

load_dotenv()

# Live provider calls (models list + three completions). Off by default: they cost
# money, depend on the network and on provider throttling, and nothing here is
# what the rest of the suite is asserting. Opt in with JOBSEARCH_LIVE_LLM_TESTS=1.
pytestmark = pytest.mark.skipif(
    os.environ.get("JOBSEARCH_LIVE_LLM_TESTS") != "1" or not os.environ.get("OPENROUTER_API_KEY"),
    reason="live LLM tests are opt-in: set JOBSEARCH_LIVE_LLM_TESTS=1 (and OPENROUTER_API_KEY)",
)

LEGACY_MODEL_FALLBACK = "gpt-4o-mini"


def configured_models(settings: Settings) -> dict[str, str]:
    """Return both configured models, retaining the old analysis fallback."""
    analysis_model = getattr(settings, "analysis_model", "") or LEGACY_MODEL_FALLBACK
    return {
        "analysis": analysis_model,
        "application": settings.application_model,
    }


def analysis_completion(settings: Settings, *, json_mode: bool = False) -> str:
    """Use the configured analysis model, falling back only on provider throttling."""
    prompts = {
        "system_prompt": "You extract JSON from text." if json_mode else "You are a helpful assistant.",
        "user_prompt": (
            'Extract the name and age: "John is 30 years old."'
            if json_mode
            else "Reply with exactly one word: hello"
        ),
    }
    client = LLMClient(
        api_key=settings.openrouter_api_key,
        model=configured_models(settings)["analysis"],
    )
    try:
        method = client.complete_json if json_mode else client.complete
        return method(**prompts)
    except TooManyRequestsResponseError:
        fallback = LLMClient(
            api_key=settings.openrouter_api_key,
            model=LEGACY_MODEL_FALLBACK,
        )
        method = fallback.complete_json if json_mode else fallback.complete
        return method(**prompts)


def test_openrouter_models_list():
    settings = Settings()
    client = LLMClient(api_key=settings.openrouter_api_key)
    result = client._client.models.list(limit=5)
    assert result is not None
    models = result.result.data
    assert len(models) > 0
    assert models[0].id
    assert models[0].pricing is not None


@pytest.mark.parametrize("task", ["analysis", "application"])
def test_openrouter_chat_completion(task: str):
    settings = Settings()
    if task == "analysis":
        response = analysis_completion(settings)
        assert isinstance(response, str)
        assert len(response) > 0
        return
    client = LLMClient(
        api_key=settings.openrouter_api_key,
        model=configured_models(settings)[task],
    )
    response = client.complete(
        system_prompt="You are a helpful assistant.",
        user_prompt="Reply with exactly one word: hello",
    )
    assert isinstance(response, str)
    assert len(response) > 0


def test_openrouter_json_completion():
    settings = Settings()
    response = analysis_completion(settings, json_mode=True)
    assert isinstance(response, str)
    parsed = __import__("json").loads(response)
    assert "name" in parsed or "John" in str(parsed)
