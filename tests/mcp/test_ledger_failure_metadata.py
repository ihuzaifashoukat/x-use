"""Ledger bookkeeping failures never erase a reserved external action ID."""
import pytest

from test_playwright_integration import async_server  # noqa: F401
from xuse.browser.errors import BrowserActionError
from xuse.mcp.browser_bridge import browser_call


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmed", [True, False])
async def test_ledger_outcome_failure_keeps_recovery_id(async_server, monkeypatch, confirmed):
    ctx = async_server.xuse_ctx
    calls = []

    async def post(text):
        calls.append(text)
        if not confirmed:
            raise BrowserActionError("send_unconfirmed")
        return {"success": True, "tweet_id": "123"}

    def fail_finish(*args):
        raise OSError("synthetic ledger outcome failure")

    ctx.session_pool.browser.post = post
    monkeypatch.setattr(ctx.safety_store, "finish", fail_finish)
    with pytest.raises(Exception) as error:
        await browser_call(ctx, "acc1", "post", "synthetic", kind="post")
    action_id = getattr(error.value, "action_id", None)
    assert action_id
    assert ctx.safety_store.reference("acc1", action_id)["status"] == "started"
    assert calls == ["synthetic"]
    if not confirmed:
        assert isinstance(error.value, BrowserActionError)
    with pytest.raises(Exception) as duplicate:
        await browser_call(ctx, "acc1", "post", "synthetic", kind="post")
    assert duplicate.value.action_id == action_id
    assert calls == ["synthetic"]
