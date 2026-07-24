"""QueueRunner: drain order, pacing, caps, backoff. Scripted fakes only."""
from datetime import datetime, timedelta, timezone

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
    """Succeeds unless the item's dedup_key is in failures."""

    def __init__(self, failures=()):
        self.calls = []
        self.failures = set(failures)

    async def __call__(self, item):
        self.calls.append(item.queue_id)
        if item.dedup_key in self.failures:
            raise RuntimeError(f"boom:{item.dedup_key}")
        return {"queue_id": item.queue_id, "success": True}


def make_runner(tmp_path, executor=None, config=None, already_done=None,
                sleep=None, clock=None):
    store = QueueStore(tmp_path / "q.jsonl")
    runner = QueueRunner(
        store,
        config or QueueConfig(min_delay_seconds=90, max_delay_seconds=240),
        executor or RecordingExecutor(),
        already_done=already_done,
        sleep_fn=sleep or FakeSleep(),
        jitter_fn=lambda low, high: low,  # deterministic: always the minimum
        now_fn=clock or Clock(),
    )
    return store, runner


def add(store, account="acc1", action="like", key=None, not_before=None):
    return store.add(account=account, action=action, payload={},
                     dedup_key=key or f"{action}_{account}_x", not_before=not_before)


@pytest.mark.asyncio
async def test_requires_executor(tmp_path):
    with pytest.raises(ValueError):
        QueueRunner(QueueStore(tmp_path / "q.jsonl"), QueueConfig(), None)


@pytest.mark.asyncio
async def test_drains_fifo_up_to_budget(tmp_path):
    store, runner = make_runner(tmp_path)
    items = [add(store, key=f"like_acc1_{i}") for i in range(3)]
    report = await runner.drain("acc1", max_actions=2)
    assert report.succeeded == 2
    assert runner.executor.calls == [items[0].queue_id, items[1].queue_id]
    assert store.get(items[2].queue_id).status == "pending"


@pytest.mark.asyncio
async def test_success_marks_done_with_completed_at(tmp_path):
    store, runner = make_runner(tmp_path)
    item = add(store)
    await runner.drain("acc1", 5)
    done = store.get(item.queue_id)
    assert done.status == "done" and done.completed_at is not None


@pytest.mark.asyncio
async def test_sleeps_between_executions_not_after_last(tmp_path):
    sleep = FakeSleep()
    store, runner = make_runner(tmp_path, sleep=sleep)
    for i in range(3):
        add(store, key=f"like_acc1_{i}")
    await runner.drain("acc1", 5)
    assert sleep.slept == [90.0, 90.0]  # before items 2 and 3 only


@pytest.mark.asyncio
async def test_not_before_items_are_deferred(tmp_path):
    store, runner = make_runner(tmp_path)
    future = add(store, key="like_acc1_f",
                 not_before=(NOW + timedelta(hours=1)).isoformat())
    report = await runner.drain("acc1", 5)
    assert report.succeeded == 0 and report.deferred == 1
    assert store.get(future.queue_id).status == "pending"


@pytest.mark.asyncio
async def test_daily_cap_stops_that_action_type_only(tmp_path):
    config = QueueConfig(min_delay_seconds=90, max_delay_seconds=240,
                         daily_caps={"like": 1, "reply": 5, "retweet": 5, "post": 5})
    store, runner = make_runner(tmp_path, config=config)
    add(store, key="like_acc1_1")
    like2 = add(store, key="like_acc1_2")
    add(store, action="reply", key="reply_acc1_1")
    report = await runner.drain("acc1", 10)
    assert report.succeeded == 2           # like1 + reply
    assert report.skipped_cap == 1         # like2
    assert report.capped_actions == ["like"]
    assert store.get(like2.queue_id).status == "pending"


@pytest.mark.asyncio
async def test_already_done_marks_done_without_executing(tmp_path):
    store, runner = make_runner(tmp_path, already_done=lambda key: key == "like_acc1_1")
    item = add(store, key="like_acc1_1")
    other = add(store, key="like_acc1_2")
    await runner.drain("acc1", 5)
    assert runner.executor.calls == [other.queue_id]
    done = store.get(item.queue_id)
    assert done.status == "done" and done.completed_at is not None


@pytest.mark.asyncio
async def test_failure_requeues_with_backoff(tmp_path):
    store, runner = make_runner(
        tmp_path, executor=RecordingExecutor(failures={"like_acc1_1"}),
        config=QueueConfig(min_delay_seconds=90, max_delay_seconds=240, max_attempts=2))
    item = add(store, key="like_acc1_1")
    report = await runner.drain("acc1", 5)
    updated = store.get(item.queue_id)
    assert report.failed == 1
    assert updated.status == "pending" and updated.attempts == 1
    assert updated.not_before is not None and updated.last_error.startswith("boom:")


@pytest.mark.asyncio
async def test_failure_at_max_attempts_marks_failed(tmp_path):
    store, runner = make_runner(
        tmp_path, executor=RecordingExecutor(failures={"like_acc1_1"}),
        config=QueueConfig(min_delay_seconds=90, max_delay_seconds=240, max_attempts=1))
    item = add(store, key="like_acc1_1")
    report = await runner.drain("acc1", 5)
    assert store.get(item.queue_id).status == "failed"
    assert report.failed == 1


@pytest.mark.asyncio
async def test_one_failure_does_not_stop_the_drain(tmp_path):
    store, runner = make_runner(
        tmp_path, executor=RecordingExecutor(failures={"like_acc1_1"}),
        config=QueueConfig(min_delay_seconds=90, max_delay_seconds=240, max_attempts=2))
    add(store, key="like_acc1_1")
    ok_item = add(store, key="like_acc1_2")
    report = await runner.drain("acc1", 5)
    assert report.failed == 1 and report.succeeded == 1
    assert store.get(ok_item.queue_id).status == "done"


@pytest.mark.asyncio
async def test_drain_all_accounts(tmp_path):
    store, runner = make_runner(tmp_path)
    add(store, account="a", key="like_a_1")
    add(store, account="b", key="like_b_1")
    report = await runner.drain(None, 5)
    assert report.succeeded == 2


@pytest.mark.asyncio
async def test_empty_queue_returns_zero_report(tmp_path):
    store, runner = make_runner(tmp_path)
    report = await runner.drain("acc1", 5)
    assert (report.succeeded, report.failed, report.skipped_cap) == (0, 0, 0)
