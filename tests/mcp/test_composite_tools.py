"""Composite tools: search -> heuristic relevance -> LLM text -> staged drafts.
Scraper and LLM are faked; drafts land in the tmp DraftStore. No browser."""
from types import SimpleNamespace

import pytest

import xuse.mcp.actions as actions_mod
import xuse.mcp.composite_tools as composite_mod

from helpers import assert_error_envelope, call_tool  # noqa: F401
from helpers import accounts, browser_factory, config_loader, draft_store  # noqa: F401
from helpers import drafts_path, make_account, mcp_server, mcp_settings  # noqa: F401
from helpers import queue_store, session_pool  # noqa: F401


class FakeTweet:
    def __init__(self, tweet_id, text, likes=0, handle="@x"):
        self.tweet_id = tweet_id
        self.text_content = text
        self.like_count = likes
        self.user_handle = handle
        self.tweet_url = f"https://x.com/x/status/{tweet_id}"
        self.media = []


class FakeScraper:
    def __init__(self, browser_manager, account_id): pass

    def scrape_tweets_by_keyword(self, keywords, limit):
        return [
            FakeTweet("1", "ai agents are changing mcp tooling", likes=50),
            FakeTweet("2", "unrelated lunch photo", likes=999),
            FakeTweet("1", "ai agents are changing mcp tooling", likes=50),  # dup id
        ]


class FakeLLM:
    client = object()  # require_llm checks this is not None

    async def generate_text(self, **kwargs):
        return "generated reply text"


@pytest.fixture
def patched(mcp_server, monkeypatch):
    monkeypatch.setattr(composite_mod, "TweetScraper", FakeScraper)
    mcp_server.xuse_ctx.llm_service = FakeLLM()
    async def fake_reply(ctx, account_id, tweet):
        return f"reply to {tweet.tweet_id}"
    async def fake_post(ctx, account_id, topic):
        return f"post about {topic}"
    monkeypatch.setattr(actions_mod, "generate_reply_text", fake_reply)
    monkeypatch.setattr(actions_mod, "generate_post_text", fake_post)
    return mcp_server


@pytest.mark.asyncio
async def test_research_and_stage_stages_relevant_drafts(patched):
    result = await call_tool(patched, "research_and_stage",
                             {"account": "acc1", "keywords": ["ai agents"]})
    assert result["ok"] is True
    assert result["considered"] == 2          # dup tweet id deduped
    assert result["skipped_irrelevant"] == 1  # lunch tweet filtered by heuristic
    assert len(result["drafts"]) == 1
    assert result["drafts"][0]["text"] == "reply to 1"
    listed = await call_tool(patched, "list_drafts", {"account": "acc1"})
    assert listed["total"] == 1 and listed["drafts"][0]["status"] == "pending"


@pytest.mark.asyncio
async def test_research_and_stage_requires_llm(mcp_server, monkeypatch):
    monkeypatch.setattr(composite_mod, "TweetScraper", FakeScraper)
    result = await call_tool(mcp_server, "research_and_stage",
                             {"account": "acc1", "keywords": ["ai"]})
    assert_error_envelope(result, "No LLM is configured")


@pytest.mark.asyncio
async def test_research_and_stage_refuses_paused_account(make_config_loader, drafts_path,
                                                         queue_store, monkeypatch):
    from xuse.mcp.drafts import DraftStore
    from xuse.mcp.server import create_server
    from xuse.mcp.sessions import SessionPool
    monkeypatch.setattr(composite_mod, "TweetScraper", FakeScraper)
    loader = make_config_loader(settings={}, accounts=[make_account("p", is_active=False)])
    server = create_server(config_loader=loader,
                           session_pool=SessionPool(loader, browser_factory=lambda d: None),
                           draft_store=DraftStore(drafts_path), queue_store=queue_store)
    server.xuse_ctx.llm_service = FakeLLM()
    result = await call_tool(server, "research_and_stage",
                             {"account": "p", "keywords": ["ai"]})
    assert_error_envelope(result, "paused")


@pytest.mark.asyncio
async def test_draft_post_variations_stages_n_drafts(patched):
    result = await call_tool(patched, "draft_post_variations",
                             {"account": "acc1", "topic": "mcp servers", "count": 3})
    assert result["ok"] is True and len(result["drafts"]) == 3
    assert result["drafts"][0]["text"] == "post about mcp servers"
    assert "take #2" in result["drafts"][1]["text"]


@pytest.mark.asyncio
async def test_draft_post_variations_caps_count(patched):
    result = await call_tool(patched, "draft_post_variations",
                             {"account": "acc1", "topic": "x", "count": 99})
    assert len(result["drafts"]) == composite_mod.MAX_VARIATIONS


@pytest.mark.asyncio
async def test_persona_enters_reply_prompt(mcp_server):
    """Real actions.generate_reply_text with a fake LLM: persona is prepended."""
    import xuse.mcp.executor as ex_mod
    seen = {}

    class RecordingLLM(FakeLLM):
        async def generate_text(self, **kwargs):
            seen.update(kwargs)
            return "ok text"

    ctx = mcp_server.xuse_ctx
    ctx.llm_service = RecordingLLM()
    await call_tool(mcp_server, "update_account",
                    {"account": "acc1", "persona": "PERSONA-MARKER voice"})
    tweet = SimpleNamespace(user_handle="@jane", text_content="hello")
    text = await actions_mod.generate_reply_text(ctx, "acc1", tweet)
    assert text == "ok text"
    assert "PERSONA-MARKER voice" in seen["prompt"]
