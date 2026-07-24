"""Account-management tool tests: CRUD over tmp accounts.json with backups."""
import json
from pathlib import Path

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


def read_disk(loader):
    return json.loads(Path(loader.accounts_file).read_text(encoding="utf-8"))


@pytest.fixture
def cookie_export(tmp_path):
    path = tmp_path / "cookies.json"
    path.write_text(json.dumps([
        {"name": "auth_token", "value": "secret-token"},
        {"name": "ct0", "value": "secret-csrf"},
    ]), encoding="utf-8")
    return path


@pytest.fixture
def isolated_config_dir(tmp_path, monkeypatch):
    """Keep cookie copies and path resolution inside tmp_path."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setattr("xuse.mcp.accounts_tools.CONFIG_DIR", config_dir)
    monkeypatch.setattr("xuse.mcp.accounts_tools.PROJECT_ROOT", tmp_path)
    return config_dir


@pytest.mark.asyncio
async def test_get_account_masks_secrets(mcp_server):
    result = await call_tool(mcp_server, "get_account", {"account": "acc1"})
    assert result["ok"] is True
    assert result["account"]["account_id"] == "acc1"
    assert "cookies" not in result["account"] and "password" not in result["account"]


@pytest.mark.asyncio
async def test_get_account_unknown_envelope(mcp_server):
    result = await call_tool(mcp_server, "get_account", {"account": "ghost"})
    assert_error_envelope(result, "ghost")


@pytest.mark.asyncio
async def test_add_account_writes_disk_and_refreshes_loader(mcp_server, config_loader, tmp_path):
    result = await call_tool(mcp_server, "add_account",
                             {"account_id": "acc3", "target_keywords": ["ai"]})
    assert result["ok"] is True
    assert [a["account_id"] for a in read_disk(config_loader)] == ["acc1", "acc2", "acc3"]
    listed = await call_tool(mcp_server, "list_accounts", {})
    assert [a["account_id"] for a in listed["accounts"]] == ["acc1", "acc2", "acc3"]
    assert len(list((tmp_path / "backups").glob("accounts-*.json"))) == 1


@pytest.mark.asyncio
async def test_add_account_rejects_duplicate(mcp_server):
    result = await call_tool(mcp_server, "add_account", {"account_id": "acc1"})
    assert_error_envelope(result, "already exists")


@pytest.mark.asyncio
async def test_add_account_rejects_unsafe_id(mcp_server):
    result = await call_tool(mcp_server, "add_account", {"account_id": "../evil"})
    assert_error_envelope(result, "Invalid account id")


@pytest.mark.asyncio
async def test_add_account_with_cookie_import(mcp_server, config_loader, cookie_export,
                                              isolated_config_dir):
    result = await call_tool(mcp_server, "add_account",
                             {"account_id": "acc3", "cookie_file": str(cookie_export)})
    assert result["ok"] is True
    entry = [a for a in read_disk(config_loader) if a["account_id"] == "acc3"][0]
    assert entry["cookie_file_path"] == "config/acc3_cookies.json"
    assert (isolated_config_dir / "acc3_cookies.json").is_file()


@pytest.mark.asyncio
async def test_add_account_rejects_invalid_cookie_file(mcp_server, tmp_path,
                                                       isolated_config_dir):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps([{"name": "nope", "value": "x"}]), encoding="utf-8")
    result = await call_tool(mcp_server, "add_account",
                             {"account_id": "acc3", "cookie_file": str(bad)})
    assert_error_envelope(result, "validation")


@pytest.mark.asyncio
async def test_update_account_partial_and_closes_session(mcp_server, config_loader,
                                                         session_pool, browser_factory):
    await session_pool.acquire("acc1")  # open a warm session
    result = await call_tool(mcp_server, "update_account",
                             {"account": "acc1", "target_keywords": ["robotics"]})
    assert result["ok"] is True
    entry = [a for a in read_disk(config_loader) if a["account_id"] == "acc1"][0]
    assert entry["target_keywords"] == ["robotics"]
    assert browser_factory.created[0].closed is True  # warm session closed
    assert session_pool.entry_for("acc1") is None


@pytest.mark.asyncio
async def test_update_account_clear_proxy_with_empty_string(mcp_server, config_loader):
    await call_tool(mcp_server, "update_account", {"account": "acc1", "proxy": "http://h:1"})
    result = await call_tool(mcp_server, "update_account", {"account": "acc1", "proxy": ""})
    assert result["ok"] is True
    entry = [a for a in read_disk(config_loader) if a["account_id"] == "acc1"][0]
    assert "proxy" not in entry


@pytest.mark.asyncio
async def test_set_account_active_toggles(mcp_server, config_loader):
    result = await call_tool(mcp_server, "set_account_active",
                             {"account": "acc1", "active": False})
    assert result["ok"] is True and result["is_active"] is False
    entry = [a for a in read_disk(config_loader) if a["account_id"] == "acc1"][0]
    assert entry["is_active"] is False


@pytest.mark.asyncio
async def test_remove_account_requires_confirm(mcp_server):
    result = await call_tool(mcp_server, "remove_account", {"account": "acc1"})
    assert_error_envelope(result, "confirm=true")


@pytest.mark.asyncio
async def test_remove_account_removes_and_refreshes(mcp_server, config_loader):
    result = await call_tool(mcp_server, "remove_account",
                             {"account": "acc1", "confirm": True})
    assert result["ok"] is True and result["removed"] is True
    assert [a["account_id"] for a in read_disk(config_loader)] == ["acc2"]
    listed = await call_tool(mcp_server, "list_accounts", {})
    assert [a["account_id"] for a in listed["accounts"]] == ["acc2"]
