"""Offline validation of shipped examples through their real consumers."""
import json
from pathlib import Path

import pytest

from xuse.browser.cookies import normalize_cookies
from xuse.browser.sessions import PatchrightSessionPool, resolve_account_proxy
from xuse.core.config_loader import ConfigLoader, LEGACY_ACCOUNT_KEY_MAP
from xuse.init_wizard import ACCOUNTS_PRESET_BLURBS, SETTINGS_PRESET_BLURBS
from xuse.models import AccountConfig, ActionConfig, LLMSettings


ROOT = Path(__file__).resolve().parents[1]
SETTINGS = sorted((ROOT / "presets/settings").glob("*.json"))
ACCOUNTS = sorted((ROOT / "presets/accounts").glob("*.json"))


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", ACCOUNTS + [ROOT / "config/accounts.example.json"], ids=lambda p: p.name)
def test_accounts_use_valid_current_fields(path):
    accounts = read(path)
    assert accounts
    for account in accounts:
        assert not (set(account) & LEGACY_ACCOUNT_KEY_MAP.keys())
        assert set(account) <= AccountConfig.model_fields.keys()
        validated = AccountConfig.model_validate(account)
        assert validated.cookie_file_path
        actions = account.get("action_config", {})
        assert set(actions) <= ActionConfig.model_fields.keys()
        for key, value in actions.items():
            if key.startswith("llm_settings_for_"):
                assert "service_preference" not in value
                LLMSettings.model_validate(value)


@pytest.mark.parametrize("path", SETTINGS, ids=lambda p: p.name)
def test_settings_load_and_preserve_explicit_backend(path, tmp_path):
    accounts = tmp_path / "accounts.json"
    accounts.write_text("[]", encoding="utf-8")
    loader = ConfigLoader(settings_file=path, accounts_file=accounts)
    assert loader.settings_error is None
    assert "api_keys" not in loader.get_settings()
    assert loader.get_setting("llm.api_key") == ""
    action = loader.get_setting("twitter_automation.action_config", {})
    assert set(action) <= ActionConfig.model_fields.keys()
    ActionConfig.model_validate(action)
    expected = "patchright" if path.name == "beginner-patchright.json" else "selenium"
    assert loader.get_setting("mcp.browser_backend") == expected
    assert loader.get_setting("mcp.draft_mode") is True


def test_wizard_describes_every_shipped_preset():
    assert {p.name for p in SETTINGS} == SETTINGS_PRESET_BLURBS.keys()
    assert {p.name for p in ACCOUNTS} == ACCOUNTS_PRESET_BLURBS.keys()


def test_recommended_setup_is_keyless_inactive_and_needs_no_proxy_pool():
    loader = ConfigLoader(
        settings_file=ROOT / "presets/settings/beginner-patchright.json",
        accounts_file=ROOT / "presets/accounts/reviewed_outreach.json",
    )
    pool = PatchrightSessionPool(loader)
    account = pool.find_account_dict("your_account_id_here")
    assert pool.max_sessions == 4
    assert pool.entry_for(account["account_id"]) is None
    assert account["is_active"] is False
    assert resolve_account_proxy(loader, account) is None
    assert loader.get_setting("queue.auto_drain.enabled") is False
    assert loader.get_setting("mcp.safety.daily_caps.message") == 10
    assert not any(value for key, value in account["action_config"].items() if key.startswith("enable_"))


def test_cookie_example_remains_importable_without_expired_credentials():
    cookies = normalize_cookies(read(ROOT / "data/cookies/dummy_cookies_example.json"))
    assert {cookie["name"] for cookie in cookies} == {"auth_token", "ct0"}
    assert all(cookie["secure"] for cookie in cookies)


def test_proxy_examples_match_async_route_contract(make_config_loader, monkeypatch):
    examples = read(ROOT / "data/proxies/dummy_proxies.json")
    monkeypatch.setenv("RESI_PASS", "sample-password")
    loader = make_config_loader(settings={"browser_settings": {"proxy_pool_strategy": "hash"}})
    for route in examples["examples"] + examples["with_env"]:
        selected = resolve_account_proxy(loader, {"account_id": "test", "proxy": route})
        assert selected["server"].startswith(("http://", "https://", "socks5://"))


@pytest.mark.parametrize("path", ACCOUNTS, ids=lambda p: p.name)
def test_account_scenarios_do_not_require_unconfigured_proxy_pools(path, make_config_loader):
    loader = make_config_loader(settings={})
    for account in read(path):
        assert resolve_account_proxy(loader, account) is None
