"""One policy boundary for every async browser action in the MCP runtime."""
import asyncio
import hashlib
import json
import math
import logging
import time
from contextlib import asynccontextmanager

from .executor import ToolError, resolve_account, require_active

logger = logging.getLogger(__name__)


async def ordinary_read(ctx, account, operation, *args, deadline=None):
    """Wait briefly for ordinary read spacing, never retry a reserved action.

    Composed workflows share a deadline so many queries cannot accumulate an
    unbounded wait. Budget limits and account pauses remain immediate refusals.
    """
    from .safety import PolicyError
    deadline = deadline if deadline is not None else time.monotonic() + 10
    for attempt in range(3):
        try:
            return await browser_call(ctx, account, operation, *args)
        except PolicyError as exc:
            wait = exc.retry_after_seconds
            if (exc.reason != "cooldown" or exc.action_id or attempt == 2
                    or not isinstance(wait, (int, float)) or not math.isfinite(wait)
                    or wait <= 0 or wait > deadline - time.monotonic()):
                raise
            await asyncio.sleep(wait)


def uses_playwright(ctx):
    # Both Chromium drivers share the async adapter and safety boundary.
    return getattr(ctx.session_pool, "backend", None) in ("patchright", "playwright")


@asynccontextmanager
async def search_session(ctx, account, legacy_factory):
    class Search:
        manager = None
        read_deadline = time.monotonic() + 10

        async def tweets(self, query, limit):
            if uses_playwright(ctx):
                result = await ordinary_read(ctx, account, "search_tweets", query, max(1, min(limit, 50)), deadline=self.read_deadline)
                entry = ctx.session_pool.entry_for(account)
                self.manager = entry.browser_manager if entry else None
                return result
            return await asyncio.to_thread(self.scraper.scrape_tweets_by_keyword, query, limit)

    search = Search()
    if uses_playwright(ctx):
        yield search
    else:
        async with ctx.session_pool.session(account) as manager:
            search.manager = manager
            search.scraper = await asyncio.to_thread(legacy_factory, manager, account)
            yield search


async def _drain_task(task):
    """Settle owned work without forwarding repeated caller cancellation.

    asyncio.wait does not cancel its children and, unlike shield, does not
    raise when the child itself finishes cancelled. Retain caller cancellation
    separately so timeout cleanup can still deliver it after bookkeeping.
    """
    cancellation = None
    while not task.done():
        try:
            await asyncio.wait({task})
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
    return cancellation


async def _settled_thread_call(function, *args, action_id=None, **kwargs):
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    cancellation = await _drain_task(task)
    try:
        result = task.result()
    except BaseException:
        if cancellation is None:
            raise
    if cancellation is not None:
        if action_id:
            cancellation.action_id = action_id
        raise cancellation
    return result


async def browser_call(ctx, account, operation, *args, kind="read", action_context=None, recipient_validator=None, recovery_read=False, **kwargs):
    account = resolve_account(ctx, account)[0]
    timeout = ctx.config_loader.get_setting("mcp.tool_timeout_seconds", 180)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ToolError("mcp.tool_timeout_seconds must be a finite positive number.")
    async def run():
        failure = None

        async def operation_task():
            nonlocal failure
            try:
                return await _browser_call_locked(ctx, account, operation, *args, kind=kind,
                    action_context=action_context, recipient_validator=recipient_validator,
                    recovery_read=recovery_read, **kwargs)
            except BaseException as exc:
                # Python 3.10 can replace a task's CancelledError at an await
                # boundary. Keep the original ledger reference independently.
                failure = exc
                raise

        task = asyncio.create_task(operation_task())

        def recovery_id():
            action_id = getattr(failure, "action_id", None)
            if task.done() and not task.cancelled():
                # Cancellation can race a completed non-cancellation failure.
                # Retrieve that exception even when its captured ID is known,
                # so the drained child never reports an unhandled exception.
                exception = task.exception()
                if exception is not None:
                    action_id = action_id or getattr(exception, "action_id", None)
                else:
                    result = task.result()
                    if isinstance(result, dict):
                        action_id = action_id or result.get("action_id")
            return action_id

        try:
            done, _ = await asyncio.wait({task}, timeout=timeout)
            if not done:
                task.cancel()
                cancellation = await _drain_task(task)
                if cancellation is not None:
                    raise cancellation
                raise asyncio.TimeoutError()
            return task.result()
        except asyncio.CancelledError as exc:
            if not task.done():
                task.cancel()
            await _drain_task(task)
            action_id = recovery_id()
            if action_id:
                exc.action_id = action_id
            raise
        except asyncio.TimeoutError:
            error = ToolError("Browser operation timed out. Inspect get_account_safety before retrying a write.")
            error.reason = "tool_timeout"
            error.action_id = recovery_id()
            raise error from None
    if ctx.safety_store is None:
        return await run()
    with ctx.safety_store.operation_lock(account):
        return await run()


async def _browser_call_locked(ctx, account, operation, *args, kind="read", action_context=None, recipient_validator=None, recovery_read=False, **kwargs):
    account, raw, _ = resolve_account(ctx, account)
    require_active(raw, account)
    policy = ctx.safety_store
    action_id = None
    action_key = None
    pause_token = None
    if kind not in ("read", "unlock"):
        material = json.dumps([operation, args, kwargs], sort_keys=True, default=str)
        action_key = hashlib.sha256(material.encode()).hexdigest()
    try:
        # Reserve before startup as account login itself can encounter a block.
        if policy is not None:
            if recovery_read or kind == "unlock":
                pause_token = await asyncio.to_thread(policy.pause_token, account)
            reservation = asyncio.create_task(asyncio.to_thread(
                policy.reserve, account, kind, action_key, action_context, recovery=recovery_read,
                **({"read_operation": operation} if kind == "read" else {})))
            try:
                action_id = await asyncio.shield(reservation)
            except asyncio.CancelledError:
                # A SQLite thread can commit after its caller is cancelled.
                # Recover the committed ID before recording uncertainty so a
                # timed-out draft never loses its recovery reference.
                try:
                    await _drain_task(reservation)
                    action_id = reservation.result()
                except Exception:
                    pass
                raise
        async with ctx.session_pool.session(account) as browser:
            _, current_raw, _ = resolve_account(ctx, account)
            require_active(current_raw, account)
            if policy is not None and not recovery_read and kind != "unlock":
                policy.check_pause(account, **({"read_operation": operation} if kind == "read" else {}))
            def validate_action_now():
                _, current_raw, _ = resolve_account(ctx, account)
                require_active(current_raw, account)
                if policy is not None:
                    policy.check_pause(account)
                if operation == "follow" and ctx.outreach_store is not None:
                    ctx.outreach_store.validate_recipient(account, args[0])
            if kind not in ("read", "unlock"):
                browser.action_validator = validate_action_now
            if operation == "send_message" and ctx.outreach_store is not None:
                def validate_recipient_now(handle):
                    validate_action_now()
                    if recipient_validator:
                        recipient_validator(handle)
                    else:
                        ctx.outreach_store.validate_recipient(account, handle)
                browser.recipient_validator = validate_recipient_now
            result = await getattr(browser, operation)(*args, **kwargs)
        if kind != "read" and (not isinstance(result, dict) or result.get("success") is not True):
            raise ToolError("Browser action has no confirmed success evidence; inspect its outcome before retrying.")
    except BaseException as exc:
        if action_id:
            exc.action_id = action_id
        if policy is not None:
            if action_id:
                try:
                    await _settled_thread_call(policy.finish, account, action_id,
                        "failed" if kind in ("read", "unlock") else "uncertain", action_id=action_id)
                except asyncio.CancelledError:
                    # The ledger call settled before delivering cancellation.
                    # Still apply a detected challenge pause and retain the
                    # original failure's action ID for the outer caller.
                    pass
                except Exception:
                    # A committed reservation still blocks duplicates. Preserve
                    # the original cancellation/error and its recovery ID.
                    logger.exception("Action outcome bookkeeping failed; reservation remains unresolved.")
            if getattr(exc, "reason", None) in ("login_required", "challenge", "rate_limited", "account_locked", "pin_required", "session_expired"):
                try:
                    await _settled_thread_call(policy.pause, account, exc.reason,
                        preserve_manual=True, action_id=action_id)
                except asyncio.CancelledError:
                    pass  # Pause has settled; preserve the original failure.
                except Exception:
                    logger.exception("Account pause bookkeeping failed.")
        raise
    if policy is not None and action_id:
        try:
            await _settled_thread_call(policy.finish, account, action_id, "succeeded", action_id=action_id)
        except Exception:
            error = ToolError("Browser action completed but its ledger outcome could not be recorded. Inspect and reconcile the action ID before any retry.")
            error.reason = "ledger_update_failed"
            error.action_id = action_id
            raise error from None
        if recovery_read or kind == "unlock":
            if not isinstance(result, dict) or result.get("success") is not True:
                raise ToolError("Recovery probe did not confirm authenticated readiness.")
            resumed = await _settled_thread_call(policy.resume_if_unchanged, account, pause_token, action_id=action_id)
            if not resumed:
                from .safety import PolicyError
                raise PolicyError("The browser recovered, but a newer account pause was retained. Inspect get_account_safety before resuming.", reason="pause_changed", action_id=action_id)
    if isinstance(result, dict) and action_id:
        result = {**result, "action_id": action_id}
    return result
