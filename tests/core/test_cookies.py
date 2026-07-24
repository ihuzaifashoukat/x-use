"""Cookie application contract: exactly one domain navigation and NO
trailing refresh. The caller's verification navigation (service.py loads
/home right after) is what sends the freshly-set cookies — the refresh was
a redundant 4th page load on every cold start (live finding, v2.3)."""
from xuse.core.browser_manager.cookies import apply_cookies


class FakeDriver:
    def __init__(self):
        self.calls = []

    def get(self, url):
        self.calls.append(("get", url))

    def refresh(self):
        self.calls.append(("refresh",))

    def add_cookie(self, cookie):
        self.calls.append(("add_cookie", cookie["name"]))


def test_apply_cookies_navigates_once_without_refresh():
    driver = FakeDriver()
    apply_cookies(
        driver,
        [{"name": "auth_token", "value": "x", "domain": ".x.com", "path": "/"}],
        "https://x.com",
    )
    assert [c for c in driver.calls if c[0] == "get"] == [("get", "https://x.com")]
    assert ("add_cookie", "auth_token") in driver.calls
    assert ("refresh",) not in driver.calls


def test_apply_cookies_without_domain_just_sets_cookies():
    driver = FakeDriver()
    apply_cookies(driver, [{"name": "ct0", "value": "y"}], None)
    assert driver.calls == [("add_cookie", "ct0")]
