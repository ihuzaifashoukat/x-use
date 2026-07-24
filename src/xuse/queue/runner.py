"""Drain engine for the action queue.

The runner owns pacing, daily caps, and retry backoff. It never touches a
browser or an LLM: execution is delegated to an injected executor callable,
which keeps this package independent of xuse.mcp (the MCP layer bridges the
executor to its browser actions; a future CLI can bridge it elsewhere).
"""
import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set

from pydantic import BaseModel, Field

from xuse.utils.sanitize import sanitize_text

from .models import QueueConfig, QueuedAction
from .store import QueueStore, is_due

logger = logging.getLogger(__name__)

ExecutorFn = Callable[[QueuedAction], Awaitable[Dict[str, Any]]]
AlreadyDoneFn = Callable[[str], bool]


def _default_now() -> datetime:
    return datetime.now(timezone.utc)


class DrainReport(BaseModel):
    account: Optional[str] = None
    already_running: bool = False
    executed: List[Dict[str, Any]] = Field(default_factory=list)
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    skipped_cap: int = 0
    skipped_inactive: int = 0
    deferred: int = 0
    capped_actions: List[str] = Field(default_factory=list)


class QueueRunner:
    """Drains due queue items through the injected executor. Injected
    callables: executor (required), already_done, sleep/jitter/now fns, and
    an optional account_filter that excludes accounts from drain-all runs."""

    def __init__(
        self,
        store: QueueStore,
        config: Optional[QueueConfig] = None,
        executor: Optional[ExecutorFn] = None,
        already_done: Optional[AlreadyDoneFn] = None,
        account_filter: Optional[Callable[[str], bool]] = None,
        sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter_fn: Callable[[float, float], float] = random.uniform,
        now_fn: Callable[[], datetime] = _default_now,
    ):
        if executor is None:
            raise ValueError("QueueRunner requires an executor callable.")
        self.store = store
        self.config = config or QueueConfig()
        self.executor = executor
        self.already_done = already_done
        self.account_filter = account_filter
        self.sleep_fn = sleep_fn
        self.jitter_fn = jitter_fn
        self.now_fn = now_fn
        self._drain_lock = asyncio.Lock()

    async def drain(self, account_id: Optional[str] = None,
                    max_actions: Optional[int] = None) -> DrainReport:
        """Execute due items with pacing and caps. account_id=None drains
        every account with due items (each bounded by max_actions).

        Only one drain may run at a time: a concurrent caller (a manual
        process_queue overlapping an auto_drain tick, or two manual calls)
        would otherwise snapshot the same pending items and execute them
        twice. The second caller gets an immediate report with
        already_running=True and nothing is executed."""
        if self._drain_lock.locked():
            logger.info("Drain already in progress; refusing a concurrent drain.")
            return DrainReport(account=account_id, already_running=True)
        async with self._drain_lock:
            # Explicit None check: max_actions=0 must clamp to a single
            # action, not silently fall back to the configured default.
            budget = max(1, int(
                self.config.max_actions_per_run if max_actions is None else max_actions))
            report = DrainReport(account=account_id)
            if account_id:
                if self.account_filter is not None and not self.account_filter(account_id):
                    # Paused means frozen, not condemned: an explicit drain on
                    # an inactive account executes nothing and leaves every
                    # item pending with its attempts untouched.
                    report.skipped_inactive = len(
                        self.store.list(account=account_id, status="pending"))
                    logger.info("Drain of inactive account '%s' skipped (%d item(s) left pending).",
                                account_id, report.skipped_inactive)
                    return report
                accounts = [account_id]
            else:
                accounts = [
                    a for a in self.store.due_accounts(self.now_fn())
                    if self.account_filter is None or self.account_filter(a)
                ]
            for acc in accounts:
                await self._drain_account(acc, budget, report)
            return report

    async def _drain_account(self, account_id: str, budget: int,
                             report: DrainReport) -> None:
        now = self.now_fn()
        pending = self.store.list(account=account_id, status="pending")
        report.deferred += sum(1 for i in pending if not is_due(i, now))
        capped: Set[str] = set()
        spent = 0
        for item in self.store.due_items(account_id, now):
            if spent >= budget:
                break
            if item.action in capped:
                report.skipped_cap += 1
                continue
            if self.already_done is not None and self.already_done(item.dedup_key):
                # Executed outside the queue; still counts toward today's cap
                # (the action really happened today, via another path).
                self.store.set_status(item.queue_id, "done",
                                      completed_at=self.now_fn().isoformat())
                continue
            cap = self.config.daily_caps.get(item.action)
            if cap is not None and self.store.count_done_today(account_id, item.action, now) >= cap:
                capped.add(item.action)
                report.capped_actions = sorted(capped | set(report.capped_actions))
                report.skipped_cap += 1
                continue
            if spent > 0:
                await self.sleep_fn(self.jitter_fn(
                    float(self.config.min_delay_seconds),
                    float(self.config.max_delay_seconds)))
            # Re-validate after the pacing window: cancel_queued_action may
            # have cancelled the item while we slept — never execute work
            # whose status moved away from pending.
            if self.store.get(item.queue_id).status != "pending":
                report.skipped += 1
                continue
            self.store.set_status(item.queue_id, "processing")
            try:
                result = await self.executor(item)
            except asyncio.CancelledError:
                # Drain cancelled mid-execution (client disconnect, scheduler
                # stop): un-zombie the item so a later drain in this process
                # can retry it — the store's load-time processing→pending
                # reset only covers restarts. The in-flight browser thread
                # may still land the action (at-least-once); executor-side
                # dedup is the guard against a duplicate on re-run.
                self.store.set_status(item.queue_id, "pending")
                raise
            except Exception as e:
                # Sanitize BEFORE persisting/reporting: executor errors can
                # carry proxy credentials (cold-start failures), and last_error
                # lands in ok:true responses and the on-disk queue JSONL.
                message = sanitize_text(e)
                attempt = self.store.record_attempt(item.queue_id, message)
                if attempt.attempts >= self.config.max_attempts:
                    self.store.set_status(item.queue_id, "failed")
                else:
                    backoff = self.config.min_delay_seconds * attempt.attempts
                    not_before = (self.now_fn() + timedelta(seconds=backoff)).isoformat()
                    self.store.set_status(item.queue_id, "pending", not_before=not_before)
                report.failed += 1
                spent += 1
                report.executed.append({
                    "queue_id": item.queue_id, "account": account_id,
                    "action": item.action, "success": False, "error": message,
                })
                logger.warning("Queue item %s failed (attempt %d): %s",
                               item.queue_id, attempt.attempts, message)
                continue
            self.store.set_status(item.queue_id, "done",
                                  completed_at=self.now_fn().isoformat())
            report.succeeded += 1
            spent += 1
            report.executed.append({
                "queue_id": item.queue_id, "account": account_id,
                "action": item.action, "success": True, "result": result,
            })
