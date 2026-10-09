"""Authored profile context and public connections on intercepted DOM pages."""
import pytest

from xuse.browser.errors import BrowserActionError
from test_messaging_dom import app, native_browser  # noqa: F401

pytestmark = pytest.mark.asyncio(loop_scope="module")

PROFILE = '''<div data-testid="UserName"><span>Alice</span><span>@alice</span><span>Follows you</span></div>
  <div data-testid="UserDescription">Builds useful AI tools</div>
  <span data-testid="UserLocation">Remote</span><span data-testid="UserJoinDate">Joined 2020</span>
  <span data-testid="UserUrl">example.test</span>
  <a href="/alice/followers">1.2K Followers</a><a href="/alice/following">42 Following</a>
  <button data-testid="sendDMFromProfile" aria-label="Message">Message</button>'''


def post(author, identifier, text):
    return f'''<article data-testid="tweet">
      <div data-testid="User-Name"><span>Person</span><span>@{author}</span></div>
      <a href="/{author}/status/{identifier}"><time datetime="2026-10-09T00:00:00Z">Now</time></a>
      <div data-testid="tweetText">{text}</div></article>'''


async def test_profile_context_filters_reposts_and_reuses_current_document(app):
    app.document("/alice", PROFILE + post("other", "123", "Unrelated repost") + post("alice", "124", "Authored AI post"))
    result = await app.adapter.get_profile_context("alice", 1)
    assert result["profile"]["handle"] == "alice"
    assert result["profile"]["display_name"] == "Alice"
    assert result["profile"]["can_message"] and result["profile"]["follows_you"]
    assert result["profile"]["followers_count"] == 1200
    assert result["profile"]["following_count"] == 42
    assert [p.tweet_id for p in result["posts"]] == ["124"]
    assert result["partial"] and result["pagination"] == "bounded_scroll"
    await app.adapter.get_profile("alice")
    assert [path for _, path, kind in app.requests if kind == "document"] == ["/alice"]


@pytest.mark.parametrize("feed,suffix", [("posts", ""), ("replies", "/with_replies"), ("media", "/media")])
async def test_profile_tabs_keep_only_exact_authored_posts(app, feed, suffix):
    app.document("/alice" + suffix, PROFILE + post("alice2", "123", "Prefix match") + post("alice", "124", "Authored item"))
    result = await app.adapter.get_profile_posts("alice", feed, 1)
    assert result["feed"] == feed and result["count"] == 1
    assert result["posts"][0].user_handle == "@alice"
    assert app.page.url == "https://x.com/alice" + suffix


async def test_profile_context_recognized_empty_is_truthful(app):
    app.document("/alice", PROFILE + "<p>No posts yet</p>")
    result = await app.adapter.get_profile_context("alice", 2)
    assert result["posts"] == [] and result["partial"]


async def test_profile_redirect_cannot_supply_another_person_context(app):
    app.document("/alice", PROFILE + '''<script>history.replaceState({},'', '/bob')</script>''')
    with pytest.raises(BrowserActionError, match="profile_mismatch"):
        await app.adapter.get_profile("alice")


@pytest.mark.parametrize("relationship", ["followers", "following"])
async def test_public_connections_exclude_sidebar_and_deduplicate(app, relationship):
    app.document("/alice/" + relationship, '''<div data-testid="primaryColumn">
      <div data-testid="UserCell"><a href="/bob">Bob</a><a href="/bob">@bob</a></div>
      <div data-testid="UserCell"><a href="/bob">Bob again</a></div>
      <div data-testid="UserCell"><a href="/carol">Carol</a></div>
      </div><aside><div data-testid="UserCell"><a href="/other">Sidebar</a></div></aside>''')
    result = await app.adapter.get_profile_connections("alice", relationship, 2)
    assert [item["handle"] for item in result["connections"]] == ["bob", "carol"]
    assert result["partial"] and result["pagination"] == "visible_only"


async def test_ambiguous_public_connection_row_fails_closed(app):
    app.document("/alice/following", '''<div data-testid="primaryColumn"><div data-testid="UserCell">
      <a href="/bob">Bob</a><a href="/carol">Carol</a></div></div>''')
    with pytest.raises(BrowserActionError, match="unsupported_dom"):
        await app.adapter.get_profile_connections("alice")


async def test_connections_recognized_empty(app):
    app.document("/alice/following", '<div data-testid="primaryColumn"><div data-testid="emptyState">None yet</div></div>')
    result = await app.adapter.get_profile_connections("alice")
    assert result["connections"] == [] and result["count"] == 0


@pytest.mark.parametrize("method,arguments", [
    ("get_profile_posts", ("alice", "invalid", 5)),
    ("get_profile_connections", ("alice", "contacts", 5)),
    ("get_profile_context", ("../alice", 5)),
])
async def test_invalid_profile_read_arguments_never_navigate(app, method, arguments):
    with pytest.raises(BrowserActionError, match="invalid_input"):
        await getattr(app.adapter, method)(*arguments)
    assert not app.requests
