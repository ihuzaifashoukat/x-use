"""Observed analytics paywall contract on fully intercepted native pages."""
import pytest

from xuse.browser.analytics import AnalyticsMixin
from xuse.browser.errors import BrowserActionError, BrowserBlocked
from xuse.browser.page import XBrowser
from test_messaging_dom import app, native_browser  # noqa: F401


pytestmark = pytest.mark.asyncio(loop_scope="module")


class AnalyticsBrowser(XBrowser):
    analytics_timeout_ms = 600


def owner_nav(handle="owner"):
    # FixtureApp.document already supplies the signed-in account switcher.
    return f'<a data-testid="AppTabBar_Profile_Link" href="/{handle}">Profile</a>'


def paywall(*, nav="owner", upgrade=True, hidden=False):
    link = '<a id="upgrade" href="https://x.com/i/premium_sign_up?referring_page=analytics">Upgrade</a>' if upgrade else ''
    return owner_nav(nav) + f'''<main><section data-testid="primaryColumn" {"hidden" if hidden else ""}>
      <h1>Analytics</h1><h2>Advanced analytics with X Premium</h2>
      <p>See your profile analytics, understand your audience and more. Upgrade to continue.</p>{link}
      </section></main><script>document.documentElement.dataset.upgrades='0';
      document.querySelector('#upgrade')?.addEventListener('click',e=>{{e.preventDefault();document.documentElement.dataset.upgrades='1';}});</script>'''


def setup(app):
    app.adapter = AnalyticsBrowser(app.page, account={"self_handles": ["owner"]})


async def test_native_observed_studio_click_returns_paywall_and_never_upgrades(app):
    setup(app)
    app.document("/home", owner_nav())
    app.document("/i/jf/creators/studio", owner_nav() + '''<button id="analytics">Analytics</button><script>
      document.documentElement.dataset.analyticsClicks='0';
      document.querySelector('#analytics').onclick=()=>{
        document.documentElement.dataset.analyticsClicks='1';
        location.href='/i/jf/creators/analytics_paywall';};</script>''')
    app.document("/i/jf/creators/analytics_paywall", paywall())
    await app.page.goto("https://x.com/home")
    result = await app.adapter.get_account_analytics()
    assert result["status"] == "premium_required" and result["metrics"] == []
    assert await app.page.locator("html").get_attribute("data-upgrades") == "0"
    assert app.page.url == "https://x.com/i/jf/creators/analytics_paywall"
    assert [path for _, path, kind in app.requests if kind == "document"] == ["/home", "/i/jf/creators/studio", "/i/jf/creators/analytics_paywall"]


async def test_native_paywall_reuses_page_and_reads_owner_again(app):
    setup(app)
    app.document("/i/jf/creators/analytics_paywall", paywall())
    await app.page.goto("https://x.com/i/jf/creators/analytics_paywall")
    result = await app.adapter.get_account_analytics()
    assert result["owner"]["handle"] == "owner" and result["status"] == "premium_required"
    assert len([r for r in app.requests if r[2] == "document"]) == 1


@pytest.mark.parametrize("body", [paywall(upgrade=False), paywall(hidden=True)])
async def test_native_incomplete_hidden_paywall_remains_unsupported(app, body):
    setup(app)
    app.document("/i/jf/creators/analytics_paywall", body)
    await app.page.goto("https://x.com/i/jf/creators/analytics_paywall")
    result = await app.adapter.get_account_analytics()
    assert result["status"] == "unsupported_dom" and result["metrics"] == []


async def test_native_owner_change_during_navigation_refuses_data(app):
    setup(app)
    app.document("/home", owner_nav())
    app.document("/i/jf/creators/studio", owner_nav("other") + '<button>Analytics</button>')
    await app.page.goto("https://x.com/home")
    with pytest.raises(BrowserActionError) as exc:
        await app.adapter.get_account_analytics()
    assert exc.value.reason == "profile_mismatch"


async def test_native_eligible_unknown_dashboard_reports_unsupported(app):
    setup(app)
    app.document("/i/jf/creators/studio", owner_nav() + '''<button>Analytics</button><script>
      document.querySelector('button:not([data-testid])').onclick=()=>{
        history.pushState(null,'','/i/jf/creators/analytics');
        document.body.insertAdjacentHTML('beforeend','<section><h1>Analytics</h1><p>Impressions 999</p></section>');};</script>''')
    await app.page.goto("https://x.com/i/jf/creators/studio")
    result = await app.adapter.get_account_analytics()
    assert result["status"] == "unsupported_dom" and result["metrics"] == []


@pytest.mark.parametrize("route,body,reason", [
    ("/i/flow/login", owner_nav(), "login_required"),
    ("/account/access", owner_nav(), "challenge"),
    ("/home", owner_nav() + '<p>Your account is locked</p>', "account_locked"),
])
async def test_native_other_blocks_never_navigate_to_analytics(app, route, body, reason):
    setup(app)
    app.document(route, body)
    await app.page.goto("https://x.com" + route)
    with pytest.raises(BrowserBlocked) as exc:
        await app.adapter.get_account_analytics()
    assert exc.value.reason == reason
    assert app.page.url == "https://x.com" + route


async def test_native_known_inbox_pin_route_allows_readonly_analytics_navigation(app):
    setup(app)
    app.document("/i/chat/pin/recovery", owner_nav() + '<p>Enter Passcode</p>')
    app.document("/i/jf/creators/studio", owner_nav() + '''<button>Analytics</button><script>
      document.querySelector('button:not([data-testid])').onclick=()=>location.href='/i/jf/creators/analytics_paywall';</script>''')
    app.document("/i/jf/creators/analytics_paywall", paywall())
    await app.page.goto("https://x.com/i/chat/pin/recovery")
    result = await app.adapter.get_account_analytics()
    assert result["status"] == "premium_required" and result["metrics"] == []
    assert not any('/messages/' in path for _, path, _ in app.requests)
