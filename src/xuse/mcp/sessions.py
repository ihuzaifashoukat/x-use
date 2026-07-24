"""Lazy, warm per-account browser session pool for the MCP server.

No browser starts until a tool actually needs one: ``acquire()`` creates a
:class:`~xuse.core.browser_manager.BrowserManager` on first use (driver start
+ cookie login, run in a worker thread with an overall timeout), then keeps
the session warm for reuse. A reaper task closes sessions idle longer than
``idle_timeout_seconds`` (default 600, from ``settings.json → mcp``).

Read-only tools (``list_accounts``, ``get_metrics``) never touch this pool.

All Selenium work stays in the feature modules / BrowserManager — this pool
only owns lifecycle (create, lock, reap, close).
"""
import asyncio
import logging
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, Optional

from xuse.core.browser_manager import BrowserManager
from xuse.core.config_loader import (
    ConfigLoader,
    LEGACY_ACCOUNT_KEY_MAP,
    normalize_account_dict,
)

logger = logging.getLogger(__name__)


class SessionError(Exception):
    """Raised when a browser session cannot be provided (unknown account,
    cold-start failure/timeout, closed pool). Surfaced as a structured tool
    error — never crashes the server."""


@dataclass
class SessionEntry:
    """One warm browser session plus its serialization lock."""

    browser_manager: Any  # BrowserManager (or a compatible fake in tests)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_used: float = field(default_factory=time.monotonic)

    def touch(self) -> None:
        self.last_used = time.monotonic()


class SessionPool:
    """Per-account pool of warm browser sessions.

    ``browser_factory`` (optional, test seam) receives the normalized account
    dict and must return an object with ``get_driver()`` / ``close_driver()``.
    """

    def __init__(
        self,
        config_loader: ConfigLoader,
        idle_timeout_seconds: float = 600.0,
        cold_start_timeout_seconds: float = 180.0,
        reap_interval_seconds: float = 60.0,
        browser_factory: Optional[Callable[[Dict[str, Any]], Any]] = None,
    ):
        self.config_loader = config_loader
        self.idle_timeout_seconds = float(idle_timeout_seconds)
        self.cold_start_timeout_seconds = float(cold_start_timeout_seconds)
        self.reap_interval_seconds = float(reap_interval_seconds)
        self._browser_factory = browser_factory
        self._entries: Dict[str, SessionEntry] = {}
        self._create_lock: Optional[asyncio.Lock] = None
        self._reaper_task: Optional[asyncio.Task] = None
        self._closed = False

    @property
    def active_accounts(self) -> Iterator[str]:
        return iter(self._entries.keys())

    def entry_for(self, account_id: str) -> Optional[SessionEntry]:
        return self._entries.get(account_id)

    def find_account_dict(self, account_id: str) -> Dict[str, Any]:
        """Normalized config dict for ``account_id``; SessionError if unknown."""
        for raw in self.config_loader.get_accounts_config():
            if isinstance(raw, dict) and raw.get("account_id") == account_id:
                return normalize_account_dict(raw)
        raise SessionError(f"Unknown account '{account_id}' — not present in config/accounts.json.")

    # -- lifecycle ---------------------------------------------------------

    async def acquire(self, account_id: str) -> SessionEntry:
        """Return the warm session for ``account_id``, creating it lazily."""
        if self._closed:
            raise SessionError("Session pool is closed.")
        self._ensure_reaper()
        entry = self._entries.get(account_id)
        if entry is not None:
            entry.touch()
            return entry
        if self._create_lock is None:
            self._create_lock = asyncio.Lock()
        async with self._create_lock:  # serialize cold starts per pool
            entry = self._entries.get(account_id)
            if entry is not None:
                entry.touch()
                return entry
            entry = await self._cold_start(account_id)
            self._entries[account_id] = entry
            return entry

    @asynccontextmanager
    async def session(self, account_id: str):
        """Acquire the account's browser under its per-account lock.

        Serializes tool calls that share one browser; refreshes the idle
        timestamp on release so active sessions are never reaped.
        """
        entry = await self.acquire(account_id)
        async with entry.lock:
            try:
                yield entry.browser_manager
            finally:
                entry.touch()

    async def close(self, account_id: str, *, wait: bool = True) -> None:
        # Pop first so no new caller can attach to the dying entry, then wait
        # out any in-flight action: closing the driver underneath an active
        # tool call would kill the browser mid-write (account tools close
        # warm sessions on update/pause/remove, possibly during a drain).
        entry = self._entries.pop(account_id, None)
        if entry is None:
            return
        if wait:
            # Shutdown (close_all) passes wait=False: the server loop is
            # already torn down by then, and a lock abandoned mid-hold on a
            # dead loop would otherwise hang the cleanup forever.
            await entry.lock.acquire()
        try:
            await asyncio.to_thread(entry.browser_manager.close_driver)
            logger.info("Closed browser session for account '%s'.", account_id)
        except Exception:
            logger.exception("Error closing browser session for '%s'.", account_id)
        finally:
            if wait and entry.lock.locked():
                entry.lock.release()

    async def close_all(self) -> None:
        """Close every session and stop the reaper. Safe to call once at shutdown."""
        self._closed = True
        task = self._reaper_task
        self._reaper_task = None
        if task is not None and not task.done():
            task.cancel()
            # The task may belong to an already-closed loop when close_all is
            # driven from a fresh loop in main()'s finally block.
            with suppress(asyncio.CancelledError, RuntimeError):
                await task
        for account_id in list(self._entries.keys()):
            await self.close(account_id, wait=False)

    # -- internals ----------------------------------------------------------

    async def _cold_start(self, account_id: str) -> SessionEntry:
        account_dict = self.find_account_dict(account_id)

        def _start() -> Any:
            if self._browser_factory is not None:
                manager = self._browser_factory(account_dict)
            else:
                manager = BrowserManager(account_config=account_dict, config_loader=self.config_loader)
            manager.get_driver()  # starts the browser and applies cookie login
            return manager

        # Shield the startup thread from the timeout: wait_for cannot kill a
        # running thread, and an unshielded cancellation would drop the future
        # — the browser would eventually start with no handle and leak. The
        # done callback closes any browser that finishes after the timeout.
        start_future = asyncio.ensure_future(asyncio.to_thread(_start))
        try:
            manager = await asyncio.wait_for(
                asyncio.shield(start_future), timeout=self.cold_start_timeout_seconds
            )
        except asyncio.TimeoutError:
            start_future.add_done_callback(self._close_late_started_browser)
            raise SessionError(
                f"Browser cold start for account '{account_id}' timed out after "
                f"{self.cold_start_timeout_seconds:.0f}s."
            ) from None
        except SessionError:
            raise
        except Exception as e:
            raise SessionError(f"Browser cold start failed for account '{account_id}': {e}") from e
        logger.info("Warm browser session ready for account '%s'.", account_id)
        return SessionEntry(browser_manager=manager)

    def _close_late_started_browser(self, future: "asyncio.Future[Any]") -> None:
        """Done callback for cold starts that outlived their timeout: close
        the orphaned browser as soon as the startup thread delivers it."""
        if future.cancelled():
            return
        try:
            manager = future.result()
        except Exception:
            return  # startup failed on its own — nothing to close
        logger.warning("Cold start finished after the timeout; closing the orphaned browser.")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        asyncio.ensure_future(asyncio.to_thread(self._close_driver_quietly, manager))

    @staticmethod
    def _close_driver_quietly(manager: Any) -> None:
        try:
            manager.close_driver()
        except Exception:
            logger.exception("Failed to close late-started browser.")

    def _ensure_reaper(self) -> None:
        if self._closed:
            return
        if self._reaper_task is None or self._reaper_task.done():
            self._reaper_task = asyncio.create_task(self._reaper_loop())

    async def _reaper_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.reap_interval_seconds)
                now = time.monotonic()
                for account_id, entry in list(self._entries.items()):
                    if entry.lock.locked():
                        continue  # in use — never reap mid-action
                    if now - entry.last_used > self.idle_timeout_seconds:
                        logger.info("Reaping idle browser session for account '%s'.", account_id)
                        await self.close(account_id)
        except asyncio.CancelledError:
            pass
