"""queue_post dedup-key shape (text + media + community), process_queue
max_actions validation, and tool-level paused-account drain behavior."""
import hashlib

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


def expected_post_key(account_id, text, media, community):
    """The contract both queue_tools and actions must agree on, verbatim:
    post_<account>_<sha1((text or '') + '|' + str(media or []) + '|' + str(community))[:12]>."""
    material = (text or "") + "|" + str(media or []) + "|" + str(community)
    return f"post_{account_id}_{hashlib.sha1(material.encode('utf-8')).hexdigest()[:12]}"


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


# ---------------------------------------------------------------------------
# Post dedup key must include media and community
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_queue_post_dedup_key_format(queue_server, queue_store):
    await call_tool(queue_server, "queue_post",
                    {"account": "acc1", "text": "shipping v2", "media": ["chart.png"]})
    item = queue_store.list(status="pending")[0]
    assert item.dedup_key == expected_post_key("acc1", "shipping v2", ["chart.png"], None)


@pytest.mark.asyncio
async def test_queue_post_dedup_key_format_with_community(queue_server, queue_store):
    await call_tool(queue_server, "queue_post",
                    {"account": "acc1", "text": "shipping v2",
                     "media": ["chart.png"], "community": "1234567890"})
    item = queue_store.list(status="pending")[0]
    assert item.dedup_key == expected_post_key(
        "acc1", "shipping v2", ["chart.png"], "1234567890")


@pytest.mark.asyncio
async def test_same_text_different_media_is_not_a_duplicate(queue_server):
    first = await call_tool(queue_server, "queue_post",
                            {"account": "acc1", "text": "same text", "media": ["a.png"]})
    second = await call_tool(queue_server, "queue_post",
                             {"account": "acc1", "text": "same text", "media": ["b.png"]})
    assert first["ok"] is True and second["ok"] is True
    assert first["queue_id"] != second["queue_id"]


@pytest.mark.asyncio
async def test_same_text_with_community_is_not_a_duplicate(queue_server):
    first = await call_tool(queue_server, "queue_post",
                            {"account": "acc1", "text": "same text"})
    second = await call_tool(queue_server, "queue_post",
                             {"account": "acc1", "text": "same text",
                              "community": "1234567890"})
    assert first["ok"] is True and second["ok"] is True


@pytest.mark.asyncio
async def test_identical_text_media_community_is_a_duplicate(queue_server):
    args = {"account": "acc1", "text": "same text", "media": ["a.png"],
            "community": "1234567890"}
    first = await call_tool(queue_server, "queue_post", args)
    assert first["ok"] is True
    second = await call_tool(queue_server, "queue_post", args)
    assert_error_envelope(second, "Already queued")


# ---------------------------------------------------------------------------
# process_queue input validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_queue_rejects_max_actions_zero(queue_server):
    result = await call_tool(queue_server, "process_queue",
                             {"account": "acc1", "max_actions": 0})
    assert_error_envelope(result, "max_actions must be >= 1")


@pytest.mark.asyncio
async def test_process_queue_rejects_negative_max_actions(queue_server):
    result = await call_tool(queue_server, "process_queue",
                             {"account": "acc1", "max_actions": -3})
    assert_error_envelope(result, "max_actions must be >= 1")


# ---------------------------------------------------------------------------
# process_queue(account) on a paused account: frozen, not condemned
# ---------------------------------------------------------------------------


@pytest.fixture
def paused_drain_server(make_config_loader, session_pool, draft_store,
                        queue_store, fake_executor):
    loader = make_config_loader(
        settings={},
        accounts=[make_account("acc1"), make_account("acc2", is_active=False)],
    )

    def is_active(account_id):
        # Mirrors server.py's _is_active_account wiring.
        for raw in loader.get_accounts_config():
            if isinstance(raw, dict) and raw.get("account_id") == account_id:
                return bool(raw.get("is_active", True))
        return False

    runner = QueueRunner(queue_store,
                         QueueConfig(min_delay_seconds=0, max_delay_seconds=0),
                         fake_executor, sleep_fn=no_sleep,
                         jitter_fn=lambda a, b: 0.0, account_filter=is_active)
    pool = session_pool.__class__(loader, browser_factory=lambda d: None)
    server = create_server(config_loader=loader, session_pool=pool,
                           draft_store=draft_store, queue_store=queue_store,
                           queue_runner=runner)
    server.xuse_ctx.processed_keys = set()
    return server


@pytest.mark.asyncio
async def test_process_queue_on_paused_account_defers_without_executing(
        paused_drain_server, queue_store, fake_executor):
    # Enqueue is refused for paused accounts, so seed the store directly.
    item = queue_store.add(account="acc2", action="post",
                           payload={"text": "weekend post"},
                           dedup_key="post_acc2_seed")
    result = await call_tool(paused_drain_server, "process_queue",
                             {"account": "acc2", "max_actions": 5})
    assert result["ok"] is True
    assert result["succeeded"] == 0 and result["failed"] == 0
    assert result["skipped_inactive"] == 1
    assert fake_executor.calls == []
    updated = queue_store.get(item.queue_id)
    assert updated.status == "pending" and updated.attempts == 0
