"""content_generator hardening: total LLM failure returns "" (never the
prompt itself as post text), an explicit temperature=0.0 survives to the
provider call, and the prompt heuristic no longer rewrites verbatim ready
text that merely mentions "post"/"write".
"""
import pytest

from xuse.features.publisher.content_generator import (
    generate_post_text_if_needed,
    maybe_generate_quote_text,
)
from xuse.models import LLMSettings


class FakeLLMService:
    def __init__(self, structured=(None, "boom"), text=None):
        self.structured_result = structured
        self.text_result = text
        self.structured_calls = []
        self.text_calls = []

    async def generate_structured(self, **kwargs):
        self.structured_calls.append(kwargs)
        return self.structured_result

    async def generate_text(self, **kwargs):
        self.text_calls.append(kwargs)
        return self.text_result


SETTINGS = LLMSettings()


# --- fix 3: never return the prompt as post text ----------------------------


@pytest.mark.asyncio
async def test_total_llm_failure_returns_empty_not_the_prompt():
    prompt = "Write an engaging X post about: our launch"
    service = FakeLLMService(structured=(None, "provider down"), text=None)
    result = await generate_post_text_if_needed(prompt, SETTINGS, service)
    assert result == ""
    assert prompt not in result


@pytest.mark.asyncio
async def test_total_llm_failure_with_long_prompt_returns_empty():
    """Prompts >270 chars used to bypass the actions.py equality guard via
    clamping — the generation path must never echo the prompt at all."""
    persona_note = "Write in this account persona:\n" + ("voice notes. " * 30) + "\n\n"
    prompt = persona_note + "Write an engaging X (Twitter) post about: coffee"
    assert len(prompt) > 270
    service = FakeLLMService(structured=(None, "provider down"), text=None)
    result = await generate_post_text_if_needed(prompt, SETTINGS, service)
    assert result == ""


@pytest.mark.asyncio
async def test_successful_generation_still_returns_text():
    service = FakeLLMService(
        structured=({"text": "Fresh brew thoughts", "hashtags": ["coffee"], "safety": {"needs_review": False}}, None),
    )
    result = await generate_post_text_if_needed("Write an engaging X post about: coffee", SETTINGS, service)
    assert "Fresh brew thoughts" in result
    assert "#coffee" in result


# --- fix 14: explicit temperature=0.0 survives ------------------------------


@pytest.mark.asyncio
async def test_explicit_zero_temperature_is_not_overridden():
    settings = LLMSettings(temperature=0.0)
    service = FakeLLMService(
        structured=({"text": "ok", "hashtags": [], "safety": {"needs_review": False}}, None),
    )
    await generate_post_text_if_needed("Write an engaging X post about: tea", settings, service)
    assert service.structured_calls[0]["temperature"] == 0.0


@pytest.mark.asyncio
async def test_unset_temperature_defaults_to_07():
    service = FakeLLMService(
        structured=({"text": "ok", "hashtags": [], "safety": {"needs_review": False}}, None),
    )
    await generate_post_text_if_needed("Write an engaging X post about: tea", SETTINGS, service)
    assert service.structured_calls[0]["temperature"] == 0.7


# --- fix 15: verbatim ready text is not rewritten ---------------------------


@pytest.mark.asyncio
async def test_ready_text_mentioning_post_passes_through_verbatim():
    ready = "Just shipped my new blog post! Check it out."
    service = FakeLLMService()
    result = await generate_post_text_if_needed(ready, SETTINGS, service)
    assert result == ready
    assert service.structured_calls == [] and service.text_calls == []


@pytest.mark.asyncio
async def test_ready_text_mentioning_write_passes_through_verbatim():
    ready = "I love how easy this library makes it to write integrations."
    service = FakeLLMService()
    result = await generate_post_text_if_needed(ready, SETTINGS, service)
    assert result == ready
    assert service.structured_calls == [] and service.text_calls == []


@pytest.mark.asyncio
async def test_instruction_shaped_text_is_treated_as_a_prompt():
    service = FakeLLMService(structured=(None, "down"), text="generated post")
    result = await generate_post_text_if_needed(
        "Write an engaging X (Twitter) post about: coffee", SETTINGS, service)
    assert service.text_calls  # the LLM was consulted
    assert result == "generated post"


@pytest.mark.asyncio
async def test_persona_prefixed_prompt_is_treated_as_a_prompt():
    """actions.py builds persona_note + 'Write an engaging X post about:' —
    that shape must still trigger generation."""
    prompt = "Write in this account persona:\nshort and punchy\n\nWrite an engaging X post about: tea"
    service = FakeLLMService(structured=(None, "down"), text="generated post")
    result = await generate_post_text_if_needed(prompt, SETTINGS, service)
    assert service.text_calls
    assert result == "generated post"


@pytest.mark.asyncio
async def test_no_llm_settings_returns_text_unchanged():
    ready = "Anything at all, write post generate whatever."
    service = FakeLLMService()
    result = await generate_post_text_if_needed(ready, None, service)
    assert result == ready
    assert service.structured_calls == [] and service.text_calls == []
