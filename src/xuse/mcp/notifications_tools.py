"""Bounded structured notification reads for the signed-in X account."""
from typing import Any, Literal

from .annotations import READ_ONLY_FROM_X
from .browser_bridge import browser_call
from .browser_tools import _require_browser


def register_notifications_tools(server, ctx):
    from .tools import guard, ok_

    @server.tool(annotations=READ_ONLY_FROM_X)
    @guard
    async def get_notifications(account: str, limit: int = 20,
                                view: Literal["all", "mentions"] = "all") -> dict[str, Any]:
        """Read bounded visible X notifications from a verified All or Mentions tab.

        Reports observed actors, related posts, type evidence and unread state;
        absent evidence remains unknown. Visiting this view may mark X
        notifications read. Results cover only the visible supported UI.
        """
        from xuse.browser.messaging import _limit
        _require_browser(ctx)
        _limit(limit, 50)
        if view not in ("all", "mentions"):
            raise ValueError("view must be all or mentions.")
        result = await browser_call(ctx, account, "get_notifications", limit, view=view)
        return ok_(account=account, **result)
