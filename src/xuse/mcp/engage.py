"""The ``engage`` MCP tool: keyword-search engagement (like/retweet) gated
by the analyzer's relevance filter.

Every candidate tweet passes the same relevance gate as the orchestrator's
likes pipeline (per-account overrides win), per-run caps come from the
account's ActionConfig, and dedup keys match batch-run formats. In draft
mode (default) each planned action becomes an individually approvable draft
and nothing executes.
"""
import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

from xuse.features.analyzer import TweetAnalyzer
from xuse.features.scraper import TweetScraper
from xuse.orchestrator import TwitterOrchestrator

from . import actions as action_executors
from . import executor as ex
from .executor import Ctx, ToolError
from .sessions import SessionError
from .tools import guard, ok_

logger = logging.getLogger(__name__)

_ENGAGE_ACTIONS = ("like", "retweet")


async def _relevance_passes(ctx: Ctx, analyzer: TweetAnalyzer, account, tweet) -> bool:
    """Analyzer gate for engagement actions (mirrors the orchestrator's
    likes-pipeline relevance filter, per-account overrides win)."""
    analysis_config = ctx.config_loader.get_setting("twitter_automation.analysis_config", {}) or {}
    acc_ac = account.action_config
    enabled = (
        acc_ac.enable_relevance_filter_likes
        if (acc_ac and acc_ac.enable_relevance_filter_likes is not None)
        else analysis_config.get("enable_relevance_filter", {}).get("likes", True)
    )
    if not enabled:
        return True
    threshold = (
        acc_ac.relevance_threshold_likes
        if (acc_ac and acc_ac.relevance_threshold_likes is not None)
        else float(analysis_config.get("thresholds", {}).get("likes_min", 0.3))
    )
    try:
        score = await analyzer.score_relevance(tweet, keywords=account.target_keywords)
    except Exception:
        logger.warning("Relevance scoring failed; treating as below threshold.", exc_info=True)
        return False
    return score >= threshold


def register_engage_tool(server, ctx: Ctx) -> None:
    """Register the engage tool on the FastMCP server."""

    @server.tool()
    @guard
    async def engage(
        account: str,
        keywords: List[str],
        actions: Optional[List[str]] = None,
        max_actions: int = 5,
    ) -> Dict[str, Any]:
        """Engage with tweets found via keyword search. `actions` is a subset
        of ["like", "retweet"] (default ["like"]); every candidate passes the
        analyzer relevance gate; `max_actions` is hard-capped by the account's
        per-run config caps. In draft mode (default) each planned action
        becomes an individually approvable draft and nothing executes."""
        account_id, _, model = ex.resolve_account(ctx, account)
        action_config = ex.current_action_config(ctx, model)
        requested = actions or ["like"]
        requested = [a.strip().lower() for a in requested if a and a.strip()]
        invalid = [a for a in requested if a not in _ENGAGE_ACTIONS]
        if invalid:
            raise ToolError(f"Unsupported engage action(s): {invalid}. Allowed: {list(_ENGAGE_ACTIONS)}.")
        if not requested:
            raise ToolError("At least one engage action is required.")
        if not keywords:
            raise ToolError("At least one keyword is required.")
        max_actions = max(1, int(max_actions))
        quotas = {
            "like": min(max_actions, action_config.max_likes_per_run),
            "retweet": min(max_actions, action_config.max_retweets_per_keyword_run),
        }
        analyzer = TweetAnalyzer(ex.get_llm(ctx), account_config=model)
        planned: List[Dict[str, Any]] = []
        async with ctx.session_pool.session(account_id) as browser_manager:
            scraper = await asyncio.to_thread(TweetScraper, browser_manager, account_id)
            for keyword in keywords:
                if all(quotas[a] <= 0 for a in requested):
                    break
                tweets = await asyncio.to_thread(
                    scraper.scrape_tweets_by_keyword, keyword, max(5, max_actions * 2)
                )
                for tweet in tweets:
                    if all(quotas[a] <= 0 for a in requested):
                        break
                    if tweet.user_handle and TwitterOrchestrator._is_own_tweet(
                        tweet.user_handle, model, browser_manager
                    ):
                        continue
                    if not await _relevance_passes(ctx, analyzer, model, tweet):
                        continue
                    for action in requested:
                        if quotas[action] <= 0:
                            continue
                        if ex.is_processed(ctx, f"{action}_{account_id}_{tweet.tweet_id}"):
                            continue
                        planned.append({"action": action, "tweet": tweet, "keyword": keyword})
                        quotas[action] -= 1
                        break
        if ctx.draft_mode:
            drafts = []
            for item in planned:
                tweet = item["tweet"]
                verb = "Like" if item["action"] == "like" else "Retweet"
                draft = ctx.draft_store.create(
                    account=account_id,
                    action=f"engage_{item['action']}",
                    payload={
                        "tweet_id": tweet.tweet_id,
                        "tweet_url": str(tweet.tweet_url) if tweet.tweet_url else None,
                        "text_content": (tweet.text_content or "")[:500],
                        "keyword": item["keyword"],
                    },
                    preview=(
                        f"{verb} as @{account_id} — tweet {tweet.tweet_id} by "
                        f"@{(tweet.user_handle or '').lstrip('@') or 'user'}: \"{(tweet.text_content or '')[:100]}\""
                    ),
                )
                drafts.append(json.loads(draft.model_dump_json()))
            return ok_(
                account=account_id,
                draft_mode=True,
                count=len(drafts),
                drafts=drafts,
                message="Nothing executed. Approve individual drafts with approve_draft(draft_id).",
            )
        results = []
        for item in planned:
            tweet = item["tweet"]
            url = str(tweet.tweet_url) if tweet.tweet_url else None
            try:
                if item["action"] == "like":
                    result = await action_executors.exec_like(ctx, account_id, tweet.tweet_id, url)
                else:
                    result = await action_executors.exec_retweet(ctx, account_id, tweet.tweet_id, url, tweet.text_content or "")
                results.append(result)
            except (ToolError, SessionError) as e:
                results.append({"account": account_id, "action": item["action"],
                                "tweet_id": tweet.tweet_id, "success": False, "error": ex.sanitize_text(e)})
            except Exception as e:
                # An unexpected executor failure must not hide the actions
                # that already executed — record it per action and continue.
                logger.error(
                    "Unexpected error executing %s on tweet %s: %s",
                    item["action"], tweet.tweet_id, e, exc_info=True,
                )
                results.append({"account": account_id, "action": item["action"],
                                "tweet_id": tweet.tweet_id, "success": False,
                                "error": f"unexpected: {ex.sanitize_text(e)}"})
        succeeded = sum(1 for r in results if r.get("success"))
        return ok_(account=account_id, draft_mode=False, executed=results, succeeded=succeeded, attempted=len(results))
