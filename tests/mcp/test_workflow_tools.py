"""One bounded read and one reviewed draft; no external action or live account."""
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from mcp.server.fastmcp import FastMCP

from xuse.browser.errors import BrowserActionError
from xuse.mcp.drafts import DraftStore
from xuse.mcp.safety import SafetyStore
from xuse.mcp.workflow_tools import register_workflow_tools
from xuse.models import ScrapedTweet
from xuse.outreach import OutreachStore

from helpers import call_tool, make_account


@pytest.fixture
def workflow_server(make_config_loader, tmp_path):
    accounts = [make_account("a"), make_account("inactive", is_active=False), make_account("b")]
    loader = make_config_loader(accounts=accounts)

    class Browser:
        def __init__(self):
            self.calls = []
            self.after_read = None
            self.result = {"profile": {"handle": "ada", "name": "Ada", "bio": "Builds tools", "url": "https://x.com/ada"},
                           "posts": [ScrapedTweet(tweet_id="123", tweet_url="https://x.com/ada/status/123",
                                                  user_handle="@Ada", text_content="An interesting recent post.",
                                                  raw_element_data={"private_dom": object()})]}

        async def get_profile_context(self, handle, limit):
            self.calls.append(("get_profile_context", handle, limit))
            if self.after_read is not None:
                self.after_read()
            return self.result

    class Pool:
        backend = "patchright"

        def __init__(self):
            self.browser = Browser()
            self.started = 0

        def find_account_dict(self, account):
            for raw in accounts:
                if raw["account_id"] == account:
                    return raw
            from xuse.mcp.sessions import SessionError
            raise SessionError("Unknown account.")

        @asynccontextmanager
        async def session(self, account):
            self.started += 1
            yield self.browser

    ctx = SimpleNamespace(config_loader=loader, session_pool=Pool(), draft_mode=False,
                          draft_store=DraftStore(tmp_path / "drafts.jsonl"),
                          outreach_store=OutreachStore(tmp_path / "outreach.sqlite3"),
                          safety_store=SafetyStore(tmp_path / "policy.sqlite3"))
    server = FastMCP("workflow-tests")
    register_workflow_tools(server, ctx)
    return server, ctx


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["patchright", "playwright"])
async def test_one_context_read_creates_exact_pending_draft_with_clear_next_steps(workflow_server, backend):
    server, ctx = workflow_server
    ctx.session_pool.backend = backend
    text = "  Hi Ada — your post about tools helped me.\nCould we compare notes?  "
    result = await call_tool(server, "prepare_outreach", {
        "account": "a", "profile": "https://x.com/Ada", "message_text": text, "post_limit": 3})
    assert result["ok"] and result["recipient"] == "@ada"
    assert ctx.session_pool.browser.calls == [("get_profile_context", "ada", 3)]
    assert ctx.safety_store.status("a")["daily_used"] == {"read": 1}
    assert len(ctx.draft_store) == 1
    draft = ctx.draft_store.get(result["draft"]["draft_id"])
    assert draft.status == "pending" and draft.action == "send_message"
    assert draft.account == "a" and draft.payload == {"recipient": "@ada", "text": text}
    assert DraftStore(ctx.draft_store._path).get(draft.draft_id).payload == draft.payload
    assert result["context"]["posts"][0]["user_handle"] == "@Ada"
    assert "raw_element_data" not in result["context"]["posts"][0]
    assert result["context"]["partial"] and result["context"]["pagination"] == "visible_only"
    assert result["next_steps"]["review"] == {"tool": "get_draft", "arguments": {"draft_id": draft.draft_id}}
    assert result["next_steps"]["approve"]["tool"] == "approve_draft"
    assert result["next_steps"]["reject"]["tool"] == "reject_draft"
    assert result["next_steps"]["recovery"]["resolution_tool"] == "resolve_action_outcome"
    assert "Nothing was sent" in result["message"]
    tool = (await server.list_tools())[0]
    assert not tool.annotations.readOnlyHint and tool.annotations.openWorldHint
    assert not tool.annotations.destructiveHint and not tool.annotations.idempotentHint


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [
    {"post_limit": 0}, {"post_limit": 11}, {"message_text": ""},
    {"message_text": " "}, {"message_text": "x" * 10001}, {"message_text": "hello\x00"},
    {"profile": "https://evil.test/ada"}, {"account": "inactive"}, {"account": "unknown"},
])
async def test_invalid_or_inactive_input_fails_before_read_or_draft(workflow_server, arguments):
    server, ctx = workflow_server
    params = {"account": "a", "profile": "ada", "message_text": "Hello"}
    params.update(arguments)
    result = await call_tool(server, "prepare_outreach", params)
    assert not result["ok"]
    assert ctx.session_pool.started == 0 and len(ctx.draft_store) == 0


@pytest.mark.asyncio
async def test_legacy_backend_is_explicitly_unsupported_without_side_effects(workflow_server):
    server, ctx = workflow_server
    ctx.session_pool.backend = "selenium"
    result = await call_tool(server, "prepare_outreach", {"account": "a", "profile": "ada", "message_text": "Hello"})
    assert not result["ok"] and "async browser backend" in result["error"]["message"]
    assert ctx.session_pool.started == 0 and len(ctx.draft_store) == 0


@pytest.mark.asyncio
async def test_account_optout_prevents_read_and_draft_but_other_account_remains_eligible(workflow_server):
    server, ctx = workflow_server
    lead = ctx.outreach_store.upsert_lead("a", "ada", notes="PRIVATE_NOTE_SENTINEL")
    ctx.outreach_store.opt_out_lead("a", lead.lead_id)
    params = {"profile": "@Ada", "message_text": "Hello"}
    result = await call_tool(server, "prepare_outreach", {"account": "a", **params})
    assert not result["ok"] and "suppressed" in result["error"]["message"]
    assert ctx.session_pool.started == 0 and len(ctx.draft_store) == 0
    result = await call_tool(server, "prepare_outreach", {"account": "b", **params})
    assert result["ok"] and result["draft"]["account"] == "b"
    assert "PRIVATE_NOTE_SENTINEL" not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["optout", "inactive"])
async def test_eligibility_is_rechecked_after_context_read(workflow_server, change):
    server, ctx = workflow_server
    lead = ctx.outreach_store.upsert_lead("a", "ada")

    def change_status():
        if change == "optout":
            ctx.outreach_store.opt_out_lead("a", lead.lead_id)
        else:
            ctx.session_pool.find_account_dict("a")["is_active"] = False

    ctx.session_pool.browser.after_read = change_status
    result = await call_tool(server, "prepare_outreach", {"account": "a", "profile": "ada", "message_text": "Hello"})
    assert not result["ok"] and len(ctx.draft_store) == 0
    assert ctx.session_pool.started == 1


@pytest.mark.asyncio
async def test_context_read_failure_never_leaves_a_draft(workflow_server):
    server, ctx = workflow_server

    async def failed_read(*args):
        raise BrowserActionError("unsupported_dom")

    ctx.session_pool.browser.get_profile_context = failed_read
    result = await call_tool(server, "prepare_outreach", {"account": "a", "profile": "ada", "message_text": "Hello"})
    assert not result["ok"] and result["error"]["reason"] == "unsupported_dom"
    assert len(ctx.draft_store) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", ["profile", "author", "unknown_author", "bound", "format"])
async def test_unverified_context_is_rejected_without_private_error_data(workflow_server, malformed, caplog):
    server, ctx = workflow_server
    result = ctx.session_pool.browser.result
    if malformed == "profile":
        result["profile"]["handle"] = "SomeoneElse"
    elif malformed == "author":
        result["posts"][0].user_handle = "@Other"
    elif malformed == "unknown_author":
        result["posts"][0].user_handle = None
    elif malformed == "bound":
        result["posts"] *= 6
    else:
        result["posts"] = [{"tweet_id": {"private": "PRIVATE_CONTEXT_SENTINEL"}}]
    response = await call_tool(server, "prepare_outreach", {"account": "a", "profile": "ada", "message_text": "Hello"})
    assert not response["ok"] and response["error"]["type"] == "ToolError"
    assert len(ctx.draft_store) == 0
    assert "PRIVATE_CONTEXT_SENTINEL" not in json.dumps(response) + caplog.text


@pytest.mark.asyncio
async def test_policy_pause_blocks_context_and_drafting(workflow_server):
    server, ctx = workflow_server
    ctx.safety_store.pause("a", "manual_pause")
    result = await call_tool(server, "prepare_outreach", {"account": "a", "profile": "ada", "message_text": "Hello"})
    assert not result["ok"] and result["error"]["reason"] == "manual_pause"
    assert ctx.session_pool.started == 0 and len(ctx.draft_store) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("pagination,expected", [
    ("visible_only", "visible_only"), ("bounded_scroll", "bounded_scroll"),
    ("full_history", "visible_only"), (None, "visible_only"),
])
async def test_context_preserves_only_supported_partial_pagination(workflow_server, pagination, expected):
    server, ctx = workflow_server
    ctx.session_pool.browser.result["pagination"] = pagination
    ctx.session_pool.browser.result["partial"] = False
    result = await call_tool(server, "prepare_outreach", {"account": "a", "profile": "ada", "message_text": "Hello"})
    assert result["ok"]
    assert result["context"]["pagination"] == expected
    assert result["context"]["partial"] is True
