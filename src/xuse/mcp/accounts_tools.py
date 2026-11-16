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
from xuse.core.local_state import private_state_file
from xuse.browser.cookies import MAX_COOKIE_FILE_BYTES, parse_cookie_export_report
from xuse.browser.errors import SessionError

from . import executor as ex
from .executor import Ctx, ToolError
from .tools import guard, ok_
from .annotations import LOCAL_DESTRUCTIVE, LOCAL_WRITE, LOCAL_WRITE_IDEMPOTENT, READ_ONLY_LOCAL

logger = logging.getLogger(__name__)

# Account ids land in file paths; same charset rule as the metrics tools.
_SAFE_ACCOUNT_ID = re.compile(r"[A-Za-z0-9_-]+")


def _writer(ctx: Ctx) -> AccountsConfigWriter:
    return AccountsConfigWriter(ctx.config_loader.accounts_file)


def _refresh(ctx: Ctx, accounts: List[Dict[str, Any]]) -> None:
    ctx.config_loader.accounts = accounts


async def _close_after_config_change(ctx: Ctx, account: str) -> None:
    close = getattr(ctx.session_pool, "close_after_config_change", None)
    # Production pools retain this closure after caller cancellation. Simple
    # injected pools can keep their existing close-only interface.
    await (close if callable(close) else ctx.session_pool.close)(account)


def _known_account(ctx: Ctx, account: str) -> Dict[str, Any]:
    for raw in ctx.config_loader.get_accounts_config():
        if isinstance(raw, dict) and raw.get("account_id") == account:
            return raw
    raise ToolError(f"Unknown account '{account}', not present in config/accounts.json.")


def _require_current_account(accounts: List[Dict[str, Any]], account: str) -> None:
    """Recheck the writer's fresh snapshot before a stale client can save."""
    matches = sum(isinstance(raw, dict) and raw.get("account_id") == account for raw in accounts)
    if not matches:
        raise ConfigWriteError(f"Account '{account}' no longer exists in the selected configuration; reload accounts before retrying.")
    if matches > 1:
        raise ConfigWriteError(f"Duplicate account id '{account}' must be corrected before mutation.")


def _cookie_status(raw: Dict[str, Any], config_loader: Any = None) -> Dict[str, Any]:
    if raw.get("cookies") is not None:
        return {"cookies_configured": True, "cookie_file": None, "cookie_file_exists": None}
    cookie_file = raw.get("cookie_file_path")
    if not isinstance(cookie_file, str) or not cookie_file:
        return {"cookies_configured": False, "cookie_file": None, "cookie_file_exists": False}
    path = Path(cookie_file)
    config_dir = Path(getattr(config_loader, "accounts_file", CONFIG_DIR / "accounts.json")).parent
    candidates = [path] if path.is_absolute() else [config_dir / path, PROJECT_ROOT / path]
    try:
        exists = any(candidate.is_file() for candidate in candidates)
    except OSError:
        exists = False
    return {"cookies_configured": True, "cookie_file": cookie_file,
            "cookie_file_exists": exists}


class _CookieRollback(NamedTuple):
    """How to undo a cookie import whose config mutation then failed.

    ``backup`` is a copy of whatever ``dest`` held BEFORE the import. It is
    None only when ``dest`` did not previously exist — i.e. when the import
    genuinely created a new file and rolling back means deleting it.
    """

    dest: Path
    backup: Optional[Path]


_MAX_REPORTED_SKIPPED = 20


def _skipped_result(skipped: List[Dict[str, str]]) -> Dict[str, Any]:
    """Result fields for skipped cookies: first 20 entries plus the true count."""
    if not skipped:
        return {}
    return {"skipped_cookies": skipped[:_MAX_REPORTED_SKIPPED], "skipped_cookie_count": len(skipped)}


def _import_cookies(account_id: str, cookie_file: str, *, config_dir: Optional[Path] = None) -> Tuple[str, Optional[_CookieRollback], List[Dict[str, str]]]:
    """Validate and atomically import cookies beside the selected accounts file.
    Returns (cookie_file_path, rollback token, skipped cookies as
    name/rule pairs). Custom homes use absolute paths
    so legacy and async backends resolve the same file. Even an export
    already at the destination is normalized through a private temporary file
    and backed up, so all backends receive the same cookie-array format."""
    if not _SAFE_ACCOUNT_ID.fullmatch(account_id or ""):
        raise ToolError("Cookie import requires an account id using letters, digits, '_' or '-'.")
    src = Path(cookie_file).expanduser()
    if not src.is_file():
        raise ToolError(f"Cookie file not found: {cookie_file}")
    try:
        with src.open("rb") as stream:
            contents = stream.read(MAX_COOKIE_FILE_BYTES + 1)
        report = parse_cookie_export_report(contents)
    except OSError:
        raise ToolError("Cookie file failed validation: export secure, unexpired x.com cookies including auth_token and ct0.") from None
    except SessionError as exc:
        message = "Cookie file failed validation: export secure, unexpired x.com cookies including auth_token and ct0."
        if exc.detail:
            message += f" ({exc.detail})"
        raise ToolError(message) from None
    data = report.cookies
    skipped = [{"name": name, "rule": rule} for name, rule in report.skipped]
    try:
        normalized = (json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    except UnicodeError:
        raise ToolError("Cookie file failed validation: invalid Unicode in the normalized export.") from None
    if len(normalized) > MAX_COOKIE_FILE_BYTES:
        raise ToolError("Cookie file failed validation: normalized export exceeds the import size limit.")
    directory = Path(config_dir) if config_dir is not None else CONFIG_DIR
    dest = directory.resolve() / f"{account_id}_cookies.json"
    backup: Optional[Path] = None
    existed = dest.exists()
    staged: Optional[Path] = None
    prepared = False
    try:
        private_state_file(dest)
        prepared = True
        if existed:
            # Config mutation may still fail after replacing this file.
            fd, tmp_name = tempfile.mkstemp(dir=str(dest.parent),
                                            prefix=f".{account_id}_cookies-", suffix=".bak")
            os.close(fd)
            backup = Path(tmp_name)
            private_state_file(backup)
            shutil.copyfile(dest, backup)
        fd, tmp_name = tempfile.mkstemp(dir=str(dest.parent),
                                        prefix=f".{account_id}_cookies-", suffix=".tmp")
        os.close(fd)
        staged = Path(tmp_name)
        private_state_file(staged)
        # Save only the validated snapshot. Storage-state origins and exporter
        # metadata are not credentials and must not be copied into our store.
        with staged.open("wb") as stream:
            stream.write(normalized)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staged, dest)
    except OSError:
        # A failed replace leaves the old file intact. A new empty file made
        # while establishing private permissions belongs to this attempt only.
        for abandoned in (staged, backup, dest if prepared and not existed else None):
            if abandoned is not None:
                try:
                    abandoned.unlink(missing_ok=True)
                except OSError:
                    pass
        raise ToolError("Cookie import could not be saved; the existing cookie file was not replaced.") from None
    logger.info("Imported cookies for account '%s' to %s.", account_id, dest)
    stored_path = f"config/{account_id}_cookies.json" if dest.parent == CONFIG_DIR.resolve() else str(dest)
    return stored_path, _CookieRollback(dest, backup), skipped


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
    """Register the five account-management tools on the MCP server."""

    @server.tool(annotations=READ_ONLY_LOCAL)
    @guard
    async def get_account(account: str) -> Dict[str, Any]:
        """Show one account's masked config (no cookies, no password, proxy
        credentials masked) plus cookie-file status. Read-only, never
        starts a browser."""
        raw = _known_account(ctx, account)
        return ok_(account=ex.mask_account(raw), **_cookie_status(raw, ctx.config_loader))

    @server.tool(annotations=LOCAL_WRITE)
    @guard
    async def add_account(account_id: str, cookie_file: Optional[str] = None,
                          proxy: Optional[str] = None,
                          target_keywords: Optional[List[str]] = None,
                          persona: Optional[str] = None,
                          is_active: bool = True) -> Dict[str, Any]:
        """Add an account to config/accounts.json (validated, backed up,
        atomic). `cookie_file` is a path ON THIS MACHINE to an exported
        x.com cookies JSON; it is validated and copied beside the selected
        accounts configuration as <account_id>_cookies.json. Cookie values never pass through
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
        skipped_cookies: List[Dict[str, str]] = []
        if cookie_file:
            cookie_rel, cookie_copied, skipped_cookies = _import_cookies(account_id, cookie_file,
                config_dir=Path(ctx.config_loader.accounts_file).parent)
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
        extra = _skipped_result(skipped_cookies)
        return ok_(account=ex.mask_account(entry),
                   message=f"Account '{account_id}' added. Previous accounts.json "
                           "is backed up in config/backups/.", **extra)

    @server.tool(annotations=LOCAL_WRITE_IDEMPOTENT)
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
        skipped_cookies: List[Dict[str, str]] = []
        if cookie_file:
            cookie_rel, cookie_copied, skipped_cookies = _import_cookies(account, cookie_file,
                config_dir=Path(ctx.config_loader.accounts_file).parent)

        def _apply(accounts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            _require_current_account(accounts, account)
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
                    # Inline credentials take precedence during browser startup.
                    # An explicit file refresh must replace that older source.
                    raw.pop("cookies", None)
            return accounts

        try:
            updated = _writer(ctx).mutate(_apply)
        except ConfigWriteError as e:
            _discard_cookie_copy(cookie_copied, account)
            raise ToolError(str(e)) from e
        _commit_cookie_copy(cookie_copied)
        _refresh(ctx, updated)
        await _close_after_config_change(ctx, account)
        extra = _skipped_result(skipped_cookies)
        return ok_(account=ex.mask_account(_known_account(ctx, account)),
                   message=f"Account '{account}' updated; warm session closed.", **extra)

    @server.tool(annotations=LOCAL_WRITE_IDEMPOTENT)
    @guard
    async def set_account_active(account: str, active: bool) -> Dict[str, Any]:
        """Enable or pause an account without deleting it. Pausing closes
        its warm browser session."""
        _known_account(ctx, account)

        def _apply(accounts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            _require_current_account(accounts, account)
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
            await _close_after_config_change(ctx, account)
        return ok_(account=account, is_active=bool(active))

    @server.tool(annotations=LOCAL_DESTRUCTIVE)
    @guard
    async def remove_account(account: str, confirm: bool = False) -> Dict[str, Any]:
        """Remove an account from config/accounts.json. Requires
        confirm=true. Closes the account's warm session. The backup written
        during the mutation (config/backups/) is the recovery path; the
        cookie file is left on disk."""
        if not confirm:
            raise ToolError("remove_account requires confirm=true.")
        _known_account(ctx, account)
        def _remove(accounts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            _require_current_account(accounts, account)
            return [raw for raw in accounts
                    if not (isinstance(raw, dict) and raw.get("account_id") == account)]
        try:
            updated = _writer(ctx).mutate(_remove)
        except ConfigWriteError as e:
            raise ToolError(str(e)) from e
        _refresh(ctx, updated)
        await _close_after_config_change(ctx, account)
        return ok_(account=account, removed=True,
                   message="Removed. Previous accounts.json is backed up in config/backups/.")
