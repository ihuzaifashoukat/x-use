"""LLMService hardening: an unconfigured client fails generate_structured
immediately with an honest reason (no misleading retries), passing both
model_name and model raises ValueError instead of being swallowed to None,
and non-dict structured answers take the parse-error path.
"""
import pytest

from xuse.core.llm_service.service import LLMService

from test_llm_service import FakeClient, make_service


@pytest.mark.asyncio
async def test_structured_unconfigured_client_fails_fast(monkeypatch, make_config_loader):
    """client=None means 'not configured' — no 3 misleading attempts ending
    in 'No response from LLM' (indistinguishable from a provider outage)."""
    service = make_service(monkeypatch, make_config_loader, client=None)
    data, err = await service.generate_structured("rate this", {"type": "object"})
    assert data is None
    assert err == "LLM not configured (no API key)"


@pytest.mark.asyncio
async def test_generate_text_rejects_both_model_name_and_model(monkeypatch, make_config_loader):
    """model_name + model together used to leave 'model' in call_params,
    raising a TypeError that the broad except swallowed to None."""
    client = FakeClient(["ok"])
    service = make_service(monkeypatch, make_config_loader, client)
    with pytest.raises(ValueError, match="pass only one of model_name/model"):
        await service.generate_text("hi", model_name="a", model="b")
    assert client.chat.completions.calls == []  # rejected before any API call


@pytest.mark.asyncio
async def test_generate_text_accepts_model_kwarg_alone(monkeypatch, make_config_loader):
    client = FakeClient(["ok"])
    service = make_service(monkeypatch, make_config_loader, client)
    text = await service.generate_text("hi", model="explicit-model")
    assert text == "ok"
    assert client.chat.completions.calls[0]["model"] == "explicit-model"


@pytest.mark.asyncio
async def test_structured_non_dict_answer_takes_parse_error_path(monkeypatch, make_config_loader):
    """A fenced array is not a successful structured result — the caller gets
    (None, err), never a list that crashes on .get()."""
    client = FakeClient(['```json\n["quote_tweet", "like"]\n```'])
    service = make_service(monkeypatch, make_config_loader, client)
    data, err = await service.generate_structured("pick an action", {"type": "object"}, max_retries=0)
    assert data is None
    assert err is not None
    assert "not an object" in err


@pytest.mark.asyncio
async def test_structured_retries_after_non_dict_answer(monkeypatch, make_config_loader):
    client = FakeClient(['[1, 2]', '{"score": 0.9}'])
    service = make_service(monkeypatch, make_config_loader, client)
    data, err = await service.generate_structured("rate this", {"type": "object"})
    assert err is None
    assert data == {"score": 0.9}
    assert len(client.chat.completions.calls) == 2
