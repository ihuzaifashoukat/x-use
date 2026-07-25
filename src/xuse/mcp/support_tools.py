"""Support MCP tools: list_drafts, get_draft, reject_draft, get_run_status,
get_account_health. All read-only except reject_draft, which is a local
status flip — nothing touches X."""
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, Optional

from xuse.core.config_loader import PROJECT_ROOT
from xuse.doctor import check_cookie_data
from xuse.models import AccountConfig

from . import executor as ex
from .executor import Ctx, ToolError
from .tools import guard, ok_

logger = logging.getLogger(__name__)

_DRAFT_STATUSES = ("pending", "approved", "executed", "failed", "rejected")
_SAFE_ACCOUNT_ID = re.compile(r"[A-Za-z0-9_-]+")


def register_support_tools(server, ctx: Ctx) -> None:
    """Register the five support tools on the FastMCP server."""

    @server.tool()
    @guard
    async def list_drafts(status: Optional[str] = None, account: Optional[str] = None,
                          limit: int = 20) -> Dict[str, Any]:
        """List drafts, newest first. `status` filters to one of
        pending/approved/executed/failed/rejected; `account` filters by
        account id; `limit` caps the response (max 100). Read-only."""
        if status is not None and status not in _DRAFT_STATUSES:
            raise ToolError(
                f"Unknown draft status '{status}'. Allowed: {list(_DRAFT_STATUSES)}.")
        drafts = ctx.draft_store.list(status=status)
        if account is not None:
            drafts = [d for d in drafts if d.account == account]
        drafts.sort(key=lambda d: d.created_at, reverse=True)
        total = len(drafts)
        limit = max(1, min(int(limit), 100))
        return ok_(total=total, returned=min(total, limit),
                   drafts=[json.loads(d.model_dump_json()) for d in drafts[:limit]])

    @server.tool()
    @guard
    async def get_draft(draft_id: str) -> Dict[str, Any]:
        """Show one draft by id. Read-only."""
        try:
            draft = ctx.draft_store.get(draft_id)
        except KeyError:
            raise ToolError(f"Unknown draft_id '{draft_id}'.") from None
        return ok_(draft=json.loads(draft.model_dump_json()))

    @server.tool()
    @guard
    async def reject_draft(draft_id: str) -> Dict[str, Any]:
        """Reject a pending draft so it can never be approved. Local status
        change only — nothing touches X."""
        try:
            draft = ctx.draft_store.get(draft_id)
        except KeyError:
            raise ToolError(f"Unknown draft_id '{draft_id}'.") from None
        if draft.status != "pending":
            raise ToolError(
                f"Draft '{draft_id}' is already {draft.status} — "
                "only pending drafts can be rejected.")
        ctx.draft_store.set_status(draft_id, "rejected")
        return ok_(draft_id=draft_id, status="rejected")

    @server.tool()
    @guard
    async def get_run_status(run_id: Optional[str] = None) -> Dict[str, Any]:
        """Poll run_cycle handles. With `run_id` omitted, lists every run
        started since this server booted. Read-only."""
        def _public(run: Dict[str, Any]) -> Dict[str, Any]:
            return {k: v for k, v in run.items() if k != "task"}

        if run_id is not None:
            run = ctx.runs.get(run_id)
            if run is None:
                raise ToolError(f"Unknown run_id '{run_id}'.")
            return ok_(run_id=run_id, **_public(run))
        return ok_(count=len(ctx.runs),
                   runs=[{"run_id": rid, **_public(r)} for rid, r in ctx.runs.items()])

    @server.tool()
    @guard
    async def get_account_health(account: str) -> Dict[str, Any]:
        """Read-only health snapshot for one account: config presence,
        cookie-file validity, metrics counters, warm-session state, queue
        depth, pending drafts. Never starts a browser. A config that fails
        validation is REPORTED (config.valid=false with the error), not
        raised — surfacing that is the point of a health check."""
        raw = ctx.session_pool.find_account_dict(account)  # SessionError if unknown
        account_id = raw.get("account_id", account)
        if not _SAFE_ACCOUNT_ID.fullmatch(account_id):
            raise ToolError(f"Invalid account id: {account_id!r}")
        config_info: Dict[str, Any] = {"is_active": bool(raw.get("is_active", True))}
        try:
            AccountConfig.model_validate(raw)
            config_info["valid"] = True
        except Exception as e:
            config_info["valid"] = False
            config_info["error"] = ex.sanitize_text(e)
        # Surface corrupt-on-disk config (the loader boots empty on parse
        # failure and records the reason on these attributes).
        for attr, key in (("accounts_error", "loader_error"),
                          ("settings_error", "settings_error")):
            loader_error = getattr(ctx.config_loader, attr, None)
            if loader_error:
                config_info[key] = ex.sanitize_text(loader_error)
        cookies_info: Dict[str, Any]
        if raw.get("cookies"):
            cookies_info = {"configured": True, "valid": True, "problems": []}
        elif raw.get("cookie_file_path"):
            cookie_path = Path(raw["cookie_file_path"])
            if not cookie_path.is_absolute():
                cookie_path = PROJECT_ROOT / raw["cookie_file_path"]
            if cookie_path.is_file():
                try:
                    data = json.loads(cookie_path.read_text(encoding="utf-8"))
                    valid, problems = check_cookie_data(data)
                    cookies_info = {"configured": True, "file": raw["cookie_file_path"],
                                    "valid": valid, "problems": problems}
                except Exception as e:
                    cookies_info = {"configured": True, "file": raw["cookie_file_path"],
                                    "valid": False, "problems": [f"unreadable: {e}"]}
            else:
                cookies_info = {"configured": True, "file": raw["cookie_file_path"],
                                "valid": False, "problems": ["cookie file not found"]}
        else:
            cookies_info = {"configured": False, "problems": ["no cookies configured"]}
        summary: Dict[str, Any] = {}
        summary_path = PROJECT_ROOT / "data" / "metrics" / f"{account_id}.json"
        if summary_path.exists():
            try:
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
            except Exception:
                summary = {"unreadable": True}
        entry = ctx.session_pool.entry_for(account_id)
        queue_counts = ctx.queue_store.counts(account=account_id) if ctx.queue_store else {}
        pending_drafts = len([d for d in ctx.draft_store.list(status="pending")
                              if d.account == account_id])
        return ok_(account=account_id,
                   config=config_info,
                   cookies=cookies_info,
                   metrics=summary or {"counters": {}},
                   session={"warm": entry is not None},
                   queue=queue_counts,
                   drafts={"pending": pending_drafts})
