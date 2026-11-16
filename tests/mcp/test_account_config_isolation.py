"""Account imports and diagnostics follow the selected configuration home."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.server.mcpserver import MCPServer

from xuse.browser.cookies import load_account_cookies
from xuse.mcp import accounts_tools

from helpers import call_tool


def server_for(loader, pool=None):
    server = MCPServer("isolated-accounts")
    accounts_tools.register_account_tools(server, SimpleNamespace(config_loader=loader, session_pool=pool))
    return server


def cookie_export(path, token="synthetic-token"):
    path.write_text(json.dumps([
        {"name": "auth_token", "value": token, "domain": ".x.com", "secure": True},
        {"name": "ct0", "value": "synthetic-csrf", "domain": ".x.com", "secure": True},
    ]), encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_get_account_uses_account_config_relative_cookie_path(make_config_loader, tmp_path):
    cookie_export(tmp_path / "relative.json")
    loader = make_config_loader(accounts=[{"account_id": "a", "cookie_file_path": "relative.json"}])
    result = await call_tool(server_for(loader), "get_account", {"account": "a"})
    assert result["ok"] and result["cookies_configured"]
    assert result["cookie_file_exists"] is True


@pytest.mark.asyncio
async def test_get_account_reports_inline_source_even_when_empty(make_config_loader, tmp_path):
    path = cookie_export(tmp_path / "fallback.json")
    loader = make_config_loader(accounts=[{"account_id": "a", "cookies": [], "cookie_file_path": str(path)}])
    result = await call_tool(server_for(loader), "get_account", {"account": "a"})
    assert result["ok"] and result["cookies_configured"]
    assert result["cookie_file"] is None and result["cookie_file_exists"] is None


@pytest.mark.asyncio
async def test_same_account_ids_in_distinct_config_homes_keep_distinct_cookies(tmp_path, monkeypatch):
    from xuse.core.config_loader import ConfigLoader
    global_dir = tmp_path / "global-config"
    monkeypatch.setattr(accounts_tools, "CONFIG_DIR", global_dir)
    records = []
    for index in range(2):
        home = tmp_path / f"home-{index}"
        home.mkdir()
        (home / "settings.json").write_text("{}", encoding="utf-8")
        (home / "accounts.json").write_text("[]", encoding="utf-8")
        source = cookie_export(home / "export.json", f"synthetic-token-{index}")
        loader = ConfigLoader(settings_file=home / "settings.json", accounts_file=home / "accounts.json")
        result = await call_tool(server_for(loader), "add_account", {"account_id": "shared", "cookie_file": str(source)})
        assert result["ok"]
        row = loader.get_accounts_config()[0]
        destination = Path(row["cookie_file_path"])
        assert destination.is_absolute() and destination.parent == home
        assert destination != source and source.exists()
        records.append((loader, row))
    for index, (loader, row) in enumerate(records):
        loaded = load_account_cookies(row, loader)
        assert next(c["value"] for c in loaded if c["name"] == "auth_token") == f"synthetic-token-{index}"
    assert not global_dir.exists()


def test_import_rejects_unsafe_existing_account_id_before_file_access(tmp_path):
    from xuse.mcp.executor import ToolError
    with pytest.raises(ToolError, match="account id"):
        accounts_tools._import_cookies("../other", str(tmp_path / "missing.json"), config_dir=tmp_path)


def test_writer_rejects_duplicate_account_ids_before_backup_or_replace(tmp_path):
    from xuse.core.config_writer import AccountsConfigWriter, ConfigWriteError
    path = tmp_path / "accounts.json"
    original = '[{"account_id":"a"}]'
    path.write_text(original, encoding="utf-8")
    with pytest.raises(ConfigWriteError, match="Duplicate account id"):
        AccountsConfigWriter(path).mutate(lambda rows: rows + [{"account_id": "a"}])
    assert path.read_text(encoding="utf-8") == original
    assert not (tmp_path / "backups").exists()


def test_writer_validation_error_never_echoes_invalid_cookie_value(tmp_path):
    from xuse.core.config_writer import AccountsConfigWriter, ConfigWriteError
    path = tmp_path / "accounts.json"
    path.write_text("[]", encoding="utf-8")
    secret = "synthetic-secret-must-not-be-echoed"
    with pytest.raises(ConfigWriteError) as failure:
        AccountsConfigWriter(path).mutate(lambda rows: [{"account_id": "a", "cookies": [
            {"name": "auth_token", "value": {"private": secret}, "domain": ".x.com"}]}])
    assert secret not in str(failure.value)
    assert "cookies.0.value" in str(failure.value)
    assert path.read_text(encoding="utf-8") == "[]"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,args", [
    ("update_account", {}), ("set_account_active", {"active": False}),
    ("remove_account", {"confirm": True}),
])
async def test_stale_account_mutation_fails_without_orphan_cookie_or_backup(make_config_loader, tmp_path, tool, args):
    loader = make_config_loader(accounts=[{"account_id": "a"}])
    loader.accounts_file.write_text("[]", encoding="utf-8")
    if tool == "update_account":
        args = {"cookie_file": str(cookie_export(tmp_path / "export.json"))}
    result = await call_tool(server_for(loader), tool, {"account": "a", **args})
    assert result["ok"] is False
    assert "no longer exists" in result["error"]["message"]
    assert not (tmp_path / "a_cookies.json").exists()
    assert not (tmp_path / "backups").exists()
    assert loader.accounts_file.read_text(encoding="utf-8") == "[]"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["add_account", "update_account"])
async def test_account_staging_failure_rolls_back_successful_cookie_import(make_config_loader, tmp_path, monkeypatch, tool):
    from xuse.core import config_writer
    target = cookie_export(tmp_path / "a_cookies.json", "synthetic-original") if tool == "update_account" else tmp_path / "a_cookies.json"
    original = target.read_bytes() if target.exists() else None
    rows = [{"account_id": "a", "cookie_file_path": str(target)}] if original else []
    loader = make_config_loader(accounts=rows)
    disk_before = loader.accounts_file.read_bytes()
    source = cookie_export(tmp_path / "export.json", "synthetic-replacement")
    allocate = config_writer.tempfile.mkstemp

    def fail_account_staging(*args, **kwargs):
        if kwargs.get("prefix") == ".accounts-":
            raise OSError("synthetic staging allocation failure")
        return allocate(*args, **kwargs)

    monkeypatch.setattr(config_writer.tempfile, "mkstemp", fail_account_staging)
    key = "account_id" if tool == "add_account" else "account"
    result = await call_tool(server_for(loader), tool, {key: "a", "cookie_file": str(source)})
    assert result["ok"] is False
    if original is None:
        assert not target.exists()
    else:
        assert target.read_bytes() == original
    assert not list(tmp_path.glob(".a_cookies-*"))
    assert loader.accounts_file.read_bytes() == disk_before


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,args", [
    ("update_account", {"proxy": "http://proxy.invalid:3128"}),
    ("set_account_active", {"active": False}),
    ("remove_account", {"confirm": True}),
])
async def test_committed_tool_change_still_closes_old_session_after_caller_cancel(make_config_loader, tool, args):
    from xuse.mcp.sessions import SessionPool
    loader = make_config_loader(accounts=[{"account_id": "a"}])
    loop = asyncio.get_running_loop()
    closed = asyncio.Event()
    manager = SimpleNamespace(get_driver=lambda: object(),
                              close_driver=lambda: loop.call_soon_threadsafe(closed.set))
    pool = SessionPool(loader, browser_factory=lambda _: manager)
    entry = await pool.acquire("a")
    await entry.lock.acquire()
    test_owns_lock = True
    task = asyncio.create_task(call_tool(server_for(loader, pool), tool, {"account": "a", **args}))
    try:
        async def committed_close_started():
            while "a" not in pool._config_close_tasks:
                await asyncio.sleep(0.01)
        await asyncio.wait_for(committed_close_started(), 5)
        retained_close = pool._config_close_tasks["a"]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        entry.lock.release()
        test_owns_lock = False
        await asyncio.wait_for(closed.wait(), 5)
        # Closure must finish on its own, before the test's shutdown cleanup.
        await asyncio.wait_for(asyncio.shield(retained_close), 5)
        assert pool.entry_for("a") is None
        disk = json.loads(loader.accounts_file.read_text(encoding="utf-8"))
        if tool == "remove_account":
            assert disk == []
        else:
            key = "proxy" if tool == "update_account" else "is_active"
            assert disk[0][key] == (args["proxy"] if key == "proxy" else False)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if test_owns_lock:
            entry.lock.release()
        await pool.close_all()


@pytest.mark.asyncio
async def test_add_account_result_lists_skipped_cookies_only_when_present(tmp_path):
    from xuse.core.config_loader import ConfigLoader
    (tmp_path / "settings.json").write_text("{}", encoding="utf-8")
    (tmp_path / "accounts.json").write_text("[]", encoding="utf-8")
    loader = ConfigLoader(settings_file=tmp_path / "settings.json", accounts_file=tmp_path / "accounts.json")
    clean = cookie_export(tmp_path / "clean.json")
    result = await call_tool(server_for(loader), "add_account", {"account_id": "one", "cookie_file": str(clean)})
    assert result["ok"] and "skipped_cookies" not in result
    noisy = tmp_path / "noisy.json"
    noisy.write_text(json.dumps(json.loads(clean.read_text()) + [
        {"name": "gt", "value": "SECRET-VALUE", "domain": ".x.com", "secure": False,
         "sameSite": "no_restriction"}]), encoding="utf-8")
    async def close(_account):
        return None

    pool = SimpleNamespace(close=close)
    result = await call_tool(server_for(loader, pool), "update_account", {"account": "one", "cookie_file": str(noisy)})
    assert result["skipped_cookies"] == [{"name": "gt", "rule": "insecure_same_site_none"}]
    assert "SECRET-VALUE" not in json.dumps(result)
