"""like_tweet truthfulness: respect navigate_to() failures, coerce pydantic
HttpUrl to str before handing it to Selenium, and match tweet articles by
exact status id (never a contains() prefix).

A raw pydantic HttpUrl is not JSON-serializable, so driver.get() raises inside
navigate_to (which returns False); liking must abort there instead of
searching the stale page. All paths run against fakes — no browser.
"""

from unittest.mock import MagicMock

import pytest
from pydantic import HttpUrl
from selenium.common.exceptions import NoSuchElementException, TimeoutException

import xuse.features.engagement as engagement_mod
from xuse.features.engagement import TweetEngagement
from xuse.models import AccountConfig


@pytest.fixture(autouse=True)
def fast_waits_and_no_sleeps(monkeypatch):
    """Deterministic, instant WebDriverWait: evaluate the condition a few
    times with no polling delay; raise TimeoutException when never truthy."""

    class FastWait:
        def __init__(self, ctx, timeout):
            self.ctx = ctx

        def until(self, condition, message=""):
            for _ in range(3):
                try:
                    r = condition(self.ctx)
                    if r:
                        return r
                except Exception:
                    pass
            raise TimeoutException("fast-wait: condition never satisfied")

    monkeypatch.setattr(engagement_mod, "WebDriverWait", FastWait)
    monkeypatch.setattr(engagement_mod.time, "sleep", lambda *_a: None)


class FakeElement:
    """Query-string-keyed fake WebElement (mirrors the scraper test fakes)."""

    def __init__(self, routes=None, attrs=None, displayed=True, enabled=True):
        self._routes = dict(routes or {})
        self._attrs = dict(attrs or {})
        self._displayed = displayed
        self._enabled = enabled
        self.clicks = 0

    def find_element(self, by, value):
        entry = self._routes.get(value)
        if entry is None:
            raise NoSuchElementException(f"not found: {value}")
        if isinstance(entry, Exception):
            raise entry
        if isinstance(entry, list):
            if not entry:
                raise NoSuchElementException(f"not found: {value}")
            return entry[0]
        return entry

    def find_elements(self, by, value):
        entry = self._routes.get(value)
        if entry is None or isinstance(entry, Exception):
            return []
        return list(entry) if isinstance(entry, list) else [entry]

    def get_attribute(self, name):
        return self._attrs.get(name)

    def is_displayed(self):
        return self._displayed

    def is_enabled(self):
        return self._enabled

    def click(self):
        self.clicks += 1


class FakeDriver(FakeElement):
    def __init__(self, routes=None, current_url="https://x.com/home"):
        super().__init__(routes=routes)
        self.current_url = current_url

    def execute_script(self, *a, **k):
        pass


ARTICLES_Q = "//article[.//a[contains(@href, '/status/')]]"
LINKS_Q = ".//a[contains(@href, '/status/')]"
UNLIKE_Q = './/button[@data-testid="unlike"]'
LIKE_Q = './/button[@data-testid="like"]'


def make_engagement(navigate_result=True, current_url="https://x.com/home", driver=None):
    driver = driver or FakeDriver(current_url=current_url)
    bm = MagicMock(name="browser_manager")
    bm.get_driver.return_value = driver
    bm.navigate_to.return_value = navigate_result
    engagement = TweetEngagement(bm, AccountConfig(account_id="acct1"))
    return engagement, bm, driver


def article_with_status(href, extra_routes=None):
    routes = {LINKS_Q: [FakeElement(attrs={"href": href})]}
    routes.update(extra_routes or {})
    return FakeElement(routes=routes)


@pytest.mark.asyncio
async def test_navigation_failure_aborts_like():
    engagement, bm, driver = make_engagement(navigate_result=False)
    spy = MagicMock(side_effect=NoSuchElementException("stale page"))
    driver.find_element = spy  # any article search on the stale page is a bug

    result = await engagement.like_tweet("123", "https://x.com/u/status/123")

    assert result is False
    bm.navigate_to.assert_called_once_with("https://x.com/u/status/123")
    spy.assert_not_called()


@pytest.mark.asyncio
async def test_httpurl_is_coerced_to_str_before_navigation():
    engagement, bm, _driver = make_engagement(navigate_result=False)

    result = await engagement.like_tweet("123", HttpUrl("https://x.com/u/status/123"))

    assert result is False  # navigation still fails; the point is the str cast
    (called_url,), _ = bm.navigate_to.call_args
    assert isinstance(called_url, str)
    assert called_url == "https://x.com/u/status/123"


@pytest.mark.asyncio
async def test_find_tweet_on_page_matches_exact_id_not_prefix():
    engagement, _bm, driver = make_engagement()
    wrong = article_with_status("https://x.com/u/status/1234")
    right = article_with_status("https://x.com/u/status/123")
    driver._routes[ARTICLES_Q] = [wrong, right]

    found = engagement._find_tweet_on_page("123")

    assert found is right


@pytest.mark.asyncio
async def test_find_tweet_on_page_returns_none_for_prefix_only():
    engagement, _bm, driver = make_engagement()
    driver._routes[ARTICLES_Q] = [article_with_status("https://x.com/u/status/1234")]

    assert engagement._find_tweet_on_page("123") is None


@pytest.mark.asyncio
async def test_like_succeeds_when_already_liked():
    engagement, _bm, driver = make_engagement(navigate_result=True)
    article = article_with_status(
        "https://x.com/u/status/123",
        extra_routes={UNLIKE_Q: [FakeElement()]},
    )
    driver._routes[ARTICLES_Q] = [article]

    result = await engagement.like_tweet("123", "https://x.com/u/status/123")

    assert result is True


@pytest.mark.asyncio
async def test_like_click_confirmed_by_unlike_flip():
    engagement, _bm, driver = make_engagement(navigate_result=True)
    like_btn = FakeElement()
    article = article_with_status(
        "https://x.com/u/status/123",
        extra_routes={LIKE_Q: like_btn},
    )

    def flip_to_unlike():
        like_btn.clicks += 1
        article._routes[UNLIKE_Q] = [FakeElement()]

    like_btn.click = flip_to_unlike
    driver._routes[ARTICLES_Q] = [article]

    result = await engagement.like_tweet("123", "https://x.com/u/status/123")

    assert result is True
    assert like_btn.clicks == 1


@pytest.mark.asyncio
async def test_like_click_without_flip_returns_false():
    engagement, _bm, driver = make_engagement(navigate_result=True)
    like_btn = FakeElement()
    article = article_with_status(
        "https://x.com/u/status/123",
        extra_routes={LIKE_Q: like_btn},  # never gains an 'unlike' button
    )
    driver._routes[ARTICLES_Q] = [article]

    result = await engagement.like_tweet("123", "https://x.com/u/status/123")

    assert result is False
    assert like_btn.clicks == 1
