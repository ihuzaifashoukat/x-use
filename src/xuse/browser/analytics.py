"""Owner analytics reads supported by observed visible X UI evidence.

The current evidenced analytics view is the Creator Studio Premium paywall.
Eligible dashboards remain unsupported until their DOM contract is observed.
No timeline counts, local action metrics, private endpoints, or subscription
actions are used to manufacture owner analytics.
"""
from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlsplit

from .errors import BrowserActionError, BrowserBlocked
from .messaging import _limit, _observed_at, _profile_handle, _safe_dom_errors


_STUDIO_URL = "https://x.com/i/jf/creators/studio"
_PAYWALL_URL = "https://x.com/i/jf/creators/analytics_paywall"
_UPGRADE_URL = "https://x.com/i/premium_sign_up?referring_page=analytics"
_PREMIUM_TITLE = "Advanced analytics with X Premium"
_PREMIUM_EXPLANATION = (
    "See your profile analytics, understand your audience and more. Upgrade to continue."
)


def parse_account_analytics_snapshot(snapshot: dict[str, Any], limit: int = 20) -> dict[str, Any]:
    """Parse a bounded evidence snapshot, preserving unavailable/unknown state.

    A recognized paywall proves only a visible subscription requirement. It
    does not prove the owner's current tier, or imply any zero-valued metrics.
    """
    limit = _limit(limit, 50)
    owner = snapshot.get("owner_handle")
    if not isinstance(owner, str) or _profile_handle("/" + owner) != owner:
        raise BrowserActionError("profile_mismatch")
    premium_required = (
        snapshot.get("url") == _PAYWALL_URL
        and snapshot.get("primary_available") is True
        and snapshot.get("analytics_heading") is True
        and snapshot.get("premium_title") is True
        and snapshot.get("premium_explanation") is True
        and snapshot.get("upgrade_urls") == [_UPGRADE_URL]
    )
    status = "premium_required" if premium_required else "unsupported_dom"
    return {
        "owner": {"handle": owner, "url": "https://x.com/" + owner,
                  "source": "visible_signed_in_profile_navigation"},
        "status": status, "available": False, "metrics": [], "count": 0, "limit": limit,
        "period": None, "observed_at": _observed_at(), "source": "browser_dom",
        "analytics_scope": "signed_in_owner", "coverage": "visible_only", "partial": True,
        "unavailable_reason": status,
        "subscription": {"status": "required" if premium_required else "unknown",
                         "visible_text": _PREMIUM_TITLE if premium_required else None},
    }


class AnalyticsMixin:
    """Requires page, navigate(), check_blocked(), and MessagingMixin helpers."""

    analytics_timeout_ms = 20_000

    async def _analytics_wait_one(self, locator: Any) -> Any:
        deadline = asyncio.get_running_loop().time() + self.analytics_timeout_ms / 1000
        while True:
            await self.check_blocked()
            visible = await self._visible(locator)
            if len(visible) == 1:
                return visible[0]
            if len(visible) > 1 or asyncio.get_running_loop().time() >= deadline:
                raise BrowserActionError("unsupported_dom")
            await asyncio.sleep(0.1)

    async def _analytics_owner(self) -> str:
        # Observed owner navigation is distinct from profile links in posts or
        # menus. Presence of a signed-in account switcher is also required.
        await self._analytics_wait_one(self.page.locator('[data-testid="SideNav_AccountSwitcher_Button"]'))
        link = await self._analytics_wait_one(self.page.locator('[data-testid="AppTabBar_Profile_Link"]'))
        handle = _profile_handle(await link.get_attribute("href"))
        if not handle:
            raise BrowserActionError("profile_mismatch")
        expected = getattr(self, "account", {}).get("self_handles") or []
        if expected and handle not in {str(item).lower().lstrip("@") for item in expected}:
            raise BrowserActionError("profile_mismatch")
        return handle

    async def _analytics_snapshot(self, owner: str) -> dict[str, Any]:
        roots = await self._visible(self.page.locator('[data-testid="primaryColumn"]'))
        snapshot = {"url": self.page.url.rstrip("/"), "owner_handle": owner,
                    "primary_available": len(roots) == 1}
        if len(roots) != 1:
            return snapshot
        scope = roots[0]
        async def unique_text(value: str) -> bool:
            return len(await self._visible(scope.get_by_text(value, exact=True))) == 1
        snapshot.update({
            "analytics_heading": await unique_text("Analytics"),
            "premium_title": await unique_text(_PREMIUM_TITLE),
            "premium_explanation": await unique_text(_PREMIUM_EXPLANATION),
            "upgrade_urls": [await link.get_attribute("href") for link in
                             await self._visible(scope.get_by_role("link", name="Upgrade", exact=True))],
        })
        return snapshot

    @_safe_dom_errors("read_failed")
    async def get_account_analytics(self, limit: int = 20) -> dict[str, Any]:
        """Read the signed-in owner's evidenced analytics availability.

        Opens observed Creator Studio route and its exact Analytics button.
        Never clicks Upgrade, changes a period, or inspects hidden API data.
        """
        limit = _limit(limit, 50)
        try:
            await self.check_blocked()
        except BrowserBlocked as exc:
            # A PIN gates encrypted inbox reads, not owner analytics. Leave
            # only the observed inbox recovery route; durable policy retains
            # its pause and all non-PIN blocks still stop navigation.
            current = urlsplit(self.page.url)
            if exc.reason != "pin_required" or current.scheme != "https" or current.netloc != "x.com" or current.path != "/i/chat/pin/recovery":
                raise
            await self.navigate(_STUDIO_URL)
        owner = await self._analytics_owner()
        if self.page.url.rstrip("/") != _PAYWALL_URL:
            if self.page.url.rstrip("/") != _STUDIO_URL:
                await self.navigate(_STUDIO_URL)
            if await self._analytics_owner() != owner:
                raise BrowserActionError("profile_mismatch")
            button = await self._analytics_wait_one(self.page.get_by_role("button", name="Analytics", exact=True))
            await button.click()
            # Wait for SPA route change, then only recognize the evidenced
            # paywall. A different eligible route is explicitly unsupported.
            deadline = asyncio.get_running_loop().time() + self.analytics_timeout_ms / 1000
            while self.page.url.rstrip("/") == _STUDIO_URL:
                await self.check_blocked()
                if asyncio.get_running_loop().time() >= deadline:
                    raise BrowserActionError("unsupported_dom")
                await asyncio.sleep(0.1)
        current = urlsplit(self.page.url)
        if current.scheme != "https" or current.netloc != "x.com":
            raise BrowserActionError("external_redirect")
        await self.check_blocked()
        if await self._analytics_owner() != owner:
            raise BrowserActionError("profile_mismatch")
        if self.page.url.rstrip("/") == _PAYWALL_URL:
            # Route change precedes hydration. An absent shell never proves
            # Premium is required; wait for the observed primary UI first.
            deadline = asyncio.get_running_loop().time() + self.analytics_timeout_ms / 1000
            while True:
                await self.check_blocked()
                snapshot = await self._analytics_snapshot(owner)
                if snapshot.get("premium_explanation") or asyncio.get_running_loop().time() >= deadline:
                    break
                await asyncio.sleep(0.1)
        else:
            snapshot = {"owner_handle": owner, "url": self.page.url}
        if await self._analytics_owner() != owner or self.page.url.rstrip("/") != snapshot["url"]:
            raise BrowserActionError("profile_mismatch")
        return parse_account_analytics_snapshot(snapshot, limit)
