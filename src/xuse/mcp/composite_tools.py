"""Composite API-tier tools: research_and_stage, draft_post_variations.

One call = a whole background-style workflow (search -> relevance -> generate
-> stage). Both require the server-side LLM (the 'llm' block / OPENAI_* env)
and both stage DRAFTS ONLY — the approval gate decides what reaches X.
"""
import asyncio
import logging
from typing import Any, Dict, List

from xuse.features.analyzer.heuristics import keyword_relevance_score
from xuse.features.scraper import TweetScraper

from . import actions, executor as ex
from .executor import Ctx, ToolError
from .tools import guard, ok_

logger = logging.getLogger(__name__)

MAX_STAGE_ITEMS = 10
MAX_VARIATIONS = 5
MIN_RELEVANCE = 0.5


def register_composite_tools(server, ctx: Ctx) -> None:
    """Register the composite (server-LLM) tools on the FastMCP server."""

    @server.tool()
    @guard
    async def research_and_stage(account: str, keywords: List[str],
                                 max_items: int = 3) -> Dict[str, Any]:
        """Search each keyword, keep relevant tweets (keyless heuristic), write
        a reply per tweet with the server-side LLM (persona-aware), and stage
        one reply DRAFT per tweet. Nothing is posted — review with list_drafts
        and approve selectively. Requires the 'llm' block in settings."""
        account_id, raw, model = ex.resolve_account(ctx, account)
        ex.require_active(raw, account_id)
        ex.require_llm(ctx)
        kws = [k.strip() for k in (keywords or []) if k and k.strip()]
        if not kws:
            raise ToolError("keywords must not be empty.")
        max_items = max(1, min(int(max_items), MAX_STAGE_ITEMS))
        seen: Dict[str, Any] = {}
        async with ctx.session_pool.session(account_id) as browser_manager:
            scraper = await asyncio.to_thread(TweetScraper, browser_manager, account_id)
            for kw in kws:
                tweets = await asyncio.to_thread(scraper.scrape_tweets_by_keyword, kw, 10)
                for tweet in tweets:
                    if tweet.tweet_id and tweet.tweet_id not in seen:
                        seen[tweet.tweet_id] = tweet
        considered = len(seen)
        relevant = [t for t in seen.values()
                    if keyword_relevance_score(t.text_content or "", kws) >= MIN_RELEVANCE]
        relevant.sort(key=lambda t: (t.like_count or 0), reverse=True)
        drafts: List[Dict[str, Any]] = []
        for tweet in relevant[:max_items]:
            text = await actions.generate_reply_text(ctx, account_id, tweet)
            tweet_url = (str(tweet.tweet_url) if tweet.tweet_url
                         else f"https://x.com/i/status/{tweet.tweet_id}")
            draft = ctx.draft_store.create(
                account=account_id,
                action="reply_to_tweet",
                payload={"tweet_url": tweet_url, "tweet_id": tweet.tweet_id,
                         "text": text, "text_content": tweet.text_content or ""},
                preview=f"Reply as @{account_id} to {tweet_url}: \"{text}\"",
            )
            handle = (tweet.user_handle or "").lstrip("@")
            drafts.append({"draft_id": draft.draft_id, "tweet_url": tweet_url,
                           "author": f"@{handle}" if handle else None, "text": text})
        return ok_(account=account_id, considered=considered,
                   skipped_irrelevant=considered - len(relevant), drafts=drafts,
                   message="Drafts staged — nothing posted. Review with list_drafts, "
                           "then approve_draft(draft_id).")

    @server.tool()
    @guard
    async def draft_post_variations(account: str, topic: str, count: int = 3) -> Dict[str, Any]:
        """Generate `count` distinct takes on a topic with the server-side LLM
        (persona-aware) and stage each as a post DRAFT. Nothing is posted —
        approve one, reject the rest. Requires the 'llm' block in settings."""
        account_id, raw, model = ex.resolve_account(ctx, account)
        ex.require_active(raw, account_id)
        ex.require_llm(ctx)
        if not topic or not topic.strip():
            raise ToolError("Topic must not be empty.")
        count = max(1, min(int(count), MAX_VARIATIONS))
        drafts: List[Dict[str, Any]] = []
        for n in range(1, count + 1):
            variant = (topic.strip() if n == 1
                       else f"{topic.strip()} (a distinctly different angle, take #{n})")
            text = await actions.generate_post_text(ctx, account_id, variant)
            draft = ctx.draft_store.create(
                account=account_id,
                action="post_tweet",
                payload={"text": text, "media": [], "community": None},
                preview=f"Post as @{account_id}: \"{text}\"",
            )
            drafts.append({"draft_id": draft.draft_id, "text": text})
        return ok_(account=account_id, topic=topic, drafts=drafts,
                   message="Drafts staged — nothing posted. Approve one with "
                           "approve_draft(draft_id); reject_draft the rest.")
