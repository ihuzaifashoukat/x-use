"""MCP tool layer for the x-use server — shared helpers, read-only tools,
and the draft-approval gate. Write tools live in ``write_tools.py`` and
``engage.py``; all are registered via :func:`register_tools`.

Every tool is a thin wrapper (validate → delegate → JSON dict) wrapped so
failures return ``{"ok": false, "error": {"type": ..., "message": ...}}`` —
the server process never crashes on a tool failure (NFR-1).
"""
import asyncio
import functools
import json
import logging
import re
from typing import Any, Dict, List, Optional

from mcp.types import CallToolResult, TextContent

from xuse.core.config_loader import PROJECT_ROOT
from xuse.features.scraper import TweetScraper
from xuse.mcp.media import (MAX_IMAGES_PER_SEARCH, image_fetch_scope, images_for_tweet,
                            media_envelope, with_images)

from . import actions, executor as ex
from .drafts import Draft
from .executor import Ctx, ToolError
from .sessions import SessionError
from xuse.browser.errors import BrowserActionError, SessionError as BrowserSessionError
from xuse.queue.store import QueueJournalError
from .browser_bridge import browser_call, uses_playwright
from .annotations import PUBLISHES_TO_X, READ_ONLY_FROM_X, READ_ONLY_LOCAL
from .local_reads import read_event_tail, read_json

logger = logging.getLogger(__name__)

# Account ids are interpolated into filesystem paths; restrict to a charset
# that cannot traverse (no dots, slashes, or backslashes).
_SAFE_ACCOUNT_ID = re.compile(r"[A-Za-z0-9_-]+")


def ok_(**fields: Any) -> Dict[str, Any]:
    return {"ok": True, **fields}


def _send_diagnostics(error) -> Optional[Dict[str, Any]]:
    """Copy only the reviewed primitive confirmation schema, never DOM data."""
    if not isinstance(error, BrowserActionError) or error.reason != "send_unconfirmed":
        return None
    source = getattr(error, "diagnostics", None)
    if type(source) is not dict:
        return None
    version, stage = source.get("schema_version"), source.get("stage")
    if (type(version) is not int or version != 1 or type(stage) is not str
            or stage not in ("route_changed", "confirmation_timeout")):
        return None
    safe = {"schema_version": 1, "stage": stage}
    if type(source.get("observer_armed")) is bool:
        safe["observer_armed"] = source["observer_armed"]
    for field in ("observer_started", "composer_empty", "scope_connected",
                  "composer_connected", "send_control_connected"):
        if field in source and (source[field] is None or type(source[field]) is bool):
            safe[field] = source[field]
    for field in ("pending_count", "failed_count"):
        if field in source:
            value = source[field]
            if value is None or (type(value) is int and 0 <= value <= 1000):
                safe[field] = value
    return safe


def guard(fn):
    """Preserve the JSON error envelope and signal failure on the MCP wire."""

    # SDK v1 wraps typing.Dict outputs in {"result": ...}; native dict[str,
    # Any] outputs have a root object schema. Match the existing successful
    # result schema instead of letting explicit error results fail validation.
    wrapped_output = fn.__annotations__.get("return") == Dict[str, Any]

    def failure(envelope):
        structured = {"result": envelope} if wrapped_output else envelope
        return CallToolResult(isError=True, structuredContent=structured,
                              content=[TextContent(type="text", text=json.dumps(envelope))])

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        try:
            return await fn(*args, **kwargs)
        except (ToolError, SessionError, BrowserSessionError, QueueJournalError) as e:
            result = ex.error_envelope(type(e).__name__, str(e))
            for field in ("reason", "retry_after_seconds", "action_id", "queue_id"):
                value = getattr(e, field, None)
                if value is not None:
                    result["error"][field] = value
            diagnostics = _send_diagnostics(e)
            if diagnostics is not None:
                result["error"]["diagnostics"] = diagnostics
            if isinstance(e, BrowserActionError) and e.reason == "send_unconfirmed":
                result["error"]["recovery"] = (
                    "Do not resend. Keep this browser session open and inspect the intended "
                    "conversation with get_conversation. Reconcile this action ID only after "
                    "observing whether the exact message was sent."
                )
            if isinstance(e, BrowserSessionError) and getattr(e, "reason", None) == "session_expired":
                result["error"]["recovery"] = "Close the session and refresh cookies if needed. Restart the MCP server if its browser disconnected, then resume account actions."
            return failure(result)
        except Exception as e:  # noqa: BLE001 — the contract is: never crash
            logger.exception("MCP tool '%s' failed.", fn.__name__)
            return failure(ex.error_envelope(type(e).__name__, str(e)))

    return wrapper


def draft_response(draft: Draft) -> Dict[str, Any]:
    return ok_(
        draft_id=draft.draft_id,
        account=draft.account,
        action=draft.action,
        payload=draft.payload,
        preview=draft.preview,
        status=draft.status,
        message="Draft created, nothing was posted. Review, then call approve_draft(draft_id) to execute.",
    )


def dump_tweet(tweet) -> Dict[str, Any]:
    data = tweet.model_dump(mode="json", exclude={"raw_element_data"})
    data.pop("raw_element_data", None)
    return data


async def attach_search_images(envelope: Dict[str, Any], tweets) -> Any:
    """Attach the first photo of up to MAX_IMAGES_PER_SEARCH tweets.

    Bounded across the whole result set, not per tweet: a 50-tweet page with
    four photos each would be 200 downloads and a payload no client wants.
    The envelope's per-tweet ``media`` URLs + alt text are always present, so
    dropping images here never loses information.
    """
    images: List[Any] = []
    attempted = 0
    for tweet in tweets:
        if attempted >= MAX_IMAGES_PER_SEARCH:
            break
        if any(m.type == "image" for m in (getattr(tweet, "media", None) or [])):
            attempted += 1
            fetched = await asyncio.to_thread(images_for_tweet, tweet, 1)
            images.extend(fetched[: MAX_IMAGES_PER_SEARCH - len(images)])
    return with_images(envelope, images)


async def scrape_single_tweet(ctx: Ctx, account_id: str, tweet_url: str, tweet_id: str):
    """Read-only fetch of one tweet's content (for auto-reply generation)."""
    if uses_playwright(ctx):
        tweets = await browser_call(ctx, account_id, "get_tweet", tweet_url)
    else:
        async with ctx.session_pool.session(account_id) as browser_manager:
            scraper = await asyncio.to_thread(TweetScraper, browser_manager, account_id)
            tweets = await asyncio.to_thread(scraper.scrape_tweets_from_url, tweet_url, "tweet", 1)
    for tweet in tweets:
        if tweet.tweet_id == tweet_id and tweet.text_content:
            return tweet
    if not uses_playwright(ctx) and tweets and tweets[0].text_content:
        return tweets[0]
    raise ToolError(
        f"Could not load the content of tweet {tweet_id} for auto-reply. "
        "Pass explicit text instead of 'auto'."
    )


def register_tools(server, ctx: Ctx) -> None:
    """Register all x-use tools on the FastMCP server."""

    @server.tool(annotations=READ_ONLY_LOCAL)
    @guard
    async def list_accounts() -> Dict[str, Any]:
        """List configured accounts with secrets stripped (no cookies, no
        passwords, proxy credentials masked). Read-only, never starts a browser."""
        accounts = [ex.mask_account(a) for a in ctx.config_loader.get_accounts_config() if isinstance(a, dict)]
        return ok_(accounts=accounts, count=len(accounts))

    @server.tool(annotations=READ_ONLY_LOCAL)
    @guard
    async def get_metrics(account: str) -> Dict[str, Any]:
        """Read recorded metrics for an account (counters + recent events).
        Read-only, never starts a browser."""
        if not _SAFE_ACCOUNT_ID.fullmatch(account):
            raise ToolError(f"Invalid account id: {account!r}")
        summary_path = PROJECT_ROOT / "data" / "metrics" / f"{account}.json"
        events_path = PROJECT_ROOT / "logs" / "accounts" / f"{account}.jsonl"
        summary: Dict[str, Any] = {
            "account_id": account,
            "counters": {"posts": 0, "replies": 0, "retweets": 0, "quote_tweets": 0, "likes": 0, "errors": 0},
            "last_run_started_at": None,
            "last_run_finished_at": None,
        }
        warning: Optional[str] = None
        if summary_path.exists():
            try:
                loaded_summary = await asyncio.to_thread(read_json, summary_path)
            except Exception:
                raise ToolError(f"Metrics file for account '{account}' is unreadable.")
            if isinstance(loaded_summary, dict):
                summary = loaded_summary
            else:
                # Corrupt-but-valid JSON (e.g. a list): never return the wrong
                # shape verbatim under ok:true — the documented summary is a dict.
                warning = (
                    f"Metrics file for account '{account}' has an unexpected shape "
                    f"({type(loaded_summary).__name__}, expected an object). Returning default counters."
                )
                logger.warning("get_metrics: %s", warning)
        recent_events, truncated = await asyncio.to_thread(read_event_tail, events_path)
        envelope = ok_(account=account, summary=summary, recent_events=recent_events,
                       events_truncated=truncated)
        if warning is not None:
            envelope["warning"] = warning
        return envelope

    @server.tool(annotations=READ_ONLY_FROM_X)
    @guard
    async def search_tweets(keywords: str, limit: int = 10, account: Optional[str] = None,
                            include_images: bool = False) -> dict[str, Any]:
        """Search recent X posts for a query string. Read-only (draft mode
        does not apply). Reuses the account's warm browser session. `account`
        defaults to the first active configured account.
        `include_images=true` additionally attaches the first photo of up to
        5 tweets as image content (bounded); the per-tweet `media` URLs +
        alt text are always present."""
        account_id, _, _ = ex.resolve_account(ctx, account)
        limit = max(1, min(int(limit), 50))
        if uses_playwright(ctx):
            tweets = await browser_call(ctx, account_id, "search_tweets", keywords, limit)
        else:
            async with ctx.session_pool.session(account_id) as browser_manager:
                scraper = await asyncio.to_thread(TweetScraper, browser_manager, account_id)
                tweets = await asyncio.to_thread(scraper.scrape_tweets_by_keyword, keywords, limit)
        envelope = ok_(account=account_id, query=keywords, count=len(tweets),
                       tweets=[dump_tweet(t) for t in tweets])
        if not include_images:
            return envelope
        with image_fetch_scope(ctx, account_id) as transport:
            envelope["media_transport"] = transport
            return await attach_search_images(envelope, tweets)

    @server.tool(annotations=READ_ONLY_FROM_X)
    @guard
    async def search_profile(profile: str, limit: int = 10, account: Optional[str] = None,
                             include_images: bool = False) -> dict[str, Any]:
        """Read recent posts from ONE X profile. `profile` takes a handle
        ("@nasa" or "nasa"), a profile URL, or a tweet URL (which resolves to
        its author). Read-only (draft mode does not apply, nothing is
        posted), but it reuses the account's browser session. `account`
        defaults to the first active configured account.
        Use this to watch specific people and competitors, and to re-read one
        of your own published posts for its public counts; use search_tweets
        for topic and keyword discovery. Profile timelines include pinned
        posts and reposts, so check `user_handle` before treating a result as
        the profile owner's own writing.
        `include_images=true` additionally attaches the first photo of up to
        5 posts as image content (bounded); the per-post `media` URLs + alt
        text are always present."""
        account_id, _, _ = ex.resolve_account(ctx, account)
        handle = ex.profile_handle_from(profile)
        profile_url = f"https://x.com/{handle}"
        limit = max(1, min(int(limit), 50))
        if uses_playwright(ctx):
            tweets = await browser_call(ctx, account_id, "search_profile", handle, limit)
        else:
            async with ctx.session_pool.session(account_id) as browser_manager:
                scraper = await asyncio.to_thread(TweetScraper, browser_manager, account_id)
                tweets = await asyncio.to_thread(scraper.scrape_tweets_from_profile, profile_url, limit)
        envelope = ok_(account=account_id, profile=f"@{handle}", profile_url=profile_url,
                       count=len(tweets), tweets=[dump_tweet(t) for t in tweets])
        if not include_images:
            return envelope
        with image_fetch_scope(ctx, account_id) as transport:
            envelope["media_transport"] = transport
            return await attach_search_images(envelope, tweets)

    @server.tool(annotations=READ_ONLY_FROM_X)
    @guard
    async def get_tweet(account: str, tweet_url: str, include_images: bool = True) -> dict[str, Any]:
        """Read-only fetch of one tweet: text, author, public counts, typed
        media (photos + video posters) and the account's persona. With
        include_images=true (default) photos also attach as image content so
        a vision-capable client sees them; the envelope always carries URLs
        + alt text as the fallback. Nothing is posted; draft mode N/A."""
        account_id, _, model = ex.resolve_account(ctx, account)
        tweet_id = ex.tweet_id_from_url(tweet_url)
        if not tweet_id:
            raise ToolError(f"Could not parse a tweet id from URL: {tweet_url}")
        original = await scrape_single_tweet(ctx, account_id, tweet_url, tweet_id)
        handle = (original.user_handle or "").lstrip("@")
        envelope = ok_(
            account=account_id,
            tweet_id=tweet_id,
            tweet_url=tweet_url,
            author=f"@{handle}" if handle else None,
            text_content=original.text_content or "",
            like_count=original.like_count or 0,
            retweet_count=original.retweet_count or 0,
            reply_count=original.reply_count or 0,
            view_count=original.view_count or 0,
            media=media_envelope(original),
            persona=getattr(model, "persona", None),
        )
        if not include_images:
            return envelope
        with image_fetch_scope(ctx, account_id) as transport:
            envelope["media_transport"] = transport
            images = await asyncio.to_thread(images_for_tweet, original)
        return with_images(envelope, images)

    @server.tool(annotations=PUBLISHES_TO_X)
    @guard
    async def approve_draft(draft_id: str) -> Dict[str, Any]:
        """Execute a pending draft created by a write tool. Runs the exact
        same execution path as direct mode (pacing, dedup, metrics). A draft
        can be approved exactly once; unknown or consumed ids return an error."""
        try:
            draft = ctx.draft_store.get(draft_id)
        except KeyError:
            raise ToolError(f"Unknown draft_id '{draft_id}'.") from None
        if draft.status != "pending":
            raise ToolError(f"Draft '{draft_id}' is already {draft.status}, it cannot be (re-)approved.")

        def record_outcome(status, *, original_error=None, result=None):
            try:
                ctx.draft_store.set_status(draft_id, status)
            except Exception:
                if original_error is not None:
                    # Keep cancellation/error metadata, including the reserved
                    # action ID. The persisted approval remains uncertain.
                    logger.exception("Draft outcome persistence failed while handling an action failure.")
                    return
                error = ToolError("Action finished but its draft outcome could not be recorded. Inspect the draft and action ledger; do not repeat the write.")
                error.reason = "draft_journal_failure"
                if isinstance(result, dict):
                    error.action_id = result.get("action_id")
                raise error from None

        ctx.draft_store.set_status(draft_id, "approved")
        try:
            result = await actions.execute_draft(ctx, draft)
        except (Exception, asyncio.CancelledError) as e:
            # Crash-window self-heal: a dedup-duplicate rejection means this
            # exact action already executed (e.g. the server died after the
            # write landed but before the "executed" append) — the draft IS
            # executed, so don't mislabel it "failed".
            message = str(e)
            action_id = getattr(e, "action_id", None)
            if draft.action == "publish_thread":
                # The journal, not a generic dedup message, is authoritative for
                # a sequence that may have published only some of its parts.
                from .thread_tools import _store, thread_result
                try:
                    progress = thread_result(_store(ctx).get(draft.payload["run_id"]))
                    state = progress["state"]
                    status = ("executed" if state == "complete" else "uncertain" if state == "uncertain"
                              else "rejected" if state == "cancelled" else "partial" if progress["published"] else "pending")
                except Exception:
                    status = "uncertain"
                record_outcome(status, original_error=e)
            elif isinstance(e, ToolError) and ("dedup" in message or "Already" in message):
                record_outcome("executed", original_error=e)
            elif uses_playwright(ctx) and action_id:
                record = ctx.safety_store.reference(draft.account, action_id) if ctx.safety_store else None
                record_outcome("executed" if record and record["status"] == "succeeded" else "uncertain", original_error=e)
            elif uses_playwright(ctx):
                # Preflight/budget denial occurred before a write reservation.
                # Keep the exact reviewed draft available after recovery.
                record_outcome("pending", original_error=e)
            else:
                record_outcome("failed", original_error=e)
            raise
        if draft.action == "publish_thread":
            state = result["state"]
            status = ("executed" if state == "complete" else "uncertain" if state == "uncertain"
                      else "rejected" if state == "cancelled" else "partial" if result["published"] else "pending")
            record_outcome(status, result=result)
            return ok_(draft_id=draft_id, status=status, result=result)
        record_outcome("executed", result=result)
        return ok_(draft_id=draft_id, status="executed", result=result)

    # Write tools: post_tweet, generate_and_post, reply_to_tweet, engage,
    # run_cycle. Queue tools land in the queue task; account/support tools
    # register in their own tasks.
    from .accounts_tools import register_account_tools
    from .composite_tools import register_composite_tools
    from .engage import register_engage_tool
    from .prompts import register_prompts
    from .proxy_tools import register_proxy_tools
    from .queue_tools import register_queue_tools
    from .resources import register_resources
    from .support_tools import register_support_tools
    from .write_tools import register_write_tools
    from .browser_tools import register_browser_tools
    from .outreach_tools import register_outreach_tools
    from .workflow_tools import register_workflow_tools
    from .context_tools import register_context_tools
    from .thread_tools import register_thread_tools
    from .analytics_tools import register_analytics_tools
    from .notifications_tools import register_notifications_tools

    register_write_tools(server, ctx)
    register_engage_tool(server, ctx)
    register_queue_tools(server, ctx)
    register_account_tools(server, ctx)
    register_support_tools(server, ctx)
    register_composite_tools(server, ctx)
    register_proxy_tools(server, ctx)
    register_browser_tools(server, ctx)
    register_outreach_tools(server, ctx)
    register_workflow_tools(server, ctx)
    register_context_tools(server, ctx)
    register_thread_tools(server, ctx)
    register_analytics_tools(server, ctx)
    register_notifications_tools(server, ctx)
    # Non-tool surfaces: workflow prompts and read-only context resources.
    register_prompts(server, ctx)
    register_resources(server, ctx)
