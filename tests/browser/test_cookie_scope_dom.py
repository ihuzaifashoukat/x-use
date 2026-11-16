"""Native cookie-store checks with synthetic values and offline contexts only."""
import asyncio
import time

import pytest
import pytest_asyncio

from xuse.browser.cookies import normalize_cookies
from xuse.browser.errors import SessionError
from test_messaging_dom import native_browser  # noqa: F401


pytestmark = pytest.mark.asyncio(loop_scope="module")


def credentials():
    return [
        {"name": "auth_token", "value": "synthetic-auth", "domain": ".x.com", "secure": True,
         "httpOnly": True},
        {"name": "ct0", "value": "synthetic-csrf", "domain": ".x.com", "secure": True},
    ]


@pytest_asyncio.fixture(loop_scope="module")
async def cookie_context(native_browser):
    context = await native_browser.new_context(service_workers="block")
    try:
        # Pages remain blank; offline mode also forbids accidental external
        # traffic while Chromium's cookie eligibility is inspected.
        await context.set_offline(True)
        yield context
    finally:
        await context.close()


@pytest.mark.parametrize("domain,host_only", [
    (".x.com", True), ("x.com", True), (".x.com", False), ("x.com", False),
])
async def test_native_cookie_scope_preserves_host_only_metadata(cookie_context, domain, host_only):
    payload = credentials()
    for cookie in payload:
        cookie.update(domain=domain, hostOnly=host_only)
    await cookie_context.add_cookies(normalize_cookies(payload))
    root_cookies = await cookie_context.cookies("https://x.com")
    # The drivers' cookies(url) helper filters domains in JavaScript and can
    # return host-only cookies for subdomains. Ask Chromium's network service
    # for actual URL eligibility, without navigating or sending requests.
    page = await asyncio.wait_for(cookie_context.new_page(), timeout=15)
    cdp = await asyncio.wait_for(cookie_context.new_cdp_session(page), timeout=15)
    try:
        subdomain_cookies = (await asyncio.wait_for(cdp.send("Network.getCookies", {
            "urls": ["https://sub.x.com"]}), timeout=15))["cookies"]
    finally:
        await cdp.detach()
        await page.close()
    assert {cookie["name"] for cookie in root_cookies} == {"auth_token", "ct0"}
    assert {cookie["name"] for cookie in subdomain_cookies} == (set() if host_only else {"auth_token", "ct0"})
    assert await cookie_context.cookies("https://evilx.com") == []
    assert await cookie_context.cookies("http://x.com") == []
    assert next(cookie for cookie in root_cookies if cookie["name"] == "auth_token")["httpOnly"] is True


async def test_native_storage_state_envelope_never_restores_origins(cookie_context):
    payload = {"cookies": credentials(), "origins": [
        {"origin": "https://x.com", "localStorage": [
            {"name": "private-state", "value": "must-not-restore"}]},
        {"origin": "https://elsewhere.invalid", "localStorage": [
            {"name": "other-state", "value": "must-not-restore-either"}]},
    ]}
    await cookie_context.add_cookies(normalize_cookies(payload))
    state = await cookie_context.storage_state()
    assert {cookie["name"] for cookie in state["cookies"]} == {"auth_token", "ct0"}
    assert state["origins"] == []
    assert await cookie_context.cookies("https://elsewhere.invalid") == []


@pytest.mark.parametrize("mode", ["session-sentinel", "session-metadata", "persistent"])
async def test_native_cookie_expiration_round_trip(cookie_context, mode):
    payload = credentials()
    expiry = int(time.time()) + 3600
    if mode == "session-sentinel":
        payload[0]["expires"] = -1
    elif mode == "session-metadata":
        payload[0].update(session=True, expirationDate=expiry)
    else:
        payload[0].update(expires=expiry, expirationDate=float(expiry), expiry=expiry)
    payload.append({"name": "expired-optional", "value": "synthetic-old", "expires": 1})
    await cookie_context.add_cookies(normalize_cookies(payload))
    stored = {cookie["name"]: cookie for cookie in await cookie_context.cookies("https://x.com")}
    assert set(stored) == {"auth_token", "ct0"}
    assert stored["auth_token"]["expires"] == (expiry if mode == "persistent" else -1)
    assert stored["ct0"]["expires"] == -1


async def test_native_rejected_import_does_not_partially_inject_credentials(cookie_context):
    payload = credentials()
    payload[1]["domain"] = "x.com.evil.invalid"
    with pytest.raises(SessionError, match="invalid_cookies"):
        await cookie_context.add_cookies(normalize_cookies(payload))
    assert await cookie_context.cookies() == []
