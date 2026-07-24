"""AutoDrainScheduler: interval loop semantics with a fake runner."""
import pytest

from xuse.queue.scheduler import AutoDrainScheduler


class FakeRunner:
    def __init__(self, fail_for=()):
        self.drains = []
        self.fail_for = set(fail_for)

    async def drain(self, account_id, max_actions):
        self.drains.append((account_id, max_actions))
        if account_id in self.fail_for:
            raise RuntimeError("drain boom")

        class Report:
            succeeded, failed, skipped_cap = 1, 0, 0

        return Report()


def make_scheduler(runner=None, accounts=("a", "b"), interval=9999, budget=3):
    return AutoDrainScheduler(runner or FakeRunner(), lambda: list(accounts),
                              interval_seconds=interval, max_actions_per_account=budget)


@pytest.mark.asyncio
async def test_tick_drains_each_account():
    runner = FakeRunner()
    await make_scheduler(runner=runner).tick()
    assert runner.drains == [("a", 3), ("b", 3)]


@pytest.mark.asyncio
async def test_tick_survives_account_failure():
    runner = FakeRunner(fail_for={"a"})
    await make_scheduler(runner=runner).tick()  # must not raise
    assert runner.drains == [("a", 3), ("b", 3)]


@pytest.mark.asyncio
async def test_tick_survives_account_ids_failure():
    scheduler = AutoDrainScheduler(FakeRunner(), lambda: 1 / 0, 9999, 3)
    await scheduler.tick()  # logs and returns


@pytest.mark.asyncio
async def test_start_is_idempotent_and_stop_is_clean():
    scheduler = make_scheduler()
    await scheduler.start()
    task = scheduler._task
    await scheduler.start()
    assert scheduler._task is task and scheduler.running
    await scheduler.stop()
    assert not scheduler.running
    await scheduler.stop()  # safe twice


@pytest.mark.asyncio
async def test_stop_without_start_is_safe():
    await make_scheduler().stop()
