"""Draft-mode tests: write tools return reviewable drafts and execute nothing
by default; approve_draft executes exactly once through the real execution
path (fake publisher asserts a single call); re-approval and unknown ids are
rejected with structured errors; drafts persist to a JSONL file.

No browser, no network — publisher, metrics, dedup, and LLM seams are faked.
"""
import json
from unittest.mock import MagicMock

import pytest

from xuse.mcp.drafts import DraftStore

from helpers import (  # noqa: F401 — imported fixtures register for this module
    FakeMetrics,
    accounts,
    assert_error_envelope,
    browser_factory,
    call_tool,
    config_loader,
    draft_store,
    drafts_path,
    make_fake_publisher,
    mcp_server,
    mcp_settings,
    queue_store,
    session_pool,
)


@pytest.fixture
def publisher_instances():
    return []


@pytest.fixture
def stubbed_ctx(mcp_server, monkeypatch, publisher_instances):
    """Stub the execution seams on the server's ctx so approve_draft runs the
    real path (resolve -> dedup -> pace -> session -> publisher -> metrics)
    without any browser, file, or LLM side effects."""
    ctx = mcp_server.xuse_ctx
    ctx.processed_keys = set()
    ctx.file_handler = MagicMock(name="file_handler")
    ctx.llm_service = MagicMock(name="llm_service")
    metrics = {}
    ctx.metrics_factory = lambda account_id: metrics.setdefault(account_id, FakeMetrics(account_id))
    monkeypatch.setattr("xuse.mcp.actions.TweetPublisher", make_fake_publisher(publisher_instances))
    return ctx


def total_publisher_calls(instances) -> int:
    return sum(len(p.calls) for p in instances)


@pytest.mark.asyncio
async def test_draft_mode_is_on_by_default(mcp_server):
    assert mcp_server.xuse_ctx.draft_mode is True


@pytest.mark.asyncio
async def test_write_tool_returns_draft_and_executes_nothing(mcp_server, browser_factory, session_pool):
    result = await call_tool(mcp_server, "post_tweet", {"account": "acc1", "text": "hello world"})

    assert result["ok"] is True
    assert result["account"] == "acc1"
    assert result["action"] == "post_tweet"
    assert result["status"] == "pending"
    assert result["payload"]["text"] == "hello world"
    assert "hello world" in result["preview"]
    assert isinstance(result["draft_id"], str) and result["draft_id"]

    # Nothing executed: no browser cold start, no warm session.
    assert browser_factory.created == []
    assert list(session_pool.active_accounts) == []


@pytest.mark.asyncio
async def test_approve_draft_executes_exactly_once(mcp_server, stubbed_ctx, publisher_instances):
    draft = await call_tool(mcp_server, "post_tweet", {"account": "acc1", "text": "review me"})
    assert total_publisher_calls(publisher_instances) == 0

    approved = await call_tool(mcp_server, "approve_draft", {"draft_id": draft["draft_id"]})
    assert approved["ok"] is True
    assert approved["status"] == "executed"
    assert approved["result"]["success"] is True
    assert total_publisher_calls(publisher_instances) == 1
    assert publisher_instances[0].calls[0]["text"] == "review me"

    # Second approval is rejected and executes nothing more.
    again = await call_tool(mcp_server, "approve_draft", {"draft_id": draft["draft_id"]})
    error = assert_error_envelope(again, "already executed")
    assert error["type"] == "ToolError"
    assert total_publisher_calls(publisher_instances) == 1

    await stubbed_ctx.session_pool.close_all()


@pytest.mark.asyncio
async def test_approve_unknown_draft_id_rejected(mcp_server, stubbed_ctx, publisher_instances):
    result = await call_tool(mcp_server, "approve_draft", {"draft_id": "nope"})
    error = assert_error_envelope(result, "Unknown draft_id")
    assert error["type"] == "ToolError"
    assert total_publisher_calls(publisher_instances) == 0


@pytest.mark.asyncio
async def test_drafts_persist_to_tmp_jsonl_and_reload(
    mcp_server, stubbed_ctx, publisher_instances, drafts_path
):
    draft = await call_tool(mcp_server, "post_tweet", {"account": "acc1", "text": "persist me"})

    # The draft was appended to the JSONL persistence file as pending.
    assert drafts_path.is_file()
    lines = [json.loads(ln) for ln in drafts_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    pending = [ln for ln in lines if ln["draft_id"] == draft["draft_id"]]
    assert pending and pending[-1]["status"] == "pending"
    assert pending[-1]["action"] == "post_tweet"
    assert pending[-1]["payload"]["text"] == "persist me"

    # A fresh store over the same file sees the pending draft (restart survival).
    reloaded = DraftStore(drafts_path)
    assert reloaded.get(draft["draft_id"]).status == "pending"

    approved = await call_tool(mcp_server, "approve_draft", {"draft_id": draft["draft_id"]})
    assert approved["ok"] is True

    # Status transitions were appended too; last write wins on reload.
    reloaded_after = DraftStore(drafts_path)
    assert reloaded_after.get(draft["draft_id"]).status == "executed"

    await stubbed_ctx.session_pool.close_all()
