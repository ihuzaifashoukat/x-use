"""Reconciling one action cannot complete or restart a whole thread."""
import pytest

from helpers import call_tool
from test_thread_tools import thread_server, integrated_server  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["succeeded", "not_sent"])
@pytest.mark.parametrize("cancelled", [False, True])
async def test_single_action_reconciliation_keeps_authoritative_thread_status(thread_server, tmp_path, outcome, cancelled):
    _, ctx, _ = thread_server
    server = integrated_server(thread_server, tmp_path)
    prepared = await call_tool(server, "prepare_thread", {
        "account": "acc1", "posts": [{"text": "one"}, {"text": "two"}]})
    ctx.session_pool.browser.fail = True
    result = await call_tool(server, "approve_draft", {"draft_id": prepared["draft_id"]})
    action_id = result["result"]["error"]["action_id"]
    if cancelled:
        await call_tool(server, "cancel_thread", {"run_id": prepared["run_id"]})
    recovered = await call_tool(server, "resolve_action_outcome", {
        "account": "acc1", "action_id": action_id, "observed_outcome": outcome})
    assert recovered["ok"]
    assert recovered["thread_state"] == ("cancelled" if cancelled else "uncertain")
    assert ctx.draft_store.get(prepared["draft_id"]).status == ("rejected" if cancelled else "uncertain")
    ctx.session_pool.browser.fail = False
    await call_tool(server, "continue_thread", {"run_id": prepared["run_id"]})
    assert len(ctx.session_pool.browser.calls) == 1
