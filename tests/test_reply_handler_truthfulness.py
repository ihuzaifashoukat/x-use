"""reply_handler truthfulness: no success without confirmation.

- A detected platform error toast (rate limit / 'went wrong' / 'Try again')
  must make reply_to_tweet return False, not True.
- The Ctrl+Enter keyboard fallback may only return True after a confirmation
  signal: the dialog detaches, or — with no dialog — a toast appears or the
  composer textarea detaches. No dialog + no confirmation => False.
- The target tweet article is matched by exact status id.

All DOM interaction is faked; WebDriverWait is replaced with an instant,
deterministic evaluator.
"""

from unittest.mock import MagicMock

import pytest
from selenium.common.exceptions import NoSuchElementException, StaleElementReferenceException, TimeoutException
from selenium.webdriver.common.keys import Keys

import xuse.features.publisher.reply_handler as rh
from xuse.models import ScrapedTweet


@pytest.fixture(autouse=True)
def fast_waits_and_no_sleeps(monkeypatch):
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

    monkeypatch.setattr(rh, "WebDriverWait", FastWait)
    monkeypatch.setattr(rh.time, "sleep", lambda *_a: None)
    monkeypatch.setattr(rh.random, "uniform", lambda a, b: 0.0)


ARTICLES_Q = "//article[.//a[contains(@href, '/status/')]]"
LINKS_Q = ".//a[contains(@href, '/status/')]"
REPLY_BTN_Q = ".//button[@data-testid='reply']"
DIALOG_Q = "//div[@role='dialog' and @aria-modal='true']"
TA_SCOPED_Q = ".//div[@data-testid='tweetTextarea_0' and @role='textbox']"
TA_GLOBAL_Q = "//div[@data-testid='tweetTextarea_0' and @role='textbox']"
ENABLED_BTN_Q = ".//button[@data-testid='tweetButton' and not(@disabled) and not(@aria-disabled='true')]"
ERROR_TOAST_Q = rh.REPLY_ERROR_TOAST_XPATH
ANY_TOAST_Q = rh.REPLY_ANY_TOAST_XPATH


class El:
    """Fake DOM node keyed on exact query strings, with staleness support."""

    def __init__(self, routes=None, text="", on_click=None, on_keys=None):
        self._routes = dict(routes or {})
        self._text = text
        self._on_click = on_click
        self._on_keys = on_keys
        self.stale = False
        self.clicks = 0
        self.keys = []

    @property
    def text(self):
        return self._text

    def click(self):
        self.clicks += 1
        if self._on_click:
            self._on_click()

    def send_keys(self, *args):
        self.keys.append(args)
        if self._on_keys:
            self._on_keys(args)

    def is_enabled(self):
        if self.stale:
            raise StaleElementReferenceException("node detached")
        return True

    def is_displayed(self):
        return True

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
        return None


class FakeDriver(El):
    def execute_script(self, *a, **k):
        pass


class _Link:
    def __init__(self, href):
        self._href = href

    def get_attribute(self, name):
        return self._href if name == "href" else None


def build_world(*, dialog=True, enabled_button=True, article_href="https://x.com/u/status/123"):
    """Assemble a fake DOM; returns (driver, refs) where refs exposes the
    textarea, dialog and enabled-post-button for state flips and assertions."""
    refs = {}
    driver = FakeDriver()

    article = El(routes={LINKS_Q: [_Link(article_href)]})
    reply_icon = El()
    article._routes[REPLY_BTN_Q] = reply_icon
    driver._routes[ARTICLES_Q] = [article]

    textarea = El()
    refs["textarea"] = textarea

    dlg = None
    if dialog:
        dlg = El(routes={TA_SCOPED_Q: textarea})
        driver._routes[DIALOG_Q] = dlg
    else:
        driver._routes[TA_GLOBAL_Q] = textarea
    refs["dialog"] = dlg

    if enabled_button:
        post_btn = El()
        refs["post_btn"] = post_btn
        (dlg if dlg is not None else driver)._routes[ENABLED_BTN_Q] = post_btn

    bm = MagicMock(name="browser_manager")
    bm.get_driver.return_value = driver
    bm.navigate_to.return_value = True
    return bm, driver, refs


def tweet(tid="123"):
    return ScrapedTweet(tweet_id=tid, tweet_url=f"https://x.com/u/status/{tid}", text_content="hi")


class TestErrorToastMeansFailure:
    def test_platform_error_toast_after_click_returns_false(self, caplog):
        bm, driver, refs = build_world()

        def reject():
            driver._routes[ERROR_TOAST_Q] = El(text="Something went wrong. Try again.")

        refs["post_btn"]._on_click = reject

        with caplog.at_level("WARNING", logger=rh.logger.name):
            result = rh.reply_to_tweet(bm, tweet(), "my reply")

        assert result is False
        assert any("Try again" in r.getMessage() or "went wrong" in r.getMessage() for r in caplog.records)

    def test_success_toast_is_not_mistaken_for_error(self):
        """A benign toast (no error text) must not trip the error matcher."""
        bm, driver, refs = build_world()

        def confirm():
            refs["dialog"].stale = True
            driver._routes[ANY_TOAST_Q] = El(text="Your reply was sent.")

        refs["post_btn"]._on_click = confirm

        assert rh.reply_to_tweet(bm, tweet(), "my reply") is True


class TestClickPathConfirmation:
    def test_dialog_detach_confirms_reply(self):
        bm, _driver, refs = build_world()
        refs["post_btn"]._on_click = lambda: setattr(refs["dialog"], "stale", True)

        assert rh.reply_to_tweet(bm, tweet(), "my reply") is True

    def test_no_confirmation_after_click_returns_false(self):
        bm, _driver, refs = build_world()
        # Click does nothing: dialog stays mounted, no toast, textarea alive.
        assert rh.reply_to_tweet(bm, tweet(), "my reply") is False


class TestKeyboardFallback:
    def test_ctrl_enter_with_dialog_detach_returns_true(self):
        bm, _driver, refs = build_world(enabled_button=False)

        def on_keys(args):
            if args == (Keys.CONTROL, Keys.ENTER):
                refs["dialog"].stale = True

        refs["textarea"]._on_keys = on_keys

        assert rh.reply_to_tweet(bm, tweet(), "my reply") is True
        assert (Keys.CONTROL, Keys.ENTER) in refs["textarea"].keys

    def test_ctrl_enter_no_dialog_with_toast_returns_true(self):
        bm, driver, refs = build_world(dialog=False, enabled_button=False)

        def on_keys(args):
            if args == (Keys.CONTROL, Keys.ENTER):
                driver._routes[ANY_TOAST_Q] = El(text="Your reply was sent.")

        refs["textarea"]._on_keys = on_keys

        assert rh.reply_to_tweet(bm, tweet(), "my reply") is True

    def test_ctrl_enter_no_dialog_no_confirmation_returns_false(self):
        bm, _driver, refs = build_world(dialog=False, enabled_button=False)

        result = rh.reply_to_tweet(bm, tweet(), "my reply")

        assert result is False
        sent = refs["textarea"].keys
        assert (Keys.CONTROL, Keys.ENTER) in sent
        # The old bare-ENTER retry (a guaranteed no-op newline) is gone.
        assert (Keys.ENTER,) not in sent


class TestExactStatusIdMatching:
    def test_prefix_id_article_is_not_replied_to(self):
        bm, _driver, _refs = build_world(article_href="https://x.com/u/status/1234")

        # Tweet id 123 must not match the article whose status id is 1234.
        assert rh.reply_to_tweet(bm, tweet("123"), "my reply") is False
