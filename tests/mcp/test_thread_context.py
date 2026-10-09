"""Thread context stays read-only and preserves media provenance over MCP."""
import json

import pytest
from mcp.types import CallToolResult, ImageContent

from xuse.models import MediaItem
from helpers import call_tool
from test_profile_read_tools import profile_server  # noqa: F401


@pytest.fixture
def thread_server(profile_server):
    env = profile_server
    browser = env.pool.browser

    async def get_thread(url, limit):
        browser.read("get_thread", url, limit)
        tweet = browser.post("ada")
        tweet.media = [MediaItem(type="image", url="https://pbs.twimg.com/media/chart.png", alt_text="Chart")]
        return {"focal_tweet_id": "123", "tweets": [tweet], "entries": [{"tweet_id": "123", "relation": "focal"}],
                "partial": True, "stop_reason": "no_growth", "observed_count": 1}

    browser.get_thread = get_thread
    return env


@pytest.mark.asyncio
async def test_registered_thread_preserves_context_without_raw_dom_or_llm(thread_server):
    env = thread_server
    result = await call_tool(env.server, "get_thread", {"tweet_url": "https://x.com/ada/status/123"})
    assert result["ok"] and result["account"] == "a"
    assert result["focal_tweet_id"] == "123" and result["partial"]
    assert result["tweets"][0]["media"][0]["alt_text"] == "Chart"
    assert "PRIVATE_RAW_DOM_SENTINEL" not in json.dumps(result)
    assert result["action_id"] and env.ctx.safety_store.status("a")["daily_used"] == {"read": 1}
    assert not env.llm_calls and len(env.ctx.draft_store) == 0
    assert env.pool.browser.calls == [("get_thread", "https://x.com/ada/status/123", 20)]


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [
    {"tweet_url": "https://attacker.invalid/ada/status/123"},
    {"tweet_url": "https://x.com/ada"},
    {"tweet_url": "https://x.com/ada/status/123", "limit": 0},
    {"tweet_url": "https://x.com/ada/status/123", "limit": 51},
    {"tweet_url": "https://x.com/ada/status/123", "account": "inactive"},
])
async def test_bad_context_input_does_not_acquire_browser(thread_server, params):
    result = await call_tool(thread_server.server, "get_thread", params)
    assert not result["ok"]
    assert thread_server.pool.started == 0


@pytest.mark.asyncio
async def test_thread_image_content_maps_to_exact_post(thread_server, monkeypatch):
    block = ImageContent(type="image", data="aGVsbG8=", mimeType="image/jpeg")
    monkeypatch.setattr("xuse.mcp.media.fetch_image", lambda url: block)
    raw = await thread_server.server.call_tool("get_thread", {
        "tweet_url": "https://x.com/ada/status/123", "include_images": True})
    assert isinstance(raw, CallToolResult)
    assert raw.content[1] == block
    assert raw.structuredContent["image_references"][0] == {
        "tweet_id": "123", "media_index": 0, "kind": "photo",
        "url": "https://pbs.twimg.com/media/chart.png", "alt_text": "Chart",
        "attached": True, "content_index": 1}
    assert raw.structuredContent["media_coverage"]["attached"] == 1
    assert json.loads(raw.content[0].text) == raw.structuredContent


@pytest.mark.asyncio
async def test_paused_thread_read_stops_before_browser(thread_server):
    thread_server.ctx.safety_store.pause("a", "manual_pause")
    result = await call_tool(thread_server.server, "get_thread", {"tweet_url": "https://x.com/ada/status/123"})
    assert not result["ok"] and thread_server.pool.started == 0
