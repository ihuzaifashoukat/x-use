"""QueueRunner robustness: cancel-during-sleep, zombie processing recovery,
paused-account drains, and max_actions validation. Scripted fakes only."""
import asyncio
from datetime import datetime, timezone

import pytest

from xuse.queue import QueueConfig, QueueRunner, QueueStore

NOW = datetime(2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, start=NOW):
        self.now = start

    def __call__(self):
        return self.now


class FakeSleep:
    def __init__(self):
        self.slept = []

    async def __call__(self, seconds):
        self.slept.append(seconds)


class RecordingExecutor:
    def __init__(self):
        self.calls = []

    async def __call__(self, item):
        self.calls.append(item.queue_id)
        return {"queue_id": item.queue_id, "success": True}


def make_runner(tmp_path, executor=None, config=None, account_filter=None,
                sleep=None, clock=None):
    store = QueueStore(tmp_path / "q.jsonl")
    runner = QueueRunner(
        store,
        config or QueueConfig(min_delay_seconds=90, max_delay_seconds=240),
        executor or RecordingExecutor(),
        account_filter=account_filter,
        sleep_fn=sleep or FakeSleep(),
        jitter_fn=lambda low, high: low,  # deterministic: always the minimum
        now_fn=clock or Clock(),
    )
    return store, runner


def add(store, account="acc1", action="like", key=None, not_before=None):
    return store.add(account=account, action=action, payload={},
                     dedup_key=key or f"{action}_{account}_x", not_before=not_before)


# ---------------------------------------------------------------------------
# C1: cancel_queued_action during the pacing sleep must stop execution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_during_pacing_sleep_skips_execution(tmp_path):
    """Item 2 cancelled while the drain paces before it must never execute."""
    store, runner = make_runner(tmp_path)
    item1 = add(store, key="like_acc1_1")
    item2 = add(store, key="like_acc1_2")

    async def cancelling_sleep(seconds):
        # What cancel_queued_action does while the drain is pacing.
        store.set_status(item2.queue_id, "cancelled")

    runner.sleep_fn = cancelling_sleep
    report = await runner.drain("acc1", 5)
    assert runner.executor.calls == [item1.queue_id]
    assert store.get(item2.queue_id).status == "cancelled"
    assert report.succeeded == 1
    assert report.skipped == 1


# ---------------------------------------------------------------------------
# Zombie processing: a drain cancelled mid-executor must not strand the item
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drain_cancelled_mid_executor_returns_item_to_pending(tmp_path):
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocking_executor(item):
        started.set()
        await release.wait()
        return {"queue_id": item.queue_id, "success": True}

    store, runner = make_runner(tmp_path, executor=blocking_executor)
    item = add(store, key="like_acc1_z")
    task = asyncio.create_task(runner.drain("acc1", 5))
    await started.wait()  # drain is inside the executor, item is "processing"
    assert store.get(item.queue_id).status == "processing"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()  # let any orphaned work unwind

    # No zombie: the item is pending again in the SAME process.
    assert store.get(item.queue_id).status == "pending"
    assert store.get(item.queue_id).attempts == 0

    # A later drain picks it up instead of skipping it forever.
    report = await runner.drain("acc1", 5)
    assert report.succeeded == 1
    assert store.get(item.queue_id).status == "done"


# ---------------------------------------------------------------------------
# Paused-account explicit drain: frozen, not condemned
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_explicit_drain_on_paused_account_skips_without_attempts(tmp_path):
    store, runner = make_runner(tmp_path, account_filter=lambda a: a != "acc2")
    item = add(store, account="acc2", key="like_acc2_1")
    report = await runner.drain("acc2", 5)
    assert runner.executor.calls == []
    assert report.succeeded == 0 and report.failed == 0
    assert report.skipped_inactive == 1
    updated = store.get(item.queue_id)
    assert updated.status == "pending" and updated.attempts == 0


@pytest.mark.asyncio
async def test_explicit_drain_on_active_account_still_runs_with_filter(tmp_path):
    store, runner = make_runner(tmp_path, account_filter=lambda a: a != "acc2")
    item = add(store, account="acc1", key="like_acc1_1")
    report = await runner.drain("acc1", 5)
    assert report.succeeded == 1
    assert report.skipped_inactive == 0
    assert store.get(item.queue_id).status == "done"


# ---------------------------------------------------------------------------
# max_actions=0 must not silently mean "the configured default budget"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_actions_zero_does_not_mean_default_budget(tmp_path):
    config = QueueConfig(min_delay_seconds=90, max_delay_seconds=240,
                         max_actions_per_run=5)
    store, runner = make_runner(tmp_path, config=config)
    for i in range(3):
        add(store, key=f"like_acc1_{i}")
    report = await runner.drain("acc1", max_actions=0)
    assert report.succeeded == 1  # clamped to one, not the 5-item default


@pytest.mark.asyncio
async def test_max_actions_none_uses_default_budget(tmp_path):
    config = QueueConfig(min_delay_seconds=90, max_delay_seconds=240,
                         max_actions_per_run=2)
    store, runner = make_runner(tmp_path, config=config)
    for i in range(3):
        add(store, key=f"like_acc1_{i}")
    report = await runner.drain("acc1")
    assert report.succeeded == 2
