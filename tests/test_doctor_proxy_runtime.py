"""Doctor validates the selected async proxy route without leaking credentials."""
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
import xuse.doctor as doctor


def loader(settings):
    def get_setting(path, default=None):
        value = settings
        for part in path.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value
    return SimpleNamespace(get_settings=lambda: settings, get_setting=get_setting)


@pytest.mark.parametrize("backend", ["patchright", "playwright"])
def test_doctor_global_env_proxy_matches_runtime_without_exposing_auth(monkeypatch, backend):
    monkeypatch.setenv("XUSE_TEST_PROXY", "http://PRIVATE_USER:PRIVATE_PASSWORD@proxy.test:8080")
    config = loader({"mcp": {"browser_backend": backend}, "browser_settings": {"proxy": "${XUSE_TEST_PROXY}"}})
    calls = []
    monkeypatch.setattr(doctor.socket, "create_connection", lambda address, **kwargs: calls.append(address) or nullcontext())
    checks = doctor._check_proxies(config, [{"account_id": "one"}], include_mcp=True)
    assert calls == [("proxy.test", 8080)]
    assert checks[0].status == "PASS"
    assert "authentication and X access not verified" in checks[0].detail
    assert "PRIVATE" not in repr(checks)


@pytest.mark.parametrize("invalid", ["${XUSE_MISSING_PROXY}", "http://PRIVATE_USER:PRIVATE_PASSWORD@proxy.test:0", "pool:missing", "socks5://PRIVATE_USER:PRIVATE_PASSWORD@proxy.test:1080"])
def test_invalid_configured_proxy_does_not_probe_global_fallback(monkeypatch, invalid):
    config = loader({"browser_settings": {"proxy": "http://fallback.test:80"}})
    monkeypatch.delenv("XUSE_MISSING_PROXY", raising=False)
    monkeypatch.setattr(doctor.socket, "create_connection", lambda *args, **kwargs: pytest.fail("must not connect"))
    checks = doctor._check_proxies(config, [{"account_id": "one", "proxy": invalid}], include_mcp=True)
    assert checks[0].status == "FAIL" and "PRIVATE" not in repr(checks)


def test_connection_error_does_not_echo_raw_exception_or_auth(monkeypatch):
    config = loader({})
    def fail(*args, **kwargs):
        raise RuntimeError("PRIVATE_PASSWORD")
    monkeypatch.setattr(doctor.socket, "create_connection", fail)
    checks = doctor._check_proxies(config, [{"account_id": "one", "proxy": "https://user:PRIVATE_PASSWORD@proxy.test"}], include_mcp=True)
    assert checks[0].status == "FAIL" and "TCP connection failed" in checks[0].detail
    assert "PRIVATE_PASSWORD" not in repr(checks)


def test_doctor_rejects_rotating_pool_for_async_sessions(monkeypatch):
    config = loader({"browser_settings": {"proxy_pool_strategy": "round_robin", "proxy_pools": {"p": ["http://proxy.test:80"]}}})
    monkeypatch.setattr(doctor.socket, "create_connection", lambda *args, **kwargs: pytest.fail("must not connect"))
    checks = doctor._check_proxies(config, [{"account_id": "one", "proxy": "pool:p"}], include_mcp=True)
    assert checks[0].status == "FAIL"


def test_direct_accounts_skip_network_probe(monkeypatch):
    monkeypatch.setattr(doctor.socket, "create_connection", lambda *args, **kwargs: pytest.fail("must not connect"))
    checks = doctor._check_proxies(loader({}), [{"account_id": "one"}], include_mcp=True)
    assert checks[0].status == "SKIP"
