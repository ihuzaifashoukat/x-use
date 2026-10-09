"""Native notification DOM scenarios on intercepted synthetic documents."""
import pytest

from xuse.browser.errors import BrowserActionError, BrowserBlocked
from xuse.browser.page import XBrowser
from test_messaging_dom import app, native_browser  # noqa: F401

pytestmark = pytest.mark.asyncio(loop_scope="module")


def tabs(view="all", *, valid=True):
    return f'''<nav role="tablist"><a role="tab" href="/notifications" aria-selected="{'true' if view == 'all' and valid else 'false'}">All</a>
      <a role="tab" href="/notifications/mentions" aria-selected="{'true' if view == 'mentions' and valid else 'false'}">Mentions</a></nav>'''


def notification(*, hidden=False, fake_attributes=False):
    return f'''<article role="article" tabindex="0" data-testid="notification" {'hidden' if hidden else ''}
      {'data-id="not-a-supported-id" data-unread="false"' if fake_attributes else ''}>
      <a href="/alpha"><div data-testid="UserAvatar-Container-alpha"></div></a><a href="/alpha">Alpha</a>
      <span aria-label="Earlier"><time datetime="2026-10-09T10:00:00Z">Earlier</time></span>
      <div data-testid="tweetText">A synthetic post preview with no permalink</div></article>'''


def post(*, permalink=True):
    timestamp = '<time datetime="2026-10-09T10:00:00Z">Earlier</time>'
    if permalink:
        timestamp = '<a href="/beta/status/321">' + timestamp + '</a>'
    return '''<article data-testid="tweet"><a href="/beta"><div data-testid="UserAvatar-Container-beta"></div></a>
      <a href="/beta">Beta</a>''' + timestamp + '''
      <div data-testid="tweetText">Synthetic post mentioning someone</div></article>'''


def setup(app):
    app.adapter = XBrowser(app.page)
    app.adapter.notifications_timeout_ms = 350


@pytest.mark.parametrize("view,route", [("all", "/notifications"), ("mentions", "/notifications/mentions")])
async def test_native_verified_view_returns_both_row_shapes_unknown_semantics(app, view, route):
    setup(app)
    app.document(route, '<section data-testid="primaryColumn">' + tabs(view) + notification(fake_attributes=True) + post() + '</section>')
    result = await app.adapter.get_notifications(view=view)
    assert result["view"] == view and result["view_verified"]
    assert result["count"] == 2 and result["partial"] and result["coverage"] == "visible_only"
    assert [item["row_kind"] for item in result["notifications"]] == ["notification", "post"]
    assert all(item["unread"] is None and item["type"] == "unknown" and item["notification_id"] is None for item in result["notifications"])
    assert result["notifications"][0]["related_posts"][0]["post_id"] is None
    assert result["notifications"][1]["related_posts"][0]["post_id"] == "321"
    assert "may mark" in result["unread_side_effects"]


async def test_native_hidden_sidebar_and_nested_rows_excluded_and_limit_bounded(app):
    setup(app)
    body = '<section data-testid="primaryColumn">' + tabs() + notification(hidden=True)
    nested = notification().replace('</article>', '<div data-testid="quoteTweet">' + post() + '</div></article>')
    body += '<aside>' + notification() + '</aside>' + nested + post() + '</section>'
    body += '<aside data-testid="sidebarColumn">' + notification() + '</aside>'
    app.document("/notifications", body)
    result = await app.adapter.get_notifications(limit=1)
    assert result["count"] == 1 and result["truncated"]
    assert result["notifications"][0]["row_kind"] == "notification"
    assert result["notifications"][0]["actors"][0]["handle"] == "alpha"
    assert result["notifications"][0]["related_posts"][0]["post_id"] is None


async def test_native_mentions_without_permalink_and_quoted_rows(app):
    setup(app)
    body = '<section data-testid="primaryColumn">' + tabs("mentions") + post(permalink=False)
    body += '<div data-testid="quoteTweet">' + post() + '</div></section>'
    app.document("/notifications/mentions", body)
    result = await app.adapter.get_notifications(view="mentions")
    item, = result["notifications"]
    assert item["type"] == "unknown" and item["row_kind"] == "post"
    assert item["related_posts"][0]["post_id"] is None and item["post_context_partial"]
    assert item["created_at_source"] == "time_datetime"


@pytest.mark.parametrize("phrase,event_type", [("followed you", "follow"), ("liked your post", "like"),
    ("liked your reply", "like"), ("liked 2 of your posts", "like")])
async def test_native_action_copy_outside_post_preview_supports_type(app, phrase, event_type):
    setup(app)
    row = notification().replace('<span aria-label="Earlier">', '<span><span>' + phrase + '</span></span><span aria-label="Earlier">')
    app.document("/notifications", '<section data-testid="primaryColumn">' + tabs() + row + '</section>')
    item, = (await app.adapter.get_notifications())["notifications"]
    assert item["type"] == event_type
    assert item["type_evidence"] == {"source": "visible_notification_action_copy", "text": phrase}


async def test_native_preview_keywords_and_ambiguous_action_copy_stay_unknown(app):
    setup(app)
    first = notification().replace('A synthetic post preview with no permalink', '<span>liked your post</span>')
    second = notification().replace('<span aria-label="Earlier">', '<span>followed you</span><span>liked your reply</span><span aria-label="Earlier">')
    third = post(permalink=False).replace('Synthetic post mentioning someone', '<span>followed you</span>')
    app.document("/notifications", '<section data-testid="primaryColumn">' + tabs() + first + second + third + '</section>')
    result = await app.adapter.get_notifications()
    assert result["count"] == 3
    assert all(item["type"] == "unknown" and item["type_evidence"] is None for item in result["notifications"])


async def test_native_reuses_verified_navigation_without_row_clicks(app):
    setup(app)
    app.document("/notifications", '<section data-testid="primaryColumn">' + tabs() + notification() + '''</section>
      <script>document.documentElement.dataset.clicks='0'; document.addEventListener('click',()=>document.documentElement.dataset.clicks++);</script>''')
    await app.adapter.get_notifications()
    await app.adapter.get_notifications()
    assert len([request for request in app.requests if request[2] == "document"]) == 1
    assert await app.page.locator("html").get_attribute("data-clicks") == "0"


@pytest.mark.parametrize("body", [tabs(valid=False) + notification(), '<p>Unknown layout</p>', tabs()])
async def test_native_unverified_or_empty_unknown_ui_is_unsupported(app, body):
    setup(app)
    app.document("/notifications", '<section data-testid="primaryColumn">' + body + '</section>')
    with pytest.raises(BrowserActionError) as error:
        await app.adapter.get_notifications()
    assert error.value.reason == "unsupported_dom"


async def test_native_same_origin_redirect_does_not_return_stale_notification_rows(app):
    setup(app)
    app.document("/notifications", '<section data-testid="primaryColumn">' + tabs() + notification() +
                 '</section><script>history.replaceState({}, "", "/home");</script>')
    with pytest.raises(BrowserActionError) as error:
        await app.adapter.get_notifications()
    assert error.value.reason == "unsupported_dom"


async def test_native_challenge_is_preserved_without_navigation(app):
    setup(app)
    app.document("/account/access", '<p>Authenticate your account</p>')
    await app.page.goto("https://x.com/account/access")
    with pytest.raises(BrowserBlocked) as error:
        await app.adapter.get_notifications()
    assert error.value.reason == "challenge"
    assert app.page.url == "https://x.com/account/access"


@pytest.mark.parametrize("path,allowed", [("/i/chat/pin/recovery", True), ("/i/chat/unknown", False)])
async def test_native_only_observed_pin_recovery_route_allows_notification_read(app, path, allowed):
    setup(app)
    app.document(path, '<input name="pin"><p>Enter your PIN</p>')
    app.document("/notifications", '<section data-testid="primaryColumn">' + tabs() + notification() + '</section>')
    await app.page.goto("https://x.com" + path)
    if allowed:
        assert (await app.adapter.get_notifications())["count"] == 1
        assert app.page.url == "https://x.com/notifications"
        assert await app.page.locator("input").count() == 0
    else:
        with pytest.raises(BrowserBlocked) as error:
            await app.adapter.get_notifications()
        assert error.value.reason == "pin_required"
        assert app.page.url == "https://x.com" + path


async def test_native_pin_recovery_with_challenge_still_stops_notification_read(app):
    setup(app)
    app.document("/i/chat/pin/recovery", '<input name="pin"><p>Authenticate your account</p>')
    await app.page.goto("https://x.com/i/chat/pin/recovery")
    with pytest.raises(BrowserBlocked) as error:
        await app.adapter.get_notifications()
    assert error.value.reason == "challenge"
    assert app.page.url == "https://x.com/i/chat/pin/recovery"


async def test_native_waits_for_hydrating_rows_and_preserves_system_notice(app):
    setup(app)
    app.document("/notifications", '<section data-testid="primaryColumn">' + tabs() + '''</section><script>
      setTimeout(()=>document.querySelector('section').insertAdjacentHTML('beforeend',
      '<article role="article" tabindex="0" data-testid="notification">Synthetic system notice</article>'),120);</script>''')
    result = await app.adapter.get_notifications()
    item, = result["notifications"]
    assert item["text"] == "Synthetic system notice" and item["actors"] == []
    assert item["related_posts"] == [] and item["unread"] is None and item["type"] == "unknown"


async def test_native_changed_tab_during_snapshot_refuses_rows(app):
    setup(app)
    app.document("/notifications", '<section data-testid="primaryColumn">' + tabs() + notification() + '</section>')
    # Change the selected view atomically during row extraction. The final
    # verifier must refuse the stale snapshot even though the route is intact.
    from xuse.browser import notifications
    original = notifications._WINDOW
    changed = original.replace('const cards=', '''root.querySelector('[href="/notifications"]').setAttribute('aria-selected','false');
      root.querySelector('[href="/notifications/mentions"]').setAttribute('aria-selected','true'); const cards=''')
    import unittest.mock
    with unittest.mock.patch.object(notifications, "_WINDOW", changed):
        with pytest.raises(BrowserActionError) as error:
            await app.adapter.get_notifications()
    assert error.value.reason == "unsupported_dom"


async def test_native_route_changes_during_final_scope_read_refuses_rows(app):
    setup(app)
    app.document("/notifications", '<section data-testid="primaryColumn">' + tabs() + notification() + '</section>')
    original = app.adapter._visible
    calls = 0

    async def changed(locator):
        nonlocal calls
        result = await original(locator)
        calls += 1
        if calls == 2:
            await app.page.evaluate('() => history.replaceState({}, "", "/notifications/mentions")')
        return result

    import unittest.mock
    with unittest.mock.patch.object(app.adapter, "_visible", changed):
        with pytest.raises(BrowserActionError) as error:
            await app.adapter.get_notifications()
    assert error.value.reason == "unsupported_dom"
    assert app.page.url == "https://x.com/notifications/mentions"
