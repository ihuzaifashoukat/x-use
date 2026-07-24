"""Opt-in background drain loop (queue.auto_drain).

Ticks on an interval and drains a small per-account budget through the
shared QueueRunner. Off by default; lives only while the host (MCP server)
runs. All pacing, caps, and backoff come from the runner.
"""
import asyncio
import logging
from typing import Callable, List, Optional

from .runner import QueueRunner

logger = logging.getLogger(__name__)


class AutoDrainScheduler:
    def __init__(self, runner: QueueRunner,
                 account_ids_fn: Callable[[], List[str]],
                 interval_seconds: float = 900.0,
                 max_actions_per_account: int = 3):
        self.runner = runner
        self.account_ids_fn = account_ids_fn
        self.interval_seconds = float(interval_seconds)
        self.max_actions_per_account = int(max_actions_per_account)
        self._task: Optional[asyncio.Task] = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        """Spawn the loop task. Idempotent."""
        if self.running:
            return
        self._task = asyncio.create_task(self._loop())
        logger.info("auto_drain started (every %.0fs, %d action(s)/account/tick).",
                    self.interval_seconds, self.max_actions_per_account)

    async def stop(self) -> None:
        """Cancel the loop and wait for it. Safe when never started."""
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, RuntimeError):
            pass

    async def _loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.interval_seconds)
                await self.tick()
        except asyncio.CancelledError:
            pass

    async def tick(self) -> None:
        """One drain pass over every account. Exposed for tests."""
        try:
            account_ids = self.account_ids_fn()
        except Exception:
            logger.exception("auto_drain: account_ids_fn failed; skipping tick.")
            return
        for account_id in account_ids:
            try:
                report = await self.runner.drain(account_id, self.max_actions_per_account)
                if report.succeeded or report.failed:
                    logger.info("auto_drain tick for '%s': %d ok, %d failed, %d capped.",
                                account_id, report.succeeded, report.failed,
                                report.skipped_cap)
            except Exception:
                logger.exception("auto_drain tick failed for account '%s'.", account_id)
