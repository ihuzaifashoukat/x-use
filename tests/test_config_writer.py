"""AccountsConfigWriter: validation, backups, atomic writes. tmp paths only."""
import json

import pytest

from xuse.core.config_writer import AccountsConfigWriter, ConfigWriteError


def write_accounts(path, accounts):
    path.write_text(json.dumps(accounts), encoding="utf-8")


def read_accounts(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_mutate_appends_and_writes(tmp_path):
    target = tmp_path / "accounts.json"
    write_accounts(target, [{"account_id": "acc1"}])
    writer = AccountsConfigWriter(target)
    updated = writer.mutate(lambda accs: [*accs, {"account_id": "acc2"}])
    assert [a["account_id"] for a in updated] == ["acc1", "acc2"]
    assert [a["account_id"] for a in read_accounts(target)] == ["acc1", "acc2"]


def test_mutate_validates_and_leaves_file_untouched(tmp_path):
    target = tmp_path / "accounts.json"
    original = [{"account_id": "acc1"}]
    write_accounts(target, original)
    writer = AccountsConfigWriter(target)
    with pytest.raises(ConfigWriteError, match="Validation failed"):
        writer.mutate(lambda accs: [*accs, {"is_active": True}])  # no account_id
    assert read_accounts(target) == original


def test_mutate_normalizes_legacy_override_keys(tmp_path):
    target = tmp_path / "accounts.json"
    write_accounts(target, [])
    writer = AccountsConfigWriter(target)
    updated = writer.mutate(lambda accs: [{"account_id": "acc1",
                                           "target_keywords_override": ["ai"]}])
    # stored as written; validation accepted it via the legacy mapping
    assert updated[0]["target_keywords_override"] == ["ai"]


def test_backup_created_and_pruned(tmp_path):
    target = tmp_path / "accounts.json"
    write_accounts(target, [{"account_id": "acc1"}])
    writer = AccountsConfigWriter(target, max_backups=2)
    for _ in range(4):
        writer.mutate(lambda accs: accs)  # no-op mutation still backs up
    backups = sorted((tmp_path / "backups").glob("accounts-*.json"))
    assert len(backups) == 2


def test_load_missing_file_returns_empty(tmp_path):
    assert AccountsConfigWriter(tmp_path / "missing.json").load() == []


def test_load_invalid_json_raises(tmp_path):
    target = tmp_path / "accounts.json"
    target.write_text("not json", encoding="utf-8")
    with pytest.raises(ConfigWriteError):
        AccountsConfigWriter(target).load()


def test_load_non_list_raises(tmp_path):
    target = tmp_path / "accounts.json"
    target.write_text("{}", encoding="utf-8")
    with pytest.raises(ConfigWriteError, match="array"):
        AccountsConfigWriter(target).load()
