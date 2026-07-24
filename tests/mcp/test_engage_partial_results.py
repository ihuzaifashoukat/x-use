"""engage: an unexpected executor exception on one action must not hide the
actions that already executed.

The execution loop catches ToolError/SessionError per action, but a broad
unexpected failure (a Selenium crash inside the executor) used to propagate
out of the loop — the tool returned an error envelope and the likes that had
ALREADY succeeded were invisible to the caller. Every action now gets a
per-action result, including unexpected failures.
"""

import pytest

import xuse.mcp.engage as engage_mod
from xuse.mcp.server import create_server

from helpers import (  # noqa: F401 — imported fixtures register for this module
    accounts,
    browser_factory,
    call_tool,
    config_loader,
    draft_store,
    drafts_path,
    make_account,
    mcp_settings,
    queue_store,
    session_pool,
)


class FakeTweet:
    def __init__(self, tweet_id):
        self.tweet_id = tweet_id
        self.text_content = f"text {tweet_id}"
        self.user_handle = "@someone"
        self.tweet_url = f"https://x.com/someone/status/{tweet_id}"
        self.media = []


class FakeScraper:
    def __init__(self, browser_manager, account_id):
        pass

    def scrape_tweets_by_keyword(self, keyword, limit):
        return [FakeTweet("111"), FakeTweet("222")]


class FakeAnalyzer:
    def __init__(self, *args, **kwargs):
        pass

    async def score_relevance(self, tweet, keywords=None):
        return 1.0


@pytest.fixture
def engage_server(config_loader, session_pool, draft_store, queue_store, monkeypatch):
    server = create_server(
        config_loader=config_loader,
        session_pool=session_pool,
        draft_store=draft_store,
        queue_store=queue_store,
        draft_mode=False,
    )
    server.xuse_ctx.processed_keys = set()  # hermetic dedup store
    monkeypatch.setattr(engage_mod, "TweetScraper", FakeScraper)
    monkeypatch.setattr(engage_mod, "TweetAnalyzer", FakeAnalyzer)
    return server


@pytest.mark.asyncio
async def test_unexpected_exception_records_failure_and_keeps_prior_results(engage_server, monkeypatch):
    calls = []

    async def fake_exec_like(ctx, account_id, tweet_id, url):
        calls.append(tweet_id)
        if tweet_id == "222":
            raise RuntimeError("selenium exploded")
        return {"account": account_id, "action": "like", "tweet_id": tweet_id, "success": True}

    monkeypatch.setattr(engage_mod.action_executors, "exec_like", fake_exec_like)

    result = await call_tool(
        engage_server,
        "engage",
        {"account": "acc1", "keywords": ["ai"], "actions": ["like"], "max_actions": 5},
    )

    assert result["ok"] is True
    assert calls == ["111", "222"]  # the loop continued past the good action
    executed = result["executed"]
    assert len(executed) == 2
    assert executed[0]["success"] is True
    assert executed[0]["tweet_id"] == "111"
    assert executed[1]["success"] is False
    assert executed[1]["tweet_id"] == "222"
    assert "selenium exploded" in executed[1]["error"]
    assert result["succeeded"] == 1
    assert result["attempted"] == 2


@pytest.mark.asyncio
async def test_tool_error_still_recorded_as_structured_failure(engage_server, monkeypatch):
    from xuse.mcp.executor import ToolError

    async def fake_exec_like(ctx, account_id, tweet_id, url):
        if tweet_id == "111":
            raise ToolError("account is paused")
        return {"account": account_id, "action": "like", "tweet_id": tweet_id, "success": True}

    monkeypatch.setattr(engage_mod.action_executors, "exec_like", fake_exec_like)

    result = await call_tool(
        engage_server,
        "engage",
        {"account": "acc1", "keywords": ["ai"], "actions": ["like"], "max_actions": 5},
    )

    assert result["ok"] is True
    executed = result["executed"]
    assert len(executed) == 2
    assert executed[0]["success"] is False
    assert "paused" in executed[0]["error"]
    assert executed[1]["success"] is True
    assert result["succeeded"] == 1
