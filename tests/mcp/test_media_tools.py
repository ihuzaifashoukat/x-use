"""Media on read tools: envelopes carry typed media + persona; images attach
as CallToolResult content blocks; include_images=False keeps plain dicts.
The scraper and image fetch are faked — no browser, no network."""
import json

import pytest

from mcp.types import CallToolResult, ImageContent

import xuse.mcp.tools as mcp_tools
import xuse.mcp.write_tools as write_tools_mod
from xuse.mcp import media as media_mod

from helpers import call_tool  # noqa: F401
from helpers import accounts, browser_factory, config_loader, draft_store  # noqa: F401
from helpers import drafts_path, mcp_server, mcp_settings, queue_store, session_pool  # noqa: F401

TWEET_URL = "https://x.com/janedoe/status/1234567890"


async def tool_content(server, name, args):
    """Return the content blocks of a tool call, tolerating both FastMCP
    return shapes: a (content, structured) tuple for plain dict results and
    a bare CallToolResult when the tool attached image content."""
    raw = await server.call_tool(name, args)
    return raw.content if isinstance(raw, CallToolResult) else raw[0]


async def tool_result(server, name, args):
    """Like helpers.call_tool (parses content[0] JSON) but image-safe: works
    when the tool returned a bare CallToolResult with image blocks."""
    content = await tool_content(server, name, args)
    return json.loads(content[0].text)


class FakeMediaItem:
    def __init__(self, type, url, alt_text=None):
        self.type, self.url, self.alt_text = type, url, alt_text


class FakeTweet:
    tweet_id = "1234567890"
    user_handle = "@janedoe"
    text_content = "look at this chart"
    like_count, retweet_count, reply_count, view_count = 42, 3, 5, 9000
    tweet_url = TWEET_URL
    media = [FakeMediaItem("image", "https://pbs.twimg.com/media/a.jpg", "a chart"),
             FakeMediaItem("video", "https://pbs.twimg.com/media/v.jpg")]

    def model_dump(self, mode="json"):
        # Mimic ScrapedTweet (pydantic) so dump_tweet can serialize the fake.
        return {
            "tweet_id": self.tweet_id,
            "user_handle": self.user_handle,
            "text_content": self.text_content,
            "like_count": self.like_count,
            "retweet_count": self.retweet_count,
            "reply_count": self.reply_count,
            "view_count": self.view_count,
            "tweet_url": self.tweet_url,
            "media": [{"type": m.type, "url": m.url, "alt_text": m.alt_text}
                      for m in self.media],
        }


FAKE_BLOCK = ImageContent(type="image", data="aGVsbG8=", mimeType="image/jpeg")


@pytest.fixture
def patched(monkeypatch):
    async def fake_scrape(ctx, account_id, tweet_url, tweet_id):
        return FakeTweet()

    monkeypatch.setattr(mcp_tools, "scrape_single_tweet", fake_scrape)
    monkeypatch.setattr(write_tools_mod, "scrape_single_tweet", fake_scrape)
    # tools.py/write_tools.py bind images_for_tweet by direct import — patch
    # every namespace that holds a reference (same pattern as scrape_single_tweet).
    fake_images = lambda tweet, limit=4: [FAKE_BLOCK]  # noqa: E731
    monkeypatch.setattr(media_mod, "images_for_tweet", fake_images)
    monkeypatch.setattr(mcp_tools, "images_for_tweet", fake_images)
    monkeypatch.setattr(write_tools_mod, "images_for_tweet", fake_images)
    return True


@pytest.mark.asyncio
async def test_get_tweet_envelope_and_images(mcp_server, patched):
    result = await tool_result(mcp_server, "get_tweet",
                               {"account": "acc1", "tweet_url": TWEET_URL})
    assert result["ok"] is True
    assert result["author"] == "@janedoe"  # no @@
    assert result["like_count"] == 42
    assert result["media"] == [
        {"type": "image", "url": "https://pbs.twimg.com/media/a.jpg", "alt_text": "a chart"},
        {"type": "video", "url": "https://pbs.twimg.com/media/v.jpg", "alt_text": None},
    ]


@pytest.mark.asyncio
async def test_get_tweet_attaches_image_content(mcp_server, patched):
    content = await tool_content(
        mcp_server, "get_tweet", {"account": "acc1", "tweet_url": TWEET_URL})
    assert any(isinstance(c, ImageContent) for c in content[1:])


@pytest.mark.asyncio
async def test_get_tweet_without_images_returns_media_urls_only(mcp_server, patched):
    content = await tool_content(
        mcp_server, "get_tweet",
        {"account": "acc1", "tweet_url": TWEET_URL, "include_images": False})
    assert len(content) == 1  # JSON text only, no image blocks


@pytest.mark.asyncio
async def test_prepare_reply_gains_media_and_persona(mcp_server, patched):
    await call_tool(mcp_server, "update_account",
                    {"account": "acc1", "persona": "casual builder voice"})
    result = await tool_result(mcp_server, "prepare_reply",
                               {"account": "acc1", "tweet_url": TWEET_URL})
    assert result["persona"] == "casual builder voice"
    assert result["media"][0]["type"] == "image"
    assert result["author"] == "@janedoe"


@pytest.mark.asyncio
async def test_search_tweets_include_images_opt_in(mcp_server, patched, monkeypatch):
    class FakeScraper:
        def __init__(self, browser_manager, account_id): pass
        def scrape_tweets_by_keyword(self, keywords, limit):
            return [FakeTweet()]

    monkeypatch.setattr(mcp_tools, "TweetScraper", FakeScraper)
    plain = await call_tool(mcp_server, "search_tweets", {"keywords": "ai", "account": "acc1"})
    assert plain["tweets"][0]["media"][0]["url"] == "https://pbs.twimg.com/media/a.jpg"

    content = await tool_content(
        mcp_server, "search_tweets",
        {"keywords": "ai", "account": "acc1", "include_images": True})
    assert any(isinstance(c, ImageContent) for c in content[1:])
