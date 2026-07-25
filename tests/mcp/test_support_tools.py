"""Support tool tests: draft listing/rejection, run status, account health."""
import pytest

from helpers import (  # noqa: F401 — imported fixtures register for this module
    accounts,
    assert_error_envelope,
    browser_factory,
    call_tool,
    config_loader,
    draft_store,
    drafts_path,
    mcp_server,
    mcp_settings,
    queue_store,
    session_pool,
)


async def make_draft(server, account="acc1", text="draft me"):
    result = await call_tool(server, "post_tweet", {"account": account, "text": text})
    assert result["ok"] is True
    return result["draft_id"]


@pytest.mark.asyncio
async def test_health_surfaces_corrupt_settings(make_config_loader, drafts_path,
                                                queue_store, tmp_path):
    """Corrupt-on-disk config: the loader records the parse error and health
    reports it (settings_error) instead of silently serving empty settings."""
    from xuse.mcp.drafts import DraftStore
    from xuse.mcp.server import create_server
    from xuse.mcp.sessions import SessionPool

    (tmp_path / "settings.json").write_text("{corrupt json", encoding="utf-8")
    (tmp_path / "accounts.json").write_text(
        '[{"account_id": "acc1", "is_active": true}]', encoding="utf-8")
    from xuse.core.config_loader import ConfigLoader
    loader = ConfigLoader(settings_file=tmp_path / "settings.json",
                          accounts_file=tmp_path / "accounts.json")
    server = create_server(
        config_loader=loader,
        session_pool=SessionPool(loader, browser_factory=lambda d: None),
        draft_store=DraftStore(drafts_path), queue_store=queue_store)
    result = await call_tool(server, "get_account_health", {"account": "acc1"})
    assert result["ok"] is True
    assert "settings_error" in result["config"]
    assert "corrupt" in result["config"]["settings_error"].lower() or \
           "parse" in result["config"]["settings_error"].lower()


@pytest.mark.asyncio
async def test_list_drafts_newest_first_and_filter(mcp_server):
    await make_draft(mcp_server, text="one")
    second = await make_draft(mcp_server, text="two")
    result = await call_tool(mcp_server, "list_drafts", {"status": "pending"})
    assert result["ok"] is True and result["total"] == 2
    assert result["drafts"][0]["draft_id"] == second  # newest first
    filtered = await call_tool(mcp_server, "list_drafts", {"account": "acc2"})
    assert filtered["total"] == 0


@pytest.mark.asyncio
async def test_list_drafts_rejects_bad_status(mcp_server):
    result = await call_tool(mcp_server, "list_drafts", {"status": "bogus"})
    assert_error_envelope(result, "Unknown draft status")


@pytest.mark.asyncio
async def test_get_draft_round_trip(mcp_server):
    draft_id = await make_draft(mcp_server)
    result = await call_tool(mcp_server, "get_draft", {"draft_id": draft_id})
    assert result["ok"] is True and result["draft"]["draft_id"] == draft_id
    missing = await call_tool(mcp_server, "get_draft", {"draft_id": "nope"})
    assert_error_envelope(missing, "Unknown draft_id")


@pytest.mark.asyncio
async def test_reject_draft_blocks_approval(mcp_server):
    draft_id = await make_draft(mcp_server)
    rejected = await call_tool(mcp_server, "reject_draft", {"draft_id": draft_id})
    assert rejected["ok"] is True and rejected["status"] == "rejected"
    approved = await call_tool(mcp_server, "approve_draft", {"draft_id": draft_id})
    assert_error_envelope(approved, "already rejected")


@pytest.mark.asyncio
async def test_reject_draft_unknown_id(mcp_server):
    result = await call_tool(mcp_server, "reject_draft", {"draft_id": "nope"})
    assert_error_envelope(result, "Unknown draft_id")


@pytest.mark.asyncio
async def test_get_run_status_lists_and_filters(mcp_server):
    ctx = mcp_server.xuse_ctx
    ctx.runs["run-1"] = {"status": "running", "accounts": ["acc1"], "pipelines": None,
                         "started_at": "2026-07-24T00:00:00+00:00", "task": object()}
    listed = await call_tool(mcp_server, "get_run_status", {})
    assert listed["ok"] is True and listed["count"] == 1
    assert listed["runs"][0]["run_id"] == "run-1"
    assert "task" not in listed["runs"][0]
    single = await call_tool(mcp_server, "get_run_status", {"run_id": "run-1"})
    assert single["status"] == "running" and "task" not in single
    missing = await call_tool(mcp_server, "get_run_status", {"run_id": "nope"})
    assert_error_envelope(missing, "Unknown run_id")


@pytest.mark.asyncio
async def test_get_account_health_aggregates(mcp_server):
    result = await call_tool(mcp_server, "get_account_health", {"account": "acc1"})
    assert result["ok"] is True
    assert result["config"]["is_active"] is True
    assert result["session"]["warm"] is False
    assert result["queue"] == {}
    assert result["drafts"]["pending"] == 0
    assert result["cookies"]["configured"] is False


@pytest.mark.asyncio
async def test_get_account_health_unknown_account(mcp_server):
    result = await call_tool(mcp_server, "get_account_health", {"account": "ghost"})
    assert_error_envelope(result, "ghost")


@pytest.mark.asyncio
async def test_health_reports_invalid_config_instead_of_failing(
    make_config_loader, drafts_path, browser_factory
):
    """An account whose config fails validation must be REPORTED by the
    health tool (config.valid=false with the error), not raised as a tool
    error — surfacing that is the point of a health check."""
    from xuse.mcp.drafts import DraftStore
    from xuse.mcp.server import create_server
    from xuse.mcp.sessions import SessionPool

    from helpers import make_account  # local import: not a fixture here

    bad = make_account("bad1", target_keywords="not-a-list")  # List[str] expected
    loader = make_config_loader(settings={}, accounts=[bad])
    pool = SessionPool(loader, browser_factory=browser_factory)
    server = create_server(config_loader=loader, session_pool=pool,
                           draft_store=DraftStore(drafts_path))
    result = await call_tool(server, "get_account_health", {"account": "bad1"})
    assert result["ok"] is True
    assert result["config"]["valid"] is False
    assert result["config"]["error"]
    assert result["config"]["is_active"] is True
