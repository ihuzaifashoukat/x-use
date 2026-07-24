"""Doctor/clients hardening: puppeteer-style cookie expiry (expires: -1) is a
valid session cookie and never crashes timestamp conversion, and the doctor
LLM check mirrors build_client's env-first resolution — a placeholder key
that disables the runtime client is a FAIL naming the effective source.
"""
import time

import pytest

import xuse.doctor as doctor_module
from xuse.doctor import Check, _check_llm_keys, check_cookie_data


@pytest.fixture(autouse=True)
def clean_llm_env(monkeypatch):
    for var in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL"):
        monkeypatch.delenv(var, raising=False)


def _cookies(**expiry_kwargs):
    return [
        {"name": "auth_token", "value": "x", **expiry_kwargs},
        {"name": "ct0", "value": "y", **expiry_kwargs},
    ]


# --- check_cookie_data: session-cookie expiry conventions -------------------


def test_expires_minus_one_is_a_valid_session_cookie():
    """Puppeteer exports use expires: -1 for session cookies; on Windows
    datetime.fromtimestamp(-1) raises OSError — this must not crash nor FAIL."""
    ok, problems = check_cookie_data(_cookies(expires=-1))
    assert ok, problems
    assert problems == []


def test_expires_zero_is_a_valid_session_cookie():
    ok, problems = check_cookie_data(_cookies(expires=0))
    assert ok, problems


def test_negative_expiry_never_raises_oserror():
    """Even exotic negative timestamps must not escape as OSError."""
    ok, _ = check_cookie_data(_cookies(expires=-1_000_000_000))
    assert ok


def test_unparseable_expiry_treated_as_session_cookie():
    ok, problems = check_cookie_data(_cookies(expires="not-a-timestamp"))
    assert ok, problems


def test_future_expiry_is_valid():
    ok, problems = check_cookie_data(_cookies(expires=time.time() + 86_400))
    assert ok, problems


def test_past_expiry_is_reported_expired():
    ok, problems = check_cookie_data(_cookies(expires=time.time() - 86_400))
    assert not ok
    assert any("expired" in p for p in problems)


def test_alternate_expiry_field_names_checked():
    ok, problems = check_cookie_data(_cookies(expirationDate=time.time() - 10))
    assert not ok
    assert any("expired" in p for p in problems)


# --- doctor LLM check mirrors build_client resolution -----------------------


def test_placeholder_env_shadowing_config_key_is_a_fail(monkeypatch):
    """Repro of the hunt finding: .env still has the wizard placeholder and
    settings.json has a real key — doctor used to PASS 'key from config'
    while the runtime client was disabled by the env placeholder."""
    monkeypatch.setenv("OPENAI_API_KEY", "YOUR_OPENAI_API_KEY")
    settings = {"llm": {"api_key": "sk-real-config-key"}}
    checks = _check_llm_keys(settings)
    assert len(checks) == 1
    assert checks[0].status == "FAIL"
    assert "placeholder" in checks[0].detail.lower()
    assert "OPENAI_API_KEY" in checks[0].detail  # names the effective source


def test_placeholder_variants_are_rejected(monkeypatch):
    for bogus in ("YOUR_OPENAI_API_KEY", "your-key", "sk-..."):
        monkeypatch.setenv("OPENAI_API_KEY", bogus)
        checks = _check_llm_keys(settings={})
        assert checks[0].status == "FAIL", bogus


def test_valid_env_key_passes_and_reports_env_source(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-env-key")
    checks = _check_llm_keys({"llm": {"api_key": "sk-config-key"}})
    assert checks[0].status == "PASS"
    assert "env var OPENAI_API_KEY" in checks[0].detail


def test_valid_config_key_passes_when_env_absent():
    checks = _check_llm_keys({"llm": {"api_key": "sk-config-key"}})
    assert checks[0].status == "PASS"
    assert "settings.json" in checks[0].detail


def test_placeholder_config_key_is_a_fail_not_a_pass():
    checks = _check_llm_keys({"llm": {"api_key": "YOUR_OPENAI_API_KEY"}})
    assert checks[0].status == "FAIL"


def test_no_key_anywhere_is_a_skip():
    checks = _check_llm_keys({})
    assert checks[0].status == "SKIP"


# --- clients side: shared placeholder detection + honest source -------------


def test_is_placeholder_key_recognition():
    from xuse.core.llm_service.clients import is_placeholder_key

    assert is_placeholder_key("openai_api_key", "YOUR_OPENAI_API_KEY")
    assert is_placeholder_key("openai_api_key", "your_openai_api_key")  # case-insensitive
    assert is_placeholder_key("openai_api_key", "your-key")
    assert is_placeholder_key("openai_api_key", "sk-...")
    assert is_placeholder_key("openai_api_key", "changeme")
    assert not is_placeholder_key("openai_api_key", "sk-real-123")
    assert not is_placeholder_key("openai_api_key", None)
    assert not is_placeholder_key("openai_api_key", "")


def test_build_client_disabled_by_placeholder_env_reports_honest_source(
    monkeypatch, make_config_loader
):
    import xuse.core.llm_service.clients as clients_module

    monkeypatch.setattr(clients_module, "load_env", lambda: None)
    monkeypatch.setenv("OPENAI_API_KEY", "YOUR_OPENAI_API_KEY")
    loader = make_config_loader(settings={"llm": {"api_key": "sk-real-config"}}, accounts=[])
    client, resolved = clients_module.build_client(loader)
    assert client is None  # env placeholder shadows the valid config key
    assert resolved["key_source"] == "env var OPENAI_API_KEY"
