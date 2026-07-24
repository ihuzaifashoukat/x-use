"""Cookie import must not orphan files when the config mutation fails
(bug-hunt cluster: config persistence).

The cookie copy lands in config/ BEFORE the writer mutation runs; when the
mutation then fails validation, the copied file used to stay behind — an
orphan, in add_account's case for an account that does not exist. The copy
is now rolled back on ConfigWriteError. The SameFileError in-place case is
untouched: a file the user placed at the convention path themselves is never
deleted.
"""
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

VALID_COOKIES = [
    {"name": "auth_token", "value": "secret-token"},
    {"name": "ct0", "value": "secret-csrf"},
]


@pytest.fixture
def cookie_export(tmp_path):
    path = tmp_path / "cookies.json"
    path.write_text(json.dumps(VALID_COOKIES), encoding="utf-8")
    return path


@pytest.fixture
def isolated_config_dir(tmp_path, monkeypatch):
    """Keep cookie copies and path resolution inside tmp_path."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setattr("xuse.mcp.accounts_tools.CONFIG_DIR", config_dir)
    monkeypatch.setattr("xuse.mcp.accounts_tools.PROJECT_ROOT", tmp_path)
    return config_dir


def read_disk(loader):
    return json.loads(Path(loader.accounts_file).read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_add_account_failed_mutation_leaves_no_cookie_orphan(
        mcp_server, config_loader, cookie_export, isolated_config_dir):
    result = await call_tool(mcp_server, "add_account", {
        "account_id": "ghost",
        "cookie_file": str(cookie_export),
        "persona": "x" * 4001,  # over the 4000-char limit -> mutation fails
    })

    assert_error_envelope(result)
    assert not (isolated_config_dir / "ghost_cookies.json").exists()
    assert [a["account_id"] for a in read_disk(config_loader)] == ["acc1", "acc2"]


@pytest.mark.asyncio
async def test_update_account_failed_mutation_leaves_no_cookie_orphan(
        mcp_server, config_loader, cookie_export, isolated_config_dir):
    result = await call_tool(mcp_server, "update_account", {
        "account": "acc1",
        "cookie_file": str(cookie_export),
        "action_config": {"bogus_knob": True},  # rejected by the write gate
    })

    assert_error_envelope(result, "bogus_knob")
    assert not (isolated_config_dir / "acc1_cookies.json").exists()
    entry = [a for a in read_disk(config_loader) if a["account_id"] == "acc1"][0]
    assert "cookie_file_path" not in entry
    assert entry["action_config"]["min_delay_between_actions_seconds"] == 0


@pytest.mark.asyncio
async def test_inplace_cookie_file_survives_failed_mutation(
        mcp_server, config_loader, isolated_config_dir):
    """SameFileError in-place case: the export already sits at
    config/<account_id>_cookies.json, so nothing was copied — a failed
    mutation must NOT delete the user's pre-existing file."""
    convention = isolated_config_dir / "ghost_cookies.json"
    convention.write_text(json.dumps(VALID_COOKIES), encoding="utf-8")

    result = await call_tool(mcp_server, "add_account", {
        "account_id": "ghost",
        "cookie_file": str(convention),
        "persona": "x" * 4001,
    })

    assert_error_envelope(result)
    assert convention.is_file()


@pytest.mark.asyncio
async def test_successful_mutation_keeps_imported_cookies(
        mcp_server, config_loader, cookie_export, isolated_config_dir):
    """Control: the rollback only fires on failure — a good mutation keeps
    both the account and its imported cookie file."""
    result = await call_tool(mcp_server, "add_account", {
        "account_id": "acc3",
        "cookie_file": str(cookie_export),
    })

    assert result["ok"] is True
    assert (isolated_config_dir / "acc3_cookies.json").is_file()
    entry = [a for a in read_disk(config_loader) if a["account_id"] == "acc3"][0]
    assert entry["cookie_file_path"] == "config/acc3_cookies.json"


@pytest.mark.asyncio
async def test_add_account_rejects_malformed_proxy(mcp_server, config_loader):
    """The writer's proxy gate through the tool layer: a malformed URL must
    be an error envelope, and nothing reaches accounts.json."""
    result = await call_tool(mcp_server, "add_account", {
        "account_id": "acc3",
        "proxy": "definitely not a url",
    })

    assert_error_envelope(result)
    assert [a["account_id"] for a in read_disk(config_loader)] == ["acc1", "acc2"]
