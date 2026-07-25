"""A failed account mutation must never destroy a live cookie file.

Regression test for the cookie-orphan cleanup: _import_cookies copies the
export over config/<id>_cookies.json, and on a failed config write
_discard_cookie_copy unlinked that same path -- deleting the account's working
login, which is not backed up anywhere, while accounts.json still pointed at
it. Rolling back must restore the previous file, and may only delete when the
import created a genuinely new one.
"""
import json

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

LIVE = [
    {"name": "auth_token", "value": "LIVE-SESSION", "domain": ".x.com", "path": "/"},
    {"name": "ct0", "value": "LIVE-CSRF", "domain": ".x.com", "path": "/"},
]
FRESH = [
    {"name": "auth_token", "value": "FRESH-SESSION", "domain": ".x.com", "path": "/"},
    {"name": "ct0", "value": "FRESH-CSRF", "domain": ".x.com", "path": "/"},
]


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    d = tmp_path / "config"
    d.mkdir()
    monkeypatch.setattr("xuse.mcp.accounts_tools.CONFIG_DIR", d)
    return d


@pytest.fixture
def export(tmp_path):
    p = tmp_path / "fresh_export.json"
    p.write_text(json.dumps(FRESH), encoding="utf-8")
    return p


@pytest.mark.asyncio
async def test_failed_update_restores_the_live_cookie_file(mcp_server, config_dir, export):
    """A bad proxy in the same call must not cost the account its login."""
    live = config_dir / "acc1_cookies.json"
    live.write_text(json.dumps(LIVE), encoding="utf-8")

    result = await call_tool(mcp_server, "update_account", {
        "account": "acc1",
        "cookie_file": str(export),
        "proxy": "not-a-valid-proxy-url",
    })

    assert_error_envelope(result)
    assert live.exists(), "the account's live cookie file was deleted by the rollback"
    assert json.loads(live.read_text(encoding="utf-8")) == LIVE, \
        "the live cookie file was left holding the un-committed import"


@pytest.mark.asyncio
async def test_successful_update_replaces_cookies_and_leaves_no_backup(
        mcp_server, config_dir, export):
    live = config_dir / "acc1_cookies.json"
    live.write_text(json.dumps(LIVE), encoding="utf-8")

    result = await call_tool(mcp_server, "update_account", {
        "account": "acc1", "cookie_file": str(export),
    })

    assert result["ok"] is True
    assert json.loads(live.read_text(encoding="utf-8")) == FRESH
    leftovers = [p.name for p in config_dir.glob(".acc1_cookies-*")]
    assert not leftovers, f"pre-import backups accumulated in config/: {leftovers}"


@pytest.mark.asyncio
async def test_failed_add_still_removes_a_newly_created_cookie_file(
        mcp_server, config_dir, export):
    """When the import created the file, rollback must delete it -- the original
    orphan-cleanup behaviour, which the fix must preserve."""
    dest = config_dir / "brand_new_cookies.json"
    assert not dest.exists()

    result = await call_tool(mcp_server, "add_account", {
        "account_id": "brand_new",
        "cookie_file": str(export),
        "proxy": "not-a-valid-proxy-url",
    })

    assert_error_envelope(result)
    assert not dest.exists(), "an orphan cookie file was left for an account that does not exist"
    leftovers = [p.name for p in config_dir.glob(".brand_new_cookies-*")]
    assert not leftovers, f"backup left behind: {leftovers}"
