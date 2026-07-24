"""Queue tool contract tests: enqueue validation, dedup, cancel, process."""
import pytest

from xuse.mcp.server import create_server
from xuse.queue import QueueConfig, QueueRunner, QueueStore

from helpers import (  # noqa: F401 — imported fixtures register for this module
    accounts,
    assert_error_envelope,
    browser_factory,
    call_tool,
    config_loader,
    draft_store,
    drafts_path,
    make_account,
    mcp_settings,
    session_pool,
)


class FakeExecutor:
    def __init__(self):
        self.calls = []

    async def __call__(self, item):
        self.calls.append((item.account, item.action, item.payload))
        return {"success": True}


async def no_sleep(_seconds):
    return None


@pytest.fixture
def queue_store(tmp_path):
    return QueueStore(tmp_path / "queue.jsonl")


@pytest.fixture
def fake_executor():
    return FakeExecutor()


@pytest.fixture
def queue_runner(queue_store, fake_executor):
    return QueueRunner(queue_store, QueueConfig(min_delay_seconds=0, max_delay_seconds=0),
                       fake_executor, sleep_fn=no_sleep, jitter_fn=lambda a, b: 0.0)


@pytest.fixture
def queue_server(config_loader, session_pool, draft_store, queue_store, queue_runner):
    server = create_server(config_loader=config_loader, session_pool=session_pool,
                           draft_store=draft_store, queue_store=queue_store,
                           queue_runner=queue_runner)
    server.xuse_ctx.processed_keys = set()  # hermetic dedup store
    return server


@pytest.mark.asyncio
async def test_queue_post_stores_full_payload(queue_server, queue_store):
    result = await call_tool(queue_server, "queue_post",
                             {"account": "acc1", "text": "scheduled hello"})
    assert result["ok"] is True and result["queued"] is True
    assert result["payload"]["text"] == "scheduled hello"
    items = queue_store.list(account="acc1", status="pending")
    assert len(items) == 1 and items[0].action == "post"
    assert "process_queue" in result["message"]


@pytest.mark.asyncio
async def test_queue_post_requires_exactly_one_of_text_or_topic(queue_server):
    result = await call_tool(queue_server, "queue_post",
                             {"account": "acc1", "text": "x", "topic": "y"})
    assert_error_envelope(result, "exactly one")


@pytest.mark.asyncio
async def test_queue_post_rejects_bad_not_before(queue_server):
    result = await call_tool(queue_server, "queue_post",
                             {"account": "acc1", "text": "x", "not_before": "next tuesday"})
    assert_error_envelope(result, "ISO 8601")


@pytest.mark.asyncio
async def test_queue_post_duplicate_is_rejected(queue_server):
    args = {"account": "acc1", "text": "same text"}
    first = await call_tool(queue_server, "queue_post", args)
    assert first["ok"] is True
    second = await call_tool(queue_server, "queue_post", args)
    assert_error_envelope(second, "Already queued")


@pytest.mark.asyncio
async def test_queue_engagement_like_stores_tweet_fields(queue_server, queue_store):
    url = "https://x.com/someone/status/1234567890"
    result = await call_tool(queue_server, "queue_engagement",
                             {"account": "acc1", "action": "like", "tweet_url": url})
    assert result["ok"] is True
    item = queue_store.list(status="pending")[0]
    assert item.action == "like" and item.payload["tweet_id"] == "1234567890"
    assert item.dedup_key == "like_acc1_1234567890"


@pytest.mark.asyncio
async def test_queue_engagement_rejects_unknown_action(queue_server):
    result = await call_tool(queue_server, "queue_engagement",
                             {"account": "acc1", "action": "follow",
                              "tweet_url": "https://x.com/a/status/1"})
    assert_error_envelope(result, "Unsupported queue action")


@pytest.mark.asyncio
async def test_queue_engagement_reply_requires_text(queue_server):
    result = await call_tool(queue_server, "queue_engagement",
                             {"account": "acc1", "action": "reply",
                              "tweet_url": "https://x.com/a/status/1"})
    assert_error_envelope(result, "requires `text`")


@pytest.mark.asyncio
async def test_queue_reply_auto_generates_text_now(queue_server, queue_store, monkeypatch):
    from xuse.models import ScrapedTweet

    async def fake_scrape(ctx, account_id, tweet_url, tweet_id):
        return ScrapedTweet(tweet_id=tweet_id, tweet_url=tweet_url,
                            text_content="original text")

    async def fake_generate(ctx, account_id, original):
        return "generated reply"

    monkeypatch.setattr("xuse.mcp.queue_tools.scrape_single_tweet", fake_scrape)
    monkeypatch.setattr("xuse.mcp.actions.generate_reply_text", fake_generate)
    result = await call_tool(queue_server, "queue_engagement",
                             {"account": "acc1", "action": "reply",
                              "tweet_url": "https://x.com/a/status/42", "text": "auto"})
    assert result["ok"] is True
    item = queue_store.list(status="pending")[0]
    assert item.payload["text"] == "generated reply"
    assert item.payload["text_content"] == "original text"


@pytest.mark.asyncio
async def test_list_queue_returns_items_and_counts(queue_server):
    await call_tool(queue_server, "queue_post", {"account": "acc1", "text": "one"})
    await call_tool(queue_server, "queue_post", {"account": "acc2", "text": "two"})
    result = await call_tool(queue_server, "list_queue", {})
    assert result["count"] == 2 and result["counts"] == {"pending": 2}
    filtered = await call_tool(queue_server, "list_queue", {"account": "acc1"})
    assert filtered["count"] == 1 and filtered["items"][0]["account"] == "acc1"


@pytest.mark.asyncio
async def test_cancel_queued_action_transitions(queue_server):
    created = await call_tool(queue_server, "queue_post", {"account": "acc1", "text": "bye"})
    queue_id = created["queue_id"]
    cancelled = await call_tool(queue_server, "cancel_queued_action", {"queue_id": queue_id})
    assert cancelled["ok"] is True and cancelled["status"] == "cancelled"
    again = await call_tool(queue_server, "cancel_queued_action", {"queue_id": queue_id})
    assert_error_envelope(again, "cannot cancel")


@pytest.mark.asyncio
async def test_cancel_unknown_queue_id(queue_server):
    result = await call_tool(queue_server, "cancel_queued_action", {"queue_id": "nope"})
    assert_error_envelope(result, "Unknown queue_id")


@pytest.mark.asyncio
async def test_process_queue_drains_via_injected_runner(queue_server, queue_store, fake_executor):
    await call_tool(queue_server, "queue_post", {"account": "acc1", "text": "go"})
    result = await call_tool(queue_server, "process_queue", {"account": "acc1", "max_actions": 5})
    assert result["ok"] is True and result["succeeded"] == 1
    assert fake_executor.calls[0][1] == "post"
    assert queue_store.list(status="done")


@pytest.mark.asyncio
async def test_process_queue_unknown_account_envelope(queue_server):
    result = await call_tool(queue_server, "process_queue", {"account": "ghost"})
    assert_error_envelope(result, "ghost")


@pytest.fixture
def paused_server(make_config_loader, session_pool, draft_store, queue_store, queue_runner):
    loader = make_config_loader(
        settings={},
        accounts=[make_account("acc1"), make_account("acc2", is_active=False)],
    )
    pool = session_pool.__class__(loader, browser_factory=lambda d: None)
    server = create_server(config_loader=loader, session_pool=pool,
                           draft_store=draft_store, queue_store=queue_store,
                           queue_runner=queue_runner)
    server.xuse_ctx.processed_keys = set()
    return server


@pytest.mark.asyncio
async def test_queue_post_rejects_paused_account(paused_server):
    result = await call_tool(paused_server, "queue_post", {"account": "acc2", "text": "hi"})
    assert_error_envelope(result, "paused")


@pytest.mark.asyncio
async def test_queue_engagement_rejects_paused_account(paused_server):
    result = await call_tool(paused_server, "queue_engagement",
                             {"account": "acc2", "action": "like",
                              "tweet_url": "https://x.com/a/status/1"})
    assert_error_envelope(result, "paused")
