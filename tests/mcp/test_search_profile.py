"""search_profile: handle parsing, canonical URL construction, and the
read-only envelope. The scraper is faked — no browser, no network."""
import pytest

import xuse.mcp.tools as mcp_tools
from xuse.mcp.executor import ToolError, profile_handle_from

from helpers import assert_error_envelope, call_tool  # noqa: F401
from helpers import accounts, browser_factory, config_loader, draft_store  # noqa: F401
from helpers import drafts_path, mcp_server, mcp_settings, queue_store, session_pool  # noqa: F401


class FakeTweet:
    def __init__(self, tweet_id="1", handle="@nasa", text="hello"):
        self.tweet_id = tweet_id
        self.user_handle = handle
        self.text_content = text
        self.media = []

    def model_dump(self, mode="json"):
        return {"tweet_id": self.tweet_id, "user_handle": self.user_handle,
                "text_content": self.text_content, "media": []}


@pytest.fixture
def scraped(monkeypatch):
    """Patch TweetScraper; record the URL search_profile navigated to."""
    calls = {}

    class FakeScraper:
        def __init__(self, browser_manager, account_id=None):
            calls["account_id"] = account_id

        def scrape_tweets_from_profile(self, profile_url, max_tweets=None):
            calls["profile_url"] = profile_url
            calls["max_tweets"] = max_tweets
            return [FakeTweet("1"), FakeTweet("2")]

    monkeypatch.setattr(mcp_tools, "TweetScraper", FakeScraper)
    return calls


# ---------------------------------------------------------------------------
# Handle parsing (pure)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [
    "nasa", "@nasa", " @nasa ",
    "https://x.com/nasa", "http://x.com/nasa", "x.com/nasa",
    "https://twitter.com/nasa", "https://www.x.com/nasa/",
    "https://mobile.twitter.com/nasa/with_replies",
    "https://x.com/nasa/status/1234567890",  # tweet URL -> its author
])
def test_accepts_every_documented_profile_form(value):
    assert profile_handle_from(value) == "nasa"


@pytest.mark.parametrize("value,fragment", [
    ("", "must not be empty"),
    ("   ", "must not be empty"),
    ("has spaces", "Invalid X handle"),
    ("waaaaaaaaaaaaaaaytoolong", "Invalid X handle"),
    ("na.sa", "Invalid X handle"),
    ("https://evil.com/nasa", "Not an X profile URL"),
    ("https://x.com.evil.com/nasa", "Not an X profile URL"),
    ("//evil.com/nasa", "Not an X profile URL"),
    ("ftp://x.com/nasa", "Unsupported URL scheme"),
    ("https://x.com/", "No handle in profile URL"),
])
def test_rejects_non_profiles(value, fragment):
    with pytest.raises(ToolError) as excinfo:
        profile_handle_from(value)
    assert fragment in str(excinfo.value)


@pytest.mark.parametrize("route", ["home", "search", "i", "messages", "notifications"])
def test_rejects_app_routes_that_look_like_handles(route):
    """https://x.com/i/status/... and friends must not be scraped as timelines."""
    with pytest.raises(ToolError) as excinfo:
        profile_handle_from(f"https://x.com/{route}/status/1234567890")
    assert "app route" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Tool contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_returns_posts_and_canonicalizes_the_url(mcp_server, scraped):
    result = await call_tool(mcp_server, "search_profile",
                             {"profile": "@nasa", "account": "acc1"})
    assert result["ok"] is True
    assert result["profile"] == "@nasa"
    assert result["profile_url"] == "https://x.com/nasa"
    assert result["count"] == 2
    assert scraped["profile_url"] == "https://x.com/nasa"


@pytest.mark.asyncio
@pytest.mark.parametrize("hostile", [
    "https://x.com/nasa/../../evil",
    "https://x.com/nasa?redirect=https://evil.com",
    "https://x.com/nasa#/../evil",
    "https://x.com/nasa/status/1/photo/1",
])
async def test_hostile_url_never_reaches_the_browser(mcp_server, scraped, hostile):
    """The scraper is handed a URL rebuilt from the handle, never the caller's
    string, so traversal segments, query params, and fragments are dropped
    rather than navigated."""
    result = await call_tool(mcp_server, "search_profile",
                             {"profile": hostile, "account": "acc1"})
    assert result["ok"] is True
    assert scraped["profile_url"] == "https://x.com/nasa"


@pytest.mark.asyncio
async def test_foreign_host_errors_before_a_session_is_acquired(mcp_server, browser_factory,
                                                                scraped):
    result = await call_tool(mcp_server, "search_profile",
                             {"profile": "https://evil.com/nasa", "account": "acc1"})
    assert_error_envelope(result, "Not an X profile URL")
    assert browser_factory.created == []


@pytest.mark.asyncio
async def test_limit_is_clamped_to_the_search_ceiling(mcp_server, scraped):
    await call_tool(mcp_server, "search_profile",
                    {"profile": "nasa", "limit": 500, "account": "acc1"})
    assert scraped["max_tweets"] == 50
    await call_tool(mcp_server, "search_profile",
                    {"profile": "nasa", "limit": 0, "account": "acc1"})
    assert scraped["max_tweets"] == 1


@pytest.mark.asyncio
async def test_account_defaults_to_the_first_active_account(mcp_server, scraped):
    result = await call_tool(mcp_server, "search_profile", {"profile": "nasa"})
    assert result["account"] == "acc1"


@pytest.mark.asyncio
async def test_unknown_account_returns_error_envelope(mcp_server, scraped):
    result = await call_tool(mcp_server, "search_profile",
                             {"profile": "nasa", "account": "ghost"})
    error = assert_error_envelope(result, "ghost")
    assert error["type"] == "SessionError"


@pytest.mark.asyncio
async def test_bad_handle_errors_before_a_session_is_acquired(mcp_server, session_pool,
                                                              browser_factory, scraped):
    """Validation is cheap and comes first — a typo must not cost a cold start."""
    result = await call_tool(mcp_server, "search_profile",
                             {"profile": "not a handle", "account": "acc1"})
    assert_error_envelope(result, "Invalid X handle")
    assert browser_factory.created == []
