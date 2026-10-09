"""Isolated protocol, journal and lifecycle regression tests; no real X."""
import asyncio
import json
from types import SimpleNamespace
from typing import Any, Dict

import pytest
from mcp import types
from mcp.server.fastmcp import FastMCP

from xuse.mcp.drafts import DraftStore
from xuse.mcp.executor import ToolError
from xuse.mcp.server import _lifespan, shutdown
from xuse.mcp.sessions import SessionPool
from xuse.mcp.tools import guard
from xuse.queue.store import QueueJournalError

from helpers import (  # noqa: F401
    accounts, browser_factory, call_tool, config_loader, mcp_settings, session_pool,
)
from test_playwright_integration import async_server  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize("return_type", [dict, dict[str, Any], Dict[str, Any]])
async def test_guard_errors_have_protocol_error_flag_and_action_id(return_type):
    server = FastMCP("isolated-audit")

    async def denied() -> dict:
        error = ToolError("Inspect this action before retrying.")
        error.action_id = "synthetic-action"
        error.reason = "tool_timeout"
        raise error

    denied.__annotations__["return"] = return_type
    server.tool()(guard(denied))

    response = await server._mcp_server.request_handlers[types.CallToolRequest](
        types.CallToolRequest(method="tools/call", params=types.CallToolRequestParams(name="denied", arguments={})))
    result = response.root
    assert result.isError is True
    envelope = result.structuredContent.get("result", result.structuredContent)
    assert envelope["error"]["action_id"] == "synthetic-action"
    assert json.loads(result.content[0].text) == envelope


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_phase", ["start", "stop"])
async def test_lifespan_scheduler_failure_still_closes_pool(fail_phase):
    closed = []

    async def close_all():
        closed.append(True)

    async def start():
        if fail_phase == "start":
            raise RuntimeError("synthetic startup failure")

    async def stop():
        if fail_phase == "stop":
            raise RuntimeError("synthetic shutdown failure")

    server = SimpleNamespace(xuse_ctx=SimpleNamespace(session_pool=SimpleNamespace(close_all=close_all)),
                             xuse_auto_drain=SimpleNamespace(start=start, stop=stop))
    with pytest.raises(RuntimeError, match="synthetic"):
        async with _lifespan(server):
            pass
    assert closed == [True]


def test_draft_create_persistence_failure_does_not_publish_in_memory(tmp_path, monkeypatch):
    store = DraftStore(tmp_path / "drafts.jsonl")

    def fail(*args):
        raise OSError("synthetic journal failure")

    monkeypatch.setattr(store, "_append", fail)
    with pytest.raises(OSError):
        store.create("synthetic-account", "post_tweet", {"text": "synthetic"}, "synthetic")
    assert len(store) == 0


def test_draft_status_persistence_failure_keeps_previous_state(tmp_path, monkeypatch):
    store = DraftStore(tmp_path / "drafts.jsonl")
    draft = store.create("synthetic-account", "post_tweet", {"text": "synthetic"}, "synthetic")

    def fail(*args):
        raise OSError("synthetic journal failure")

    monkeypatch.setattr(store, "_append", fail)
    with pytest.raises(OSError):
        store.set_status(draft.draft_id, "approved")
    assert draft.status == store.get(draft.draft_id).status == "pending"
    assert DraftStore(tmp_path / "drafts.jsonl").get(draft.draft_id).status == "pending"


@pytest.mark.asyncio
async def test_acquire_while_close_drains_never_starts_second_browser(session_pool, browser_factory):
    entry = await session_pool.acquire("acc1")
    await entry.lock.acquire()
    close = asyncio.create_task(session_pool.close("acc1"))
    acquire = None
    try:
        await asyncio.sleep(0)
        acquire = asyncio.create_task(session_pool.acquire("acc1"))
        await asyncio.sleep(0.03)
        assert len(browser_factory.created) == 1
        assert not acquire.done()
    finally:
        entry.lock.release()
        await close
        if acquire is not None:
            await acquire
        await session_pool.close_all()


def test_draft_journal_corruption_never_restores_earlier_pending_state(tmp_path):
    path = tmp_path / "drafts.jsonl"
    store = DraftStore(path)
    store.create("synthetic-account", "post_tweet", {"text": "synthetic"}, "synthetic")
    with path.open("a", encoding="utf-8") as stream:
        stream.write('{"synthetic_interrupted_record":')
    with pytest.raises(ValueError, match="journal"):
        DraftStore(path)


def test_interrupted_approval_is_uncertain_after_restart(tmp_path):
    path = tmp_path / "drafts.jsonl"
    store = DraftStore(path)
    draft = store.create("synthetic-account", "post_tweet", {"text": "synthetic"}, "synthetic")
    store.set_status(draft.draft_id, "approved")
    assert DraftStore(path).get(draft.draft_id).status == "uncertain"


@pytest.mark.asyncio
async def test_shutdown_cancels_background_cycles_before_closing_sessions():
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def cycle():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def close_all():
        assert cancelled.is_set()

    task = asyncio.create_task(cycle())
    await started.wait()
    ctx = SimpleNamespace(runs={"synthetic-run": {"status": "running", "task": task}},
                          session_pool=SimpleNamespace(close_all=close_all))
    try:
        await shutdown(SimpleNamespace(xuse_ctx=ctx))
        assert task.done()
        assert ctx.runs["synthetic-run"]["status"] == "cancelled"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_confirmed_write_retains_action_id_if_draft_outcome_append_fails(async_server, monkeypatch):
    ctx = async_server.xuse_ctx
    draft = await call_tool(async_server, "post_tweet", {"account": "acc1", "text": "synthetic"})
    append = ctx.draft_store._append

    def fail_outcome(candidate):
        if candidate.status == "executed":
            raise OSError("synthetic outcome append failure")
        append(candidate)

    monkeypatch.setattr(ctx.draft_store, "_append", fail_outcome)
    result = await call_tool(async_server, "approve_draft", {"draft_id": draft["draft_id"]})
    assert not result["ok"]
    action_id = result["error"]["action_id"]
    assert ctx.safety_store.reference("acc1", action_id)["status"] == "succeeded"
    assert ctx.draft_store.get(draft["draft_id"]).status == "approved"
    repeated = await call_tool(async_server, "approve_draft", {"draft_id": draft["draft_id"]})
    assert not repeated["ok"]
    assert ctx.session_pool.browser.calls == [("post", "synthetic")]


@pytest.mark.asyncio
@pytest.mark.parametrize("return_type", [dict[str, Any], Dict[str, Any]])
async def test_queue_journal_errors_preserve_protocol_recovery_ids(return_type):
    server = FastMCP("isolated-queue-audit")

    async def failed_outcome() -> dict:
        raise QueueJournalError("synthetic-queue", "synthetic-action")

    failed_outcome.__annotations__["return"] = return_type
    server.tool()(guard(failed_outcome))
    response = await server._mcp_server.request_handlers[types.CallToolRequest](
        types.CallToolRequest(method="tools/call", params=types.CallToolRequestParams(name="failed_outcome", arguments={})))
    result = response.root
    assert result.isError
    envelope = json.loads(result.content[0].text)
    assert envelope["error"]["reason"] == "queue_journal_failure"
    assert envelope["error"]["queue_id"] == "synthetic-queue"
    assert envelope["error"]["action_id"] == "synthetic-action"
