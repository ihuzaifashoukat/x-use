"""Read-only signed-in X owner analytics availability tools."""
from typing import Any

from .annotations import READ_ONLY_FROM_X
from .browser_bridge import browser_call
from .browser_tools import _require_browser


def register_analytics_tools(server, ctx):
    from .tools import guard, ok_

    @server.tool(annotations=READ_ONLY_FROM_X)
    @guard
    async def get_account_analytics(account: str, limit: int = 20) -> dict[str, Any]:
        """Read the signed-in owner's X analytics availability. Requires X Premium when the observed paywall says so; unsupported layouts return no metrics. Distinct from local action metrics and public post counts."""
        from xuse.browser.messaging import _limit
        _require_browser(ctx)
        _limit(limit, 50)
        result = await browser_call(ctx, account, "get_account_analytics", limit)
        return ok_(account=account, **result)
