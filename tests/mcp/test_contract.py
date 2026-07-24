"""MCP contract tests: the server exposes exactly the nine documented tools,
their schemas accept the documented parameters, and tool failures return
structured error envelopes (``{"ok": false, "error": {...}}``) instead of
raising through the server (NFR-1).

All tests run against injected fakes — no browser, no network.
"""
from typing import Any, Dict

import pytest

from xuse.mcp.drafts import DraftStore
from xuse.mcp.server import create_server
from xuse.mcp.sessions import SessionPool

from helpers import (  # noqa: F401 — imported fixtures register for this module
    accounts,
    assert_error_envelope,
    browser_factory,
    call_tool,
    config_loader,
    draft_store,
    drafts_path,
    make_account,
    mcp_server,
    mcp_settings,
    queue_store,
    session_pool,
)

# The nine documented tools and the parameter names each must accept.
EXPECTED_TOOLS: Dict[str, Dict[str, Any]] = {
    "list_accounts": {"params": set(), "required": set()},
    "get_metrics": {"params": {"account"}, "required": {"account"}},
    "search_tweets": {"params": {"keywords", "limit", "account"}, "required": {"keywords"}},
    "approve_draft": {"params": {"draft_id"}, "required": {"draft_id"}},
    "post_tweet": {"params": {"account", "text", "media", "community"}, "required": {"account", "text"}},
    "generate_and_post": {"params": {"account", "topic"}, "required": {"account", "topic"}},
    "reply_to_tweet": {"params": {"account", "tweet_url", "text"}, "required": {"account", "tweet_url"}},
    "engage": {"params": {"account", "keywords", "actions", "max_actions"}, "required": {"account", "keywords"}},
    "run_cycle": {"params": {"account", "pipelines"}, "required": set()},
    "queue_post": {"params": {"account", "text", "topic", "media", "community", "not_before"},
                   "required": {"account"}},
    "queue_engagement": {"params": {"account", "action", "tweet_url", "text"},
                         "required": {"account", "action", "tweet_url"}},
    "list_queue": {"params": {"account", "status"}, "required": set()},
    "cancel_queued_action": {"params": {"queue_id"}, "required": {"queue_id"}},
    "process_queue": {"params": {"account", "max_actions"}, "required": set()},
}


@pytest.mark.asyncio
async def test_server_registers_exactly_the_documented_tools(mcp_server):
    tools = await mcp_server.list_tools()
    assert {t.name for t in tools} == set(EXPECTED_TOOLS)


@pytest.mark.asyncio
async def test_tool_schemas_accept_documented_params(mcp_server):
    tools = {t.name: t for t in await mcp_server.list_tools()}
    for name, spec in EXPECTED_TOOLS.items():
        schema = tools[name].inputSchema or {}
        properties = set(schema.get("properties", {}))
        required = set(schema.get("required", []))
        assert spec["params"] <= properties, f"{name}: missing params {spec['params'] - properties}"
        assert required == spec["required"], f"{name}: required {required} != {spec['required']}"


@pytest.mark.asyncio
async def test_unknown_account_returns_error_envelope(mcp_server):
    result = await call_tool(mcp_server, "post_tweet", {"account": "ghost", "text": "hi"})
    error = assert_error_envelope(result, "ghost")
    assert error["type"] == "SessionError"


@pytest.mark.asyncio
async def test_empty_tweet_text_returns_error_envelope(mcp_server):
    result = await call_tool(mcp_server, "post_tweet", {"account": "acc1", "text": "   "})
    error = assert_error_envelope(result, "must not be empty")
    assert error["type"] == "ToolError"


@pytest.mark.asyncio
async def test_unknown_draft_id_returns_error_envelope(mcp_server):
    result = await call_tool(mcp_server, "approve_draft", {"draft_id": "no-such-draft"})
    error = assert_error_envelope(result, "Unknown draft_id")
    assert error["type"] == "ToolError"


@pytest.mark.asyncio
async def test_engage_rejects_unsupported_action(mcp_server):
    result = await call_tool(
        mcp_server, "engage", {"account": "acc1", "keywords": ["ai"], "actions": ["teleport"]}
    )
    error = assert_error_envelope(result, "Unsupported engage action")
    assert error["type"] == "ToolError"


@pytest.mark.asyncio
async def test_run_cycle_without_accounts_returns_error_envelope(make_config_loader, drafts_path):
    loader = make_config_loader(settings={}, accounts=[])
    pool = SessionPool(loader, browser_factory=lambda d: None)
    server = create_server(config_loader=loader, session_pool=pool, draft_store=DraftStore(drafts_path))
    result = await call_tool(server, "run_cycle", {})
    error = assert_error_envelope(result, "No active account")
    assert error["type"] == "ToolError"


@pytest.mark.asyncio
async def test_run_cycle_rejects_unknown_pipeline_with_canonical_names(mcp_server):
    """Pipeline names are shared with the CLI (xuse.pipelines) — a stale alias
    like 'reposts' errors and the message lists the canonical names."""
    result = await call_tool(mcp_server, "run_cycle", {"pipelines": "reposts,likes"})
    error = assert_error_envelope(result, "Unknown pipeline")
    assert error["type"] == "ToolError"
    for name in ("competitor_reposts", "keyword_replies", "content_curation", "community_engagement"):
        assert name in error["message"]


@pytest.mark.asyncio
async def test_search_tweets_unknown_account_returns_error_envelope(mcp_server):
    result = await call_tool(mcp_server, "search_tweets", {"keywords": "ai", "account": "ghost"})
    error = assert_error_envelope(result, "ghost")
    assert error["type"] == "SessionError"


@pytest.mark.asyncio
async def test_cold_start_failure_returns_error_envelope_not_exception(make_config_loader, drafts_path):
    """A browser cold-start blowup surfaces as an envelope — never raised through."""

    def exploding_factory(account_dict):
        raise RuntimeError("boom: no chromedriver")

    loader = make_config_loader(settings={}, accounts=[make_account("acc1")])
    pool = SessionPool(loader, browser_factory=exploding_factory)
    server = create_server(
        config_loader=loader, session_pool=pool, draft_store=DraftStore(drafts_path), draft_mode=False
    )
    result = await call_tool(server, "search_tweets", {"keywords": "ai", "account": "acc1"})
    error = assert_error_envelope(result, "cold start")
    assert error["type"] == "SessionError"
    await pool.close_all()
