"""Session-pool lifecycle hardening — cancellation safety.

- Cancelling ``acquire()`` mid-cold-start must not orphan the browser: when
  the shielded startup thread later delivers a manager, the pool closes it.
- Cancelling ``close(wait=True)`` while an in-flight action holds the session
  lock must not leak the session: the entry is re-registered so the browser
  stays tracked and a later close (or the reaper) can finish the job.

All sessions come from a fake browser factory — no real Selenium.
"""
import asyncio
import threading

import pytest

from xuse.mcp.sessions import SessionPool

from helpers import (  # noqa: F401 — imported fixtures register for this module
    FakeBrowserManager,
    accounts,
    browser_factory,
    config_loader,
    mcp_settings,
    session_pool,
)


@pytest.mark.asyncio
async def test_cancel_acquire_during_cold_start_closes_late_started_browser(config_loader):
    """A cold start that finishes after its waiter was cancelled must be
    closed by the done callback, not leaked as an untracked browser process."""
    started = []
    release = threading.Event()

    def blocking_factory(account_dict):
        manager = FakeBrowserManager()
        started.append(manager)
        release.wait(5)  # stay parked inside the startup thread until released
        return manager

    pool = SessionPool(config_loader, cold_start_timeout_seconds=60.0,
                       browser_factory=blocking_factory)
    acquire_task = asyncio.create_task(pool.acquire("acc1"))
    await asyncio.sleep(0.1)  # let the startup thread park inside the factory

    acquire_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await acquire_task

    release.set()  # the shielded startup thread now runs to completion
    for _ in range(250):  # startup finishes -> done callback -> close task
        if started and started[0].closed:
            break
        await asyncio.sleep(0.02)

    assert started and started[0].driver_started is True
    assert started[0].closed is True
    assert pool.entry_for("acc1") is None
    await pool.close_all()


@pytest.mark.asyncio
async def test_close_cancelled_while_waiting_keeps_session_tracked(session_pool, browser_factory):
    """close(wait=True) cancelled while parked on an in-flight action must
    re-register the entry: the browser stays tracked and a later close works."""
    entry = await session_pool.acquire("acc1")
    manager = browser_factory.created[0]

    async with entry.lock:  # simulate an in-flight tool action
        close_task = asyncio.create_task(session_pool.close("acc1"))
        await asyncio.sleep(0.05)
        assert not close_task.done()

        close_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await close_task

        # The session is still tracked and the browser was not closed.
        assert session_pool.entry_for("acc1") is not None
        assert manager.closed is False

    # Once the in-flight action released the lock, a fresh close completes.
    await session_pool.close("acc1")
    assert manager.closed is True
    assert session_pool.entry_for("acc1") is None
    await session_pool.close_all()
