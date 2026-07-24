"""Account-management MCP tools: get_account, add_account, update_account,
set_account_active, remove_account.

All mutations go through AccountsConfigWriter (validate -> backup -> atomic
replace) and refresh the in-memory ConfigLoader afterwards. Secrets never
cross the wire: cookie import is by server-side file path only, and every
response passes through the mask_account filter.
"""
import json
import logging
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from xuse.core.config_loader import CONFIG_DIR, PROJECT_ROOT
from xuse.core.config_writer import AccountsConfigWriter, ConfigWriteError
from xuse.doctor import check_cookie_data

from . import executor as ex
from .executor import Ctx, ToolError
from .tools import guard, ok_

logger = logging.getLogger(__name__)

# Account ids land in file paths; same charset rule as the metrics tools.
_SAFE_ACCOUNT_ID = re.compile(r"[A-Za-z0-9_-]+")


def _writer(ctx: Ctx) -> AccountsConfigWriter:
    return AccountsConfigWriter(ctx.config_loader.accounts_file)


def _refresh(ctx: Ctx, accounts: List[Dict[str, Any]]) -> None:
    ctx.config_loader.accounts = accounts


def _known_account(ctx: Ctx, account: str) -> Dict[str, Any]:
    for raw in ctx.config_loader.get_accounts_config():
        if isinstance(raw, dict) and raw.get("account_id") == account:
            return raw
    raise ToolError(f"Unknown account '{account}' — not present in config/accounts.json.")


def _cookie_status(raw: Dict[str, Any]) -> Dict[str, Any]:
    if raw.get("cookies"):
        return {"cookies_configured": True, "cookie_file": None, "cookie_file_exists": None}
    cookie_file = raw.get("cookie_file_path")
    if not cookie_file:
        return {"cookies_configured": False, "cookie_file": None, "cookie_file_exists": False}
    path = Path(cookie_file)
    if not path.is_absolute():
        path = PROJECT_ROOT / cookie_file
    return {"cookies_configured": True, "cookie_file": cookie_file,
            "cookie_file_exists": path.is_file()}


def _import_cookies(account_id: str, cookie_file: str) -> str:
    """Validate and copy a cookie export to config/<account_id>_cookies.json.
    Returns the repo-relative cookie_file_path (wizard convention)."""
    src = Path(cookie_file).expanduser()
    if not src.is_file():
        raise ToolError(f"Cookie file not found: {cookie_file}")
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except Exception as e:
        raise ToolError(f"Cookie file is not valid JSON: {e}") from e
    valid, problems = check_cookie_data(data)
    if not valid:
        raise ToolError("Cookie file failed validation: " + "; ".join(problems))
    dest = CONFIG_DIR / f"{account_id}_cookies.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    logger.info("Imported cookies for account '%s' to %s.", account_id, dest)
    return f"config/{account_id}_cookies.json"


def register_account_tools(server, ctx: Ctx) -> None:
    """Register the five account-management tools on the FastMCP server."""

    @server.tool()
    @guard
    async def get_account(account: str) -> Dict[str, Any]:
        """Show one account's masked config (no cookies, no password, proxy
        credentials masked) plus cookie-file status. Read-only — never
        starts a browser."""
        raw = _known_account(ctx, account)
        return ok_(account=ex.mask_account(raw), **_cookie_status(raw))

    @server.tool()
    @guard
    async def add_account(account_id: str, cookie_file: Optional[str] = None,
                          proxy: Optional[str] = None,
                          target_keywords: Optional[List[str]] = None,
                          is_active: bool = True) -> Dict[str, Any]:
        """Add an account to config/accounts.json (validated, backed up,
        atomic). `cookie_file` is a path ON THIS MACHINE to an exported
        x.com cookies JSON; it is validated and copied to
        config/<account_id>_cookies.json. Cookie values never pass through
        the tool call."""
        if not _SAFE_ACCOUNT_ID.fullmatch(account_id or ""):
            raise ToolError(
                f"Invalid account id {account_id!r}: use letters, digits, '_' or '-'.")
        for raw in ctx.config_loader.get_accounts_config():
            if isinstance(raw, dict) and raw.get("account_id") == account_id:
                raise ToolError(f"Account '{account_id}' already exists.")
        cookie_rel = _import_cookies(account_id, cookie_file) if cookie_file else None
        entry: Dict[str, Any] = {
            "account_id": account_id,
            "is_active": bool(is_active),
            "target_keywords": list(target_keywords or []),
        }
        if cookie_rel:
            entry["cookie_file_path"] = cookie_rel
        if proxy:
            entry["proxy"] = proxy
        try:
            updated = _writer(ctx).mutate(lambda accounts: [*accounts, entry])
        except ConfigWriteError as e:
            raise ToolError(str(e)) from e
        _refresh(ctx, updated)
        return ok_(account=ex.mask_account(entry),
                   message=f"Account '{account_id}' added. Previous accounts.json "
                           "is backed up in config/backups/.")

    @server.tool()
    @guard
    async def update_account(account: str, is_active: Optional[bool] = None,
                             proxy: Optional[str] = None,
                             target_keywords: Optional[List[str]] = None,
                             competitor_profiles: Optional[List[str]] = None,
                             self_handles: Optional[List[str]] = None,
                             cookie_file: Optional[str] = None,
                             action_config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Partially update an account. Only provided fields change (None =
        leave unchanged); pass an empty string to clear `proxy`.
        `cookie_file` re-imports cookies like add_account. The account's
        warm browser session is closed, so the next action cold-starts on
        the new config."""
        _known_account(ctx, account)
        cookie_rel = _import_cookies(account, cookie_file) if cookie_file else None

        def _apply(accounts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            for raw in accounts:
                if not (isinstance(raw, dict) and raw.get("account_id") == account):
                    continue
                if is_active is not None:
                    raw["is_active"] = bool(is_active)
                if proxy is not None:
                    if proxy == "":
                        raw.pop("proxy", None)
                    else:
                        raw["proxy"] = proxy
                if target_keywords is not None:
                    raw["target_keywords"] = list(target_keywords)
                if competitor_profiles is not None:
                    raw["competitor_profiles"] = list(competitor_profiles)
                if self_handles is not None:
                    raw["self_handles"] = list(self_handles)
                if action_config is not None:
                    raw["action_config"] = dict(action_config)
                if cookie_rel:
                    raw["cookie_file_path"] = cookie_rel
            return accounts

        try:
            updated = _writer(ctx).mutate(_apply)
        except ConfigWriteError as e:
            raise ToolError(str(e)) from e
        _refresh(ctx, updated)
        await ctx.session_pool.close(account)
        return ok_(account=ex.mask_account(_known_account(ctx, account)),
                   message=f"Account '{account}' updated; warm session closed.")

    @server.tool()
    @guard
    async def set_account_active(account: str, active: bool) -> Dict[str, Any]:
        """Enable or pause an account without deleting it. Pausing closes
        its warm browser session."""
        _known_account(ctx, account)

        def _apply(accounts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            for raw in accounts:
                if isinstance(raw, dict) and raw.get("account_id") == account:
                    raw["is_active"] = bool(active)
            return accounts

        try:
            updated = _writer(ctx).mutate(_apply)
        except ConfigWriteError as e:
            raise ToolError(str(e)) from e
        _refresh(ctx, updated)
        if not active:
            await ctx.session_pool.close(account)
        return ok_(account=account, is_active=bool(active))

    @server.tool()
    @guard
    async def remove_account(account: str, confirm: bool = False) -> Dict[str, Any]:
        """Remove an account from config/accounts.json. Requires
        confirm=true. Closes the account's warm session. The backup written
        during the mutation (config/backups/) is the recovery path; the
        cookie file is left on disk."""
        if not confirm:
            raise ToolError("remove_account requires confirm=true.")
        _known_account(ctx, account)
        try:
            updated = _writer(ctx).mutate(
                lambda accounts: [
                    a for a in accounts
                    if not (isinstance(a, dict) and a.get("account_id") == account)
                ])
        except ConfigWriteError as e:
            raise ToolError(str(e)) from e
        _refresh(ctx, updated)
        await ctx.session_pool.close(account)
        return ok_(account=account, removed=True,
                   message="Removed. Previous accounts.json is backed up in config/backups/.")
