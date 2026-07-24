"""prepare_reply: the agent-native reply path. Read-only — returns the
tweet's content and the account context so the calling agent can write the
reply itself. No server-side LLM, nothing posted, no drafts created.
"""
import pytest

from xuse.models import ScrapedTweet

from helpers import (  # noqa: F401 — imported fixtures register for this module
    accounts,
    assert_error_envelope,
    browser_factory,
    call_tool,
    config_loader,
    draft_store,
    drafts_path,
    mcp_server,
    mcp_settings,
    queue_store,
    session_pool,
)


@pytest.fixture
def stubbed_scrape(monkeypatch):
    async def fake_scrape(ctx, account_id, tweet_url, tweet_id):
        return ScrapedTweet(
            tweet_id=tweet_id,
            tweet_url=tweet_url,
            text_content="MCP servers are quietly becoming the agent runtime.",
            user_handle="someone",
        )

    monkeypatch.setattr("xuse.mcp.write_tools.scrape_single_tweet", fake_scrape)
    return fake_scrape


@pytest.mark.asyncio
async def test_prepare_reply_returns_context_without_llm(
    mcp_server, stubbed_scrape, browser_factory, draft_store
):
    result = await call_tool(
        mcp_server, "prepare_reply",
        {"account": "acc1", "tweet_url": "https://x.com/someone/status/12345"},
    )
    assert result["ok"] is True
    assert result["account"] == "acc1"
    assert result["tweet_id"] == "12345"
    assert result["author"] == "@someone"
    assert "MCP servers" in result["text_content"]
    assert result["account_keywords"] == ["ai"]  # from the acc1 fixture
    assert result["max_reply_chars"] == 270
    # Read-only: no browser started (scrape stubbed), no draft created.
    assert browser_factory.created == []
    assert len(draft_store) == 0


@pytest.mark.asyncio
async def test_prepare_reply_rejects_bad_url(mcp_server):
    result = await call_tool(mcp_server, "prepare_reply",
                             {"account": "acc1", "tweet_url": "https://x.com/someone"})
    error = assert_error_envelope(result, "Could not parse")
    assert error["type"] == "ToolError"


@pytest.mark.asyncio
async def test_prepare_reply_unknown_account(mcp_server):
    result = await call_tool(mcp_server, "prepare_reply",
                             {"account": "ghost", "tweet_url": "https://x.com/a/status/1"})
    error = assert_error_envelope(result, "ghost")
    assert error["type"] == "SessionError"
