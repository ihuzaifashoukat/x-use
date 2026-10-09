"""Test fixture isolation against synthetic cookies outside its private home."""
import json

import pytest

from conftest import isolate_mcp_state as isolate_fixture
from xuse.browser import cookies
from xuse.browser.errors import SessionError
from xuse.core import config_loader
from xuse.core.browser_manager import constants, service
from xuse.utils import proxy_manager


@pytest.mark.parametrize("backend", ["playwright", "selenium"])
def test_isolation_fixture_prevents_relative_cookie_fallback_outside_home(
    tmp_path, monkeypatch, backend
):
    outside = tmp_path / "synthetic-checkout"
    (outside / "config").mkdir(parents=True)
    payload = [{"name": name, "value": "outside-home-synthetic-sentinel",
                "domain": ".x.com", "secure": True}
               for name in ("auth_token", "ct0")]
    filename = "synthetic-relative-cookies.json"
    (outside / filename).write_text(json.dumps(payload), encoding="utf-8")
    (outside / "config" / filename).write_text(json.dumps(payload), encoding="utf-8")
    (outside / "data").mkdir()
    outside_proxy_state = outside / "data" / "proxy_pools_state.json"
    outside_proxy_state.write_text('{"fixture": 9}', encoding="utf-8")
    # Model modules imported before the fixture runs, with roots still bound
    # to an outside checkout. Every outside file here is synthetic test data.
    monkeypatch.setattr(cookies, "PROJECT_ROOT", outside)
    monkeypatch.setattr(service, "PROJECT_ROOT", outside)
    monkeypatch.setattr(service, "APP_CONFIG_DIR", outside / "config")
    monkeypatch.setattr(service, "DEFAULT_WDM_CACHE_PATH", outside / ".wdm_cache")
    monkeypatch.setattr(constants, "PROJECT_ROOT", outside)
    monkeypatch.setattr(constants, "CONFIG_PROJECT_ROOT", outside)
    monkeypatch.setattr(constants, "DEFAULT_WDM_CACHE_PATH", outside / ".wdm_cache")
    monkeypatch.setattr(proxy_manager, "PROJECT_ROOT", outside)

    with pytest.MonkeyPatch.context() as isolation_patch:
        home = isolate_fixture.__wrapped__(tmp_path / "isolated-test", isolation_patch)
        loader = config_loader.ConfigLoader()
        assert loader.accounts_file == home / "config" / "accounts.json"
        if backend == "playwright":
            with pytest.raises(SessionError) as failure:
                cookies.load_account_cookies({"cookie_file_path": filename}, loader)
            assert failure.value.reason == "missing_cookies"
        else:
            loader.settings = {"browser_settings": {
                "proxy_pools": {"fixture": ["http://first.invalid:80", "http://second.invalid:80"]},
                "proxy_pool_strategy": "round_robin"}}
            manager = service.BrowserManager(
                account_config={"cookie_file_path": filename, "proxy": "pool:fixture"}, config_loader=loader)
            assert manager.cookies_data is None
            assert manager.wdm_cache_path == home / ".wdm_cache"
            assert manager.effective_proxy == "http://first.invalid:80"
            assert json.loads((home / "data" / "proxy_pools_state.json").read_text()) == {"fixture": 1}
        assert constants.PROJECT_ROOT == home
        assert constants.CONFIG_PROJECT_ROOT == home
        assert constants.DEFAULT_WDM_CACHE_PATH == home / ".wdm_cache"
        assert proxy_manager.PROJECT_ROOT == home
        assert not (outside / ".wdm_cache").exists()
        assert outside_proxy_state.read_text(encoding="utf-8") == '{"fixture": 9}'
