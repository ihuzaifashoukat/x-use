"""New owner analytics and inbox options retain the shared read policy."""
import pytest
from mcp.server.fastmcp import FastMCP

from xuse.browser.errors import BrowserBlocked
from xuse.mcp.analytics_tools import register_analytics_tools
from xuse.mcp.browser_tools import register_browser_tools
from helpers import assert_error_envelope, call_tool
from test_profile_read_tools import profile_server  # noqa: F401


@pytest.fixture
def read_server(profile_server):
    env = profile_server
    env.server = FastMCP("bounded-owner-reads")
    register_browser_tools(env.server, env.ctx)
    register_analytics_tools(env.server, env.ctx)
    browser = env.pool.browser

    async def get_account_analytics(limit):
        browser.read("get_account_analytics", limit)
        return {"status": "premium_required", "owner": {"handle": "owner"}, "metrics": [],
                "available": False, "period": None, "partial": True, "source": "browser_dom"}

    async def get_inbox(limit, *, inbox_filter, unread_first, folder):
        browser.read("get_inbox", limit, inbox_filter, unread_first, folder)
        return {"conversations": [], "partial": True, "unread_unknown_count": 2,
                "inbox_filter": inbox_filter, "source": "browser_dom"}

    async def search_conversations(query, limit, *, inbox_filter, unread_first, folder):
        browser.read("search_conversations", query, limit, inbox_filter, unread_first, folder)
        return {"conversations": [], "partial": True, "inbox_filter": inbox_filter}

    async def get_conversation(conversation_id, limit, *, before_message_id):
        browser.read("get_conversation", conversation_id, limit, before_message_id)
        return {"messages": [], "partial": True, "history_coverage": "visible_only"}

    browser.get_account_analytics = get_account_analytics
    browser.get_inbox = get_inbox
    browser.search_conversations = search_conversations
    browser.get_conversation = get_conversation
    return env


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,arguments,expected", [
    ("get_account_analytics", {}, ("get_account_analytics", 20)),
    ("get_inbox", {}, ("get_inbox", 20, "all", False, "inbox")),
    ("get_inbox", {"inbox_filter": "unread", "unread_first": True}, ("get_inbox", 20, "unread", True, "inbox")),
    ("get_inbox", {"folder": "other"}, ("get_inbox", 20, "all", False, "other")),
    ("search_conversations", {"query": "hello", "inbox_filter": "read", "folder": "requests"}, ("search_conversations", "hello", 20, "read", False, "requests")),
    ("get_conversation", {"conversation_id": "10-20", "before_message_id": "stable"}, ("get_conversation", "10-20", 50, "stable")),
])
async def test_registered_reads_forward_options_once_and_do_not_stage(read_server, tool, arguments, expected):
    env = read_server
    result = await call_tool(env.server, tool, {"account": "a", **arguments})
    assert result["ok"] and env.pool.browser.calls == [expected]
    assert env.pool.started == 1 and result["partial"]
    assert env.ctx.safety_store.status("a")["daily_used"] == {"read": 1}
    assert len(env.ctx.draft_store) == len(env.ctx.queue_store) == 0 and env.llm_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,arguments", [
    ("get_account_analytics", {"limit": 0}), ("get_account_analytics", {"limit": 51}),
    ("get_inbox", {"inbox_filter": "unknown"}), ("get_inbox", {"limit": 51}),
    ("search_conversations", {"query": "hello", "inbox_filter": "Unread"}),
    ("search_conversations", {"query": "   "}),
    ("get_conversation", {"conversation_id": "https://evil.test/messages/10-20"}),
    ("get_conversation", {"conversation_id": "10-20", "before_message_id": " "}),
])
async def test_invalid_options_fail_before_read_policy_reservation(read_server, tool, arguments):
    env = read_server
    result = await call_tool(env.server, tool, {"account": "a", **arguments})
    assert_error_envelope(result)
    assert env.pool.started == 0 and env.pool.browser.calls == []
    assert env.ctx.safety_store.status("a")["daily_used"] == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ["inactive", "missing"])
async def test_analytics_requires_active_configured_account(read_server, account):
    env = read_server
    assert_error_envelope(await call_tool(env.server, "get_account_analytics", {"account": account}))
    assert env.pool.started == 0 and env.pool.browser.calls == []


@pytest.mark.asyncio
async def test_analytics_has_readonly_annotation_and_distinct_scope(read_server):
    env = read_server
    tool = next(tool for tool in await env.server.list_tools() if tool.name == "get_account_analytics")
    assert tool.annotations.readOnlyHint is True and tool.annotations.openWorldHint is True
    result = await call_tool(env.server, tool.name, {"account": "a"})
    assert result["status"] == "premium_required" and result["metrics"] == []
    assert not result["available"] and result["period"] is None


@pytest.mark.asyncio
async def test_analytics_refuses_legacy_backend_without_startup(read_server):
    env = read_server
    env.pool.backend = "selenium"
    assert_error_envelope(await call_tool(env.server, "get_account_analytics", {"account": "a"}))
    assert env.pool.started == 0


@pytest.mark.asyncio
async def test_analytics_login_block_is_structured_and_pauses_reads(read_server):
    env = read_server
    env.pool.browser.failure = BrowserBlocked("login_required")
    error = assert_error_envelope(await call_tool(env.server, "get_account_analytics", {"account": "a"}))
    assert error["reason"] == "login_required" and env.ctx.safety_store.status("a")["paused"]


@pytest.mark.asyncio
async def test_analytics_pin_safe_read_does_not_clear_durable_pause(read_server):
    env = read_server
    env.ctx.safety_store.pause("a", "pin_required")
    result = await call_tool(env.server, "get_account_analytics", {"account": "a"})
    assert result["ok"] and result["status"] == "premium_required"
    assert env.ctx.safety_store.status("a")["pause"]["reason"] == "pin_required"


@pytest.mark.asyncio
async def test_inbox_folder_schema_exposes_exact_choices(read_server):
    tools = {tool.name: tool for tool in await read_server.server.list_tools()}
    for name in ("get_inbox", "search_conversations"):
        folder = tools[name].inputSchema["properties"]["folder"]
        assert folder["enum"] == ["inbox", "requests", "other"]
        assert folder["default"] == "inbox"
