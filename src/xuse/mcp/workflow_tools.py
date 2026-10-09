"""Bounded profile context and one individually reviewed outreach draft."""
import json
from typing import Any

from xuse.models import ScrapedTweet
from xuse.outreach import OutreachError
from xuse.outreach.store import normalize_handle

from . import executor as ex
from .annotations import LOCAL_WRITE
from .browser_bridge import browser_call, uses_playwright
from .executor import ToolError
from .tools import dump_tweet, guard, ok_

MAX_CONTEXT_POSTS = 10
MAX_MESSAGE_CHARS = 10000


def _recipient_allowed(ctx, account: str, handle: str) -> None:
    if ctx.outreach_store is None:
        raise ToolError("Outreach store is not configured on this server.")
    try:
        ctx.outreach_store.validate_recipient(account, handle)
    except OutreachError as exc:
        raise ToolError(str(exc)) from None


def _verified_context(result: Any, handle: str, post_limit: int) -> dict[str, Any]:
    """Fail closed before drafting if context cannot identify the recipient."""
    if not isinstance(result, dict) or not isinstance(result.get("profile"), dict):
        raise ToolError("Profile context did not identify the requested X profile.")
    profile = dict(result["profile"])
    try:
        profile_handle = normalize_handle(profile.get("handle"))
    except OutreachError:
        raise ToolError("Profile context did not confirm the requested X handle.") from None
    if profile_handle != handle:
        raise ToolError("Profile context did not confirm the requested X handle.")
    posts = result.get("posts")
    if not isinstance(posts, list) or len(posts) > post_limit:
        raise ToolError("Profile post context exceeds the requested bound or has an invalid format.")
    verified_posts = []
    for post in posts:
        try:
            tweet = post if isinstance(post, ScrapedTweet) else ScrapedTweet.model_validate(post)
            author = normalize_handle(tweet.user_handle)
        except Exception:
            # Model errors can include source text and extra DOM data.
            raise ToolError("Profile post context did not confirm a valid author.") from None
        if author != handle:
            raise ToolError("Selected posts must be authored by the requested X profile.")
        verified_posts.append(dump_tweet(tweet.model_copy(update={"raw_element_data": None})))
    profile["handle"] = handle
    try:
        json.dumps(profile, allow_nan=False)
    except (TypeError, ValueError):
        raise ToolError("Profile context has an invalid format.") from None
    pagination = result.get("pagination")
    if pagination not in ("visible_only", "bounded_scroll"):
        pagination = "visible_only"
    return {"profile": profile, "posts": verified_posts, "count": len(verified_posts),
            "partial": True, "pagination": pagination, "source": "browser_dom"}


def register_workflow_tools(server, ctx) -> None:
    """Register one composed read-and-stage workflow; never execute delivery."""

    @server.tool(annotations=LOCAL_WRITE.model_copy(update={"openWorldHint": True}))
    @guard
    async def prepare_outreach(account: str, profile: str, message_text: str,
                               post_limit: int = 5) -> dict[str, Any]:
        """Fetch profile details and up to 10 authored posts, then prepare ONE
        local DM draft with your exact supplied text. Use get_profile_context to
        research and write a personalized message first. Context is a bounded
        visible subset; it is not a complete post history. No server LLM or
        message send occurs. Review the returned recipient, context and draft,
        then approve_draft individually; draft_mode does not bypass review.
        Opt-outs and account activation are rechecked after the browser read.
        """
        if not uses_playwright(ctx):
            raise ToolError("This workflow requires the patchright or playwright async browser backend.")
        if isinstance(post_limit, bool) or not isinstance(post_limit, int) or not 1 <= post_limit <= MAX_CONTEXT_POSTS:
            raise ToolError("post_limit must be an integer between 1 and 10.")
        if (not isinstance(message_text, str) or not message_text.strip()
                or len(message_text) > MAX_MESSAGE_CHARS or "\x00" in message_text):
            raise ToolError("Message text must contain 1 to 10000 characters without null bytes.")
        account_id, raw, _ = ex.resolve_account(ctx, account)
        ex.require_active(raw, account_id)
        handle = ex.profile_handle_from(profile).lower()
        _recipient_allowed(ctx, account_id, handle)
        # A single policy reservation avoids a second read hitting cooldown
        # and ensures failed context retrieval never leaves a partial draft.
        result = await browser_call(ctx, account_id, "get_profile_context", handle, post_limit)
        context = _verified_context(result, handle, post_limit)
        _, current_raw, _ = ex.resolve_account(ctx, account_id)
        ex.require_active(current_raw, account_id)
        _recipient_allowed(ctx, account_id, handle)
        recipient = "@" + handle
        draft = ctx.draft_store.create(
            account=account_id, action="send_message",
            payload={"recipient": recipient, "text": message_text},
            preview=f"Send a DM to {recipient} from {account_id}:\n{message_text}",
        )
        draft_args = {"draft_id": draft.draft_id}
        return ok_(
            account=account_id, recipient=recipient, context=context,
            draft=draft.model_dump(mode="json"),
            next_steps={
                "review": {"tool": "get_draft", "arguments": draft_args},
                "approve": {"tool": "approve_draft", "arguments": draft_args},
                "reject": {"tool": "reject_draft", "arguments": draft_args},
                "recovery": {"tool": "get_account_safety", "arguments": {"account": account_id},
                             "resolution_tool": "resolve_action_outcome",
                             "instruction": "After an uncertain approval, inspect X and its action ID before reconciling succeeded or not_sent."},
            },
            message="One local draft prepared. Nothing was sent. Review its context and exact payload before individual approval.",
        )
