"""SettingsConfigWriter: load -> mutate -> backup -> atomic replace, dict-shaped."""
import json

import pytest

from xuse.core.settings_writer import SettingsConfigWriter, SettingsWriteError


def test_mutate_roundtrip_and_backup(tmp_path):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(json.dumps({"browser_settings": {"type": "chrome"}}))
    writer = SettingsConfigWriter(settings_file, backups_dir=tmp_path / "backups")

    updated = writer.mutate(lambda s: {**s, "queue": {"max_actions_per_run": 5}})

    assert updated["queue"]["max_actions_per_run"] == 5
    assert json.loads(settings_file.read_text())["browser_settings"]["type"] == "chrome"
    backups = list((tmp_path / "backups").glob("settings-*.json"))
    assert len(backups) == 1


def test_mutate_requires_dict_result(tmp_path):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text("{}")
    writer = SettingsConfigWriter(settings_file)
    with pytest.raises(SettingsWriteError):
        writer.mutate(lambda s: ["not", "a", "dict"])
    assert settings_file.read_text() == "{}"  # original untouched


def test_missing_file_starts_empty(tmp_path):
    writer = SettingsConfigWriter(tmp_path / "settings.json")
    updated = writer.mutate(lambda s: {"a": 1})
    assert updated == {"a": 1}
