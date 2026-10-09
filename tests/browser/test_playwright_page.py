import asyncio

import pytest

from xuse.browser.errors import BrowserActionError, BrowserBlocked, SessionError
from xuse.browser.page import XBrowser, status_id, tweet_from_snapshot, validate_x_url


@pytest.mark.parametrize("url", [
    "http://x.com/home", "https://x.com.evil.test/home", "https://evilx.com/home",
    "https://x.com@evil.test/home", "https://user:secret@x.com/home",
    "https://twitter.com/home", "https://x.com:8443/home", "https://x.com/home\n",
])
def test_navigation_is_bound_to_https_x_origin(url):
    with pytest.raises(BrowserActionError, match="invalid_url"):
        validate_x_url(url)


def test_status_id_has_exact_boundary_and_accepts_media_suffix():
    assert status_id("/person/status/123") == "123"
    assert status_id("https://x.com/person/status/1234/photo/1?ref=1") == "1234"
    assert status_id("/i/web/status/123?x=1") == "123"
    assert status_id("https://x.com/person/status/123abc") is None
    assert status_id("https://evil.test/person/status/123") is None


def test_tweet_snapshot_parsing_counts_media_datetime_and_tags():
    tweet = tweet_from_snapshot({
        "status_url": "/person/status/123", "text": "Hello @friend #Python 🧵 1/2",
        "name": "Person", "handle": "@person", "created_at": "2026-10-08T12:00:00Z",
        "like_count": "1.2K", "reply_count": "3", "retweet_count": "1,234",
        "media": [{"type": "image", "url": "https://pbs.twimg.com/media/example.jpg", "alt_text": "Photo"},
                  {"type": "image", "url": "https://pbs.twimg.com/media/example.jpg"},
                  {"type": "video", "url": "blob:private"}],
    })
    assert tweet.tweet_id == "123" and tweet.like_count == 1200 and tweet.retweet_count == 1234
    assert tweet.created_at.tzinfo is not None
    assert tweet.tags == ["#Python"] and tweet.mentions == ["@friend"] and tweet.is_thread_candidate
    assert len(tweet.media) == 1 and tweet.media[0].alt_text == "Photo"


def test_media_only_tweet_is_kept_and_malformed_permalink_is_skipped():
    assert tweet_from_snapshot({"status_url": "/person/status/123", "text": ""}) is not None
    assert tweet_from_snapshot({"status_url": "/person/status/123abc", "text": "Hello"}) is None


class Locator:
    def __init__(self, page, key, visible=False, children=None, snapshot=None):
        self.page = page
        self.key = key
        self.visible = visible
        self.children = children
        self.snapshot = snapshot
        self.clicks = 0
        self.first = self

    async def count(self):
        return len(self.children) if self.children is not None else int(self.visible)

    def nth(self, index):
        return self.children[index] if self.children is not None else self

    async def is_visible(self):
        return self.visible

    async def wait_for(self, **kwargs):
        if not self.visible:
            raise RuntimeError("secret-cookie-in-raw-browser-error")

    async def click(self):
        self.clicks += 1
        if self.page.confirm_clicks and self.key == "like":
            self.page.unlike.visible = True

    async def evaluate(self, _):
        return self.snapshot

    def get_by_test_id(self, key):
        if key == "like":
            return self.page.like
        if key == "unlike":
            return self.page.unlike
        return Locator(self.page, key)

    def locator(self, key):
        if key.startswith('[data-testid="like"]:not('):
            return self.page.like
        if key.startswith('[data-testid="unlike"]:not('):
            return self.page.unlike
        return Locator(self.page, key)

    def filter(self, **kwargs):
        return self


class Page:
    def __init__(self, snapshots=None, confirm_clicks=True, url="https://x.com/home", response_status=200):
        self.url = url
        self.confirm_clicks = confirm_clicks
        self.response_status = response_status
        self.like = Locator(self, "like", True)
        self.unlike = Locator(self, "unlike", False)
        self.cards = [Locator(self, "article", True, snapshot=data) for data in (snapshots or [])]
        self.goto_calls = []
        self.fail_goto = False

    async def goto(self, url, **kwargs):
        self.goto_calls.append(url)
        if self.fail_goto:
            raise RuntimeError("secret-session-value")
        self.url = url
        return type("Response", (), {"status": self.response_status})()

    def locator(self, key):
        if key == 'article[data-testid="tweet"]':
            return Locator(self, key, bool(self.cards), children=self.cards)
        return Locator(self, key)

    def get_by_test_id(self, key):
        return Locator(self, key, key == "SideNav_AccountSwitcher_Button")

    def get_by_text(self, *args, **kwargs):
        return Locator(self, "text")


@pytest.mark.asyncio
async def test_target_matches_primary_id_exactly_and_never_prefix_matches():
    page = Page([{"status_url": "/person/status/1234", "text": "other"},
                 {"status_url": "/person/status/123", "text": "target"}])
    browser = XBrowser(page)
    tweets = await browser.get_tweet("https://x.com/person/status/123")
    assert [tweet.text_content for tweet in tweets] == ["target"]
    page.cards = page.cards[:1]
    with pytest.raises(BrowserActionError, match="tweet_not_found"):
        await browser.get_tweet("https://x.com/person/status/123")


@pytest.mark.asyncio
async def test_like_requires_observed_inverse_control_and_does_not_toggle_existing_like():
    page = Page([{"status_url": "/person/status/123", "text": "target"}])
    browser = XBrowser(page)
    result = await browser.like("https://x.com/person/status/123")
    assert result["success"] is True and result["evidence"]["observed"] == "unlike_control"
    assert page.like.clicks == 1
    result = await browser.like("https://x.com/person/status/123")
    assert result["status"] == "already_done" and page.like.clicks == 1


@pytest.mark.asyncio
async def test_ambiguous_click_is_unknown_and_never_retried():
    page = Page([{"status_url": "/person/status/123", "text": "target"}], confirm_clicks=False)
    browser = XBrowser(page)
    with pytest.raises(BrowserActionError, match="outcome_unknown") as error:
        await browser.like("https://x.com/person/status/123")
    assert page.like.clicks == 1
    assert "secret-cookie" not in str(error.value)


@pytest.mark.asyncio
async def test_navigation_rate_limit_and_login_stop_without_leaking_browser_errors():
    page = Page(response_status=429)
    browser = XBrowser(page)
    with pytest.raises(BrowserBlocked, match="rate_limited"):
        await browser.navigate("https://x.com/home")
    page.response_status = 200
    with pytest.raises(BrowserBlocked, match="login_required"):
        await browser.navigate("https://x.com/i/flow/login")
    page.fail_goto = True
    with pytest.raises(BrowserActionError) as error:
        await browser.navigate("https://x.com/home")
    assert str(error.value) == "Browser session unavailable (navigation_failed)."
    assert error.value.__cause__ is None


@pytest.mark.asyncio
async def test_invalid_community_fails_before_any_navigation_or_write():
    page = Page()
    browser = XBrowser(page)
    with pytest.raises(BrowserActionError, match="community_unverified"):
        await browser.post("A reviewed post", community="123")
    assert page.goto_calls == []


def test_unknown_error_reason_is_sanitized_for_both_message_and_structure():
    error = SessionError("secret-credential-value")
    assert error.reason == "session_unavailable"
    assert "secret" not in str(error)


@pytest.mark.asyncio
async def test_submission_confirmation_requires_new_exact_text_and_known_own_author():
    page = Page([{"status_url": "/person/status/123", "text": "Reviewed message", "handle": "@person"}])
    browser = XBrowser(page, account={"self_handles": ["person"]})
    result = await browser._observe_submission({"122"}, "Reviewed message", "post")
    assert result["success"] and result["evidence"]["tweet_id"] == "123"
    browser.action_confirmation_timeout_ms = 1
    with pytest.raises(BrowserActionError, match="outcome_unknown"):
        await browser._observe_submission({"123"}, "Reviewed message", "post")
    browser.account = {"self_handles": ["other"]}
    with pytest.raises(BrowserActionError, match="outcome_unknown"):
        await browser._observe_submission(set(), "Reviewed message", "post")


@pytest.mark.asyncio
async def test_real_dom_fixture_preserves_primary_permalink_and_ignores_quoted_text():
    api = pytest.importorskip("playwright.async_api")
    async with api.async_playwright() as runtime:
        try:
            chromium = await runtime.chromium.launch(channel="chrome", headless=True)
        except Exception:
            pytest.skip("Local Chrome runtime is unavailable for the offline DOM fixture.")
        try:
            page = await chromium.new_page()
            await page.route("**/*", lambda route: route.abort())
            await page.set_content('''<article data-testid="tweet">
              <div data-testid="User-Name"><span>Person</span><span>@person</span></div>
              <a href="/person/status/1234"><time datetime="2026-10-08T12:00:00Z">Today</time></a>
              <div data-testid="tweetText">Original text</div>
              <div role="link"><a href="/quoted/status/123"><time>Yesterday</time></a>
                <div data-testid="tweetText">Quoted text</div></div>
              <button data-testid="like">1.2K</button>
              <a href="/person/status/1234/analytics">42</a>
            </article>''')
            snapshots = await XBrowser(page)._snapshots()
            tweet = tweet_from_snapshot(snapshots[0])
            assert tweet.tweet_id == "1234" and tweet.text_content == "Original text"
            assert tweet.like_count == 1200 and tweet.view_count == 42
        finally:
            await chromium.close()
