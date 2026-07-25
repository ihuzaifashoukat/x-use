"""Write-side MCP tools: post_tweet, generate_and_post, reply_to_tweet,
run_cycle. (engage lives in ``engage.py``; read-only tools and approve_draft
in ``tools.py``.)

Draft mode (default on): write tools build the full payload — including
LLM-generated text where applicable — store a draft, and return it without
touching the browser. ``approve_draft`` executes it. ``run_cycle`` is the
legacy batch path and is not draftable.
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from xuse.mcp.media import images_for_tweet, media_envelope, with_images
from xuse.orchestrator import TwitterOrchestrator
from xuse.pipelines import PIPELINE_FLAGS as _PIPELINE_FLAGS

from . import actions, executor as ex
from .executor import Ctx, ToolError
from .tools import draft_response, guard, ok_, scrape_single_tweet
from .annotations import PUBLISHES_TO_X, READ_ONLY_FROM_X

logger = logging.getLogger(__name__)


def register_write_tools(server, ctx: Ctx) -> None:
    """Register the write-side tools (except engage) on the FastMCP server."""

    @server.tool(annotations=PUBLISHES_TO_X)
    @guard
    async def post_tweet(
        account: str,
        text: str,
        media: Optional[List[str]] = None,
        community: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Post a tweet from an account. `media` = local file paths; `community`
        = target community id. In draft mode (default) returns a draft for
        review and posts nothing; approve_draft(draft_id) executes it."""
        account_id, _, _ = ex.resolve_account(ctx, account)
        if not text or not text.strip():
            raise ToolError("Tweet text must not be empty.")
        if ctx.draft_mode:
            preview = f"Post as @{account_id}: \"{text}\""
            if media:
                preview += f" (+{len(media)} media file(s))"
            if community:
                preview += f" → community {community}"
            draft = ctx.draft_store.create(
                account=account_id,
                action="post_tweet",
                payload={"text": text, "media": list(media or []), "community": community},
                preview=preview,
            )
            return draft_response(draft)
        result = await actions.exec_post(ctx, account_id, text=text, media=media, community=community)
        return ok_(**result)

    @server.tool(annotations=PUBLISHES_TO_X)
    @guard
    async def generate_and_post(account: str, topic: str) -> Dict[str, Any]:
        """Generate a post about `topic` with the configured LLM and post it.
        In draft mode (default) the generated text is stored in the draft's
        payload/preview so a human reviews the actual content before anything
        is posted."""
        account_id, _, _ = ex.resolve_account(ctx, account)
        if not topic or not topic.strip():
            raise ToolError("Topic must not be empty.")
        text = await actions.generate_post_text(ctx, account_id, topic)
        if ctx.draft_mode:
            draft = ctx.draft_store.create(
                account=account_id,
                action="generate_and_post",
                payload={"text": text, "media": [], "community": None, "topic": topic},
                preview=f"Post AI-generated text as @{account_id}: \"{text}\"",
            )
            return draft_response(draft)
        result = await actions.exec_post(ctx, account_id, text=text)
        return ok_(generated_text=text, **result)

    @server.tool(annotations=PUBLISHES_TO_X)
    @guard
    async def reply_to_tweet(account: str, tweet_url: str, text: str = "auto") -> Dict[str, Any]:
        """Reply to a tweet. Pass explicit `text`, or "auto" to generate the
        reply with the LLM from the tweet's actual content (a read-only fetch
        is performed so the draft shows the real reply text). In draft mode
        (default) nothing is posted until approve_draft(draft_id)."""
        account_id, _, _ = ex.resolve_account(ctx, account)
        tweet_id = ex.tweet_id_from_url(tweet_url)
        if not tweet_id:
            raise ToolError(f"Could not parse a tweet id from URL: {tweet_url}")
        text_content = ""
        if text.strip().lower() == "auto":
            original = await scrape_single_tweet(ctx, account_id, tweet_url, tweet_id)
            text_content = original.text_content or ""
            reply_text = await actions.generate_reply_text(ctx, account_id, original)
        else:
            reply_text = text.strip()[: ex.MAX_REPLY_CHARS]
            if not reply_text:
                raise ToolError("Reply text must not be empty.")
        if ctx.draft_mode:
            draft = ctx.draft_store.create(
                account=account_id,
                action="reply_to_tweet",
                payload={
                    "tweet_url": tweet_url,
                    "tweet_id": tweet_id,
                    "text": reply_text,
                    "text_content": text_content,
                },
                preview=f"Reply as @{account_id} to {tweet_url}: \"{reply_text}\"",
            )
            return draft_response(draft)
        result = await actions.exec_reply(ctx, account_id, tweet_url, reply_text, tweet_id, text_content)
        return ok_(**result)

    @server.tool(annotations=READ_ONLY_FROM_X)
    @guard
    async def prepare_reply(account: str, tweet_url: str, include_images: bool = True) -> dict[str, Any]:
        """Read-only reply context: the tweet's text AND media (photos attach
        as image content so your model can see them), plus the account's
        keywords and persona. YOUR model writes the reply itself, no
        server-side LLM, nothing posted. Then pass your text to
        reply_to_tweet(text=...) or queue_engagement(action="reply", text=...)."""
        account_id, _, model = ex.resolve_account(ctx, account)
        tweet_id = ex.tweet_id_from_url(tweet_url)
        if not tweet_id:
            raise ToolError(f"Could not parse a tweet id from URL: {tweet_url}")
        original = await scrape_single_tweet(ctx, account_id, tweet_url, tweet_id)
        handle = (original.user_handle or "").lstrip("@")  # scraper handles arrive @-prefixed
        envelope = ok_(
            account=account_id,
            tweet_id=tweet_id,
            tweet_url=tweet_url,
            author=f"@{handle}" if handle else None,
            text_content=original.text_content or "",
            media=media_envelope(original),
            persona=getattr(model, "persona", None),
            account_keywords=list(getattr(model, "target_keywords", None) or []),
            max_reply_chars=ex.MAX_REPLY_CHARS,
            message="Write the reply with your own model, then call "
                    "reply_to_tweet or queue_engagement(action='reply') with the text.",
        )
        if not include_images:
            return envelope
        images = await asyncio.to_thread(images_for_tweet, original)
        return with_images(envelope, images)

    @server.tool(annotations=PUBLISHES_TO_X)
    @guard
    async def run_cycle(account: Optional[str] = None, pipelines: Optional[str] = None) -> Dict[str, Any]:
        """Run the legacy batch automation cycle in the background and return
        a run handle immediately (progress goes to the logs, this never
        silent-blocks). `account` limits the run to one account; `pipelines`
        is a comma-separated subset of: competitor_reposts, keyword_replies,
        keyword_retweets, likes, content_curation, community_engagement
        (mapped onto the account's ActionConfig enable flags for this run
        only, config files are never mutated; same names as the CLI's
        `x-use run --pipeline`). Draft mode does not apply to batch cycles."""
        raw_accounts = ctx.config_loader.get_accounts_config()
        targets: List[Dict[str, Any]] = []
        for raw in raw_accounts:
            if not isinstance(raw, dict) or not raw.get("is_active", True):
                continue
            if account and raw.get("account_id") != account:
                continue
            targets.append(dict(raw))
        if not targets:
            raise ToolError(f"No active account found for run_cycle(account={account!r}).")
        if pipelines:
            requested = [p.strip().lower() for p in pipelines.split(",") if p.strip()]
            invalid = [p for p in requested if p not in _PIPELINE_FLAGS]
            if invalid:
                raise ToolError(f"Unknown pipeline(s): {invalid}. Allowed: {sorted(_PIPELINE_FLAGS)}.")
            for target in targets:
                ac = dict(target.get("action_config") or target.get("action_config_override") or {})
                for flag in _PIPELINE_FLAGS.values():
                    ac[flag] = False
                for name in requested:
                    ac[_PIPELINE_FLAGS[name]] = True
                target["action_config"] = ac  # in-memory only — never written to disk
        orchestrator = TwitterOrchestrator()
        run_id = uuid4().hex[:12]
        account_ids = [t.get("account_id", "?") for t in targets]

        async def _runner() -> None:
            results = await asyncio.gather(
                *(orchestrator._process_account(t) for t in targets), return_exceptions=True
            )
            failures = [r for r in results if isinstance(r, Exception)]
            for failure in failures:
                logger.error("run_cycle %s account failure: %s", run_id, failure)
            ctx.runs[run_id]["status"] = "failed" if len(failures) == len(results) else "finished"

        task = asyncio.create_task(_runner())
        ctx.runs[run_id] = {
            "status": "running",
            "accounts": account_ids,
            "pipelines": pipelines,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "task": task,
        }
        return ok_(run_id=run_id, status="running", accounts=account_ids,
                   message="Cycle running in background. Watch logs/accounts/<account_id>.jsonl for progress.")
