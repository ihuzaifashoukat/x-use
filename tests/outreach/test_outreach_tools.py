"""MCP envelopes/annotations and active-account boundaries without a browser."""
import json
from types import SimpleNamespace

import pytest
from mcp.server.fastmcp import FastMCP

from xuse.mcp.drafts import DraftStore
from xuse.mcp.outreach_tools import register_outreach_tools
from xuse.mcp.sessions import SessionError
from xuse.outreach import OutreachStore


@pytest.fixture
def outreach_server(tmp_path, make_config_loader):
    accounts = [{"account_id": "a", "is_active": True},
                {"account_id": "paused", "is_active": False}]
    config = make_config_loader(accounts=accounts)

    class LocalAccountLookup:
        def find_account_dict(self, account):
            for raw in accounts:
                if raw["account_id"] == account:
                    return raw
            raise SessionError("Unknown account.")

    ctx = SimpleNamespace(config_loader=config, session_pool=LocalAccountLookup(),
                          draft_store=DraftStore(tmp_path / "drafts.jsonl"), draft_mode=False,
                          outreach_store=OutreachStore(tmp_path / "outreach.sqlite3"))
    server = FastMCP("local-outreach-test")
    register_outreach_tools(server, ctx)
    return server, ctx


async def invoke(server, tool_name, **arguments):
    from mcp.types import CallToolResult
    result = await server.call_tool(tool_name, arguments)
    content = result.content if isinstance(result, CallToolResult) else result[0]
    return json.loads(content[0].text)


@pytest.mark.asyncio
async def test_local_tools_workflow_stages_even_with_draft_mode_off(outreach_server):
    server, ctx = outreach_server
    lead = (await invoke(server, "upsert_lead", account="a", handle="Ada",
                         display_name="Ada", notes="private"))["lead"]
    campaign = (await invoke(server, "create_campaign", account="a", name="Hello",
                             message_template="Hello {name}"))["campaign"]
    added = await invoke(server, "add_campaign_leads", account="a",
                         campaign_id=campaign["campaign_id"], lead_ids=[lead["lead_id"]])
    assert added["ok"] is True
    prepared = await invoke(server, "prepare_campaign_messages", account="a",
                            campaign_id=campaign["campaign_id"])
    assert prepared["ok"] is True and prepared["count"] == 1
    assert prepared["drafts"][0]["action"] == "send_message"
    assert prepared["drafts"][0]["payload"]["text"] == "Hello Ada"
    assert ctx.draft_store.list()[0].status == "pending"
    tools = await server.list_tools()
    assert len(tools) == 12
    assert all(tool.annotations.openWorldHint is False for tool in tools)
    reads = {"list_leads", "get_lead", "get_campaign", "list_campaigns", "get_campaign_summary"}
    assert all(tool.annotations.readOnlyHint == (tool.name in reads) for tool in tools)


@pytest.mark.asyncio
async def test_active_account_preparation_and_account_isolation(outreach_server):
    server, _ = outreach_server
    lead = (await invoke(server, "upsert_lead", account="paused", handle="Ada"))["lead"]
    campaign = (await invoke(server, "create_campaign", account="paused", name="Hello",
                             message_template="Hi {handle}"))["campaign"]
    await invoke(server, "add_campaign_leads", account="paused",
                 campaign_id=campaign["campaign_id"], lead_ids=[lead["lead_id"]])
    result = await invoke(server, "prepare_campaign_messages", account="paused",
                          campaign_id=campaign["campaign_id"])
    assert result["ok"] is False and "paused" in result["error"]["message"]
    foreign = await invoke(server, "get_lead", account="a", lead_id=lead["lead_id"])
    assert foreign["ok"] is False and foreign["error"]["type"] == "ToolError"
    unknown = await invoke(server, "list_leads", account="missing")
    assert unknown["ok"] is False


@pytest.mark.asyncio
async def test_opt_out_and_bad_templates_return_sanitized_envelopes(outreach_server, caplog):
    server, ctx = outreach_server
    lead = (await invoke(server, "upsert_lead", account="a", handle="Ada",
                         notes="PRIVATE_NOTE_SENTINEL"))["lead"]
    opted_out = await invoke(server, "opt_out_lead", account="a", lead_id=lead["lead_id"])
    assert opted_out["lead"]["opt_out"] is True
    reset = await invoke(server, "update_lead_status", account="a", lead_id=lead["lead_id"], status="new")
    assert reset["ok"] is False
    invalid = await invoke(server, "create_campaign", account="a", name="Hello",
                           message_template="Hi {PRIVATE_NOTE_SENTINEL}")
    assert invalid["ok"] is False and invalid["error"]["type"] == "ToolError"
    assert "PRIVATE_NOTE_SENTINEL" not in invalid["error"]["message"]
    assert "PRIVATE_NOTE_SENTINEL" not in caplog.text
    assert ctx.outreach_store.is_suppressed("a", "@Ada")
