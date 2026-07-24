"""Custom/generated User-Agent must actually reach the browser.

- Plain Chrome gets a proper '--user-agent=' switch (the old bare
  'user-agent=' token was parsed as a positional URL argument, not a switch).
- undetected-chromedriver manages its own UA: no --user-agent argument is
  added on that path (the for_undetected gate is respected).
- Firefox has no UA CLI switch at all; it needs the
  'general.useragent.override' preference.
"""

import pytest

pytest.importorskip("selenium")

from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.firefox.options import Options as FirefoxOptions

from xuse.core.browser_manager.options import configure_driver_options

UA = "MyCustomUA/1.0"


def build(browser_type, *, for_undetected=False, custom_user_agent=UA):
    options = ChromeOptions() if browser_type == "chrome" else FirefoxOptions()
    return configure_driver_options(
        options,
        browser_type,
        headless=True,
        window_size=None,
        proxy=None,
        additional_options=None,
        custom_user_agent=custom_user_agent,
        for_undetected=for_undetected,
    )


def ua_args(options):
    return [a for a in options.arguments if "user-agent" in a.lower()]


class TestChrome:
    def test_plain_chrome_gets_prefixed_switch(self):
        options = build("chrome")
        args = ua_args(options)
        assert args == [f"--user-agent={UA}"]

    def test_no_unprefixed_bare_token(self):
        options = build("chrome")
        assert all(a.startswith("--") for a in ua_args(options))

    def test_generated_ua_is_applied_when_no_custom(self):
        options = build("chrome", custom_user_agent=None)
        args = ua_args(options)
        assert len(args) == 1 and args[0].startswith("--user-agent=")
        assert len(args[0]) > len("--user-agent=")

    def test_undetected_chromedriver_path_untouched(self):
        options = build("chrome", for_undetected=True)
        assert ua_args(options) == []


class TestFirefox:
    def test_firefox_uses_override_preference(self):
        options = build("firefox")
        prefs = dict(options.preferences)
        assert prefs.get("general.useragent.override") == UA

    def test_firefox_gets_no_cli_token(self):
        options = build("firefox")
        assert ua_args(options) == []

    def test_generated_ua_is_applied_when_no_custom(self):
        options = build("firefox", custom_user_agent=None)
        prefs = dict(options.preferences)
        ua = prefs.get("general.useragent.override")
        assert isinstance(ua, str) and ua
