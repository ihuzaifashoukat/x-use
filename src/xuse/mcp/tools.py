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

from xuse.core.config_loader import PROJECT_ROOT
from xuse.features.scraper import TweetScraper
from xuse.mcp.media import (MAX_IMAGES_PER_SEARCH, images_for_tweet,
                            media_envelope, with_images)

from . import actions, executor as ex
from .drafts import Draft
from .executor import Ctx, ToolError
from .sessions import SessionError

logger = logging.getLogger(__name__)

# Account ids are interpolated into filesystem paths; restrict to a charset
# that cannot traverse (no dots, slashes, or backslashes).
_SAFE_ACCOUNT_ID = re.compile(r"[A-Za-z0-9_-]+")


def ok_(**fields: Any) -> Dict[str, Any]:
    return {"ok": True, **fields}


def guard(fn):
    """Convert any tool failure into the structured error envelope (NFR-1)."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        try:
            return await fn(*args, **kwargs)
        except (ToolError, SessionError) as e:
            return ex.error_envelope(type(e).__name__, str(e))
        except Exception as e:  # noqa: BLE001 — the contract is: never crash
            logger.exception("MCP tool '%s' failed.", fn.__name__)
            return ex.error_envelope(type(e).__name__, str(e))

    return wrapper


def draft_response(draft: Draft) -> Dict[str, Any]:
    return ok_(
        draft_id=draft.draft_id,
        account=draft.account,
        action=draft.action,
        payload=draft.payload,
        preview=draft.preview,
        status=draft.status,
        message="Draft created — nothing was posted. Review, then call approve_draft(draft_id) to execute.",
    )


def dump_tweet(tweet) -> Dict[str, Any]:
    data = tweet.model_dump(mode="json")
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
    for tweet in tweets:
        if len(images) >= MAX_IMAGES_PER_SEARCH:
            break
        if any(m.type == "image" for m in (getattr(tweet, "media", None) or [])):
            fetched = await asyncio.to_thread(images_for_tweet, tweet, 1)
            images.extend(fetched[: MAX_IMAGES_PER_SEARCH - len(images)])
    return with_images(envelope, images)


async def scrape_single_tweet(ctx: Ctx, account_id: str, tweet_url: str, tweet_id: str):
    """Read-only fetch of one tweet's content (for auto-reply generation)."""
    async with ctx.session_pool.session(account_id) as browser_manager:
        scraper = await asyncio.to_thread(TweetScraper, browser_manager, account_id)
        tweets = await asyncio.to_thread(scraper.scrape_tweets_from_url, tweet_url, "tweet", 1)
    for tweet in tweets:
        if tweet.tweet_id == tweet_id and tweet.text_content:
            return tweet
    if tweets and tweets[0].text_content:
        return tweets[0]
    raise ToolError(
        f"Could not load the content of tweet {tweet_id} for auto-reply. "
        "Pass explicit text instead of 'auto'."
    )


def register_tools(server, ctx: Ctx) -> None:
    """Register all x-use tools on the FastMCP server."""

    @server.tool()
    @guard
    async def list_accounts() -> Dict[str, Any]:
        """List configured accounts with secrets stripped (no cookies, no
        passwords, proxy credentials masked). Read-only — never starts a browser."""
        accounts = [ex.mask_account(a) for a in ctx.config_loader.get_accounts_config() if isinstance(a, dict)]
        return ok_(accounts=accounts, count=len(accounts))

    @server.tool()
    @guard
    async def get_metrics(account: str) -> Dict[str, Any]:
        """Read recorded metrics for an account (counters + recent events).
        Read-only — never starts a browser."""
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
                loaded_summary = json.loads(summary_path.read_text(encoding="utf-8"))
            except Exception:
                raise ToolError(f"Metrics file for account '{account}' is unreadable.")
            if isinstance(loaded_summary, dict):
                summary = loaded_summary
            else:
                # Corrupt-but-valid JSON (e.g. a list): never return the wrong
                # shape verbatim under ok:true — the documented summary is a dict.
                warning = (
                    f"Metrics file for account '{account}' has an unexpected shape "
                    f"({type(loaded_summary).__name__}, expected an object) — returning default counters."
                )
                logger.warning("get_metrics: %s", warning)
        recent_events: List[Dict[str, Any]] = []
        if events_path.exists():
            lines = [ln for ln in events_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
            for line in lines[-20:]:
                try:
                    recent_events.append(json.loads(line))
                except Exception:
                    continue
        envelope = ok_(account=account, summary=summary, recent_events=recent_events)
        if warning is not None:
            envelope["warning"] = warning
        return envelope

    @server.tool()
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
        async with ctx.session_pool.session(account_id) as browser_manager:
            scraper = await asyncio.to_thread(TweetScraper, browser_manager, account_id)
            tweets = await asyncio.to_thread(scraper.scrape_tweets_by_keyword, keywords, limit)
        envelope = ok_(account=account_id, query=keywords, count=len(tweets),
                       tweets=[dump_tweet(t) for t in tweets])
        if not include_images:
            return envelope
        return await attach_search_images(envelope, tweets)

    @server.tool()
    @guard
    async def search_profile(profile: str, limit: int = 10, account: Optional[str] = None,
                             include_images: bool = False) -> dict[str, Any]:
        """Read recent posts from ONE X profile. `profile` takes a handle
        ("@nasa" or "nasa"), a profile URL, or a tweet URL (which resolves to
        its author). Read-only (draft mode does not apply — nothing is
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
        async with ctx.session_pool.session(account_id) as browser_manager:
            scraper = await asyncio.to_thread(TweetScraper, browser_manager, account_id)
            tweets = await asyncio.to_thread(
                scraper.scrape_tweets_from_profile, profile_url, limit)
        envelope = ok_(account=account_id, profile=f"@{handle}", profile_url=profile_url,
                       count=len(tweets), tweets=[dump_tweet(t) for t in tweets])
        if not include_images:
            return envelope
        return await attach_search_images(envelope, tweets)

    @server.tool()
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
        images = await asyncio.to_thread(images_for_tweet, original)
        return with_images(envelope, images)

    @server.tool()
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
            raise ToolError(f"Draft '{draft_id}' is already {draft.status} — it cannot be (re-)approved.")
        ctx.draft_store.set_status(draft_id, "approved")
        try:
            result = await actions.execute_draft(ctx, draft)
        except Exception as e:
            # Crash-window self-heal: a dedup-duplicate rejection means this
            # exact action already executed (e.g. the server died after the
            # write landed but before the "executed" append) — the draft IS
            # executed, so don't mislabel it "failed".
            message = str(e)
            if isinstance(e, ToolError) and ("dedup" in message or "Already" in message):
                ctx.draft_store.set_status(draft_id, "executed")
            else:
                ctx.draft_store.set_status(draft_id, "failed")
            raise
        ctx.draft_store.set_status(draft_id, "executed")
        return ok_(draft_id=draft_id, status="executed", result=result)

    # Write tools: post_tweet, generate_and_post, reply_to_tweet, engage,
    # run_cycle. Queue tools land in the queue task; account/support tools
    # register in their own tasks.
    from .accounts_tools import register_account_tools
    from .composite_tools import register_composite_tools
    from .engage import register_engage_tool
    from .proxy_tools import register_proxy_tools
    from .queue_tools import register_queue_tools
    from .support_tools import register_support_tools
    from .write_tools import register_write_tools

    register_write_tools(server, ctx)
    register_engage_tool(server, ctx)
    register_queue_tools(server, ctx)
    register_account_tools(server, ctx)
    register_support_tools(server, ctx)
    register_composite_tools(server, ctx)
    register_proxy_tools(server, ctx)
