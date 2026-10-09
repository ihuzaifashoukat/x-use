"""Inbox PIN pauses permit only explicit public reads and never clear pauses."""
from contextlib import asynccontextmanager

import pytest

from test_playwright_integration import async_server  # noqa: F401
from xuse.mcp.browser_bridge import browser_call
from xuse.mcp.safety import PolicyError


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["get_profile", "get_profile_context", "get_profile_posts",
    "get_profile_connections", "get_tweet", "get_thread", "search_tweets", "search_profile",
    "get_home_feed", "get_notifications", "get_account_analytics"])
async def test_pin_pause_permits_public_reads_without_resuming(async_server, operation):
    ctx = async_server.xuse_ctx
    ctx.safety_store.pause("acc1", "pin_required")
    token = ctx.safety_store.pause_token("acc1")

    async def read():
        return {"visible": True}

    setattr(ctx.session_pool.browser, operation, read)
    result = await browser_call(ctx, "acc1", operation)
    assert result["visible"] and result["action_id"]
    assert ctx.safety_store.pause_token("acc1") == token
    assert ctx.safety_store.status("acc1")["pause"]["reason"] == "pin_required"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,kind", [("get_inbox", "read"), ("get_conversation", "read"),
    ("search_conversations", "read"), ("future_unreviewed_read", "read"), ("post", "post"),
    ("send_message", "message"), ("follow", "follow"), ("get_profile", "post")])
async def test_pin_pause_denies_inbox_unknown_operations_and_writes(async_server, operation, kind):
    ctx = async_server.xuse_ctx
    ctx.safety_store.pause("acc1", "pin_required")
    with pytest.raises(PolicyError) as error:
        await browser_call(ctx, "acc1", operation, kind=kind)
    assert error.value.reason == "pin_required"
    assert ctx.session_pool.started == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["manual_pause", "challenge", "rate_limited", "login_required",
                                   "account_locked", "session_expired"])
async def test_public_reads_do_not_bypass_other_pauses(async_server, reason):
    ctx = async_server.xuse_ctx
    ctx.safety_store.pause("acc1", reason)
    with pytest.raises(PolicyError) as error:
        await browser_call(ctx, "acc1", "get_profile")
    assert error.value.reason == reason
    assert ctx.session_pool.started == 0


@pytest.mark.asyncio
async def test_pause_changed_during_session_start_blocks_public_read(async_server):
    ctx = async_server.xuse_ctx
    policy = ctx.safety_store
    policy.pause("acc1", "pin_required")
    called = []

    async def read():
        called.append(True)
        return {}

    @asynccontextmanager
    async def session(account):
        policy.pause(account, "manual_pause")
        yield ctx.session_pool.browser

    ctx.session_pool.browser.get_profile = read
    ctx.session_pool.session = session
    with pytest.raises(PolicyError) as error:
        await browser_call(ctx, "acc1", "get_profile")
    assert error.value.reason == "manual_pause"
    assert not called
    assert policy.status("acc1")["pause"]["reason"] == "manual_pause"
