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

logger = logging.getLogger(__name__)


async def exec_post(
    ctx: Ctx,
    account_id: str,
    text: str,
    media: Optional[List[str]] = None,
    community: Optional[str] = None,
) -> Dict[str, Any]:
    account_id, raw, model = ex.resolve_account(ctx, account_id)
    ex.require_active(raw, account_id)
    action_config = ex.current_action_config(ctx, model)
    if community:
        model = model.model_copy(update={"post_to_community": True, "community_id": community})
    dedup_key = f"post_{account_id}_{hashlib.sha1((text or '').encode('utf-8')).hexdigest()[:12]}"
    if ex.is_processed(ctx, dedup_key):
        raise ToolError("An identical post was already executed for this account (dedup).")
    content = TweetContent(text=text, local_media_paths=list(media) if media else None)
    await ex.pace(ctx, account_id, action_config)
    async with ctx.session_pool.session(account_id) as browser_manager:
        publisher = TweetPublisher(browser_manager, ex.get_llm(ctx), model)
        # llm_settings=None → text posts verbatim (no re-generation of reviewed drafts)
        success = await publisher.post_new_tweet(content, llm_settings=None)
    metrics = ex.metrics_for(ctx, account_id)
    metrics.log_event("post", "success" if success else "failure", {"source": "mcp"})
    if not success:
        metrics.increment("errors")
        raise ToolError("Post failed — see the account event log for details.")
    metrics.increment("posts")
    ex.mark_processed(ctx, dedup_key)
    ex.mark_action_now(ctx, account_id)
    return {"account": account_id, "action": "post_tweet", "success": True}


async def exec_reply(
    ctx: Ctx,
    account_id: str,
    tweet_url: str,
    reply_text: str,
    tweet_id: Optional[str] = None,
    text_content: str = "",
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
    tweet = ScrapedTweet(tweet_id=tweet_id, tweet_url=tweet_url, text_content=text_content or "")
    await ex.pace(ctx, account_id, action_config)
    async with ctx.session_pool.session(account_id) as browser_manager:
        publisher = TweetPublisher(browser_manager, ex.get_llm(ctx), model)
        success = await publisher.reply_to_tweet(tweet, reply_text[:ex.MAX_REPLY_CHARS])
    metrics = ex.metrics_for(ctx, account_id)
    metrics.log_event("reply", "success" if success else "failure", {"tweet_id": tweet_id, "source": "mcp"})
    if not success:
        metrics.increment("errors")
        raise ToolError(f"Reply to tweet {tweet_id} failed — see the account event log.")
    metrics.increment("replies")
    ex.mark_processed(ctx, dedup_key)
    ex.mark_action_now(ctx, account_id)
    return {"account": account_id, "action": "reply_to_tweet", "tweet_id": tweet_id, "success": True}


async def exec_like(ctx: Ctx, account_id: str, tweet_id: str, tweet_url: Optional[str]) -> Dict[str, Any]:
    account_id, raw, model = ex.resolve_account(ctx, account_id)
    ex.require_active(raw, account_id)
    action_config = ex.current_action_config(ctx, model)
    dedup_key = f"like_{account_id}_{tweet_id}"
    if ex.is_processed(ctx, dedup_key):
        raise ToolError(f"Already liked tweet {tweet_id} from this account (dedup).")
    await ex.pace(ctx, account_id, action_config)
    async with ctx.session_pool.session(account_id) as browser_manager:
        engagement = TweetEngagement(browser_manager, model)
        success = await engagement.like_tweet(tweet_id=tweet_id, tweet_url=tweet_url)
    metrics = ex.metrics_for(ctx, account_id)
    metrics.log_event("like", "success" if success else "failure", {"tweet_id": tweet_id, "source": "mcp"})
    if not success:
        metrics.increment("errors")
        raise ToolError(f"Like on tweet {tweet_id} failed — see the account event log.")
    metrics.increment("likes")
    ex.mark_processed(ctx, dedup_key)
    ex.mark_action_now(ctx, account_id)
    return {"account": account_id, "action": "like", "tweet_id": tweet_id, "success": True}


async def exec_retweet(ctx: Ctx, account_id: str, tweet_id: str, tweet_url: Optional[str],
                       text_content: str = "") -> Dict[str, Any]:
    account_id, raw, model = ex.resolve_account(ctx, account_id)
    ex.require_active(raw, account_id)
    action_config = ex.current_action_config(ctx, model)
    dedup_key = f"retweet_{account_id}_{tweet_id}"
    if ex.is_processed(ctx, dedup_key):
        raise ToolError(f"Already retweeted tweet {tweet_id} from this account (dedup).")
    tweet = ScrapedTweet(tweet_id=tweet_id, tweet_url=tweet_url, text_content=text_content or "")
    await ex.pace(ctx, account_id, action_config)
    async with ctx.session_pool.session(account_id) as browser_manager:
        publisher = TweetPublisher(browser_manager, ex.get_llm(ctx), model)
        success = await publisher.retweet_tweet(tweet)
    metrics = ex.metrics_for(ctx, account_id)
    metrics.log_event("retweet", "success" if success else "failure", {"tweet_id": tweet_id, "source": "mcp"})
    if not success:
        metrics.increment("errors")
        raise ToolError(f"Retweet of tweet {tweet_id} failed — see the account event log.")
    metrics.increment("retweets")
    ex.mark_processed(ctx, dedup_key)
    ex.mark_action_now(ctx, account_id)
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
    prompt = f"Write an engaging X (Twitter) post about: {topic}"
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
    prompt = (
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
    if draft.action in ("post_tweet", "generate_and_post"):
        return await exec_post(
            ctx, draft.account,
            text=payload.get("text", ""),
            media=payload.get("media") or None,
            community=payload.get("community"),
        )
    if draft.action == "reply_to_tweet":
        return await exec_reply(
            ctx, draft.account,
            tweet_url=payload.get("tweet_url", ""),
            reply_text=payload.get("text", ""),
            tweet_id=payload.get("tweet_id"),
            text_content=payload.get("text_content", ""),
        )
    if draft.action == "engage_like":
        return await exec_like(ctx, draft.account, payload["tweet_id"], payload.get("tweet_url"))
    if draft.action == "engage_retweet":
        return await exec_retweet(
            ctx, draft.account, payload["tweet_id"], payload.get("tweet_url"),
            text_content=payload.get("text_content", ""),
        )
    raise ToolError(f"Unsupported draft action '{draft.action}'.")
