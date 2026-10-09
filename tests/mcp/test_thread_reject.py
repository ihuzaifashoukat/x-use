"""Pending thread rejection revokes journal authorization under run ownership."""
import asyncio

import pytest

from helpers import call_tool
from test_thread_tools import integrated_server, thread_server  # noqa: F401
from xuse.mcp import thread_tools


async def blocked_thread(thread_server, tmp_path):
    _, ctx, _ = thread_server
    server = integrated_server(thread_server, tmp_path)
    ctx.safety_store.caps["post"] = 0
    prepared = await call_tool(server, "prepare_thread", {
        "account": "acc1", "posts": [{"text": "exact reviewed text"}]})
    approved = await call_tool(server, "approve_draft", {"draft_id": prepared["draft_id"]})
    assert approved["status"] == "pending"
    assert ctx.thread_store.get(prepared["run_id"])["authorized"]
    assert not ctx.session_pool.browser.calls
    return server, ctx, prepared


@pytest.mark.asyncio
async def test_reject_pending_blocked_thread_revokes_durable_authorization(thread_server, tmp_path):
    server, ctx, prepared = await blocked_thread(thread_server, tmp_path)
    rejected = await call_tool(server, "reject_draft", {"draft_id": prepared["draft_id"]})
    assert rejected["ok"] and rejected["status"] == "rejected"
    run = ctx.thread_store.get(prepared["run_id"])
    assert run["state"] == "cancelled" and not run["authorized"]
    ctx.safety_store.caps["post"] = 5
    continued = await call_tool(server, "continue_thread", {"run_id": prepared["run_id"]})
    assert continued["state"] == "cancelled" and continued["status"] == "rejected"
    assert not ctx.session_pool.browser.calls


@pytest.mark.asyncio
async def test_reject_cannot_claim_success_during_pending_thread_continuation(thread_server, tmp_path, monkeypatch):
    server, ctx, prepared = await blocked_thread(thread_server, tmp_path)
    ctx.safety_store.caps["post"] = 5
    entered, release = asyncio.Event(), asyncio.Event()
    original = thread_tools._check_media

    async def held_preflight(post):
        entered.set()
        await release.wait()
        await original(post)

    monkeypatch.setattr(thread_tools, "_check_media", held_preflight)
    task = asyncio.create_task(call_tool(server, "continue_thread", {"run_id": prepared["run_id"]}))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        rejected = await call_tool(server, "reject_draft", {"draft_id": prepared["draft_id"]})
        assert not rejected["ok"] and rejected["error"]["reason"] == "thread_busy"
        assert ctx.draft_store.get(prepared["draft_id"]).status == "pending"
        assert ctx.thread_store.get(prepared["run_id"])["authorized"]
    finally:
        release.set()
    continued = await asyncio.wait_for(task, 2)
    assert continued["state"] == "complete" and len(ctx.session_pool.browser.calls) == 1


@pytest.mark.asyncio
async def test_reject_pending_interrupted_thread_retains_uncertain_evidence(thread_server, tmp_path):
    _, ctx, _ = thread_server
    server = integrated_server(thread_server, tmp_path)
    prepared = await call_tool(server, "prepare_thread", {
        "account": "acc1", "posts": [{"text": "exact reviewed text"}]})
    ctx.thread_store.authorize(prepared["run_id"])
    ctx.thread_store.start(prepared["run_id"], 0, None)
    rejected = await call_tool(server, "reject_draft", {"draft_id": prepared["draft_id"]})
    assert rejected["ok"]
    run = ctx.thread_store.get(prepared["run_id"])
    assert run["state"] == "cancelled" and not run["authorized"]
    assert run["segments"][0]["state"] == "uncertain"
    assert run["error"]["previous_error"]["reason"] == "interrupted_write"
    assert not ctx.session_pool.browser.calls
