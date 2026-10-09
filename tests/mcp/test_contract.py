"""MCP contract tests: the server exposes exactly the documented tools — the
count is pinned by ``EXPECTED_TOOLS`` —
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

# Every documented tool and the parameter names each must accept.
EXPECTED_TOOLS: Dict[str, Dict[str, Any]] = {
    "get_thread": {"params": {"tweet_url", "account", "limit", "include_images"}, "required": {"tweet_url"}},
    "prepare_thread": {"params": {"account", "posts", "reply_to"}, "required": {"account", "posts"}},
    "get_thread_run": {"params": {"run_id"}, "required": {"run_id"}},
    "continue_thread": {"params": {"run_id"}, "required": {"run_id"}},
    "cancel_thread": {"params": {"run_id"}, "required": {"run_id"}},
    "list_accounts": {"params": set(), "required": set()},
    "get_metrics": {"params": {"account"}, "required": {"account"}},
    "get_account_analytics": {"params": {"account", "limit"}, "required": {"account"}},
    "search_tweets": {"params": {"keywords", "limit", "account", "include_images"},
                      "required": {"keywords"}},
    "search_profile": {"params": {"profile", "limit", "account", "include_images"},
                       "required": {"profile"}},
    "get_tweet": {"params": {"account", "tweet_url", "include_images"}, "required": {"account", "tweet_url"}},
    "approve_draft": {"params": {"draft_id"}, "required": {"draft_id"}},
    "post_tweet": {"params": {"account", "text", "media", "community"}, "required": {"account", "text"}},
    "generate_and_post": {"params": {"account", "topic"}, "required": {"account", "topic"}},
    "reply_to_tweet": {"params": {"account", "tweet_url", "text", "media"}, "required": {"account", "tweet_url"}},
    "prepare_reply": {"params": {"account", "tweet_url", "include_images"}, "required": {"account", "tweet_url"}},
    "engage": {"params": {"account", "keywords", "actions", "max_actions"}, "required": {"account", "keywords"}},
    "run_cycle": {"params": {"account", "pipelines"}, "required": set()},
    "queue_post": {"params": {"account", "text", "topic", "media", "community", "not_before"},
                   "required": {"account"}},
    "queue_engagement": {"params": {"account", "action", "tweet_url", "text"},
                         "required": {"account", "action", "tweet_url"}},
    "list_queue": {"params": {"account", "status", "limit", "offset"}, "required": set()},
    "cancel_queued_action": {"params": {"queue_id"}, "required": {"queue_id"}},
    "process_queue": {"params": {"account", "max_actions"}, "required": set()},
    "get_account": {"params": {"account"}, "required": {"account"}},
    "add_account": {"params": {"account_id", "cookie_file", "proxy", "target_keywords",
                               "persona", "is_active"}, "required": {"account_id"}},
    "update_account": {"params": {"account", "is_active", "proxy", "target_keywords",
                                  "competitor_profiles", "self_handles", "persona",
                                  "cookie_file", "action_config"}, "required": {"account"}},
    "set_account_active": {"params": {"account", "active"}, "required": {"account", "active"}},
    "remove_account": {"params": {"account", "confirm"}, "required": {"account"}},
    "list_drafts": {"params": {"status", "account", "limit"}, "required": set()},
    "get_draft": {"params": {"draft_id"}, "required": {"draft_id"}},
    "reject_draft": {"params": {"draft_id"}, "required": {"draft_id"}},
    "get_run_status": {"params": {"run_id"}, "required": set()},
    "get_account_health": {"params": {"account"}, "required": {"account"}},
    "research_and_stage": {"params": {"account", "keywords", "max_items"},
                           "required": {"account", "keywords"}},
    "draft_post_variations": {"params": {"account", "topic", "count"},
                              "required": {"account", "topic"}},
    "list_proxies": {"params": set(), "required": set()},
    "add_proxy": {"params": {"pool", "proxy_url"}, "required": {"pool", "proxy_url"}},
    "remove_proxy": {"params": {"pool", "proxy_url", "confirm"}, "required": {"pool", "proxy_url"}},
    "test_proxy": {"params": {"account", "proxy_url"}, "required": set()},
    "get_inbox": {"params": {"account", "limit", "inbox_filter", "unread_first", "folder"}, "required": {"account"}},
    "get_conversation": {"params": {"account", "conversation_id", "limit", "before_message_id"}, "required": {"account", "conversation_id"}},
    "search_conversations": {"params": {"account", "query", "limit", "inbox_filter", "unread_first", "folder"}, "required": {"account", "query"}},
    "send_message": {"params": {"account", "recipient", "text"}, "required": {"account", "recipient", "text"}},
    "follow_profile": {"params": {"account", "profile"}, "required": {"account", "profile"}},
    "get_profile": {"params": {"account", "profile"}, "required": {"account", "profile"}},
    "get_profile_context": {"params": {"account", "profile", "post_limit"}, "required": {"account", "profile"}},
    "get_profile_posts": {"params": {"account", "profile", "feed", "limit"}, "required": {"account", "profile"}},
    "get_profile_connections": {"params": {"account", "profile", "relationship", "limit"}, "required": {"account", "profile"}},
    "prepare_outreach": {"params": {"account", "profile", "message_text", "post_limit"}, "required": {"account", "profile", "message_text"}},
    "get_home_feed": {"params": {"account", "limit"}, "required": {"account"}},
    "get_notifications": {"params": {"account", "limit", "view"}, "required": {"account"}},
    "get_session_status": {"params": {"account"}, "required": set()},
    "close_session": {"params": {"account"}, "required": {"account"}},
    "get_account_safety": {"params": {"account"}, "required": {"account"}},
    "pause_account_actions": {"params": {"account"}, "required": {"account"}},
    "resume_account_actions": {"params": {"account"}, "required": {"account"}},
    "resolve_action_outcome": {"params": {"account", "action_id", "observed_outcome"}, "required": {"account", "action_id", "observed_outcome"}},
    "unlock_inbox": {"params": {"account", "pin_env_var"}, "required": {"account"}},
    "upsert_lead": {"params": {"account", "handle", "display_name", "company", "notes", "tags", "status"}, "required": {"account", "handle"}},
    "list_leads": {"params": {"account", "status", "tag", "limit", "offset"}, "required": {"account"}},
    "get_lead": {"params": {"account", "lead_id"}, "required": {"account", "lead_id"}},
    "update_lead_status": {"params": {"account", "lead_id", "status"}, "required": {"account", "lead_id", "status"}},
    "opt_out_lead": {"params": {"account", "lead_id"}, "required": {"account", "lead_id"}},
    "create_campaign": {"params": {"account", "name", "message_template"}, "required": {"account", "name", "message_template"}},
    "get_campaign": {"params": {"account", "campaign_id", "limit", "offset"}, "required": {"account", "campaign_id"}},
    "list_campaigns": {"params": {"account", "status", "limit", "offset"}, "required": {"account"}},
    "add_campaign_leads": {"params": {"account", "campaign_id", "lead_ids"}, "required": {"account", "campaign_id", "lead_ids"}},
    "set_campaign_status": {"params": {"account", "campaign_id", "status"}, "required": {"account", "campaign_id", "status"}},
    "prepare_campaign_messages": {"params": {"account", "campaign_id", "max_messages"}, "required": {"account", "campaign_id"}},
    "get_campaign_summary": {"params": {"account", "campaign_id"}, "required": {"account", "campaign_id"}},
}


@pytest.mark.asyncio
async def test_server_registers_exactly_the_expected_tools(mcp_server):
    """The tool inventory is pinned by EXPECTED_TOOLS."""
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
