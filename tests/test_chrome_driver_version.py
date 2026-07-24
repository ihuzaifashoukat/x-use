"""undetected-chromedriver version pinning: init_chrome_driver passes the
configured or auto-detected Chrome major version to UC (otherwise UC fetches
the latest driver, which refuses a browser one major behind — found in live
testing on Chrome 150 vs driver 151).
"""
import pytest

pytest.importorskip("selenium")

from selenium.webdriver.chrome.options import Options as ChromeOptions

from xuse.core.browser_manager import drivers


class _FakeUC:
    calls = []

    @staticmethod
    def Chrome(**kwargs):
        _FakeUC.calls.append(kwargs)
        return object()


@pytest.fixture
def fake_uc(monkeypatch):
    _FakeUC.calls = []
    monkeypatch.setattr(drivers, "uc", _FakeUC)
    monkeypatch.setattr(drivers, "UC_AVAILABLE", True)
    return _FakeUC


def _init(version_main=None):
    return drivers.init_chrome_driver(
        ChromeOptions(), use_undetected=True,
        configured_path=None, service_args=None, version_main=version_main)


def test_configured_version_main_is_passed(fake_uc):
    _init(version_main=150)
    assert fake_uc.calls[0]["version_main"] == 150


def test_auto_detection_used_when_not_configured(fake_uc, monkeypatch):
    monkeypatch.setattr(drivers, "_detect_chrome_major_version", lambda: 149)
    _init()
    assert fake_uc.calls[0]["version_main"] == 149


def test_no_version_main_when_undetectable(fake_uc, monkeypatch):
    monkeypatch.setattr(drivers, "_detect_chrome_major_version", lambda: None)
    _init()
    assert "version_main" not in fake_uc.calls[0]
