"""Proxy-management MCP tools: list_proxies, add_proxy, remove_proxy,
test_proxy.

Pools live in settings.json (browser_settings.proxy_pools); accounts point at
a direct URL or "pool:<name>". Mutations go through SettingsConfigWriter
(backup + atomic replace) and refresh the in-memory ConfigLoader. Every URL
in every response is fully userinfo-masked (scheme://***@host:port).
test_proxy drives a THROWAWAY, cookieless browser (no account cookies, never
the warm session pool): startup performs an anonymous x.com/home load, then
it fetches an IP-echo endpoint — so the reported latency includes browser
boot.
"""
import asyncio
import json
import logging
import re
import time
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from xuse.core.browser_manager import BrowserManager
from xuse.core.config_loader import PROJECT_ROOT
from xuse.core.settings_writer import SettingsConfigWriter, SettingsWriteError
from xuse.utils.proxy_manager import ProxyManager
from xuse.utils.proxy_manager import validate_proxy_url as _validate_proxy_url
from xuse.utils.sanitize import mask_url_userinfo

from . import executor as ex
from .executor import Ctx, ToolError
from .tools import guard, ok_
from .annotations import LOCAL_DESTRUCTIVE, LOCAL_WRITE_IDEMPOTENT, READ_ONLY_FROM_X, READ_ONLY_LOCAL

logger = logging.getLogger(__name__)

_VALID_SCHEMES = ("http", "https", "socks4", "socks5")
IP_ECHO_URL = "https://api.ipify.org?format=json"


def mask_proxy(url: Optional[str]) -> Optional[str]:
    """Mask the whole userinfo: http://user:pass@host:port -> http://***@host:port.

    Delegates to the single implementation in xuse.utils.sanitize.
    """
    return mask_url_userinfo(url)


def validate_proxy_url(url: str) -> str:
    """Trim + validate scheme and host. URLs containing ${ENV_VAR} pass a
    structural check only — the env var may legitimately resolve later.
    Delegates to the shared validator in xuse.utils.proxy_manager so the MCP
    tools and the accounts write gate cannot drift."""
    try:
        return _validate_proxy_url(url)
    except ValueError as e:
        raise ToolError(str(e)) from e


def _pools_snapshot(ctx: Ctx) -> Dict[str, Any]:
    pools_raw = ctx.config_loader.get_setting("browser_settings.proxy_pools", {}) or {}
    state_file = ctx.config_loader.get_setting(
        "browser_settings.proxy_pool_state_file", "data/proxy_pools_state.json")
    state: Dict[str, Any] = {}
    state_path = PROJECT_ROOT / state_file
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            state = {}
    return {
        name: {
            "size": len(urls),
            "proxies": [mask_proxy(u) for u in urls],
            "rotation_cursor": state.get(name),
        }
        for name, urls in pools_raw.items()
    }


def _mutate_pools(ctx: Ctx, fn) -> Dict[str, Any]:
    writer = SettingsConfigWriter(ctx.config_loader.settings_file)

    def _apply(settings: Dict[str, Any]) -> Dict[str, Any]:
        browser = settings.setdefault("browser_settings", {})
        pools = browser.setdefault("proxy_pools", {})
        fn(pools)
        return settings

    try:
        updated = writer.mutate(_apply)
    except SettingsWriteError as e:
        raise ToolError(ex.sanitize_text(str(e))) from e
    ctx.config_loader.settings = updated
    return updated


def register_proxy_tools(server, ctx: Ctx) -> None:
    """Register the four proxy-management tools on the FastMCP server."""

    @server.tool(annotations=READ_ONLY_LOCAL)
    @guard
    async def list_proxies() -> Dict[str, Any]:
        """List proxy pools (size, masked members, rotation cursor), the global
        pool strategy, each account's assignment, and the global fallback
        proxy. Read-only, never starts a browser."""
        accounts = [
            {"account_id": a["account_id"], "proxy": mask_proxy(a.get("proxy"))}
            for a in ctx.config_loader.get_accounts_config()
            if isinstance(a, dict) and a.get("account_id")
        ]
        fallback = ctx.config_loader.get_setting("browser_settings.proxy")
        return ok_(
            pools=_pools_snapshot(ctx),
            strategy=ctx.config_loader.get_setting(
                "browser_settings.proxy_pool_strategy", "hash"),
            accounts=accounts,
            global_proxy=mask_proxy(fallback) if fallback else None,
        )

    @server.tool(annotations=LOCAL_WRITE_IDEMPOTENT)
    @guard
    async def add_proxy(pool: str, proxy_url: str) -> Dict[str, Any]:
        """Add a proxy URL to a named pool (created if new) in settings.json.
        Validated, deduped, backed up, atomic. Credentials are masked in the
        response. Assign it to accounts via update_account(proxy="pool:<name>")
        or a direct URL."""
        if not pool or not pool.strip():
            raise ToolError("pool must not be empty.")
        pool = pool.strip()
        url = validate_proxy_url(proxy_url)

        def _fn(pools: Dict[str, Any]) -> None:
            members = pools.setdefault(pool, [])
            if url in members:
                raise ToolError(f"Proxy already present in pool '{pool}'.")
            members.append(url)

        _mutate_pools(ctx, _fn)
        return ok_(pool=pool, pools=_pools_snapshot(ctx),
                   message=f"Added to pool '{pool}'. Assign with "
                           f"update_account(account, proxy=\"pool:{pool}\").")

    @server.tool(annotations=LOCAL_DESTRUCTIVE)
    @guard
    async def remove_proxy(pool: str, proxy_url: str, confirm: bool = False) -> Dict[str, Any]:
        """Remove a proxy URL from a pool. Requires confirm=true. The response
        lists accounts still assigned pool:<name>. Emptying a referenced pool
        is allowed but reported (those accounts fall back to no proxy)."""
        if not confirm:
            raise ToolError("remove_proxy requires confirm=true.")
        url = (proxy_url or "").strip()

        def _fn(pools: Dict[str, Any]) -> None:
            members = pools.get(pool)
            if not members or url not in members:
                raise ToolError(f"Proxy not found in pool '{pool}'.")
            members.remove(url)

        _mutate_pools(ctx, _fn)
        affected = [
            a["account_id"]
            for a in ctx.config_loader.get_accounts_config()
            if isinstance(a, dict) and a.get("proxy") == f"pool:{pool}"
        ]
        return ok_(pool=pool, removed=True, pools=_pools_snapshot(ctx),
                   affected_accounts=affected)

    @server.tool(annotations=READ_ONLY_FROM_X)
    @guard
    async def test_proxy(account: Optional[str] = None,
                         proxy_url: Optional[str] = None) -> Dict[str, Any]:
        """Probe a proxy with a throwaway browser: resolves the effective proxy
        (explicit proxy_url wins; otherwise the account's direct URL or
        pool:<name> assignment), opens an IP-echo page, and reports the egress
        IP + latency. The probe browser is throwaway and cookieless, never
        account cookies or the warm session pool, but startup performs an
        anonymous x.com/home load before the IP-echo fetch, so latency_ms
        includes browser boot. With a round_robin pool, probing via `account`
        advances the pool's rotation cursor. Credentials masked in the
        response."""
        if proxy_url and proxy_url.strip():
            resolved = validate_proxy_url(proxy_url)
        elif account:
            account_id, raw, _ = ex.resolve_account(ctx, account)
            value = raw.get("proxy")
            if not value:
                raise ToolError(
                    f"Account '{account_id}' has no proxy configured. "
                    "Pass proxy_url or assign one with update_account.")
            resolved = ProxyManager(ctx.config_loader).resolve(value, account_id)
            if not resolved:
                raise ToolError(f"Could not resolve a proxy from {mask_proxy(value)!r}.")
        else:
            raise ToolError("Pass proxy_url or account.")

        def _probe() -> Dict[str, Any]:
            manager = BrowserManager(account_config={
                "account_id": "__proxy_probe__", "proxy": resolved, "is_active": True})
            try:
                started = time.monotonic()
                driver = manager.get_driver()
                # Dead proxies hang until Chrome's default navigation timeout
                # (~2 min); cap it so a bad proxy fails in seconds.
                driver.set_page_load_timeout(25)
                driver.get(IP_ECHO_URL)
                latency_ms = int((time.monotonic() - started) * 1000)
                match = re.search(r'"ip"\s*:\s*"([^"]+)"', driver.page_source or "")
                if not match:
                    # Chrome error pages (ERR_PROXY_CONNECTION_FAILED etc.)
                    # load WITHOUT raising — no "ip" in the source means the
                    # probe failed, never ok:true with a null IP (live finding).
                    raise ToolError(
                        f"Proxy {mask_proxy(resolved)} did not report an egress IP "
                        "(connection failed or blocked).")
                return {"egress_ip": match.group(1),
                        "latency_ms": latency_ms}
            finally:
                manager.close_driver()

        try:
            probe = await asyncio.to_thread(_probe)
        except ToolError:
            raise
        except Exception as e:
            raise ToolError(ex.sanitize_text(
                f"Proxy probe failed for {mask_proxy(resolved)}: {e}")) from e
        return ok_(proxy=mask_proxy(resolved), **probe)
