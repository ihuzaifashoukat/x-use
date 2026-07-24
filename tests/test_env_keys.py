"""Tests for LLM API key resolution (xuse.core.llm_service.clients +
xuse.utils.env): OPENAI_API_KEY beats settings.json, the legacy
api_keys.openai_api_key is a last-resort fallback, placeholders are
rejected, base_url/model resolve env > llm block > defaults, legacy
gemini/azure keys are ignored with a warning, a missing .env file is a
no-op, and key values never appear in logs.

No network: the AsyncOpenAI constructor does not call out at init.
"""
import logging
import os

import pytest

import xuse.utils.env as env_module
from xuse.core.llm_service import clients as clients_module
from xuse.core.llm_service.clients import _resolve_api_key, build_client


@pytest.fixture(autouse=True)
def clean_llm_env(monkeypatch):
    """Every test starts with no LLM settings in the process environment."""
    for var in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def no_real_dotenv(monkeypatch):
    """Isolate build_client from any real project-root .env file."""
    monkeypatch.setattr(clients_module, "load_env", lambda: None)


@pytest.fixture
def fresh_dotenv_loader(monkeypatch, tmp_path):
    """Point load_env at a tmp project root and reset its one-shot flag."""
    monkeypatch.setattr(env_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(env_module, "_loaded", False)
    return tmp_path


# --- _resolve_api_key precedence -------------------------------------------


def test_env_var_beats_settings_json(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-secret-AAA")
    value, source = _resolve_api_key("openai_api_key", "sk-config-secret-BBB")
    assert value == "sk-env-secret-AAA"
    assert source == "env var OPENAI_API_KEY"


def test_settings_json_is_fallback_when_env_absent():
    value, source = _resolve_api_key("openai_api_key", "sk-config-secret-BBB")
    assert value == "sk-config-secret-BBB"
    assert source == "settings.json"


def test_blank_env_var_falls_through_to_settings_json(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "   ")
    value, source = _resolve_api_key("openai_api_key", "sk-config-secret-BBB")
    assert value == "sk-config-secret-BBB"
    assert source == "settings.json"


def test_no_key_anywhere_resolves_to_none():
    value, source = _resolve_api_key("openai_api_key", None)
    assert value is None
    assert source == "none"


# --- build_client: key sources ----------------------------------------------

@pytest.mark.skipif(not clients_module.OPENAI_AVAILABLE, reason="openai SDK not installed")
def test_build_client_uses_env_key_over_config(monkeypatch, make_config_loader):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-secret-AAA")
    loader = make_config_loader(
        settings={"llm": {"api_key": "sk-config-secret-BBB"}}, accounts=[])
    client, resolved = build_client(loader)
    assert client is not None
    assert client.api_key == "sk-env-secret-AAA"
    assert resolved["key_source"] == "env var OPENAI_API_KEY"


@pytest.mark.skipif(not clients_module.OPENAI_AVAILABLE, reason="openai SDK not installed")
def test_build_client_uses_llm_block_key(make_config_loader):
    loader = make_config_loader(
        settings={"llm": {"api_key": "sk-llm-block-CCC",
                          "base_url": "https://openrouter.ai/api/v1",
                          "model": "openai/gpt-4o-mini"}}, accounts=[])
    client, resolved = build_client(loader)
    assert client is not None
    assert client.api_key == "sk-llm-block-CCC"
    assert str(client.base_url).rstrip("/") == "https://openrouter.ai/api/v1"
    assert resolved["model"] == "openai/gpt-4o-mini"
    assert resolved["base_url"] == "https://openrouter.ai/api/v1"


@pytest.mark.skipif(not clients_module.OPENAI_AVAILABLE, reason="openai SDK not installed")
def test_build_client_falls_back_to_legacy_openai_key(make_config_loader):
    loader = make_config_loader(
        settings={"api_keys": {"openai_api_key": "sk-legacy-DDD"}}, accounts=[])
    client, resolved = build_client(loader)
    assert client is not None
    assert client.api_key == "sk-legacy-DDD"
    assert resolved["model"] == clients_module.DEFAULT_MODEL
    assert resolved["base_url"] is None


@pytest.mark.skipif(not clients_module.OPENAI_AVAILABLE, reason="openai SDK not installed")
def test_placeholder_keys_are_rejected(monkeypatch, make_config_loader):
    monkeypatch.setenv("OPENAI_API_KEY", "YOUR_OPENAI_API_KEY")
    loader = make_config_loader(
        settings={"llm": {"api_key": "YOUR_OPENAI_API_KEY"}}, accounts=[])
    client, _ = build_client(loader)
    assert client is None


def test_no_key_returns_none_client(make_config_loader):
    loader = make_config_loader(settings={}, accounts=[])
    client, resolved = build_client(loader)
    assert client is None
    assert resolved["key_source"] == "none"


def test_env_base_url_and_model_override(monkeypatch, make_config_loader):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-AAA")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("OPENAI_MODEL", "llama3.1")
    loader = make_config_loader(
        settings={"llm": {"base_url": "https://ignored.example/v1", "model": "ignored"}}, accounts=[])
    client, resolved = build_client(loader)
    assert resolved["base_url"] == "http://localhost:11434/v1"
    assert resolved["model"] == "llama3.1"
    if clients_module.OPENAI_AVAILABLE:
        assert str(client.base_url).rstrip("/") == "http://localhost:11434/v1"


def test_legacy_gemini_azure_keys_warn_and_are_ignored(make_config_loader, caplog):
    loader = make_config_loader(
        settings={"api_keys": {"gemini_api_key": "AIza-SENTINEL-CONFIG-888",
                               "azure_openai_api_key": "azure-sentinel-777"}}, accounts=[])
    with caplog.at_level(logging.WARNING):
        client, _ = build_client(loader)
    assert client is None  # no usable key — legacy keys do not initialize anything
    assert "ignored" in caplog.text
    assert "AIza-SENTINEL-CONFIG-888" not in caplog.text
    assert "azure-sentinel-777" not in caplog.text


# --- .env loading ------------------------------------------------------------


def test_missing_dotenv_is_a_noop(fresh_dotenv_loader, monkeypatch):
    monkeypatch.delenv("XUSE_TEST_SENTINEL", raising=False)
    env_module.load_env()  # must not raise, must not change the environment
    assert os.environ.get("XUSE_TEST_SENTINEL") is None
    env_module.load_env()  # idempotent second call
    assert os.environ.get("XUSE_TEST_SENTINEL") is None


def test_existing_dotenv_is_loaded(fresh_dotenv_loader, monkeypatch):
    monkeypatch.delenv("XUSE_TEST_SENTINEL", raising=False)
    (fresh_dotenv_loader / ".env").write_text("XUSE_TEST_SENTINEL=loaded-123\n", encoding="utf-8")
    env_module.load_env()
    assert os.environ.get("XUSE_TEST_SENTINEL") == "loaded-123"


def test_dotenv_never_overrides_process_env(fresh_dotenv_loader, monkeypatch):
    monkeypatch.setenv("XUSE_TEST_SENTINEL", "from-process-env")
    (fresh_dotenv_loader / ".env").write_text("XUSE_TEST_SENTINEL=from-dotenv\n", encoding="utf-8")
    env_module.load_env()
    assert os.environ.get("XUSE_TEST_SENTINEL") == "from-process-env"


# --- key values never reach the logs ----------------------------------------


@pytest.mark.skipif(not clients_module.OPENAI_AVAILABLE, reason="openai SDK not installed")
def test_key_values_never_appear_in_logs(monkeypatch, make_config_loader, caplog):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-SENTINEL-ENV-999")
    loader = make_config_loader(
        settings={"api_keys": {"gemini_api_key": "AIza-SENTINEL-CONFIG-888"}}, accounts=[])
    with caplog.at_level(logging.INFO):
        build_client(loader)
    assert "sk-SENTINEL-ENV-999" not in caplog.text
    assert "AIza-SENTINEL-CONFIG-888" not in caplog.text
    # The safe-to-log source label is present instead.
    assert "env var OPENAI_API_KEY" in caplog.text
