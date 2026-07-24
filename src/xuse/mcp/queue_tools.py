"""Queue-side MCP tools: queue_post, queue_engagement, list_queue,
cancel_queued_action, process_queue.

The queue is the scheduled gate: enqueue tools build the FULL payload now
(LLM text generated at enqueue time, so list_queue shows exactly what will
fire) and store it; execution happens only via an explicit process_queue
call, or the opt-in auto_drain worker. Draft mode does not change queue
semantics.
"""
import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from xuse.queue import ALL_STATUSES, QueueStore

from . import actions, executor as ex
from .executor import Ctx, ToolError
from .tools import guard, ok_, scrape_single_tweet

logger = logging.getLogger(__name__)

_ENGAGEMENT_ACTIONS = ("like", "retweet", "reply")


def build_executor(ctx: Ctx):
    """Bridge the queue runner's ExecutorFn to the MCP browser executors."""

    async def _execute(item) -> Dict[str, Any]:
        payload = item.payload
        if item.action == "post":
            return await actions.exec_post(
                ctx, item.account, text=payload.get("text", ""),
                media=payload.get("media") or None, community=payload.get("community"))
        if item.action == "reply":
            return await actions.exec_reply(
                ctx, item.account, tweet_url=payload.get("tweet_url", ""),
                reply_text=payload.get("text", ""), tweet_id=payload.get("tweet_id"),
                text_content=payload.get("text_content", ""))
        if item.action == "like":
            return await actions.exec_like(ctx, item.account, payload["tweet_id"],
                                           payload.get("tweet_url"))
        if item.action == "retweet":
            return await actions.exec_retweet(ctx, item.account, payload["tweet_id"],
                                              payload.get("tweet_url"),
                                              text_content=payload.get("text_content", ""))
        raise ToolError(f"Unsupported queue action '{item.action}'.")

    return _execute


def _store(ctx: Ctx) -> QueueStore:
    if ctx.queue_store is None:
        raise ToolError("Queue store is not configured on this server.")
    return ctx.queue_store


def _parse_not_before(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        raise ToolError(f"not_before must be an ISO 8601 timestamp, got: {value!r}") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat()


def _enqueue(ctx: Ctx, *, account_id: str, action: str, payload: Dict[str, Any],
             dedup_key: str, not_before: Optional[str]) -> Dict[str, Any]:
    store = _store(ctx)
    if ex.is_processed(ctx, dedup_key):
        raise ToolError("This exact action was already executed for this account (dedup).")
    existing = store.find_pending_by_dedup_key(dedup_key)
    if existing is not None:
        raise ToolError(f"Already queued as {existing.queue_id} (dedup).")
    item = store.add(account=account_id, action=action, payload=payload,
                     dedup_key=dedup_key, not_before=not_before)
    position = len(store.list(account=account_id, status="pending"))
    logger.info("Queued %s for account '%s' as %s.", action, account_id, item.queue_id)
    return ok_(queued=True, queue_id=item.queue_id, account=account_id, action=action,
               position=position, payload=payload,
               message="Queued — nothing executes until process_queue is called "
                       "(or the auto_drain worker runs, if enabled).")


def register_queue_tools(server, ctx: Ctx) -> None:
    """Register the five queue tools on the FastMCP server."""

    @server.tool()
    @guard
    async def queue_post(account: str, text: Optional[str] = None,
                         topic: Optional[str] = None,
                         media: Optional[List[str]] = None,
                         community: Optional[str] = None,
                         not_before: Optional[str] = None) -> Dict[str, Any]:
        """Schedule a post for paced execution. Pass `text` verbatim, or a
        `topic` to generate the text NOW with the configured LLM (the stored
        payload is final — list_queue shows exactly what will be posted).
        `not_before` is an optional ISO 8601 timestamp. Nothing executes
        until process_queue is called (or auto_drain, if enabled)."""
        account_id, raw, _ = ex.resolve_account(ctx, account)
        if not raw.get("is_active", True):
            raise ToolError(
                f"Account '{account_id}' is paused (is_active=false). "
                "Re-enable it with set_account_active before queueing work.")
        if (text is None) == (topic is None):
            raise ToolError("Pass exactly one of `text` or `topic`.")
        final_text = text.strip() if text is not None else ""
        if topic is not None:
            final_text = await actions.generate_post_text(ctx, account_id, topic)
        if not final_text:
            raise ToolError("Post text must not be empty.")
        nb = _parse_not_before(not_before) if not_before else None
        dedup_key = f"post_{account_id}_{hashlib.sha1(final_text.encode('utf-8')).hexdigest()[:12]}"
        return _enqueue(ctx, account_id=account_id, action="post",
                        payload={"text": final_text, "media": list(media or []),
                                 "community": community},
                        dedup_key=dedup_key, not_before=nb)

    @server.tool()
    @guard
    async def queue_engagement(account: str, action: str, tweet_url: str,
                               text: Optional[str] = None) -> Dict[str, Any]:
        """Queue a like, retweet, or reply for paced execution. `action` is
        one of like/retweet/reply. reply requires `text` ("auto" scrapes the
        tweet and generates the reply NOW, so the stored payload is final).
        Nothing executes until process_queue is called."""
        account_id, raw, _ = ex.resolve_account(ctx, account)
        if not raw.get("is_active", True):
            raise ToolError(
                f"Account '{account_id}' is paused (is_active=false). "
                "Re-enable it with set_account_active before queueing work.")
        action = (action or "").strip().lower()
        if action not in _ENGAGEMENT_ACTIONS:
            raise ToolError(
                f"Unsupported queue action '{action}'. Allowed: {list(_ENGAGEMENT_ACTIONS)}.")
        tweet_id = ex.tweet_id_from_url(tweet_url)
        if not tweet_id:
            raise ToolError(f"Could not parse a tweet id from URL: {tweet_url}")
        payload: Dict[str, Any] = {"tweet_id": tweet_id, "tweet_url": tweet_url}
        if action == "reply":
            if text is None or not text.strip():
                raise ToolError("reply requires `text` (or \"auto\").")
            if text.strip().lower() == "auto":
                original = await scrape_single_tweet(ctx, account_id, tweet_url, tweet_id)
                payload["text_content"] = original.text_content or ""
                payload["text"] = await actions.generate_reply_text(ctx, account_id, original)
            else:
                payload["text"] = text.strip()[: ex.MAX_REPLY_CHARS]
        dedup_key = f"{action}_{account_id}_{tweet_id}"
        return _enqueue(ctx, account_id=account_id, action=action,
                        payload=payload, dedup_key=dedup_key, not_before=None)

    @server.tool()
    @guard
    async def list_queue(account: Optional[str] = None,
                         status: Optional[str] = None) -> Dict[str, Any]:
        """List queued actions with their full payloads (exactly what will
        fire), plus per-status counts. Read-only — never starts a browser."""
        store = _store(ctx)
        if status is not None and status not in ALL_STATUSES:
            raise ToolError(f"Unknown status '{status}'. Allowed: {list(ALL_STATUSES)}.")
        items = store.list(account=account, status=status)
        items.sort(key=lambda i: i.created_at)
        return ok_(count=len(items), counts=store.counts(account=account),
                   items=[i.model_dump(mode="json") for i in items])

    @server.tool()
    @guard
    async def cancel_queued_action(queue_id: str) -> Dict[str, Any]:
        """Cancel a pending or failed queued action. Done, processing, and
        already-cancelled items cannot transition."""
        store = _store(ctx)
        try:
            item = store.get(queue_id)
        except KeyError:
            raise ToolError(f"Unknown queue_id '{queue_id}'.") from None
        if item.status not in ("pending", "failed"):
            raise ToolError(f"Queue item '{queue_id}' is {item.status} — cannot cancel.")
        store.set_status(queue_id, "cancelled")
        return ok_(queue_id=queue_id, status="cancelled")

    @server.tool()
    @guard
    async def process_queue(account: Optional[str] = None,
                            max_actions: int = 5) -> Dict[str, Any]:
        """Drain queued actions with jittered pacing and daily caps. THIS
        CALL IS THE APPROVAL GATE for queued work: items execute through the
        same pacing/dedup/metrics path as every other write action. With
        `account` omitted, every account with due items drains (each bounded
        by max_actions and its own daily caps)."""
        if ctx.queue_runner is None:
            raise ToolError("Queue runner is not configured on this server.")
        account_id = None
        if account is not None:
            account_id, _, _ = ex.resolve_account(ctx, account)
        report = await ctx.queue_runner.drain(account_id, max_actions)
        return ok_(**report.model_dump())
