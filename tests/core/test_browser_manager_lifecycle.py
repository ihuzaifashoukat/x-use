"""BrowserManager lifecycle hardening (fake drivers only — no real browser):

- ``get_driver()`` must quit an unresponsive driver before replacing it.
- Dict-shaped cookie JSON is rejected at load time: no post-start crash, no
  leaked browser, driver still returned without cookies.
- ``pool:<name>`` proxies resolve through ``ProxyManager`` (import path fix).
"""
import json

import pytest
from selenium.common.exceptions import WebDriverException

from xuse.core.browser_manager.constants import PROJECT_ROOT
from xuse.core.browser_manager.cookies import load_cookies_from_file
from xuse.core.browser_manager.service import BrowserManager
from xuse.core.config_loader import CONFIG_DIR as APP_CONFIG_DIR


class FakeWebDriver:
    """Minimal WebDriver stand-in recording lifecycle calls."""

    def __init__(self, responsive: bool = True):
        self.responsive = responsive
        self.quit_called = False
        self.pages = []
        self.cookies_added = []

    @property
    def current_url(self):
        if not self.responsive:
            raise WebDriverException("session gone")
        return "about:blank"

    def quit(self):
        self.quit_called = True

    def set_page_load_timeout(self, seconds):
        pass

    def set_script_timeout(self, seconds):
        pass

    def get(self, url):
        self.pages.append(url)

    def add_cookie(self, cookie):
        self.cookies_added.append(cookie)


def make_manager(make_config_loader, tmp_path, account_config=None, extra_browser_settings=None):
    """BrowserManager backed by tmp config files; WDM cache kept in tmp_path."""
    browser_settings = {
        "webdriver_manager_cache_path": str(tmp_path / "wdm_cache"),
        "enable_stealth": False,
    }
    if extra_browser_settings:
        browser_settings.update(extra_browser_settings)
    loader = make_config_loader(settings={"browser_settings": browser_settings}, accounts=[])
    return BrowserManager(account_config=account_config or {}, config_loader=loader)


def patch_driver_start(monkeypatch, driver):
    """Patch firefox driver init (the default browser type) + login wait."""
    monkeypatch.setattr(
        "xuse.core.browser_manager.service.init_firefox_driver",
        lambda options, configured_path=None, service_args=None: driver,
    )
    monkeypatch.setattr(
        "xuse.core.browser_manager.service.wait_for_signed_in",
        lambda drv, max_wait_seconds=0: False,
    )


# -- unresponsive driver replacement ----------------------------------------


def test_get_driver_quits_unresponsive_driver_before_replacing(make_config_loader, tmp_path, monkeypatch):
    manager = make_manager(make_config_loader, tmp_path)
    old_driver = FakeWebDriver(responsive=False)
    manager.driver = old_driver
    new_driver = FakeWebDriver()
    patch_driver_start(monkeypatch, new_driver)

    driver = manager.get_driver()

    assert old_driver.quit_called is True
    assert driver is new_driver
    assert manager.driver is new_driver


# -- cookie file shape validation --------------------------------------------


def test_load_cookies_from_file_rejects_dict_shape(tmp_path):
    cookie_file = tmp_path / "cookies.json"
    cookie_file.write_text(json.dumps({"cookies": [{"name": "a", "value": "b"}]}), encoding="utf-8")
    assert load_cookies_from_file(str(cookie_file), APP_CONFIG_DIR, PROJECT_ROOT) is None


def test_load_cookies_from_file_rejects_non_dict_entries(tmp_path):
    cookie_file = tmp_path / "cookies.json"
    cookie_file.write_text(json.dumps(["not-a-cookie"]), encoding="utf-8")
    assert load_cookies_from_file(str(cookie_file), APP_CONFIG_DIR, PROJECT_ROOT) is None


def test_load_cookies_from_file_rejects_entries_missing_name_or_value(tmp_path):
    cookie_file = tmp_path / "cookies.json"
    cookie_file.write_text(json.dumps([{"domain": ".x.com"}]), encoding="utf-8")
    assert load_cookies_from_file(str(cookie_file), APP_CONFIG_DIR, PROJECT_ROOT) is None


def test_load_cookies_from_file_accepts_valid_list(tmp_path):
    cookie_file = tmp_path / "cookies.json"
    cookies = [{"name": "auth_token", "value": "x", "domain": ".x.com"}]
    cookie_file.write_text(json.dumps(cookies), encoding="utf-8")
    assert load_cookies_from_file(str(cookie_file), APP_CONFIG_DIR, PROJECT_ROOT) == cookies


def test_dict_shaped_cookie_file_does_not_crash_get_driver(make_config_loader, tmp_path, monkeypatch):
    cookie_file = tmp_path / "cookies.json"
    cookie_file.write_text(json.dumps({"cookies": [{"name": "a", "value": "b"}]}), encoding="utf-8")
    manager = make_manager(make_config_loader, tmp_path, account_config={"cookies": str(cookie_file)})
    assert manager.cookies_data is None

    driver = FakeWebDriver()
    patch_driver_start(monkeypatch, driver)
    assert manager.get_driver() is driver
    assert driver.cookies_added == []
    manager.close_driver()
    assert driver.quit_called is True


# -- proxy pool resolution ----------------------------------------------------


def test_proxy_pool_reference_is_resolved(make_config_loader, tmp_path):
    manager = make_manager(
        make_config_loader,
        tmp_path,
        account_config={"account_id": "acc1", "proxy": "pool:resi"},
        extra_browser_settings={"proxy_pools": {"resi": ["http://user:pass@proxy1.example:8080"]}},
    )
    assert manager.effective_proxy == "http://user:pass@proxy1.example:8080"
