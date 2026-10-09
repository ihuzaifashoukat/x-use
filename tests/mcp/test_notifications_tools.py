"""Structured notification reads retain typed schemas and the shared policy."""
import json

import pytest
from mcp import types
from mcp.server.fastmcp import FastMCP

from xuse.mcp.notifications_tools import register_notifications_tools
from helpers import assert_error_envelope, call_tool
from test_profile_read_tools import profile_server  # noqa: F401


@pytest.fixture
def notifications_server(profile_server):
    env = profile_server
    env.server = FastMCP("notifications-contract")
    register_notifications_tools(env.server, env.ctx)

    async def notifications(limit, *, view):
        env.pool.browser.read("get_notifications", limit, view)
        return {"notifications": [{"text": "Synthetic notification", "type": "unknown", "unread": None,
                                    "notification_id": None, "actors": [], "related_posts": []}],
                "view": view, "view_verified": True, "count": 1, "partial": True,
                "coverage": "visible_only", "source": "browser_dom",
                "unread_side_effects": "Visiting notifications may mark them read on X."}

    env.pool.browser.get_notifications = notifications
    return env


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments,expected", [({}, (20, "all")), ({"view": "mentions", "limit": 7}, (7, "mentions"))])
async def test_notifications_options_forwarded_once_with_unknown_state(notifications_server, arguments, expected):
    env = notifications_server
    result = await call_tool(env.server, "get_notifications", {"account": "a", **arguments})
    assert result["ok"] and result["notifications"][0]["unread"] is None
    assert env.pool.browser.calls == [("get_notifications", *expected)]
    assert env.ctx.safety_store.status("a")["daily_used"] == {"read": 1}
    assert len(env.ctx.draft_store) == len(env.ctx.queue_store) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [{"limit": 0}, {"limit": 51}, {"view": "verified"}, {"view": "Mentions"}])
async def test_notifications_invalid_options_fail_before_browser(notifications_server, arguments):
    env = notifications_server
    # The protocol validates Literal values before invoking the handler.
    response = await env.server._mcp_server.request_handlers[types.CallToolRequest](
        types.CallToolRequest(method="tools/call", params=types.CallToolRequestParams(
            name="get_notifications", arguments={"account": "a", **arguments})))
    assert response.root.isError
    assert env.pool.started == 0 and env.pool.browser.calls == []
    assert env.ctx.safety_store.status("a")["daily_used"] == {}


@pytest.mark.asyncio
async def test_notifications_legacy_backend_disabled_without_startup(notifications_server):
    env = notifications_server
    env.pool.backend = "selenium"
    assert_error_envelope(await call_tool(env.server, "get_notifications", {"account": "a"}))
    assert env.pool.started == 0


@pytest.mark.asyncio
async def test_notifications_protocol_schema_and_read_annotation(notifications_server):
    env = notifications_server
    tool, = await env.server.list_tools()
    assert tool.name == "get_notifications"
    assert tool.inputSchema["properties"]["view"]["enum"] == ["all", "mentions"]
    assert set(tool.inputSchema["required"]) == {"account"}
    assert tool.annotations.readOnlyHint and tool.annotations.openWorldHint
    result = await env.server._mcp_server.request_handlers[types.CallToolRequest](
        types.CallToolRequest(method="tools/call", params=types.CallToolRequestParams(
            name="get_notifications", arguments={"account": "a"})))
    assert not result.root.isError
    assert result.root.structuredContent == json.loads(result.root.content[0].text)
