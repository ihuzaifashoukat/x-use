"""retweet_or_quote truthfulness.

- A QUOTE request on an already-reposted tweet must NOT report success: the
  quote composer never opens, so claiming success would silently drop the
  quote text while callers record a fabricated quote_tweet. Plain retweets on
  an already-reposted tweet stay idempotent-success.
- After confirming a plain retweet, the button must flip to 'unretweet';
  without the flip there is no confirmation and the result is False.
- A quote post requires a confirmation signal (toast or composer detach).
- The target article is matched by exact status id.
"""

from unittest.mock import MagicMock

import pytest
from selenium.common.exceptions import NoSuchElementException, StaleElementReferenceException, TimeoutException

import xuse.features.publisher.retweet_handler as rt
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

    monkeypatch.setattr(rt, "WebDriverWait", FastWait)
    # retweet_handler delegates error-toast detection to reply_handler, whose
    # WebDriverWait reference must be fast too (otherwise: real 3s polls).
    import xuse.features.publisher.reply_handler as rh

    monkeypatch.setattr(rh, "WebDriverWait", FastWait)
    monkeypatch.setattr(rt.time, "sleep", lambda *_a: None)
    monkeypatch.setattr(rt.random, "uniform", lambda a, b: 0.0)


ARTICLES_Q = "//article[.//a[contains(@href, '/status/')]]"
LINKS_Q = ".//a[contains(@href, '/status/')]"
UNRETWEET_Q = ".//button[@data-testid='unretweet']"
RETWEET_Q = ".//button[@data-testid='retweet']"
QUOTE_OPT_Q = "//div[@role='menuitem' and contains(., 'Quote')]"
QUOTE_TA_Q = "//div[@data-testid='tweetTextarea_0' and @role='textbox']"
QUOTE_POST_Q = "//button[@data-testid='tweetButton']"
DROPDOWN_Q = "//div[@data-testid='Dropdown' or @role='menu']"
RT_CONFIRM_Q = ".//*[@data-testid='retweetConfirm']"
ANY_TOAST_Q = "//div[@data-testid='toast']"


class El:
    def __init__(self, routes=None, text="", on_click=None):
        self._routes = dict(routes or {})
        self._text = text
        self._on_click = on_click
        self.stale = False
        self.clicks = 0
        self.keys = []
        self.queries = []

    @property
    def text(self):
        return self._text

    def click(self):
        self.clicks += 1
        if self._on_click:
            self._on_click()

    def send_keys(self, *args):
        self.keys.append(args)

    def is_enabled(self):
        if self.stale:
            raise StaleElementReferenceException("node detached")
        return True

    def is_displayed(self):
        return True

    def find_element(self, by, value):
        self.queries.append(value)
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


def build(*, already_reposted=False, article_href="https://x.com/u/status/555"):
    refs = {}
    driver = FakeDriver()
    article = El(routes={LINKS_Q: [_Link(article_href)]})
    driver._routes[ARTICLES_Q] = [article]
    refs["article"] = article
    refs["driver"] = driver

    if already_reposted:
        article._routes[UNRETWEET_Q] = El()
    else:
        rt_icon = El()
        article._routes[RETWEET_Q] = rt_icon
        refs["rt_icon"] = rt_icon

    bm = MagicMock(name="browser_manager")
    bm.get_driver.return_value = driver
    bm.navigate_to.return_value = True
    return bm, refs


def tweet(tid="555"):
    return ScrapedTweet(tweet_id=tid, tweet_url=f"https://x.com/u/status/{tid}", text_content="hi")


class TestAlreadyReposted:
    def test_quote_request_on_already_reposted_returns_false(self):
        bm, refs = build(already_reposted=True)

        result = rt.retweet_or_quote(bm, tweet(), final_quote_text="my thoughtful quote")

        assert result is False
        # The quote composer must never be sought after the early verdict.
        assert QUOTE_OPT_Q not in refs["driver"].queries
        assert QUOTE_POST_Q not in refs["driver"].queries

    def test_plain_retweet_on_already_reposted_stays_idempotent_true(self):
        bm, _refs = build(already_reposted=True)

        assert rt.retweet_or_quote(bm, tweet(), final_quote_text=None) is True


class TestPlainRetweet:
    def test_confirm_click_followed_by_unretweet_flip_returns_true(self):
        bm, refs = build()
        driver = refs["driver"]

        def open_menu():
            confirm_btn = El(on_click=lambda: refs["article"]._routes.__setitem__(UNRETWEET_Q, El()))
            refs["confirm_btn"] = confirm_btn
            driver._routes[DROPDOWN_Q] = El(routes={RT_CONFIRM_Q: confirm_btn})

        refs["rt_icon"]._on_click = open_menu

        assert rt.retweet_or_quote(bm, tweet(), final_quote_text=None) is True
        assert refs["confirm_btn"].clicks == 1

    def test_confirm_click_without_unretweet_flip_returns_false(self):
        bm, refs = build()
        driver = refs["driver"]

        def open_menu():
            refs["confirm_btn"] = El()  # click never flips the button state
            driver._routes[DROPDOWN_Q] = El(routes={RT_CONFIRM_Q: refs["confirm_btn"]})

        refs["rt_icon"]._on_click = open_menu

        assert rt.retweet_or_quote(bm, tweet(), final_quote_text=None) is False
        assert refs["confirm_btn"].clicks == 1


class TestQuoteTweet:
    def _wire_quote_ui(self, refs, on_post):
        driver = refs["driver"]

        def open_menu():
            quote_opt = El(on_click=lambda: driver._routes.__setitem__(QUOTE_TA_Q, El()))
            post_btn = El(on_click=on_post)
            driver._routes[QUOTE_OPT_Q] = quote_opt
            driver._routes[QUOTE_POST_Q] = post_btn
            refs["quote_opt"] = quote_opt
            refs["post_btn"] = post_btn

        refs["rt_icon"]._on_click = open_menu

    def test_quote_post_with_toast_confirmation_returns_true(self):
        bm, refs = build()
        self._wire_quote_ui(refs, on_post=lambda: refs["driver"]._routes.__setitem__(ANY_TOAST_Q, El(text="Your post was sent.")))

        assert rt.retweet_or_quote(bm, tweet(), final_quote_text="great point") is True
        assert refs["post_btn"].clicks == 1

    def test_quote_post_with_composer_detach_returns_true(self):
        bm, refs = build()

        def detach():
            # Composer re-render: the quote textarea node is discarded.
            refs["driver"]._routes[QUOTE_TA_Q].stale = True

        self._wire_quote_ui(refs, on_post=detach)

        assert rt.retweet_or_quote(bm, tweet(), final_quote_text="great point") is True

    def test_quote_post_without_confirmation_returns_false(self):
        bm, refs = build()
        self._wire_quote_ui(refs, on_post=lambda: None)

        assert rt.retweet_or_quote(bm, tweet(), final_quote_text="great point") is False


class TestExactStatusIdMatching:
    def test_prefix_id_article_is_not_retweeted(self):
        bm, _refs = build(already_reposted=True, article_href="https://x.com/u/status/5555")

        assert rt.retweet_or_quote(bm, tweet("555"), final_quote_text=None) is False
