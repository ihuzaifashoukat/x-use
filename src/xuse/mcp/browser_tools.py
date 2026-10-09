"""Inbox, relationship and recovery tools for the async browser runtime."""
import asyncio
import os
import re
from typing import Any, Literal, Optional

from . import executor as ex
from .annotations import LOCAL_WRITE, LOCAL_WRITE_IDEMPOTENT, READ_ONLY_FROM_X, READ_ONLY_LOCAL
from .browser_bridge import browser_call, uses_playwright
from .executor import ToolError


def _require_browser(ctx):
    if not uses_playwright(ctx):
        raise ToolError("This tool requires the patchright or playwright async browser backend.")


def _recipient(value):
    from xuse.browser.messaging import normalize_recipient
    return normalize_recipient(value)


async def execute_browser_draft(ctx, draft):
    _require_browser(ctx)
    account, raw, _ = ex.resolve_account(ctx, draft.account)
    ex.require_active(raw, account)
    payload = draft.payload
    if draft.action == "follow_profile":
        handle = ex.profile_handle_from(payload["profile"])
        ctx.outreach_store.validate_recipient(account, handle)
        return await browser_call(ctx, account, "follow", handle, kind="follow",
                                  action_context={"draft_id": draft.draft_id})
    recipient = _recipient(payload["recipient"])
    lead_id, campaign_id = payload.get("lead_id"), payload.get("campaign_id")
    if bool(lead_id) != bool(campaign_id):
        raise ToolError("Campaign messages require both lead_id and campaign_id.")
    if campaign_id:
        lead = ctx.outreach_store.validate_delivery(account, lead_id, campaign_id, expected_draft_id=draft.draft_id)
        if recipient.lower().lstrip("@") != lead.handle.lower().lstrip("@"):
            raise ToolError("The draft recipient does not match its campaign lead.")
    elif recipient.startswith("@"):
        ctx.outreach_store.validate_recipient(account, recipient)
    def validate_recipient(handle):
        _, current_raw, _ = ex.resolve_account(ctx, account)
        ex.require_active(current_raw, account)
        ctx.outreach_store.validate_recipient(account, handle)
        if campaign_id:
            current_lead = ctx.outreach_store.validate_delivery(account, lead_id, campaign_id, expected_draft_id=draft.draft_id)
            if handle.lower().lstrip("@") != current_lead.handle.lower().lstrip("@"):
                raise ToolError("Campaign conversation recipient mismatch.")
    context = {"draft_id": draft.draft_id, "campaign_id": campaign_id, "lead_id": lead_id}
    result = await browser_call(ctx, account, "send_message", recipient, payload["text"], kind="message",
                                action_context=context, recipient_validator=validate_recipient)
    if campaign_id:
        try:
            ctx.outreach_store.record_delivery(account, lead_id, campaign_id, "delivered", expected_draft_id=draft.draft_id)
        except Exception:
            error = ToolError("Message was confirmed, but its local campaign outcome could not be saved. Reconcile this action as succeeded; do not resend.")
            error.action_id = result.get("action_id")
            raise error from None
    return {"account": account, "action": "send_message", **result}


def register_browser_tools(server, ctx):
    from .tools import draft_response, guard, ok_

    @server.tool(annotations=READ_ONLY_FROM_X)
    @guard
    async def get_inbox(account: str, limit: int = 20, inbox_filter: str = "all",
                        unread_first: bool = False, folder: Literal["inbox", "requests", "other"] = "inbox") -> dict[str, Any]:
        """Read visible inbox/requests/other summaries with all/unread/read filtering. Native Unread selection is verified when available; never opens or accepts a conversation."""
        from xuse.browser.messaging import _inbox_options, _limit
        _require_browser(ctx)
        _limit(limit, 50)
        _inbox_options(inbox_filter, unread_first, folder)
        result = await browser_call(ctx, account, "get_inbox", limit,
                                    inbox_filter=inbox_filter, unread_first=unread_first, folder=folder)
        return ok_(account=account, **result)

    @server.tool(annotations=READ_ONLY_FROM_X)
    @guard
    async def get_conversation(account: str, conversation_id: str, limit: int = 50,
                               before_message_id: Optional[str] = None) -> dict[str, Any]:
        """Read bounded visible conversation context and observed request/reply state. Cursor stays within the visible window; opening may clear X unread state. Does not accept requests."""
        from xuse.browser.messaging import _limit, _message_cursor, conversation_url
        _require_browser(ctx)
        _limit(limit, 100)
        _message_cursor(before_message_id)
        conversation_url(conversation_id)
        result = await browser_call(ctx, account, "get_conversation", conversation_id, limit,
                                    before_message_id=before_message_id)
        return ok_(account=account, **result)

    @server.tool(annotations=READ_ONLY_FROM_X)
    @guard
    async def search_conversations(account: str, query: str, limit: int = 20,
                                   inbox_filter: str = "all", unread_first: bool = False,
                                   folder: Literal["inbox", "requests", "other"] = "inbox") -> dict[str, Any]:
        """Search the visible inbox locally; results are partial, not full message-history search."""
        _require_browser(ctx)
        from xuse.browser.messaging import _inbox_options, _limit
        _limit(limit, 50)
        _inbox_options(inbox_filter, unread_first, folder)
        if not isinstance(query, str) or not query.strip() or len(query) > 200:
            raise ValueError("query must contain between 1 and 200 characters.")
        result = await browser_call(ctx, account, "search_conversations", query, limit,
                                    inbox_filter=inbox_filter, unread_first=unread_first, folder=folder)
        return ok_(account=account, **result)

    @server.tool(annotations=LOCAL_WRITE)
    @guard
    async def send_message(account: str, recipient: str, text: str) -> dict[str, Any]:
        """Prepare a reviewed DM draft for one handle or conversation. Always requires approve_draft, even when draft_mode is off."""
        _require_browser(ctx)
        account, raw, _ = ex.resolve_account(ctx, account)
        ex.require_active(raw, account)
        recipient = _recipient(recipient)
        if not text or not text.strip() or len(text) > 10000:
            raise ToolError("Message text must contain 1 to 10000 characters.")
        if recipient.startswith("@"):
            ctx.outreach_store.validate_recipient(account, recipient)
        draft = ctx.draft_store.create(account, "send_message", {"recipient": recipient, "text": text},
                                       f"Send a DM to {recipient} from {account}: {text}")
        return draft_response(draft)

    @server.tool(annotations=LOCAL_WRITE)
    @guard
    async def follow_profile(account: str, profile: str) -> dict[str, Any]:
        """Prepare a follow draft for one X profile; approve_draft performs the action."""
        _require_browser(ctx)
        account, raw, _ = ex.resolve_account(ctx, account)
        ex.require_active(raw, account)
        handle = ex.profile_handle_from(profile)
        ctx.outreach_store.validate_recipient(account, handle)
        return draft_response(ctx.draft_store.create(account, "follow_profile", {"profile": handle},
                                                     f"Follow @{handle} from {account}"))

    @server.tool(annotations=READ_ONLY_FROM_X)
    @guard
    async def get_profile(account: str, profile: str) -> dict[str, Any]:
        """Read public profile details using visible X page elements."""
        _require_browser(ctx)
        result = await browser_call(ctx, account, "get_profile", ex.profile_handle_from(profile))
        return ok_(account=account, **result)

    @server.tool(annotations=READ_ONLY_FROM_X)
    @guard
    async def get_home_feed(account: str, limit: int = 20) -> dict[str, Any]:
        """Read posts currently visible in the home feed; no engagement actions."""
        from .tools import dump_tweet
        _require_browser(ctx)
        tweets = await browser_call(ctx, account, "get_home_feed", max(1, min(limit, 50)))
        return ok_(account=account, tweets=[dump_tweet(t) for t in tweets], partial=True)

    @server.tool(annotations=READ_ONLY_LOCAL)
    @guard
    async def get_session_status(account: Optional[str] = None) -> dict[str, Any]:
        """Inspect browser session metadata without reading cookies or starting a browser."""
        if account:
            ex.resolve_account(ctx, account)
        accounts = [account] if account else list(ctx.session_pool.active_accounts)
        return ok_(backend=getattr(ctx.session_pool, "backend", "selenium"),
                   sessions=[{"account": a, "warm": ctx.session_pool.entry_for(a) is not None} for a in accounts])

    @server.tool(annotations=LOCAL_WRITE_IDEMPOTENT)
    @guard
    async def close_session(account: str) -> dict[str, Any]:
        """Close one account's browser context; retain the operator's cookie file."""
        ex.resolve_account(ctx, account)
        await ctx.session_pool.close(account)
        return ok_(account=account, closed=True)

    @server.tool(annotations=READ_ONLY_LOCAL)
    @guard
    async def get_account_safety(account: str) -> dict[str, Any]:
        """Read local limits, pause reason, and writes needing outcome reconciliation."""
        ex.resolve_account(ctx, account)
        if ctx.safety_store is None:
            return ok_(account=account, available=False, backend="selenium")
        return ok_(account=account, **await asyncio.to_thread(ctx.safety_store.status, account))

    @server.tool(annotations=LOCAL_WRITE_IDEMPOTENT)
    @guard
    async def pause_account_actions(account: str) -> dict[str, Any]:
        """Pause browser actions durably until the operator explicitly resumes them."""
        ex.resolve_account(ctx, account)
        if ctx.safety_store is None:
            raise ToolError("Durable action policy is unavailable on the legacy backend.")
        await asyncio.to_thread(ctx.safety_store.pause, account, "manual_pause")
        return ok_(account=account, paused=True)

    @server.tool(annotations=LOCAL_WRITE_IDEMPOTENT.model_copy(update={"openWorldHint": True}))
    @guard
    async def resume_account_actions(account: str) -> dict[str, Any]:
        """After resolving a challenge, probe the signed-in home page and clear the local pause. Does not reset budgets."""
        _require_browser(ctx)
        ex.resolve_account(ctx, account)
        await browser_call(ctx, account, "verify_session", recovery_read=True)
        return ok_(account=account, paused=False)

    @server.tool(annotations=LOCAL_WRITE_IDEMPOTENT)
    @guard
    async def resolve_action_outcome(account: str, action_id: str, observed_outcome: str) -> dict[str, Any]:
        """Operator reconciliation only: after inspecting X, record succeeded or not_sent for an uncertain action. Incorrect not_sent can cause duplicates."""
        ex.resolve_account(ctx, account)
        if ctx.safety_store is None:
            raise ToolError("Durable action policy is unavailable.")
        with ctx.safety_store.operation_lock(account):
            reference = await asyncio.to_thread(ctx.safety_store.reference, account, action_id)
            result = await asyncio.to_thread(ctx.safety_store.reconcile, account, action_id, observed_outcome)
            if reference:
                campaign_id, lead_id, draft_id = reference.get("campaign_id"), reference.get("lead_id"), reference.get("draft_id")
                if campaign_id and lead_id:
                    ctx.outreach_store.record_delivery(account, lead_id, campaign_id,
                                                       "delivered" if observed_outcome == "succeeded" else "failed",
                                                       expected_draft_id=draft_id)
                if draft_id:
                    try:
                        draft = ctx.draft_store.get(draft_id)
                        if draft.action == "publish_thread":
                            from .thread_tools import _store, thread_result
                            progress = thread_result(_store(ctx).get(draft.payload["run_id"]))
                            state = progress["state"]
                            status = ("executed" if state == "complete" else "rejected" if state == "cancelled"
                                      else "uncertain" if state == "uncertain" else "partial" if progress["published"] else "pending")
                            ctx.draft_store.set_status(draft_id, status)
                            result["thread_run_id"] = progress["run_id"]
                            result["thread_state"] = state
                            result["thread_recovery"] = "This reconciles one action only. Inspect get_thread_run; uncertain segments remain stopped and are never resent."
                        else:
                            latest = await asyncio.to_thread(ctx.safety_store.latest_draft_action, account, draft_id)
                            if draft.account != account or not latest or latest["action_id"] != action_id:
                                result["draft_update_skipped"] = "A newer action owns this draft's current outcome."
                            elif observed_outcome == "not_sent" and draft.status == "rejected":
                                result["draft_update_skipped"] = "The draft has been rejected."
                            else:
                                ctx.draft_store.set_status(draft_id, "executed" if observed_outcome == "succeeded" else ("rejected" if campaign_id else "pending"))
                    except KeyError:
                        result["draft_missing"] = True
        return ok_(account=account, **result)

    @server.tool(annotations=LOCAL_WRITE_IDEMPOTENT.model_copy(update={"openWorldHint": True}))
    @guard
    async def unlock_inbox(account: str, pin_env_var: str = "XUSE_INBOX_PIN") -> dict[str, Any]:
        """Unlock the owner's encrypted inbox once using a server environment variable. PIN never appears in tool arguments, drafts, logs or stored state."""
        _require_browser(ctx)
        ex.resolve_account(ctx, account)
        if not re.fullmatch(r"XUSE_[A-Z0-9_]*PIN", pin_env_var):
            raise ToolError("Use an XUSE_ environment variable ending in PIN.")
        pin = os.environ.get(pin_env_var, "")
        if not re.fullmatch(r"[0-9]{4,12}", pin):
            raise ToolError("The server PIN environment variable is missing or invalid.")
        result = await browser_call(ctx, account, "unlock_messages", pin, kind="unlock")
        return ok_(account=account, **result)

    @server.tool(annotations=READ_ONLY_FROM_X)
    @guard
    async def get_profile_context(account: str, profile: str, post_limit: int = 5) -> dict[str, Any]:
        """Preferred outreach read: exact profile details and bounded authored posts together. No draft or send."""
        from .tools import dump_tweet
        _require_browser(ctx)
        if not 1 <= post_limit <= 10:
            raise ToolError("post_limit must be between 1 and 10.")
        result = await browser_call(ctx, account, "get_profile_context", ex.profile_handle_from(profile), post_limit)
        return ok_(account=account, **{**result, "posts": [dump_tweet(post) for post in result["posts"]]})

    @server.tool(annotations=READ_ONLY_FROM_X)
    @guard
    async def get_profile_posts(account: str, profile: str, feed: str = "posts", limit: int = 10) -> dict[str, Any]:
        """Read one author's posts, posts-and-replies, or media tab. feed: posts|replies|media. Results are bounded and partial."""
        from .tools import dump_tweet
        _require_browser(ctx)
        if feed not in {"posts", "replies", "media"} or not 1 <= limit <= 50:
            raise ToolError("Use feed posts, replies, or media and a limit between 1 and 50.")
        result = await browser_call(ctx, account, "get_profile_posts", ex.profile_handle_from(profile), feed, limit)
        return ok_(account=account, **{**result, "posts": [dump_tweet(post) for post in result["posts"]]})

    @server.tool(annotations=READ_ONLY_FROM_X)
    @guard
    async def get_profile_connections(account: str, profile: str, relationship: str = "following", limit: int = 20) -> dict[str, Any]:
        """Read visible public followers/following. This is not a private address book or complete contact export."""
        _require_browser(ctx)
        if relationship not in {"followers", "following"} or not 1 <= limit <= 50:
            raise ToolError("Use relationship followers or following and a limit between 1 and 50.")
        result = await browser_call(ctx, account, "get_profile_connections", ex.profile_handle_from(profile), relationship, limit)
        return ok_(account=account, **result)
