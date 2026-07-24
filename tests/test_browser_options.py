"""Chrome option construction: anti-fingerprint experimental options are
applied for plain chromedriver but skipped for undetected-chromedriver
(UC applies its own; passing excludeSwitches through UC's capability flow
makes modern chromedriver reject the session — found in live testing).
"""
import pytest

pytest.importorskip("selenium")

from selenium.webdriver.chrome.options import Options as ChromeOptions

from xuse.core.browser_manager.options import configure_driver_options


def _build(for_undetected):
    return configure_driver_options(
        ChromeOptions(),
        "chrome",
        headless=False,
        window_size=None,
        proxy=None,
        additional_options=None,
        for_undetected=for_undetected,
    )


def test_plain_chromedriver_gets_antifingerprint_options():
    options = _build(for_undetected=False)
    assert options.experimental_options.get("excludeSwitches") == ["enable-automation"]
    assert options.experimental_options.get("useAutomationExtension") is False
    assert "--disable-blink-features=AutomationControlled" in options.arguments


def test_undetected_chromedriver_skips_antifingerprint_options():
    options = _build(for_undetected=True)
    assert "excludeSwitches" not in options.experimental_options
    assert "useAutomationExtension" not in options.experimental_options
    assert "--disable-blink-features=AutomationControlled" not in options.arguments
