"""Pacing (NFR-4) hardening tests.

Two pinned behaviors:
1. Pacing spaces *attempts*, not just successes — a failed write still drove
   the real browser against X, so the attempt timestamp must be marked
   BEFORE the write executes; retries after a failure wait out the delay.
2. The pace check-and-set is atomic per account (a per-account asyncio.Lock
   on the Ctx) — two concurrent same-account writes cannot both pace against
   the same stale timestamp and fire back-to-back.

All tests run against injected fakes — no browser, no network.
"""
import asyncio
import time
from typing import List
from unittest.mock import MagicMock

import pytest

import xuse.mcp.actions as actions
import xuse.mcp.executor as ex
from xuse.mcp.drafts import DraftStore
from xuse.mcp.executor import Ctx, ToolError
from xuse.mcp.sessions import SessionPool
from xuse.models import ActionConfig

from helpers import (  # noqa: F401 — imported fixtures register for this module
    FakeBrowserFactory,
    FakeMetrics,
    make_account,
)

MIN_DELAY_SECONDS = 1
# Tolerance below the full delay: scheduling jitter must never eat ~15%.
PACE_TOLERANCE = 0.85


def make_recording_publisher(instances: list, succeed: bool) -> type:
    """TweetPublisher fake that records the entry time of every write call."""

    class RecordingPublisher:
        def __init__(self, browser_manager, llm_service, account_config) -> None:
            self.calls: List[dict] = []
            instances.append(self)

        async def post_new_tweet(self, content, llm_settings=None) -> bool:
            self.calls.append({"text": content.text, "t": time.monotonic()})
            return succeed

        async def reply_to_tweet(self, tweet, text) -> bool:
            self.calls.append({"text": text, "t": time.monotonic()})
            return succeed

        async def retweet_tweet(self, tweet) -> bool:
            self.calls.append({"text": "retweet", "t": time.monotonic()})
            return succeed

    return RecordingPublisher


def make_recording_engagement(instances: list, succeed: bool) -> type:
    """TweetEngagement fake that records the entry time of like calls."""

    class RecordingEngagement:
        def __init__(self, browser_manager, account_config) -> None:
            self.calls: List[dict] = []
            instances.append(self)

        async def like_tweet(self, tweet_id=None, tweet_url=None) -> bool:
            self.calls.append({"tweet_id": tweet_id, "t": time.monotonic()})
            return succeed

    return RecordingEngagement


def all_call_times(instances: list) -> List[float]:
    return sorted(call["t"] for publisher in instances for call in publisher.calls)


@pytest.fixture
def publisher_instances() -> list:
    return []


@pytest.fixture
def engagement_instances() -> list:
    return []


@pytest.fixture
def paced_ctx(make_config_loader, tmp_path):
    """Ctx whose acc1 enforces a 1s minimum spacing between write actions."""
    loader = make_config_loader(accounts=[
        make_account("acc1", action_config={
            "min_delay_between_actions_seconds": MIN_DELAY_SECONDS,
            "max_delay_between_actions_seconds": 0,
        }),
        make_account("acc2"),
    ])
    pool = SessionPool(loader, browser_factory=FakeBrowserFactory())
    ctx = Ctx(config_loader=loader, session_pool=pool,
              draft_store=DraftStore(tmp_path / "drafts.jsonl"))
    ctx.processed_keys = set()
    ctx.file_handler = MagicMock(name="file_handler")
    ctx.llm_service = MagicMock(name="llm_service")
    ctx.metrics_factory = lambda account_id: FakeMetrics(account_id)
    return ctx


# ---------------------------------------------------------------------------
# 1. Pacing marks attempts, not just successes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_write_marks_the_attempt_timestamp(paced_ctx, publisher_instances, monkeypatch):
    """A browser-level failure must still stamp last_action_at — before the
    fix, two failing attempts left the dict empty and retries fired with
    zero pacing delay."""
    monkeypatch.setattr("xuse.mcp.actions.TweetPublisher",
                        make_recording_publisher(publisher_instances, succeed=False))

    with pytest.raises(ToolError):
        await actions.exec_post(paced_ctx, "acc1", text="attempt one")

    assert "acc1" in paced_ctx.last_action_at
    await paced_ctx.session_pool.close_all()


@pytest.mark.asyncio
async def test_two_failing_writes_are_paced_apart(paced_ctx, publisher_instances, monkeypatch):
    monkeypatch.setattr("xuse.mcp.actions.TweetPublisher",
                        make_recording_publisher(publisher_instances, succeed=False))

    with pytest.raises(ToolError):
        await actions.exec_post(paced_ctx, "acc1", text="failing write one")
    with pytest.raises(ToolError):
        await actions.exec_post(paced_ctx, "acc1", text="failing write two")

    times = all_call_times(publisher_instances)
    assert len(times) == 2
    assert times[1] - times[0] >= MIN_DELAY_SECONDS * PACE_TOLERANCE
    await paced_ctx.session_pool.close_all()


@pytest.mark.asyncio
async def test_failed_attempts_mark_pacing_timestamp_on_every_exec_path(
        paced_ctx, publisher_instances, engagement_instances, monkeypatch):
    """All four exec_* paths mark the attempt before the write executes."""
    monkeypatch.setattr("xuse.mcp.actions.TweetPublisher",
                        make_recording_publisher(publisher_instances, succeed=False))
    monkeypatch.setattr("xuse.mcp.actions.TweetEngagement",
                        make_recording_engagement(engagement_instances, succeed=False))

    with pytest.raises(ToolError):
        await actions.exec_reply(paced_ctx, "acc1", "https://x.com/u/status/111", "nice")
    assert "acc1" in paced_ctx.last_action_at

    paced_ctx.last_action_at.clear()
    with pytest.raises(ToolError):
        await actions.exec_like(paced_ctx, "acc1", "222", "https://x.com/u/status/222")
    assert "acc1" in paced_ctx.last_action_at

    paced_ctx.last_action_at.clear()
    with pytest.raises(ToolError):
        await actions.exec_retweet(paced_ctx, "acc1", "333", "https://x.com/u/status/333")
    assert "acc1" in paced_ctx.last_action_at

    await paced_ctx.session_pool.close_all()


# ---------------------------------------------------------------------------
# 2. Pacing is atomic per account under concurrency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_same_account_writes_second_waits_min_delay(
        paced_ctx, publisher_instances, monkeypatch):
    """Two concurrent writes for one account: the second must wait out the
    full min_delay measured from the first write's attempt — before the fix
    both paced against the same stale timestamp and fired together."""
    monkeypatch.setattr("xuse.mcp.actions.TweetPublisher",
                        make_recording_publisher(publisher_instances, succeed=True))

    await asyncio.gather(
        actions.exec_post(paced_ctx, "acc1", text="concurrent write A"),
        actions.exec_post(paced_ctx, "acc1", text="concurrent write B"),
    )

    times = all_call_times(publisher_instances)
    assert len(times) == 2
    assert times[1] - times[0] >= MIN_DELAY_SECONDS * PACE_TOLERANCE
    await paced_ctx.session_pool.close_all()


@pytest.mark.asyncio
async def test_pace_serializes_concurrent_calls(paced_ctx):
    """Two concurrent pace() calls against a just-marked account must not
    sleep the same delay in parallel and then fire back-to-back."""
    action_config = ActionConfig(min_delay_between_actions_seconds=MIN_DELAY_SECONDS)
    await ex.mark_action_now(paced_ctx, "acc1")

    finished: List[float] = []

    async def run() -> None:
        await ex.pace(paced_ctx, "acc1", action_config)
        finished.append(time.monotonic())

    await asyncio.gather(run(), run())

    assert len(finished) == 2
    assert finished[1] - finished[0] >= MIN_DELAY_SECONDS * PACE_TOLERANCE


@pytest.mark.asyncio
async def test_pacing_lock_is_per_account(paced_ctx):
    """Account A's fresh timestamp must not slow account B (locks and
    timestamps are per-account, never global)."""
    action_config = ActionConfig(min_delay_between_actions_seconds=MIN_DELAY_SECONDS)
    await ex.mark_action_now(paced_ctx, "acc1")

    started = time.monotonic()
    await ex.pace(paced_ctx, "acc2", action_config)
    assert time.monotonic() - started < MIN_DELAY_SECONDS * 0.5
