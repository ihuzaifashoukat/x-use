"""MCP resources: read-only context a client can attach to a conversation.

Resources are not a second copy of the read-only tools. A tool is something the
model decides to call; a resource is context the *client* attaches, so the model
already has it without spending a turn. The four here are the things a drafting
turn always wants in hand: who the accounts are, who this account is (persona),
and what is currently waiting for review.

Everything is served from files already on disk. No resource starts a browser,
none reaches X, and all of them run the same masking as the tools, so no cookie,
password, or proxy credential can leave through this surface.
"""
import json
import logging
import re
from typing import Any, Dict, List

from . import executor as ex
from .executor import Ctx

logger = logging.getLogger(__name__)

# Account ids arrive from a URI template and are matched against config; keep
# the same charset rule the tools use so a crafted URI cannot smuggle a path.
_SAFE_ACCOUNT_ID = re.compile(r"[A-Za-z0-9_-]+")

JSON_MIME = "application/json"
MARKDOWN_MIME = "text/markdown"


def _dump(payload: Any) -> str:
    return json.dumps(payload, indent=2, default=str)


def _accounts(ctx: Ctx) -> List[Dict[str, Any]]:
    return [a for a in ctx.config_loader.get_accounts_config() if isinstance(a, dict)]


def _find(ctx: Ctx, account_id: str) -> Dict[str, Any]:
    """Locate one account or raise. Resources have no error envelope: unlike a
    tool call, a bad resource read should surface as a protocol error rather
    than an ok:true body describing a failure."""
    if not _SAFE_ACCOUNT_ID.fullmatch(account_id or ""):
        raise ValueError(f"Invalid account id: {account_id!r}")
    for raw in _accounts(ctx):
        if raw.get("account_id") == account_id:
            return raw
    known = [a.get("account_id") for a in _accounts(ctx) if a.get("account_id")]
    raise ValueError(f"Unknown account '{account_id}'. Configured: {known or 'none'}.")


def register_resources(server, ctx: Ctx) -> None:
    """Register the four read-only resources on the FastMCP server."""

    @server.resource(
        "xuse://accounts",
        name="x-use accounts",
        description="Every configured X account with secrets stripped: id, active state, "
                    "target keywords, and whether a persona is set. Never starts a browser.",
        mime_type=JSON_MIME,
    )
    def accounts_resource() -> str:
        rows = []
        for raw in _accounts(ctx):
            masked = ex.mask_account(raw)
            rows.append({
                "account_id": masked.get("account_id"),
                "is_active": masked.get("is_active", True),
                "target_keywords": masked.get("target_keywords") or [],
                "has_persona": bool(masked.get("persona")),
                "has_proxy": bool(masked.get("proxy")),
            })
        return _dump({"count": len(rows), "accounts": rows})

    @server.resource(
        "xuse://accounts/{account_id}",
        name="x-use account config",
        description="One account's full masked configuration: keywords, competitor profiles, "
                    "self handles, persona, and action config. No cookies, no password, "
                    "proxy credentials masked.",
        mime_type=JSON_MIME,
    )
    def account_resource(account_id: str) -> str:
        return _dump(ex.mask_account(_find(ctx, account_id)))

    @server.resource(
        "xuse://accounts/{account_id}/persona",
        name="x-use account persona",
        description="The account's persona as markdown: voice, topics, reply style, and "
                    "avoid-list. Attach this before writing anything as the account.",
        mime_type=MARKDOWN_MIME,
    )
    def persona_resource(account_id: str) -> str:
        raw = _find(ctx, account_id)
        persona = raw.get("persona")
        if not persona:
            # Returning prose rather than raising: "no persona yet" is a normal
            # state for a fresh account, and the remedy is more useful than an error.
            return (
                f"# {account_id}\n\nNo persona is set for this account.\n\n"
                "Set one with `update_account(account, persona=...)`: voice, topics, "
                "reply style, what to avoid, and one or two example replies. "
                "Until then, anything written as this account will read as generic.\n"
            )
        return f"# {account_id}\n\n{persona}\n"

    @server.resource(
        "xuse://drafts/pending",
        name="x-use pending drafts",
        description="Every draft awaiting approval, across all accounts, with the exact text "
                    "that would publish. Nothing here has reached X.",
        mime_type=JSON_MIME,
    )
    def pending_drafts_resource() -> str:
        drafts = ctx.draft_store.list(status="pending")
        drafts.sort(key=lambda d: d.created_at, reverse=True)
        return _dump({
            "count": len(drafts),
            "note": "Pending only. approve_draft(draft_id) publishes; reject_draft(draft_id) discards.",
            "drafts": [json.loads(d.model_dump_json()) for d in drafts],
        })
