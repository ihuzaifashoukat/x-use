"""Native engagement controls remain bound to the verified primary post."""
import pytest

from xuse.browser.errors import BrowserActionError
from test_messaging_dom import app, native_browser  # noqa: F401

pytestmark = pytest.mark.asyncio(loop_scope="module")


def card(identifier):
    return f'''<article data-testid="tweet" data-id="{identifier}">
      <a href="/person/status/{identifier}"><time>Today</time></a>
      <div data-testid="tweetText">Post {identifier}</div>
      <button data-testid="like" onclick="document.documentElement.dataset.clicked=this.closest('article').dataset.id;this.dataset.testid='unlike'">Like</button>
      </article>'''


async def test_recycled_positional_card_cannot_like_another_post(app):
    app.document("/person/status/123", card("123") + card("456"))
    original = app.adapter._wait_visible
    recycled = False

    async def wait_and_recycle(locator, timeout_ms=10000):
        nonlocal recycled
        result = await original(locator, timeout_ms)
        if not recycled and await result.get_attribute("data-testid") == "like":
            recycled = True
            await app.page.locator('article[data-testid="tweet"]').first.evaluate("card => card.parentElement.append(card)")
        return result

    app.adapter._wait_visible = wait_and_recycle
    with pytest.raises(BrowserActionError):
        await app.adapter.like("https://x.com/person/status/123")
    assert await app.page.locator("html").get_attribute("data-clicked") is None


async def test_quote_inverse_control_cannot_mark_primary_post_already_liked(app):
    body = card("123").replace('</article>', '''
      <div data-testid="quoteTweet"><a href="/other/status/456"><time>Yesterday</time></a>
      <button data-testid="unlike">Unlike quote</button></div></article>''')
    app.document("/person/status/123", body)
    result = await app.adapter.like("https://x.com/person/status/123")
    assert result["status"] == "confirmed"
    assert await app.page.locator("html").get_attribute("data-clicked") == "123"


@pytest.mark.parametrize("action", ["like", "retweet", "reply"])
async def test_quote_controls_cannot_receive_primary_action(app, action):
    body = card("123").replace('data-testid="like"', 'data-testid="unused"').replace('</article>', f'''
      <div data-testid="quoteTweet"><a href="/other/status/456"><time>Yesterday</time></a>
      <button data-testid="{action}" onclick="document.documentElement.dataset.clicked='quote'">Quote control</button>
      </div></article>''')
    app.document("/person/status/123", body)
    original = app.adapter._wait_visible

    async def wait_static_fixture(locator, timeout_ms=10000):
        return await original(locator, timeout_ms=500)

    app.adapter._wait_visible = wait_static_fixture
    with pytest.raises(BrowserActionError):
        if action == "reply":
            await app.adapter.reply("https://x.com/person/status/123", "Reviewed reply")
        else:
            await getattr(app.adapter, action)("https://x.com/person/status/123")
    assert await app.page.locator("html").get_attribute("data-clicked") is None


async def test_reply_preview_with_multiple_primary_posts_is_not_exact_target_proof(app):
    from test_post_dom import nested_reply_document

    app.document("/person/status/123", nested_reply_document(ambiguous="preview"))
    with pytest.raises(BrowserActionError):
        await app.adapter.reply("https://x.com/person/status/123", "Reviewed reply")
    assert await app.page.locator("#reply-text").inner_text() == ""
    assert await app.clicks() == 0


async def test_recycled_card_before_repost_confirmation_cannot_submit(app):
    body = card("123").replace('data-testid="like"', 'data-testid="retweet"').replace(
        'onclick="document.documentElement.dataset.clicked=this.closest(\'article\').dataset.id;this.dataset.testid=\'unlike\'"',
        'onclick="document.querySelector(\'[data-testid=retweetConfirm]\').hidden=false"')
    app.document("/person/status/123", body + card("456") + '''
      <button hidden data-testid="retweetConfirm" onclick="document.documentElement.dataset.clicked='confirmed'">Repost</button>''')
    original = app.adapter._wait_visible

    async def wait_and_recycle(locator, timeout_ms=10000):
        result = await original(locator, timeout_ms)
        if await result.get_attribute("data-testid") == "retweetConfirm":
            await app.page.locator('article[data-testid="tweet"]').first.evaluate("card => card.parentElement.append(card)")
        return result

    app.adapter._wait_visible = wait_and_recycle
    with pytest.raises(BrowserActionError):
        await app.adapter.retweet("https://x.com/person/status/123")
    assert await app.page.locator("html").get_attribute("data-clicked") is None


async def test_profile_route_change_cannot_follow_stale_profile(app):
    app.document("/alice", '''<div id="profile">
      <div data-testid="UserName"><span>Alice</span><span>@alice</span></div>
      <button data-testid="123-follow" onclick="document.documentElement.dataset.clicked='follow';this.dataset.testid='123-unfollow'">Follow</button>
      </div>''')
    original = app.adapter._wait_visible

    async def wait_and_redirect(locator, timeout_ms=10000):
        result = await original(locator, timeout_ms)
        if await result.get_attribute("id") == "profile":
            await app.page.evaluate("history.replaceState({}, '', '/bob')")
        return result

    app.adapter._wait_visible = wait_and_redirect
    with pytest.raises(BrowserActionError):
        await app.adapter.follow("alice")
    assert await app.page.locator("html").get_attribute("data-clicked") is None
