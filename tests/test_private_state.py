"""Private state permissions, link refusal and real Windows DACL checks."""
import os
import stat
import sqlite3
import subprocess

import pytest

from xuse.core.local_state import PrivateStateError, private_sqlite_file, private_state_file
from xuse.mcp import accounts_tools


def _windows_sddl(path):
    environment = dict(os.environ, XUSE_TEST_ACL_PATH=str(path))
    # Windows PowerShell 5.1 must discover its own built-in modules. An
    # inherited PowerShell 7 module path can load incompatible Security
    # type data and prevent Get-Acl from running at all.
    environment = {key: value for key, value in environment.items()
                   if key.casefold() != "psmodulepath"}
    return subprocess.check_output(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
         "(Get-Acl -LiteralPath $env:XUSE_TEST_ACL_PATH).Sddl"],
        text=True, env=environment,
    ).strip()


def test_windows_acl_helper_does_not_inherit_another_powershell_module_path(tmp_path, monkeypatch):
    monkeypatch.setenv("PSModulePath", "synthetic-powershell7-modules")
    monkeypatch.setenv("XUSE_TEST_ACL_SENTINEL", "preserved")
    path = tmp_path / "synthetic-state"

    def read_sddl(command, *, text, env):
        assert command[0] == "powershell.exe" and text is True
        assert not any(key.casefold() == "psmodulepath" for key in env)
        assert env["XUSE_TEST_ACL_PATH"] == str(path)
        assert env["XUSE_TEST_ACL_SENTINEL"] == "preserved"
        return "synthetic-sddl\n"

    monkeypatch.setattr(subprocess, "check_output", read_sddl)
    assert _windows_sddl(path) == "synthetic-sddl"


@pytest.mark.skipif(os.name == "nt", reason="Windows uses directory ACLs rather than POSIX mode bits")
def test_existing_permissive_state_is_restricted_to_owner(tmp_path):
    path = tmp_path / "state"
    path.write_text("private local data", encoding="utf-8")
    path.chmod(0o666)
    private_state_file(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text(encoding="utf-8") == "private local data"


@pytest.mark.skipif(os.name == "nt", reason="Windows uses directory ACLs rather than POSIX mode bits")
def test_cookie_import_and_rollback_backup_are_owner_only(tmp_path, monkeypatch):
    import json
    import time
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setattr(accounts_tools, "CONFIG_DIR", config)
    source = tmp_path / "source.json"
    source.write_text(json.dumps([
        {"name": name, "value": "synthetic_value", "domain": ".x.com", "path": "/",
         "secure": True, "expirationDate": time.time() + 3600}
        for name in ("auth_token", "ct0")]), encoding="utf-8")
    live = config / "account_cookies.json"
    live.write_text("old export", encoding="utf-8")
    live.chmod(0o644)
    _, rollback = accounts_tools._import_cookies("account", str(source))
    assert stat.S_IMODE(live.stat().st_mode) == 0o600
    assert stat.S_IMODE(rollback.backup.stat().st_mode) == 0o600
    accounts_tools._discard_cookie_copy(rollback, "account")
    assert live.read_text(encoding="utf-8") == "old export"
    assert stat.S_IMODE(live.stat().st_mode) == 0o600


def test_directory_and_hardlink_leaf_do_not_change_outside_file(tmp_path):
    outside = tmp_path / "outside"
    outside.write_text("outside sentinel", encoding="utf-8")
    before = outside.stat()
    linked = tmp_path / "state"
    os.link(outside, linked)
    with pytest.raises(PrivateStateError):
        private_state_file(linked)
    assert outside.read_text(encoding="utf-8") == "outside sentinel"
    assert outside.stat().st_mode == before.st_mode
    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(PrivateStateError):
        private_state_file(directory)
    assert directory.is_dir()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink permission and descriptor checks")
def test_symlink_leaf_and_parent_refuse_before_creation_or_mode_change(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim"
    victim.write_text("outside sentinel", encoding="utf-8")
    victim.chmod(0o644)
    leaf = tmp_path / "state"
    leaf.symlink_to(victim)
    with pytest.raises(PrivateStateError):
        private_state_file(leaf)
    parent = tmp_path / "linked_parent"
    parent.symlink_to(outside, target_is_directory=True)
    with pytest.raises(PrivateStateError):
        private_state_file(parent / "new" / "state")
    # Do not normalize away a linked parent before judging it.
    with pytest.raises(PrivateStateError):
        private_state_file(parent / ".." / "normalized_state")
    assert not (outside / "new").exists()
    assert not (tmp_path / "normalized_state").exists()
    assert stat.S_IMODE(victim.stat().st_mode) == 0o644
    assert victim.read_text(encoding="utf-8") == "outside sentinel"


def test_private_state_keeps_sqlite_connections_and_journaling_working(tmp_path):
    path = tmp_path / "state.sqlite3"
    private_state_file(path)
    first = sqlite3.connect(path)
    second = sqlite3.connect(path)
    try:
        first.execute("PRAGMA journal_mode=WAL")
        first.execute("CREATE TABLE records (value TEXT)")
        first.execute("INSERT INTO records VALUES ('synthetic')")
        first.commit()
        # Repeated hardening with open SQLite handles must remain usable.
        private_state_file(path)
        assert second.execute("SELECT value FROM records").fetchone() == ("synthetic",)
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize("change", [
    {"domain": ".example.org"}, {"secure": False}, {"expires": "never"},
])
def test_account_import_rejects_cookies_the_browser_cannot_use(tmp_path, monkeypatch, change):
    import json
    source = tmp_path / "source.json"
    cookies = [{"name": name, "value": "synthetic", "secure": True, "domain": ".x.com"}
               for name in ("auth_token", "ct0")]
    cookies[0].update(change)
    source.write_text(json.dumps(cookies), encoding="utf-8")
    target = tmp_path / "config"
    monkeypatch.setattr(accounts_tools, "CONFIG_DIR", target)
    with pytest.raises(accounts_tools.ToolError, match="failed validation"):
        accounts_tools._import_cookies("account", str(source))
    assert not target.exists()


def test_account_import_copies_validated_snapshot_when_source_changes(tmp_path, monkeypatch):
    import json
    source = tmp_path / "source.json"
    content = json.dumps([{"name": name, "value": "synthetic", "secure": True}
                          for name in ("auth_token", "ct0")])
    source.write_text(content, encoding="utf-8")
    target = tmp_path / "config"
    monkeypatch.setattr(accounts_tools, "CONFIG_DIR", target)
    original = accounts_tools.normalize_cookies
    def change_after_validation(data):
        result = original(data)
        source.write_text("changed after validation", encoding="utf-8")
        return result
    monkeypatch.setattr(accounts_tools, "normalize_cookies", change_after_validation)
    accounts_tools._import_cookies("account", str(source))
    assert (target / "account_cookies.json").read_text(encoding="utf-8") == content


@pytest.mark.skipif(os.name != "nt", reason="Real Windows file ACLs")
def test_windows_new_and_permissive_existing_file_have_only_expected_acl(tmp_path):
    from xuse.core.windows_permissions import _Api
    api = _Api()
    sid = api.user_sid()
    parent = tmp_path / "parent"
    parent.mkdir()
    sddl = _windows_sddl
    parent_before = sddl(parent)
    existing = parent / "existing"
    existing.write_text("outside sentinel", encoding="utf-8")
    # Establish a genuinely permissive baseline before exercising the fix.
    subprocess.run(["icacls.exe", str(existing), "/grant", "*S-1-1-0:(R)"], check=True, capture_output=True)
    assert "WD" in sddl(existing) or "S-1-1-0" in sddl(existing)
    for path in (existing, parent / "new"):
        private_state_file(path)
        actual = sddl(path)
        dacl = actual.split("D:", 1)[1].split("S:", 1)[0]
        assert dacl.startswith("P")
        assert set(__import__("re").findall(r"\(A;;FA;;;([^;()]+)\)", dacl)) == {sid, "SY", "BA"}
        assert dacl.count("(") == 3
    assert existing.read_text(encoding="utf-8") == "outside sentinel"
    assert sddl(parent) == parent_before


@pytest.mark.skipif(os.name != "nt", reason="Windows junction refusal without symlink privileges")
def test_windows_junction_parent_and_leaf_never_modify_target(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim"
    victim.write_text("outside sentinel", encoding="utf-8")
    sddl = _windows_sddl
    before = sddl(victim)
    junction = tmp_path / "junction"
    # Paths come solely from pytest's synthetic temporary directory; this
    # creates a junction and never removes or moves its target.
    subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(junction), str(outside)], check=True, capture_output=True)
    for path in (junction, junction / "victim", junction / "new" / "state"):
        with pytest.raises(PrivateStateError):
            private_state_file(path)
    assert victim.read_text(encoding="utf-8") == "outside sentinel"
    assert sddl(victim) == before
    assert not (outside / "new").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL failures must fail closed")
def test_windows_acl_failure_is_explicit_and_keeps_contents(tmp_path, monkeypatch):
    from xuse.core import windows_permissions
    path = tmp_path / "state"
    path.write_text("outside sentinel", encoding="utf-8")
    def refuse(*args, **kwargs):
        raise OSError("synthetic ACL failure")
    monkeypatch.setattr(windows_permissions._Api, "verify", refuse)
    with pytest.raises(PrivateStateError, match="preparation failed"):
        private_state_file(path)
    assert path.read_text(encoding="utf-8") == "outside sentinel"


def _grant_windows_users_read(path):
    subprocess.run(["icacls.exe", str(path), "/grant", "*S-1-5-32-545:(OI)(CI)(R)"],
                   check=True, capture_output=True)


@pytest.mark.skipif(os.name != "nt", reason="Real Windows SQLite sidecar inheritance")
@pytest.mark.parametrize("store_kind", ["safety", "threads", "outreach"])
def test_windows_sqlite_stores_refuse_broad_parent_before_database_creation(tmp_path, store_kind):
    from xuse.mcp.safety import SafetyStore
    from xuse.mcp.thread_store import ThreadStore
    from xuse.outreach.store import OutreachStore
    parent = tmp_path / "broad"
    parent.mkdir()
    _grant_windows_users_read(parent)
    before = _windows_sddl(parent)
    path = parent / "state.sqlite3"

    def open_store():
        if store_kind == "safety":
            SafetyStore(path).status("synthetic")
        elif store_kind == "threads":
            ThreadStore(path)
        else:
            OutreachStore(path)

    with pytest.raises(PrivateStateError, match="private directory"):
        open_store()
    assert not list(parent.iterdir())
    assert _windows_sddl(parent) == before
    # A private main-file DACL cannot compensate for its broad directory.
    path.write_text("outside sentinel", encoding="utf-8")
    private_state_file(path)
    with pytest.raises(PrivateStateError, match="private directory"):
        open_store()
    assert path.read_text(encoding="utf-8") == "outside sentinel"
    assert list(parent.iterdir()) == [path]
    assert _windows_sddl(parent) == before


@pytest.mark.skipif(os.name != "nt", reason="Real Windows SQLite sidecar inheritance")
def test_windows_sqlite_new_private_directory_keeps_wal_and_shm_private(tmp_path):
    from xuse.core.windows_permissions import _Api, verify_private_sqlite_path
    parent = tmp_path / "broad"
    parent.mkdir()
    _grant_windows_users_read(parent)
    before = _windows_sddl(parent)
    path = parent / "new-private" / "nested" / "state.sqlite3"
    private_sqlite_file(path)
    verify_private_sqlite_path(path.parent, directory=True)
    sid = _Api().user_sid()
    first, second = sqlite3.connect(path), sqlite3.connect(path)
    try:
        assert first.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        first.execute("CREATE TABLE records (value TEXT)")
        first.execute("INSERT INTO records VALUES ('synthetic')")
        first.commit()
        assert second.execute("SELECT value FROM records").fetchone() == ("synthetic",)
        for suffix in ("-wal", "-shm"):
            sidecar = path.with_name(path.name + suffix)
            assert sidecar.exists()
            verify_private_sqlite_path(sidecar, directory=False)
            dacl = _windows_sddl(sidecar).split("D:", 1)[1].split("S:", 1)[0]
            trustees = set(__import__("re").findall(r"\(A;[^;]*;[^;]*;;;([^;()]+)\)", dacl))
            assert trustees == {sid, "SY", "BA"}
        private_sqlite_file(path)
        assert second.execute("SELECT value FROM records").fetchone() == ("synthetic",)
        assert _windows_sddl(parent) == before
    finally:
        first.close()
        second.close()


@pytest.mark.skipif(os.name != "nt", reason="Real Windows SQLite journal inheritance")
def test_windows_sqlite_rollback_journal_is_private(tmp_path):
    from xuse.core.windows_permissions import verify_private_sqlite_path
    path = tmp_path / "new-private" / "state.sqlite3"
    private_sqlite_file(path)
    connection = sqlite3.connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("CREATE TABLE records (value TEXT)")
        journal = path.with_name(path.name + "-journal")
        assert journal.exists()
        verify_private_sqlite_path(journal, directory=False)
        private_sqlite_file(path)
        connection.commit()
    finally:
        connection.close()


@pytest.mark.skipif(os.name != "nt", reason="Real Windows state directory creation")
@pytest.mark.parametrize("first_store", ["drafts", "queue"])
def test_windows_plain_state_first_creates_directory_safe_for_sqlite(tmp_path, first_store):
    from xuse.mcp.drafts import DraftStore
    from xuse.mcp.safety import SafetyStore
    from xuse.mcp.thread_store import ThreadStore
    from xuse.outreach.store import OutreachStore
    from xuse.queue.store import QueueStore
    parent = tmp_path / "broad"
    parent.mkdir()
    _grant_windows_users_read(parent)
    before = _windows_sddl(parent)
    directory = parent / "fresh-home" / "data"
    # Server setup creates drafts before its SQLite stores. Generic state
    # preparation must create missing directories with private inheritance.
    state_path = directory / (first_store + ".jsonl")
    if first_store == "drafts":
        item = DraftStore(state_path).create("synthetic", "post_tweet", {"text": "exact"}, "preview")
    else:
        item = QueueStore(state_path).add(account="synthetic", action="post", payload={"text": "exact"}, dedup_key="synthetic-key")
    assert item.status == "pending" and state_path.is_file()
    assert SafetyStore(directory / "safety.sqlite3").status("synthetic")["daily_used"] == {}
    ThreadStore(directory / "threads.sqlite3")
    assert OutreachStore(directory / "outreach.sqlite3").list_leads("synthetic") == []
    assert _windows_sddl(parent) == before


@pytest.mark.skipif(os.name != "nt", reason="Real Windows SQLite sidecar inheritance")
def test_windows_sqlite_rechecks_directory_and_existing_sidecars(tmp_path):
    path = tmp_path / "new-private" / "state.sqlite3"
    private_sqlite_file(path)
    sidecar = path.with_name(path.name + "-wal")
    sidecar.write_text("outside sentinel", encoding="utf-8")
    subprocess.run(["icacls.exe", str(sidecar), "/grant", "*S-1-5-32-545:(R)"],
                   check=True, capture_output=True)
    before = _windows_sddl(sidecar)
    with pytest.raises(PrivateStateError, match="sidecars"):
        private_sqlite_file(path)
    assert _windows_sddl(sidecar) == before
    assert sidecar.read_text(encoding="utf-8") == "outside sentinel"
    sidecar.unlink()
    _grant_windows_users_read(path.parent)
    before = _windows_sddl(path.parent)
    with pytest.raises(PrivateStateError, match="private directory"):
        private_sqlite_file(path)
    assert _windows_sddl(path.parent) == before
