"""Public-route reuse and identity checks on fully intercepted native pages."""

import time

import pytest

from xuse.browser.errors import BrowserActionError, BrowserBlocked
from xuse.browser.page import XBrowser, tweet_from_snapshot
from test_messaging_dom import app, native_browser  # noqa: F401
from test_profile_dom import PROFILE, post
from test_target_action_dom import card


pytestmark = pytest.mark.asyncio(loop_scope="module")
URL = "https://x.com/person/status/123"


def documents(app):
    return [path for _, path, kind in app.requests if kind == "document"]


async def test_repeated_profile_reads_and_posts_reuse_proven_document(app):
    app.document("/alice", PROFILE + post("alice", "124", "Authored"))
    await app.adapter.get_profile("alice")
    await app.adapter.get_profile_context("alice", 1)
    await app.adapter.get_profile_posts("alice", limit=1)
    assert documents(app) == ["/alice"]


async def test_post_read_thread_and_like_reuse_proven_document(app):
    app.document("/person/status/123", '<main data-testid="primaryColumn">' + card("123") + '</main>')
    await app.adapter.get_tweet(URL)
    await app.adapter.get_thread(URL, 1)
    result = await app.adapter.like(URL)
    assert result["status"] == "confirmed"
    assert documents(app) == ["/person/status/123"]


async def test_post_read_then_reply_reuses_verified_original_document(app):
    from test_post_dom import nested_reply_document

    app.document("/person/status/123", nested_reply_document())
    await app.adapter.get_tweet(URL)
    result = await app.adapter.reply(URL, "Reviewed reply in the synthetic fixture.")
    assert result["success"] and result["evidence"]["reply_to"] == "123"
    assert await app.clicks() == 1
    assert documents(app) == ["/person/status/123"]


async def test_same_profile_url_still_checks_challenge(app):
    app.document("/alice", PROFILE)
    await app.adapter.get_profile("alice")
    await app.page.locator("body").evaluate("node => node.insertAdjacentHTML('beforeend', '<p>Verify you are human</p>')")
    with pytest.raises(BrowserBlocked, match="challenge"):
        await app.adapter.get_profile("alice")
    assert documents(app) == ["/alice"]


async def test_same_origin_wrong_status_redirect_cannot_supply_target(app):
    app.document("/person/status/123", card("123") + "<script>history.replaceState({},'', '/person/status/456')</script>")
    with pytest.raises(BrowserActionError, match="tweet_not_found"):
        await app.adapter.get_tweet(URL)


async def test_quote_only_target_is_ineligible_for_direct_post_read(app):
    app.document("/person/status/123", card("456").replace('</article>', '<div data-testid="quoteTweet">' + card("123") + '</div></article>'))
    with pytest.raises(BrowserActionError, match="tweet_not_found"):
        await app.adapter.get_tweet(URL)


async def test_public_collect_excludes_hidden_sidebar_quote_and_promoted_cards(app):
    body = post("alice", "124", "Own")
    body += post("alice", "125", "Hidden").replace('<article ', '<article hidden ')
    body += '<aside>' + post("alice", "126", "Sidebar") + '</aside>'
    body += post("bob", "127", "Outer").replace('</article>', '<div data-testid="quoteTweet">' + post("alice", "128", "Quote") + '</div></article>')
    body += '<div data-testid="placementTracking">' + post("alice", "129", "Ad") + '</div>'
    app.document("/alice", PROFILE + body)
    result = await app.adapter.get_profile_posts("alice", limit=10)
    assert [tweet.tweet_id for tweet in result["posts"]] == ["124"]


async def test_synthetic_sequence_measures_navigation_count_and_latency(app, record_property):
    app.document("/person/status/123", '<main data-testid="primaryColumn">' + card("123") + '</main>')
    started = time.perf_counter()
    for _ in range(4):
        await app.adapter.get_tweet(URL)
    elapsed = time.perf_counter() - started
    record_property("document_navigations", len(documents(app)))
    record_property("sequence_seconds", round(elapsed, 4))
    assert len(documents(app)) == 1


@pytest.mark.parametrize("kind", ["post", "profile"])
async def test_same_url_wrong_dom_recovers_with_one_read_navigation(app, kind):
    if kind == "post":
        app.document("/person/status/123", card("123"))
        read = lambda: app.adapter.get_tweet(URL)
        selector, replacement = "article", card("456")
    else:
        app.document("/alice", PROFILE)
        read = lambda: app.adapter.get_profile("alice")
        selector, replacement = '[data-testid="UserName"]', '<div data-testid="UserName">Bob @bob</div>'
    await read()
    await app.page.locator(selector).evaluate("(node, replacement) => node.outerHTML=replacement", replacement)
    result = await read()
    assert result[0].tweet_id == "123" if kind == "post" else result["handle"] == "alice"
    assert len(documents(app)) == 2


async def test_wrong_content_at_same_requested_url_fails_after_fresh_load(app):
    app.document("/person/status/123", card("456"))
    with pytest.raises(BrowserActionError, match="tweet_not_found"):
        await app.adapter.get_tweet(URL)
    assert len(documents(app)) == 1


async def test_hidden_first_card_does_not_mask_visible_exact_post(app):
    app.document("/person/status/123", card("456").replace('<article ', '<article hidden ') + card("123"))
    original = app.adapter._wait_visible

    async def short_wait(locator, timeout_ms=10000):
        return await original(locator, timeout_ms=100)

    app.adapter._wait_visible = short_wait
    tweets = await app.adapter.get_tweet(URL)
    assert tweets[0].tweet_id == "123"


async def test_hidden_first_card_does_not_mask_profile_posts(app):
    app.document("/alice", PROFILE + post("alice", "125", "Hidden").replace('<article ', '<article hidden ') + post("alice", "124", "Visible"))
    original = app.adapter._wait_visible

    async def short_wait(locator, timeout_ms=10000):
        return await original(locator, timeout_ms=100)

    app.adapter._wait_visible = short_wait
    result = await app.adapter.get_profile_posts("alice", limit=1)
    assert result["posts"][0].tweet_id == "124"


async def test_changed_target_never_reuses_previous_post_document(app):
    app.document("/person/status/123", card("123") + card("456"))
    app.document("/person/status/456", card("456"))
    await app.adapter.get_tweet(URL)
    tweets = await app.adapter.get_tweet("https://x.com/person/status/456")
    assert tweets[0].tweet_id == "456"
    assert documents(app) == ["/person/status/123", "/person/status/456"]


async def test_same_route_new_document_is_not_previous_navigation_proof(app):
    app.document("/person/status/123", card("123"))
    await app.adapter.get_tweet(URL)
    await app.page.reload(wait_until="domcontentloaded")
    await app.adapter.get_tweet(URL)
    assert len(documents(app)) == 3


async def test_explicit_refresh_and_default_navigation_always_load(app):
    app.document("/person/status/123", card("123"))
    await app.adapter.get_tweet(URL)

    async def ready():
        return True

    await app.adapter.navigate(URL, refresh=True, ready=ready)
    await app.adapter.navigate(URL)
    assert len(documents(app)) == 3


async def test_expired_reuse_window_requires_fresh_document(app):
    app.document("/person/status/123", card("123"))
    await app.adapter.get_tweet(URL)
    route, loaded, document = app.adapter._reusable_navigation
    app.adapter._reusable_navigation = (route, loaded - 31, document)
    await app.adapter.get_tweet(URL)
    assert len(documents(app)) == 2


async def test_profile_scroll_invalidates_reuse_and_preserves_initial_read_window(app):
    app.document("/alice", PROFILE + post("alice", "124", "First") + '<div style="height:2000px"></div>')
    await app.adapter.get_profile_context("alice", 5)
    await app.adapter.get_profile_posts("alice", limit=1)
    assert len(documents(app)) == 2


async def test_profile_media_tab_uses_fresh_content_when_header_cannot_prove_selected_feed(app):
    app.document("/alice/media", PROFILE + post("alice", "124", "Media tab content"))
    await app.adapter.get_profile_posts("alice", feed="media", limit=1)
    await app.page.locator("article").evaluate("(node, replacement) => node.outerHTML=replacement", post("alice", "125", "Different feed content"))
    result = await app.adapter.get_profile_posts("alice", feed="media", limit=1)
    assert result["posts"][0].tweet_id == "124"
    assert len(documents(app)) == 2


async def test_reuse_challenge_preserves_block_and_does_not_refresh_away_evidence(app):
    app.document("/person/status/123", card("123"))
    await app.adapter.get_tweet(URL)
    await app.page.locator("body").evaluate("node => node.insertAdjacentHTML('beforeend', '<p>Rate limit exceeded</p>')")
    with pytest.raises(BrowserBlocked, match="rate_limited"):
        await app.adapter.like(URL)
    assert len(documents(app)) == 1
    assert await app.page.locator("html").get_attribute("data-clicked") is None


async def test_expired_same_route_still_preserves_current_challenge(app):
    app.document("/person/status/123", card("123"))
    await app.adapter.get_tweet(URL)
    route, loaded, document = app.adapter._reusable_navigation
    app.adapter._reusable_navigation = (route, loaded - 31, document)
    await app.page.locator("body").evaluate("node => node.insertAdjacentHTML('beforeend', '<p>Verify you are human</p>')")
    with pytest.raises(BrowserBlocked, match="challenge"):
        await app.adapter.get_tweet(URL)
    assert len(documents(app)) == 1


async def test_duplicate_primary_target_is_ambiguous(app):
    app.document("/person/status/123", card("123") + card("123"))
    with pytest.raises(BrowserActionError, match="tweet_not_found"):
        await app.adapter.get_tweet(URL)


async def test_external_redirect_fails_closed_even_with_matching_post_dom(native_browser):
    context = await native_browser.new_context(service_workers="block")
    page = await context.new_page()
    requests = []

    async def intercept(route):
        requests.append(route.request.url)
        if route.request.url == URL:
            await route.fulfill(status=302, headers={"location": "https://outside.invalid/post"}, body="")
        else:
            await route.fulfill(status=200, content_type="text/html", body=card("123"))

    await context.route("**/*", intercept)
    try:
        # Intercepted redirects can be rejected by the driver before the final
        # origin check; both outcomes must stop the read without returning cards.
        with pytest.raises((BrowserBlocked, BrowserActionError)) as failure:
            await XBrowser(page).get_tweet(URL)
        assert failure.value.reason in {"external_redirect", "navigation_failed"}
        assert requests[0] == URL
    finally:
        await context.close()


async def test_profile_route_change_during_collection_cannot_return_stale_context(app):
    app.document("/alice", PROFILE + post("alice", "124", "First") + "<script>window.addEventListener('wheel', () => history.replaceState({}, '', '/bob'))</script>")
    with pytest.raises(BrowserActionError, match="profile_mismatch"):
        await app.adapter.get_profile_context("alice", 5)


async def test_snapshot_rejects_conflicting_permalink_author_and_handles_bad_media():
    assert tweet_from_snapshot({"status_url": "/alice/status/123", "text": "Body", "handle": "@bob"}) is None
    result = tweet_from_snapshot({"status_url": "/alice/status/123", "text": "Body", "media": [
        {"type": "image", "url": "https://[broken"},
        {"type": "image", "url": "https://x.com/valid.jpg"},
    ]})
    assert [str(item.url) for item in result.media] == ["https://x.com/valid.jpg"]
