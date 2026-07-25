"""Account-management MCP tools: get_account, add_account, update_account,
set_account_active, remove_account.

All mutations go through AccountsConfigWriter (validate -> backup -> atomic
replace) and refresh the in-memory ConfigLoader afterwards. Secrets never
cross the wire: cookie import is by server-side file path only, and every
response passes through the mask_account filter.
"""
import json
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

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


class _CookieRollback(NamedTuple):
    """How to undo a cookie import whose config mutation then failed.

    ``backup`` is a copy of whatever ``dest`` held BEFORE the import. It is
    None only when ``dest`` did not previously exist — i.e. when the import
    genuinely created a new file and rolling back means deleting it.
    """

    dest: Path
    backup: Optional[Path]


def _import_cookies(account_id: str, cookie_file: str) -> Tuple[str, Optional[_CookieRollback]]:
    """Validate and copy a cookie export to config/<account_id>_cookies.json.
    Returns (repo-relative cookie_file_path, rollback token) — the token is
    None when the export already sat at the convention path and was used in
    place, so callers can roll back exactly the copies they made."""
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
    if src.resolve() == dest.resolve():
        # The export already sits at the convention path (e.g. the user named
        # it config/<account_id>_cookies.json) — copying it onto itself raises
        # SameFileError; validate and use it in place instead.
        logger.info("Cookie file for account '%s' already at %s; using in place.", account_id, dest)
        return f"config/{account_id}_cookies.json", None
    dest.parent.mkdir(parents=True, exist_ok=True)
    backup: Optional[Path] = None
    if dest.exists():
        # dest is this account's LIVE cookie file and copyfile is about to
        # clobber it. If the config mutation then fails, rolling back by
        # deleting dest would destroy a working login that is not backed up
        # anywhere, so stash the current contents first and restore them.
        fd, tmp_name = tempfile.mkstemp(dir=str(dest.parent),
                                        prefix=f".{account_id}_cookies-",
                                        suffix=".bak")
        os.close(fd)
        backup = Path(tmp_name)
        shutil.copy2(dest, backup)
    shutil.copyfile(src, dest)
    logger.info("Imported cookies for account '%s' to %s.", account_id, dest)
    return f"config/{account_id}_cookies.json", _CookieRollback(dest, backup)


def _discard_cookie_copy(rollback: Optional[_CookieRollback], account_id: str) -> None:
    """Roll back a cookie import whose config mutation then failed.

    Restores the account's previous cookie file when the import overwrote one,
    and deletes the copy only when the import created a brand-new file (for
    add_account, one belonging to an account that does not exist). Best-effort:
    the mutation error takes precedence."""
    if rollback is None:
        return
    try:
        if rollback.backup is not None:
            os.replace(rollback.backup, rollback.dest)
            logger.info("Restored the previous cookie file for '%s' after a failed mutation.",
                        account_id)
        else:
            rollback.dest.unlink()
            logger.info("Discarded cookie copy %s after failed mutation for '%s'.",
                        rollback.dest, account_id)
    except OSError:
        logger.warning("Could not roll back the cookie import at %s after a failed mutation.",
                       rollback.dest)


def _commit_cookie_copy(rollback: Optional[_CookieRollback]) -> None:
    """Drop the pre-import backup once the config mutation has succeeded, so
    the stashed copies do not accumulate in config/."""
    if rollback is None or rollback.backup is None:
        return
    try:
        rollback.backup.unlink()
    except OSError:
        logger.warning("Could not remove the cookie backup %s.", rollback.backup)


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
                          persona: Optional[str] = None,
                          is_active: bool = True) -> Dict[str, Any]:
        """Add an account to config/accounts.json (validated, backed up,
        atomic). `cookie_file` is a path ON THIS MACHINE to an exported
        x.com cookies JSON; it is validated and copied to
        config/<account_id>_cookies.json. Cookie values never pass through
        the tool call. `persona` is freeform markdown describing the
        account's voice/engagement style (max 4000 chars)."""
        if not _SAFE_ACCOUNT_ID.fullmatch(account_id or ""):
            raise ToolError(
                f"Invalid account id {account_id!r}: use letters, digits, '_' or '-'.")
        for raw in ctx.config_loader.get_accounts_config():
            if isinstance(raw, dict) and raw.get("account_id") == account_id:
                raise ToolError(f"Account '{account_id}' already exists.")
        cookie_rel: Optional[str] = None
        cookie_copied: Optional[_CookieRollback] = None
        if cookie_file:
            cookie_rel, cookie_copied = _import_cookies(account_id, cookie_file)
        entry: Dict[str, Any] = {
            "account_id": account_id,
            "is_active": bool(is_active),
            "target_keywords": list(target_keywords or []),
        }
        if cookie_rel:
            entry["cookie_file_path"] = cookie_rel
        if proxy:
            entry["proxy"] = proxy
        if persona:
            entry["persona"] = persona
        try:
            updated = _writer(ctx).mutate(lambda accounts: [*accounts, entry])
        except ConfigWriteError as e:
            _discard_cookie_copy(cookie_copied, account_id)
            raise ToolError(str(e)) from e
        _commit_cookie_copy(cookie_copied)
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
                             persona: Optional[str] = None,
                             cookie_file: Optional[str] = None,
                             action_config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Partially update an account. Only provided fields change (None =
        leave unchanged); pass an empty string to clear `proxy`. `persona`
        sets the freeform persona text; pass an empty string to clear it.
        `cookie_file` re-imports cookies like add_account. The account's
        warm browser session is closed, so the next action cold-starts on
        the new config."""
        _known_account(ctx, account)
        cookie_rel: Optional[str] = None
        cookie_copied: Optional[_CookieRollback] = None
        if cookie_file:
            cookie_rel, cookie_copied = _import_cookies(account, cookie_file)

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
                if persona is not None:
                    if persona == "":
                        raw.pop("persona", None)
                    else:
                        raw["persona"] = persona
                if action_config is not None:
                    raw["action_config"] = dict(action_config)
                if cookie_rel:
                    raw["cookie_file_path"] = cookie_rel
            return accounts

        try:
            updated = _writer(ctx).mutate(_apply)
        except ConfigWriteError as e:
            _discard_cookie_copy(cookie_copied, account)
            raise ToolError(str(e)) from e
        _commit_cookie_copy(cookie_copied)
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
