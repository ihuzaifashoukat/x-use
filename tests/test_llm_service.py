"""LLMService (single OpenAI-compatible client): model resolution, message
building, None-when-unconfigured, and structured-JSON retry that drops
JSON mode after the first attempt."""
import pytest

from xuse.core.llm_service.service import LLMService


class _Msg:
    def __init__(self, content):
        self.content = content


class _Resp:
    def __init__(self, content, finish_reason="stop"):
        self.choices = [type("C", (), {"message": _Msg(content),
                                     "finish_reason": finish_reason})()]


class FakeCompletions:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, tuple):
            return _Resp(outcome[0], outcome[1])
        return _Resp(outcome)


class FakeClient:
    def __init__(self, outcomes):
        self.chat = type("Chat", (), {"completions": FakeCompletions(outcomes)})()


def make_service(monkeypatch, make_config_loader, client, model="cfg-model"):
    monkeypatch.setattr(
        "xuse.core.llm_service.service.build_client",
        lambda loader: (client, {"model": model, "base_url": None, "key_source": "test"}),
    )
    return LLMService(make_config_loader(settings={}, accounts=[]))


@pytest.mark.asyncio
async def test_unconfigured_returns_none(monkeypatch, make_config_loader):
    service = make_service(monkeypatch, make_config_loader, client=None)
    assert await service.generate_text("hello") is None


@pytest.mark.asyncio
async def test_uses_configured_model_and_builds_messages(monkeypatch, make_config_loader):
    client = FakeClient(["  some text  "])
    service = make_service(monkeypatch, make_config_loader, client)
    text = await service.generate_text("write a post", system_prompt="be terse")
    assert text == "some text"
    call = client.chat.completions.calls[0]
    assert call["model"] == "cfg-model"
    assert call["max_tokens"] == 1200  # default (reasoning-model headroom)
    assert call["messages"] == [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "write a post"},
    ]


@pytest.mark.asyncio
async def test_model_name_override_wins(monkeypatch, make_config_loader):
    client = FakeClient(["ok"])
    service = make_service(monkeypatch, make_config_loader, client)
    await service.generate_text("hi", model_name="special-model", max_tokens=50, temperature=0.1)
    call = client.chat.completions.calls[0]
    assert call["model"] == "special-model"
    assert call["max_tokens"] == 50
    assert call["temperature"] == 0.1


@pytest.mark.asyncio
async def test_api_error_returns_none(monkeypatch, make_config_loader):
    client = FakeClient([RuntimeError("provider down")])
    service = make_service(monkeypatch, make_config_loader, client)
    assert await service.generate_text("hi") is None


def test_default_max_tokens_has_reasoning_headroom():
    """Reasoning models (e.g. kimi-k3) burn completion budget on hidden
    reasoning tokens; the old 150 default starved replies to empty (live
    finding, v2.3)."""
    from xuse.models import LLMSettings
    assert LLMSettings().max_tokens == 1200


@pytest.mark.asyncio
async def test_empty_content_returns_empty_and_warns(monkeypatch, make_config_loader, caplog):
    client = FakeClient([""])
    service = make_service(monkeypatch, make_config_loader, client)
    with caplog.at_level("WARNING"):
        text = await service.generate_text("hi")
    assert text == ""
    assert any("empty content" in r.message for r in caplog.records)
    assert len(client.chat.completions.calls) == 1  # finish_reason=stop: no retry


@pytest.mark.asyncio
async def test_length_starvation_retries_with_bigger_budget(monkeypatch, make_config_loader):
    """Empty content with finish_reason='length' (reasoning models burning the
    budget on hidden tokens) retries once with max(doubled, 1200)."""
    client = FakeClient([("", "length"), "recovered reply"])
    service = make_service(monkeypatch, make_config_loader, client)
    text = await service.generate_text("reply", max_tokens=150)
    assert text == "recovered reply"
    first, second = client.chat.completions.calls
    assert first["max_tokens"] == 150
    assert second["max_tokens"] == 1200  # max(2*150, 1200)


@pytest.mark.asyncio
async def test_structured_retries_without_json_mode(monkeypatch, make_config_loader):
    client = FakeClient([RuntimeError("response_format unsupported"), '{"score": 0.9}'])
    service = make_service(monkeypatch, make_config_loader, client)
    data, err = await service.generate_structured("rate this", {"type": "object"})
    assert err is None
    assert data == {"score": 0.9}
    first, second = client.chat.completions.calls
    assert first["response_format"] == {"type": "json_object"}
    assert "response_format" not in second
