"""Composed ordinary reads honor durable spacing without retrying writes."""
from types import SimpleNamespace

import pytest

from helpers import call_tool
from test_playwright_integration import async_server  # noqa: F401
from xuse.mcp import browser_bridge
from xuse.mcp.safety import PolicyError


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["research_and_stage", "engage"])
async def test_multiple_keywords_wait_for_read_spacing(async_server, monkeypatch, tool):
    ctx = async_server.xuse_ctx
    now, waits, queries = [100000], [], []
    ctx.safety_store.clock = lambda: now[0]
    ctx.safety_store.read_interval = 2
    ctx.llm_service = SimpleNamespace(client=object(), config_loader=ctx.config_loader)

    async def sleep(seconds):
        waits.append(seconds)
        now[0] += seconds

    async def search(query, limit):
        queries.append(query)
        return []

    monkeypatch.setattr(browser_bridge.asyncio, "sleep", sleep)
    ctx.session_pool.browser.search_tweets = search
    result = await call_tool(async_server, tool, {"account": "acc1", "keywords": ["one", "two"]})
    assert result["ok"] and queries == ["one", "two"] and waits == [2]
    assert ctx.safety_store.status("acc1")["daily_used"]["read"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", ["daily_budget", "minute_budget", "long_cooldown"])
async def test_composite_budget_or_long_interval_refuses_without_sleep(async_server, monkeypatch, limit):
    ctx = async_server.xuse_ctx
    ctx.llm_service = SimpleNamespace(client=object())
    ctx.safety_store.clock = lambda: 100000
    if limit == "daily_budget":
        ctx.safety_store.caps["read"] = 1
    elif limit == "minute_budget":
        ctx.safety_store.per_minute = 1
    else:
        ctx.safety_store.read_interval = 30
    waits = []

    async def sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(browser_bridge.asyncio, "sleep", sleep)
    result = await call_tool(async_server, "research_and_stage", {"account": "acc1", "keywords": ["one", "two"]})
    assert not result["ok"] and not waits
    assert result["error"]["reason"] == ("cooldown" if limit == "long_cooldown" else limit)
    assert len(ctx.session_pool.browser.calls) == 1


@pytest.mark.asyncio
async def test_read_retry_never_repeats_a_reserved_action(monkeypatch):
    calls = []

    async def reserved(*args):
        calls.append(args)
        raise PolicyError("reserved", reason="cooldown", retry_after_seconds=1, action_id="claimed")

    monkeypatch.setattr(browser_bridge, "browser_call", reserved)
    with pytest.raises(PolicyError):
        await browser_bridge.ordinary_read(None, "account", "search_tweets", "query", 1)
    assert len(calls) == 1
