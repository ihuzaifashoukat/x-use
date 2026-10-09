"""Protocol diagnostics carry only bounded primitives, never source content."""
import json
from typing import Any, Dict

import pytest
from mcp import types
from mcp.server.fastmcp import FastMCP

from xuse.browser.errors import BrowserActionError
from xuse.mcp.executor import ToolError
from xuse.mcp.tools import guard


async def wire_error(error, return_type=dict[str, Any]):
    server = FastMCP("isolated-diagnostics-audit")

    async def unconfirmed() -> dict:
        raise error

    unconfirmed.__annotations__["return"] = return_type
    server.tool()(guard(unconfirmed))
    response = await server._mcp_server.request_handlers[types.CallToolRequest](
        types.CallToolRequest(method="tools/call", params=types.CallToolRequestParams(name="unconfirmed", arguments={})))
    return response.root


@pytest.mark.asyncio
@pytest.mark.parametrize("return_type", [dict[str, Any], Dict[str, Any]])
@pytest.mark.parametrize("stage", ["route_changed", "confirmation_timeout"])
async def test_send_diagnostics_wire_preserves_safe_primitives_and_recovery(return_type, stage):
    error = BrowserActionError("send_unconfirmed")
    error.action_id = "synthetic-action"
    expected = {"schema_version": 1, "stage": stage, "observer_armed": True,
                "observer_started": None, "composer_empty": False, "pending_count": 0,
                "failed_count": None, "scope_connected": True, "composer_connected": None,
                "send_control_connected": False}
    error.diagnostics = dict(expected)
    result = await wire_error(error, return_type)
    assert result.isError is True
    envelope = json.loads(result.content[0].text)
    assert envelope["error"]["reason"] == "send_unconfirmed"
    assert envelope["error"]["action_id"] == "synthetic-action"
    assert envelope["error"]["diagnostics"] == expected
    assert result.structuredContent.get("result", result.structuredContent) == envelope


@pytest.mark.asyncio
async def test_send_diagnostics_strips_source_content_and_invalid_primitive_values():
    error = BrowserActionError("send_unconfirmed")
    error.action_id = "synthetic-action"
    error.diagnostics = {"schema_version": 1, "stage": "confirmation_timeout", "observer_armed": False,
        "observer_started": "synthetic-private-value", "composer_empty": {"text": "synthetic-private-value"},
        "pending_count": True, "failed_count": 1001, "scope_connected": [True], "composer_connected": 1,
        "send_control_connected": "synthetic-private-value", "url": "https://example.invalid/synthetic-private-value",
        "text": "synthetic-private-value", "ids": ["synthetic-private-value"], "extra": {"secret": "synthetic-private-value"}}
    result = await wire_error(error)
    envelope = json.loads(result.content[0].text)
    assert envelope["error"]["diagnostics"] == {"schema_version": 1, "stage": "confirmation_timeout", "observer_armed": False}
    assert "synthetic-private-value" not in result.model_dump_json()
    assert result.isError and envelope["error"]["action_id"] == "synthetic-action"


@pytest.mark.asyncio
@pytest.mark.parametrize("diagnostics", [None, "synthetic-private-value", {"schema_version": True, "stage": "route_changed"},
    {"schema_version": 1.0, "stage": "route_changed"}, {"schema_version": 2, "stage": "route_changed"},
    {"schema_version": 1, "stage": ["route_changed"]}, {"schema_version": 1, "stage": "synthetic-private-value"}])
async def test_send_diagnostics_unsupported_schema_is_omitted(diagnostics):
    error = BrowserActionError("send_unconfirmed")
    error.diagnostics = diagnostics
    result = await wire_error(error)
    assert result.isError
    assert "diagnostics" not in json.loads(result.content[0].text)["error"]
    assert "synthetic-private-value" not in result.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [BrowserActionError("read_failed"), ToolError("synthetic failure")])
async def test_other_errors_never_receive_send_diagnostics(error):
    error.diagnostics = {"schema_version": 1, "stage": "route_changed"}
    if isinstance(error, ToolError):
        error.reason = "send_unconfirmed"
    result = await wire_error(error)
    assert "diagnostics" not in json.loads(result.content[0].text)["error"]
