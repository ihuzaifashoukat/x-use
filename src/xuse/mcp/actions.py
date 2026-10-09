"""Browser/LLM action executors for the x-use MCP tools.

One executor per write action, used by direct mode AND ``approve_draft`` so
both paths share pacing, dedup, and metrics. Each is a thin adapter over the
existing engine modules (publisher, engagement, content generator) — no
Selenium logic here. Blocking calls go through ``asyncio.to_thread``; the
existing async facades are awaited directly, matching the orchestrator.
"""
import hashlib
import logging
from typing import Any, Dict, List, Optional

from xuse.features.engagement import TweetEngagement
from xuse.features.publisher import TweetPublisher
from xuse.features.publisher.content_generator import generate_post_text_if_needed
from xuse.models import ScrapedTweet, TweetContent

from . import executor as ex
from .executor import Ctx, ToolError
from .browser_bridge import browser_call, uses_playwright

logger = logging.getLogger(__name__)


async def _browser_write(ctx, account_id, operation, kind, dedup_key, *args, **kwargs):
    result = await browser_call(ctx, account_id, operation, *args, kind=kind, **kwargs)
    try:
        ex.mark_processed(ctx, dedup_key)
    except Exception:
        # The durable browser ledger already prevents a duplicate. Bookkeeping
        # failure must not turn a confirmed external action into a retry.
        logger.warning("Legacy dedup recording failed after a confirmed browser action.")
    try:
        metrics = ex.metrics_for(ctx, account_id)
        metrics.log_event(kind, "success", {"source": "mcp", "backend": ctx.session_pool.backend})
        metrics.increment({"post": "posts", "reply": "replies", "retweet": "retweets", "like": "likes"}.get(kind, kind))
    except Exception:
        logger.warning("Metrics recording failed after a confirmed browser action.")
    return {"account": account_id, "action": operation, **result}


async def exec_post(
    ctx: Ctx,
    account_id: str,
    text: str,
    media: Optional[List[str]] = None,
    community: Optional[str] = None,
    action_context=None,
    media_manifest=None,
) -> Dict[str, Any]:
    account_id, raw, model = ex.resolve_account(ctx, account_id)
    ex.require_active(raw, account_id)
    action_config = ex.current_action_config(ctx, model)
    if community:
        model = model.model_copy(update={"post_to_community": True, "community_id": community})
    # Same text with different media or community is NOT the same post — the
    # key discriminates on the full payload. Keep byte-identical with
    # queue_tools.queue_post so the enqueue gate and this executor agree.
    key_material = (text or "") + "|" + str(media or []) + "|" + str(community)
    dedup_key = f"post_{account_id}_{hashlib.sha1(key_material.encode('utf-8')).hexdigest()[:12]}"
    if ex.is_processed(ctx, dedup_key):
        raise ToolError("An identical post was already executed for this account (dedup).")
    if uses_playwright(ctx):
        options = {"media_manifest": media_manifest} if media_manifest is not None else {}
        return await _browser_write(ctx, account_id, "post", "post", dedup_key, text,
                                    media=media, community=community, action_context=action_context, **options)
    content = TweetContent(text=text, local_media_paths=list(media) if media else None)
    await ex.pace(ctx, account_id, action_config)
    await ex.mark_action_now(ctx, account_id)  # pace attempts, not just successes
    async with ctx.session_pool.session(account_id) as browser_manager:
        publisher = TweetPublisher(browser_manager, ex.get_llm(ctx), model)
        # llm_settings=None → text posts verbatim (no re-generation of reviewed drafts)
        success = await publisher.post_new_tweet(content, llm_settings=None)
    if success:
        # Dedup is the crash-safety net: persist it BEFORE any metrics I/O, so
        # a metrics failure can never strand a live post without its key.
        ex.mark_processed(ctx, dedup_key)
    try:
        metrics = ex.metrics_for(ctx, account_id)
        metrics.log_event("post", "success" if success else "failure", {"source": "mcp"})
        metrics.increment("posts" if success else "errors")
    except Exception:
        # Metrics must never masquerade as an action failure.
        logger.exception("[mcp] metrics recording failed for account '%s'; the post outcome is unaffected.", account_id)
    if not success:
        raise ToolError("Post failed — see the account event log for details.")
    return {"account": account_id, "action": "post_tweet", "success": True}


async def exec_reply(
    ctx: Ctx,
    account_id: str,
    tweet_url: str,
    reply_text: str,
    tweet_id: Optional[str] = None,
    text_content: str = "",
    action_context=None,
    media: Optional[List[str]] = None,
    media_manifest=None,
) -> Dict[str, Any]:
    account_id, raw, model = ex.resolve_account(ctx, account_id)
    ex.require_active(raw, account_id)
    action_config = ex.current_action_config(ctx, model)
    tweet_id = tweet_id or ex.tweet_id_from_url(tweet_url)
    if not tweet_id:
        raise ToolError(f"Could not parse a tweet id from URL: {tweet_url}")
    if not reply_text or not reply_text.strip():
        raise ToolError("Reply text must not be empty.")
    dedup_key = f"reply_{account_id}_{tweet_id}"  # same key format as the orchestrator
    if ex.is_processed(ctx, dedup_key):
        raise ToolError(f"Already replied to tweet {tweet_id} from this account (dedup).")
    if uses_playwright(ctx):
        options = {"media": media} if media else {}
        if media_manifest is not None:
            options["media_manifest"] = media_manifest
        return await _browser_write(ctx, account_id, "reply", "reply", dedup_key, tweet_url, reply_text,
                                    action_context=action_context, **options)
    if media:
        raise ToolError("Reply attachments require the patchright or playwright browser backend.")
    tweet = ScrapedTweet(tweet_id=tweet_id, tweet_url=tweet_url, text_content=text_content or "")
    await ex.pace(ctx, account_id, action_config)
    await ex.mark_action_now(ctx, account_id)  # pace attempts, not just successes
    async with ctx.session_pool.session(account_id) as browser_manager:
        publisher = TweetPublisher(browser_manager, ex.get_llm(ctx), model)
        success = await publisher.reply_to_tweet(tweet, reply_text[:ex.MAX_REPLY_CHARS])
    if success:
        # Dedup first (crash-safety net), metrics second — see exec_post.
        ex.mark_processed(ctx, dedup_key)
    try:
        metrics = ex.metrics_for(ctx, account_id)
        metrics.log_event("reply", "success" if success else "failure", {"tweet_id": tweet_id, "source": "mcp"})
        metrics.increment("replies" if success else "errors")
    except Exception:
        logger.exception("[mcp] metrics recording failed for account '%s'; the reply outcome is unaffected.", account_id)
    if not success:
        raise ToolError(f"Reply to tweet {tweet_id} failed — see the account event log.")
    return {"account": account_id, "action": "reply_to_tweet", "tweet_id": tweet_id, "success": True}


async def exec_like(ctx: Ctx, account_id: str, tweet_id: str, tweet_url: Optional[str], *, action_context=None) -> Dict[str, Any]:
    account_id, raw, model = ex.resolve_account(ctx, account_id)
    ex.require_active(raw, account_id)
    action_config = ex.current_action_config(ctx, model)
    dedup_key = f"like_{account_id}_{tweet_id}"
    if ex.is_processed(ctx, dedup_key):
        raise ToolError(f"Already liked tweet {tweet_id} from this account (dedup).")
    if uses_playwright(ctx):
        return await _browser_write(ctx, account_id, "like", "like", dedup_key,
                                    tweet_url or f"https://x.com/i/status/{tweet_id}", action_context=action_context)
    await ex.pace(ctx, account_id, action_config)
    await ex.mark_action_now(ctx, account_id)  # pace attempts, not just successes
    async with ctx.session_pool.session(account_id) as browser_manager:
        engagement = TweetEngagement(browser_manager, model)
        success = await engagement.like_tweet(tweet_id=tweet_id, tweet_url=tweet_url)
    if success:
        # Dedup first (crash-safety net), metrics second — see exec_post.
        ex.mark_processed(ctx, dedup_key)
    try:
        metrics = ex.metrics_for(ctx, account_id)
        metrics.log_event("like", "success" if success else "failure", {"tweet_id": tweet_id, "source": "mcp"})
        metrics.increment("likes" if success else "errors")
    except Exception:
        logger.exception("[mcp] metrics recording failed for account '%s'; the like outcome is unaffected.", account_id)
    if not success:
        raise ToolError(f"Like on tweet {tweet_id} failed — see the account event log.")
    return {"account": account_id, "action": "like", "tweet_id": tweet_id, "success": True}


async def exec_retweet(ctx: Ctx, account_id: str, tweet_id: str, tweet_url: Optional[str],
                       text_content: str = "", *, action_context=None) -> Dict[str, Any]:
    account_id, raw, model = ex.resolve_account(ctx, account_id)
    ex.require_active(raw, account_id)
    action_config = ex.current_action_config(ctx, model)
    dedup_key = f"retweet_{account_id}_{tweet_id}"
    if ex.is_processed(ctx, dedup_key):
        raise ToolError(f"Already retweeted tweet {tweet_id} from this account (dedup).")
    if uses_playwright(ctx):
        return await _browser_write(ctx, account_id, "retweet", "retweet", dedup_key,
                                    tweet_url or f"https://x.com/i/status/{tweet_id}", action_context=action_context)
    tweet = ScrapedTweet(tweet_id=tweet_id, tweet_url=tweet_url, text_content=text_content or "")
    await ex.pace(ctx, account_id, action_config)
    await ex.mark_action_now(ctx, account_id)  # pace attempts, not just successes
    async with ctx.session_pool.session(account_id) as browser_manager:
        publisher = TweetPublisher(browser_manager, ex.get_llm(ctx), model)
        success = await publisher.retweet_tweet(tweet)
    if success:
        # Dedup first (crash-safety net), metrics second — see exec_post.
        ex.mark_processed(ctx, dedup_key)
    try:
        metrics = ex.metrics_for(ctx, account_id)
        metrics.log_event("retweet", "success" if success else "failure", {"tweet_id": tweet_id, "source": "mcp"})
        metrics.increment("retweets" if success else "errors")
    except Exception:
        logger.exception("[mcp] metrics recording failed for account '%s'; the retweet outcome is unaffected.", account_id)
    if not success:
        raise ToolError(f"Retweet of tweet {tweet_id} failed — see the account event log.")
    return {"account": account_id, "action": "retweet", "tweet_id": tweet_id, "success": True}


# ---------------------------------------------------------------------------
# LLM-backed text generation (used to build draft payloads so the human
# reviews the actual content before anything executes)
# ---------------------------------------------------------------------------


async def generate_post_text(ctx: Ctx, account_id: str, topic: str) -> str:
    account_id, _, model = ex.resolve_account(ctx, account_id)
    action_config = ex.current_action_config(ctx, model)
    settings = ex.llm_settings_for(model, action_config, "post")
    service = ex.require_llm(ctx)
    persona_note = (f"Write in this account persona:\n{model.persona}\n\n"
                    if getattr(model, "persona", None) else "")
    prompt = f"{persona_note}Write an engaging X (Twitter) post about: {topic}"
    text = await generate_post_text_if_needed(prompt, settings, service)
    if not text or not text.strip():
        raise ToolError("LLM returned no usable post text.")
    # Brand-safety guard: generate_post_text_if_needed falls back to returning
    # the clamped PROMPT when every generation attempt fails — never let an
    # instruction string through as post content.
    if text.strip() == prompt.strip():
        raise ToolError("LLM generation failed (provider unreachable or returned no text).")
    return text.strip()


async def generate_reply_text(ctx: Ctx, account_id: str, original: ScrapedTweet) -> str:
    account_id, _, model = ex.resolve_account(ctx, account_id)
    action_config = ex.current_action_config(ctx, model)
    settings = ex.llm_settings_for(model, action_config, "reply")
    service = ex.require_llm(ctx)
    # Same reply prompt pattern as the orchestrator's keyword-reply pipeline.
    # Scraper handles arrive @-prefixed; normalize so the prompt never shows "@@".
    handle = (original.user_handle or "").lstrip("@") or "user"
    persona_note = (f"Write in this account persona:\n{model.persona}\n\n"
                    if getattr(model, "persona", None) else "")
    prompt = (
        f"{persona_note}"
        f"Write a concise, natural reply under {ex.MAX_REPLY_CHARS} characters. This is a standalone tweet. "
        "Avoid hashtags, links, and emojis unless essential. One short paragraph.\n\n"
        f"Original tweet by @{handle}:\n"
        f"\"{original.text_content}\"\n\nYour reply:"
    )
    text = await service.generate_text(
        prompt=prompt,
        service_preference=settings.service_preference,
        model_name=settings.model_name_override,
        max_tokens=settings.max_tokens,
        temperature=settings.temperature,
    )
    text = (text or "")[:ex.MAX_REPLY_CHARS].rstrip()
    if not text:
        raise ToolError("LLM returned no usable reply text.")
    return text


# ---------------------------------------------------------------------------
# Draft dispatch (approve_draft path)
# ---------------------------------------------------------------------------


async def execute_draft(ctx: Ctx, draft) -> Dict[str, Any]:
    """Execute an approved draft via the same executors as direct mode."""
    payload = draft.payload
    context = {"draft_id": draft.draft_id}
    if draft.action == "publish_thread":
        from .thread_tools import execute_thread_draft
        return await execute_thread_draft(ctx, draft)
    if draft.action in ("send_message", "follow_profile"):
        from .browser_tools import execute_browser_draft
        return await execute_browser_draft(ctx, draft)
    if draft.action in ("post_tweet", "generate_and_post"):
        return await exec_post(
            ctx, draft.account,
            text=payload.get("text", ""),
            media=payload.get("media") or None,
            community=payload.get("community"),
            action_context=context,
        )
    if draft.action == "reply_to_tweet":
        return await exec_reply(
            ctx, draft.account,
            tweet_url=payload.get("tweet_url", ""),
            reply_text=payload.get("text", ""),
            tweet_id=payload.get("tweet_id"),
            text_content=payload.get("text_content", ""),
            action_context=context,
            **({"media": payload["media"]} if payload.get("media") else {}),
        )
    if draft.action == "engage_like":
        return await exec_like(ctx, draft.account, payload["tweet_id"], payload.get("tweet_url"), action_context=context)
    if draft.action == "engage_retweet":
        return await exec_retweet(
            ctx, draft.account, payload["tweet_id"], payload.get("tweet_url"),
            text_content=payload.get("text_content", ""),
            action_context=context,
        )
    raise ToolError(f"Unsupported draft action '{draft.action}'.")
