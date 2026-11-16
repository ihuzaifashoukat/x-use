"""MCP cookie imports normalize exports and preserve files on write failure."""
import json

import pytest

from xuse.mcp import accounts_tools
from xuse.browser.cookies import normalize_cookies


def cookies():
    return [{"name": name, "value": "synthetic-" + name, "secure": True, "domain": ".x.com"}
            for name in ("auth_token", "ct0")]


@pytest.fixture
def paths(tmp_path, monkeypatch):
    source = tmp_path / "export.json"
    config = tmp_path / "config"
    monkeypatch.setattr(accounts_tools, "CONFIG_DIR", config)
    return source, config / "account_cookies.json"


def test_storage_state_import_persists_only_normalized_cookies(paths):
    source, dest = paths
    source.write_text(json.dumps({"cookies": cookies(), "origins": [
        {"origin": "https://x.com", "localStorage": [{"name": "extra", "value": "do-not-copy"}]}]}))
    relative, rollback, _skipped = accounts_tools._import_cookies("account", str(source))
    assert relative == "config/account_cookies.json"
    assert json.loads(dest.read_text()) == normalize_cookies(cookies())
    assert "do-not-copy" not in dest.read_text()
    assert "do-not-copy" in source.read_text()
    accounts_tools._commit_cookie_copy(rollback)


def test_duplicate_json_keys_rejected_before_touching_destination(paths):
    source, dest = paths
    source.write_text('[{"name":"auth_token","value":"evil","value":"synthetic","secure":true},'
                      '{"name":"ct0","value":"synthetic","secure":true}]')
    with pytest.raises(accounts_tools.ToolError, match="failed validation"):
        accounts_tools._import_cookies("account", str(source))
    assert not dest.exists()


def test_same_path_envelope_normalizes_and_rollback_restores_exact_original(paths):
    _source, dest = paths
    dest.parent.mkdir()
    original = json.dumps({"cookies": cookies(), "origins": [{"private": "original-metadata"}]})
    dest.write_text(original)
    _relative, rollback, _skipped = accounts_tools._import_cookies("account", str(dest))
    assert json.loads(dest.read_text()) == normalize_cookies(cookies())
    assert "original-metadata" not in dest.read_text()
    accounts_tools._discard_cookie_copy(rollback, "account")
    assert dest.read_text() == original
    assert list(dest.parent.glob(".*")) == []


def test_unicode_export_remains_loadable_after_normalization(paths):
    from types import SimpleNamespace
    from xuse.browser.cookies import load_account_cookies, MAX_COOKIE_FILE_BYTES
    source, dest = paths
    payload = cookies() + [{"name": f"optional_{i}", "value": "界" * 5000} for i in range(64)]
    source.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    assert source.stat().st_size < MAX_COOKIE_FILE_BYTES
    _relative, rollback, _skipped = accounts_tools._import_cookies("account", str(source))
    assert dest.stat().st_size <= MAX_COOKIE_FILE_BYTES
    assert load_account_cookies({"cookie_file_path": str(dest)}, SimpleNamespace()) == normalize_cookies(payload)
    accounts_tools._commit_cookie_copy(rollback)


def test_normalized_size_limit_checked_before_replacing_file(paths, monkeypatch):
    source, dest = paths
    raw = json.dumps(cookies(), separators=(",", ":")).encode()
    source.write_bytes(raw)
    dest.parent.mkdir()
    dest.write_text("previous-login")
    monkeypatch.setattr(accounts_tools, "MAX_COOKIE_FILE_BYTES", len(raw))
    with pytest.raises(accounts_tools.ToolError, match="normalized export exceeds"):
        accounts_tools._import_cookies("account", str(source))
    assert dest.read_text() == "previous-login"
    assert list(dest.parent.glob(".*")) == []


@pytest.mark.parametrize("existing", [False, True])
def test_failed_atomic_import_preserves_prior_file_and_removes_temporary_files(paths, monkeypatch, existing):
    source, dest = paths
    source.write_text(json.dumps(cookies()))
    if existing:
        dest.parent.mkdir()
        dest.write_text("prior private export")
    def fail_replace(*args):
        raise OSError("synthetic-private-filesystem-detail")
    monkeypatch.setattr(accounts_tools.os, "replace", fail_replace)
    with pytest.raises(accounts_tools.ToolError) as error:
        accounts_tools._import_cookies("account", str(source))
    assert "synthetic-private" not in str(error.value)
    assert dest.read_text() == "prior private export" if existing else not dest.exists()
    assert list(dest.parent.glob(".*")) == []


def test_skipped_optional_cookie_is_reported_and_not_stored(paths):
    source, dest = paths
    bad = {"name": "gt", "value": "SECRET-VALUE", "secure": False, "domain": ".x.com",
           "sameSite": "no_restriction"}
    source.write_text(json.dumps(cookies() + [bad]))
    _relative, rollback, skipped = accounts_tools._import_cookies("account", str(source))
    assert skipped == [{"name": "gt", "rule": "insecure_same_site_none"}]
    stored = json.loads(dest.read_text())
    assert [c["name"] for c in stored] == ["auth_token", "ct0"]
    assert "SECRET-VALUE" not in dest.read_text()
    accounts_tools._commit_cookie_copy(rollback)


def test_failure_message_includes_safe_detail_without_values(paths):
    source, dest = paths
    broken = cookies()
    broken[1]["secure"] = False
    broken[1]["value"] = "SECRET-VALUE"
    source.write_text(json.dumps(broken))
    with pytest.raises(accounts_tools.ToolError) as error:
        accounts_tools._import_cookies("account", str(source))
    assert "ct0: insecure" in str(error.value)
    assert "SECRET-VALUE" not in str(error.value)
    assert not dest.exists()
