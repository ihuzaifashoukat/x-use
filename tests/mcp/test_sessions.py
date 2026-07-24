"""Session-pool tests: read-only tools never start a browser; the first
write-tool need cold-starts a session; warm sessions are reused; idle
sessions are reaped; close_all shuts everything down and seals the pool.

All sessions come from a fake browser factory — no real Selenium.
"""
import asyncio
import time
from unittest.mock import MagicMock

import pytest

from xuse.mcp.server import create_server
from xuse.mcp.sessions import SessionError, SessionPool
from xuse.mcp.drafts import DraftStore

from helpers import (  # noqa: F401 — imported fixtures register for this module
    FakeMetrics,
    accounts,
    browser_factory,
    call_tool,
    config_loader,
    draft_store,
    drafts_path,
    make_account,
    make_fake_publisher,
    mcp_server,
    mcp_settings,
    queue_store,
    session_pool,
)


@pytest.mark.asyncio
async def test_read_only_tools_never_create_a_session(mcp_server, browser_factory, session_pool):
    listed = await call_tool(mcp_server, "list_accounts", {})
    assert listed["ok"] is True
    assert listed["count"] == 2

    metrics = await call_tool(mcp_server, "get_metrics", {"account": "acc1"})
    assert metrics["ok"] is True
    assert metrics["summary"]["counters"]["posts"] == 0

    assert browser_factory.created == []
    assert list(session_pool.active_accounts) == []


@pytest.mark.asyncio
async def test_first_write_tool_cold_starts_then_reuses_warm_session(
    make_config_loader, drafts_path, browser_factory, monkeypatch
):
    """Direct mode (draft_mode=False): the first write tool cold-starts one
    browser session; the second write reuses it (factory still called once)."""
    loader = make_config_loader(settings={}, accounts=[make_account("acc1")])
    pool = SessionPool(loader, browser_factory=browser_factory)
    server = create_server(
        config_loader=loader, session_pool=pool, draft_store=DraftStore(drafts_path), draft_mode=False
    )
    ctx = server.xuse_ctx
    ctx.processed_keys = set()
    ctx.file_handler = MagicMock(name="file_handler")
    ctx.llm_service = MagicMock(name="llm_service")
    metrics = {}
    ctx.metrics_factory = lambda account_id: metrics.setdefault(account_id, FakeMetrics(account_id))
    instances = []
    monkeypatch.setattr("xuse.mcp.actions.TweetPublisher", make_fake_publisher(instances))

    assert browser_factory.created == []  # nothing started at server creation

    first = await call_tool(server, "post_tweet", {"account": "acc1", "text": "first post"})
    assert first["ok"] is True
    assert len(browser_factory.created) == 1  # cold start happened exactly once
    assert browser_factory.created[0].driver_started is True
    assert list(pool.active_accounts) == ["acc1"]

    second = await call_tool(server, "post_tweet", {"account": "acc1", "text": "second post"})
    assert second["ok"] is True
    assert len(browser_factory.created) == 1  # warm reuse — no new cold start

    await pool.close_all()


@pytest.mark.asyncio
async def test_warm_session_reused_at_pool_level(session_pool, browser_factory):
    async with session_pool.session("acc1"):
        pass
    async with session_pool.session("acc1"):
        pass
    assert len(browser_factory.created) == 1
    await session_pool.close_all()


@pytest.mark.asyncio
async def test_idle_session_is_reaped(config_loader, browser_factory):
    pool = SessionPool(
        config_loader,
        idle_timeout_seconds=0.05,
        reap_interval_seconds=0.01,
        browser_factory=browser_factory,
    )
    entry = await pool.acquire("acc1")
    assert "acc1" in list(pool.active_accounts)

    entry.last_used = time.monotonic() - 100  # simulate a long-idle session
    await asyncio.sleep(0.3)  # let the reaper run several iterations

    assert "acc1" not in list(pool.active_accounts)
    assert browser_factory.created[0].closed is True
    await pool.close_all()


@pytest.mark.asyncio
async def test_close_all_closes_every_session_and_seals_pool(session_pool, browser_factory):
    await session_pool.acquire("acc1")
    await session_pool.acquire("acc2")
    assert len(browser_factory.created) == 2

    await session_pool.close_all()

    assert all(manager.closed for manager in browser_factory.created)
    assert list(session_pool.active_accounts) == []
    with pytest.raises(SessionError, match="closed"):
        await session_pool.acquire("acc1")
