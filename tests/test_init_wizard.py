"""Wizard hardening: account ids must match [A-Za-z0-9_-]+ (they land in file
paths), cookie import never SameFileErrors when the export already sits at
the destination, _write_env preserves existing lines/comments, and choosing
"skip" never writes an empty accounts.json.
"""
import json

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
