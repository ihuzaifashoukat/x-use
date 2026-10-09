"""Wizard hardening: account ids must match [A-Za-z0-9_-]+ (they land in file
paths), cookie import never SameFileErrors when the export already sits at
the destination, _write_env preserves existing lines/comments, and choosing
"skip" never writes an empty accounts.json.
"""
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import typer

import xuse.init_wizard as wizard

VALID_COOKIES = [{"name": "auth_token", "value": "x"}, {"name": "ct0", "value": "y"}]


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setattr(wizard, "CONFIG_DIR", config_dir)
    return config_dir


# --- 11a: account id charset -------------------------------------------------


def test_import_cookies_rejects_unsafe_account_id():
    for bad in ("../evil", "..\\evil", "a/b", "a b", "", "dot.name"):
        with pytest.raises(ValueError, match="Invalid account id"):
            wizard._import_cookies(bad)


def test_accounts_step_reprompts_until_id_is_safe(isolated_config, monkeypatch):
    monkeypatch.setattr(wizard, "_choose_preset", lambda kind, blurbs: None)
    answers = iter(["../evil", "also/bad", "good-1", ""])  # id, id, id, cookie path
    monkeypatch.setattr(typer, "prompt", lambda *a, **k: next(answers))
    monkeypatch.setattr(typer, "confirm", lambda *a, **k: True)

    wizard._accounts_step()

    data = json.loads((isolated_config / "accounts.json").read_text(encoding="utf-8"))
    assert data[0]["account_id"] == "good-1"
    assert data[0]["cookie_file_path"] == "config/good-1_cookies.json"
    assert not (isolated_config.parent / "evil_cookies.json").exists()


def test_accounts_step_accepts_safe_id_first_try(isolated_config, monkeypatch):
    monkeypatch.setattr(wizard, "_choose_preset", lambda kind, blurbs: None)
    answers = iter(["acc_1", ""])
    monkeypatch.setattr(typer, "prompt", lambda *a, **k: next(answers))
    monkeypatch.setattr(typer, "confirm", lambda *a, **k: True)

    wizard._accounts_step()

    data = json.loads((isolated_config / "accounts.json").read_text(encoding="utf-8"))
    assert data[0]["account_id"] == "acc_1"


# --- 11b: cookie export already at the destination ----------------------------


def test_import_cookies_uses_export_in_place_when_already_at_dest(isolated_config, monkeypatch):
    dest = isolated_config / "acc_cookies.json"
    dest.write_text(json.dumps(VALID_COOKIES), encoding="utf-8")
    monkeypatch.setattr(typer, "prompt", lambda *a, **k: str(dest))

    def no_confirm(*a, **k):  # pragma: no cover - must not be reached
        raise AssertionError("no overwrite confirmation expected for in-place import")

    monkeypatch.setattr(typer, "confirm", no_confirm)

    rel = wizard._import_cookies("acc")  # must not raise SameFileError
    assert rel == "config/acc_cookies.json"
    assert json.loads(dest.read_text(encoding="utf-8")) == VALID_COOKIES


def test_import_copies_export_into_config(isolated_config, tmp_path, monkeypatch):
    src = tmp_path / "export.json"
    src.write_text(json.dumps(VALID_COOKIES), encoding="utf-8")
    monkeypatch.setattr(typer, "prompt", lambda *a, **k: str(src))

    rel = wizard._import_cookies("acc")
    assert rel == "config/acc_cookies.json"
    assert json.loads((isolated_config / "acc_cookies.json").read_text(encoding="utf-8")) == VALID_COOKIES


def test_import_declined_overwrite_keeps_existing_dest(isolated_config, tmp_path, monkeypatch):
    src = tmp_path / "export.json"
    src.write_text(json.dumps(VALID_COOKIES), encoding="utf-8")
    dest = isolated_config / "acc_cookies.json"
    dest.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(typer, "prompt", lambda *a, **k: str(src))
    monkeypatch.setattr(typer, "confirm", lambda *a, **k: False)

    rel = wizard._import_cookies("acc")
    assert rel == "config/acc_cookies.json"
    assert dest.read_text(encoding="utf-8") == "[]"  # untouched


# --- 11c: _write_env preserves the existing file ------------------------------


def test_write_env_preserves_comments_blanks_and_other_keys(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# proxy creds\nOTHER=1\n\nOPENAI_BASE_URL=https://old.example\n", encoding="utf-8")

    wizard._write_env(env, {"OPENAI_API_KEY": "sk-new", "OPENAI_BASE_URL": "https://new.example"})

    lines = env.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "# proxy creds"
    assert lines[1] == "OTHER=1"
    assert lines[2] == ""
    assert "OPENAI_BASE_URL=https://new.example" in lines
    assert "OPENAI_BASE_URL=https://old.example" not in lines
    assert lines.count("OPENAI_BASE_URL=https://new.example") == 1  # updated in place
    assert lines[-1] == "OPENAI_API_KEY=sk-new"  # missing key appended


def test_write_env_creates_header_for_new_file(tmp_path):
    env = tmp_path / ".env"
    wizard._write_env(env, {"OPENAI_API_KEY": "sk-x"})
    lines = env.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("# x-use secrets")
    assert "OPENAI_API_KEY=sk-x" in lines


# --- 11d: skip never writes an empty accounts.json ----------------------------


def test_accounts_step_skip_writes_no_accounts_json(isolated_config, monkeypatch):
    monkeypatch.setattr(wizard, "_choose_preset", lambda kind, blurbs: None)
    monkeypatch.setattr(typer, "confirm", lambda *a, **k: False)  # decline configuring

    wizard._accounts_step()

    assert not (isolated_config / "accounts.json").exists()


def test_accounts_step_keeps_existing_file_on_skip(isolated_config, monkeypatch):
    existing = [{"account_id": "keepme"}]
    (isolated_config / "accounts.json").write_text(json.dumps(existing), encoding="utf-8")
    monkeypatch.setattr(wizard, "_choose_preset", lambda kind, blurbs: None)

    wizard._accounts_step()  # no prompts/confirms at all

    assert json.loads((isolated_config / "accounts.json").read_text(encoding="utf-8")) == existing


@pytest.mark.parametrize("kind,recommended", [
    ("settings", "beginner-patchright.json"),
    ("accounts", "reviewed_outreach.json"),
])
@pytest.mark.parametrize("existing", [False, True])
def test_preset_enter_recommends_mcp_only_for_new_config(
    isolated_config, tmp_path, monkeypatch, kind, recommended, existing,
):
    presets = tmp_path / "presets"
    folder = presets / kind
    folder.mkdir(parents=True)
    (folder / "a-legacy.json").write_text("{}", encoding="utf-8")
    (folder / recommended).write_text("{}", encoding="utf-8")
    monkeypatch.setattr(wizard, "PRESETS_DIR", presets)
    if existing:
        (isolated_config / f"{kind}.json").write_text("{}", encoding="utf-8")
    defaults = []

    def press_enter(*args, **kwargs):
        defaults.append(kwargs["default"])
        return kwargs["default"]

    monkeypatch.setattr(typer, "prompt", press_enter)
    chosen = wizard._choose_preset(kind, {})
    assert defaults == [0 if existing else 2]
    assert chosen == (None if existing else folder / recommended)


def test_explicit_skip_overrides_recommended_preset(isolated_config, monkeypatch):
    monkeypatch.setattr(typer, "prompt", lambda *args, **kwargs: 0)
    assert wizard._choose_preset("settings", wizard.SETTINGS_PRESET_BLURBS) is None


def test_source_presets_are_independent_of_private_data_home(tmp_path):
    root = Path(__file__).resolve().parents[1]
    data_home = tmp_path / "private data"
    environment = dict(os.environ, X_USE_HOME=str(data_home), PYTHONPATH=str(root / "src"))
    program = (
        "import json; from xuse import init_wizard as w; "
        "print(json.dumps({'presets': str(w.PRESETS_DIR), 'data': str(w.PROJECT_ROOT)}))"
    )
    result = subprocess.run([sys.executable, "-c", program], cwd=tmp_path,
                            env=environment, capture_output=True, text=True, check=True)
    found = json.loads(result.stdout)
    assert Path(found["presets"]) == root / "presets"
    assert Path(found["data"]) == data_home
    assert not data_home.exists()


def test_installed_fallback_creates_private_inactive_config_and_preserves_rerun(tmp_path, monkeypatch):
    import xuse.skills_installer as skills

    data_home = tmp_path / "new private home"
    monkeypatch.setattr(wizard, "CONFIG_DIR", data_home / "config")
    monkeypatch.setattr(wizard, "PROJECT_ROOT", data_home)
    monkeypatch.setattr(wizard, "PRESETS_DIR", tmp_path / "no checkout presets")
    monkeypatch.setattr(typer, "prompt", lambda *args, **kwargs: kwargs["default"])
    monkeypatch.setattr(typer, "confirm", lambda *args, **kwargs: False)
    monkeypatch.setattr(skills, "install_skills", lambda: {"installed": []})

    wizard.run_wizard()

    settings_path = data_home / "config/settings.json"
    accounts_path = data_home / "config/accounts.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    accounts = json.loads(accounts_path.read_text(encoding="utf-8"))
    assert settings["mcp"] == {
        "browser_backend": "patchright", "browser_headless": True, "draft_mode": True,
    }
    assert settings["queue"]["auto_drain"]["enabled"] is False
    assert len(accounts) == 1 and accounts[0]["is_active"] is False
    assert wizard._validate_accounts(accounts) == []
    assert not (data_home / ".env").exists()
    assert not list(data_home.glob("**/*cookies*.json"))
    if os.name == "nt":
        from xuse.core.windows_permissions import verify_private_sqlite_path
        for path in (settings_path, accounts_path):
            verify_private_sqlite_path(path, directory=False)
    else:
        for path in (settings_path, accounts_path):
            assert stat.S_IMODE(path.stat().st_mode) == 0o600

    settings_path.write_text('{"operator":"keep these exact bytes"}', encoding="utf-8")
    accounts_path.write_text('[{"account_id":"keep-existing"}]', encoding="utf-8")
    original = [path.read_bytes() for path in (settings_path, accounts_path)]
    wizard.run_wizard()
    assert [path.read_bytes() for path in (settings_path, accounts_path)] == original
