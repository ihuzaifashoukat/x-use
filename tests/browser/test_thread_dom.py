"""Bounded conversation extraction in fully intercepted native browser pages."""

import html
import json

import pytest

from xuse.browser.errors import BrowserActionError, BrowserBlocked
from xuse.browser.page import _SNAPSHOT
from test_messaging_dom import app, native_browser  # noqa: F401


pytestmark = pytest.mark.asyncio(loop_scope="module")
URL = "https://x.com/alice/status/200"


def post(identifier, author="alice", text="Visible post", *, before="", after="", attrs=""):
    return f'''<article data-testid="tweet" {attrs}>
      {before}<div data-testid="User-Name"><span>{author.title()}</span><span>@{author}</span></div>
      <a href="/{author}/status/{identifier}"><time datetime="2026-10-09T00:00:00Z">Now</time></a>
      <div data-testid="tweetText">{html.escape(text)}</div>{after}</article>'''


def document(body, script="", outside=""):
    return '<main data-testid="primaryColumn" id="timeline">' + body + '</main>' + outside + '<script>' + script + '</script>'


async def test_exact_focal_and_multi_author_context_keep_unknown_parents(app):
    app.document("/alice/status/200", document(
        post("199", "bob") + post("200") + post("201") + post("202", "carol")))
    result = await app.adapter.get_thread(URL, 4)
    assert [tweet.tweet_id for tweet in result["tweets"]] == ["199", "200", "201", "202"]
    assert [entry["relation"] for entry in result["entries"]] == ["visible_context", "focal", "visible_context", "visible_context"]
    assert [entry["position"] for entry in result["entries"]] == list(range(4))
    assert all(entry["parent_tweet_id"] is None for entry in result["entries"])
    assert result["focal_tweet_id"] == "200" and result["partial"]
    assert result["observed_count"] == 4 and result["stop_reason"] == "limit_reached"


@pytest.mark.parametrize("source", ["missing", "quote", "sidebar", "hidden", "promoted", "redirect"])
async def test_unverified_or_ineligible_focal_fails_closed(app, source):
    fake = post("200")
    content, outside, script = post("2000", "bob"), "", ""
    if source == "quote":
        content += post("201", before='<div data-testid="quoteTweet">' + fake + '</div>')
    elif source == "sidebar":
        outside = '<aside>' + fake + '</aside>'
    elif source == "hidden":
        content += post("200", attrs="hidden")
    elif source == "promoted":
        content += post("200", after='<span data-testid="promotedIndicator">Ad</span>')
    elif source == "redirect":
        content += fake
        script = "history.replaceState({}, '', '/bob/status/201');"
    app.document("/alice/status/200", document(content, script, outside))
    with pytest.raises(BrowserActionError, match="tweet_not_found"):
        await app.adapter.get_thread(URL)


async def test_quote_time_before_primary_does_not_corrupt_focal_or_media(app):
    quote = '<div data-testid="quoteTweet">' + post("999", "bob", "Quoted text", after='''
      <div data-testid="tweetPhoto"><img src="https://x.com/quote.jpg" alt="Quoted photo"></div>
      <video poster="https://x.com/quote-poster.jpg"></video>''') + '</div>'
    own = '<div data-testid="tweetPhoto"><img src="https://x.com/own.jpg" alt="Own photo"></div>'
    app.document("/alice/status/200", document(post("200", text="Own text", before=quote, after=own) + post("201")))
    result = await app.adapter.get_thread(URL, 2)
    assert [tweet.tweet_id for tweet in result["tweets"]] == ["200", "201"]
    focal = result["tweets"][0]
    assert focal.text_content == "Own text" and focal.user_handle == "@alice"
    assert [(item.type, str(item.url), item.alt_text) for item in focal.media] == [("image", "https://x.com/own.jpg", "Own photo")]


async def test_hidden_sidebar_and_promoted_context_are_excluded(app):
    content = post("200") + post("201", attrs="hidden") + post("202", after='<span data-testid="socialContext">Promoted</span>')
    content += '<div data-testid="placementTracking">' + post("203") + '</div>'
    outside = '<aside>' + post("204") + '</aside>'
    app.document("/alice/status/200", document(content, outside=outside))
    result = await app.adapter.get_thread(URL, 10)
    assert [tweet.tweet_id for tweet in result["tweets"]] == ["200"]
    assert result["observed_count"] == 1 and result["stop_reason"] == "no_growth"
    assert result["partial"]


async def test_virtualized_scroll_deduplicates_and_preserves_late_dom_order(app):
    replacement = post("199", "bob", "Late ancestor") + post("200") + post("201", "carol")
    next_page = post("201", "carol") + post("202", "dave")
    script = '''let wheels=0; window.addEventListener('wheel', () => {
      wheels++; setTimeout(() => {document.querySelector('#timeline').innerHTML=
        wheels === 1 ? ''' + json.dumps(replacement) + " : " + json.dumps(next_page) + ''';}, 80);
    });'''
    app.document("/alice/status/200", document(post("200"), script))
    result = await app.adapter.get_thread(URL, 4)
    assert [tweet.tweet_id for tweet in result["tweets"]] == ["199", "200", "201", "202"]
    assert result["observed_count"] == 4 and result["stop_reason"] == "limit_reached"
    assert result["entries"][1]["relation"] == "focal"


async def test_focal_loaded_after_first_context_card_is_waited_for(app):
    script = "setTimeout(() => document.querySelector('#timeline').insertAdjacentHTML('beforeend', " + json.dumps(post("200")) + "), 150);"
    app.document("/alice/status/200", document(post("199", "bob"), script))
    result = await app.adapter.get_thread(URL, 2)
    assert [tweet.tweet_id for tweet in result["tweets"]] == ["199", "200"]


async def test_late_context_loads_during_bounded_no_growth_wait(app):
    script = "setTimeout(() => document.querySelector('#timeline').insertAdjacentHTML('beforeend', " + json.dumps(post("201", "bob")) + "), 450);"
    app.document("/alice/status/200", document(post("200"), script))
    result = await app.adapter.get_thread(URL, 2)
    assert [tweet.tweet_id for tweet in result["tweets"]] == ["200", "201"]
    assert result["stop_reason"] == "limit_reached"


async def test_media_only_card_has_image_and_video_metadata(app):
    media = '''<div data-testid="tweetPhoto"><img src="https://x.com/image.jpg" alt="A diagram"></div>
      <video poster="https://x.com/poster.jpg" src="https://x.com/clip.mp4" aria-label="Demo video"></video>
      <video src="blob:https://x.com/example" poster="https://x.com/blob-poster.jpg"></video>'''
    app.document("/alice/status/200", document(post("200", text="", after=media)))
    result = await app.adapter.get_thread(URL, 1)
    focal = result["tweets"][0]
    assert focal.text_content == ""
    assert [(item.type, str(item.url)) for item in focal.media] == [
        ("image", "https://x.com/image.jpg"), ("video", "https://x.com/poster.jpg"),
        ("video", "https://x.com/blob-poster.jpg")]
    snapshot = await app.page.get_by_test_id("tweet").evaluate(_SNAPSHOT)
    assert snapshot["media"][1]["poster_url"] == "https://x.com/poster.jpg"
    assert snapshot["media"][1]["source_url"] == "https://x.com/clip.mp4"
    assert snapshot["media"][2]["source_url"] is None
    assert str(focal.media[1].poster_url) == "https://x.com/poster.jpg"
    assert str(focal.media[1].source_url) == "https://x.com/clip.mp4"
    assert focal.media[2].source_url is None


async def test_small_limit_keeps_focal_even_after_long_visible_context(app):
    app.document("/alice/status/200", document(post("197") + post("198") + post("199") + post("200") + post("201")))
    result = await app.adapter.get_thread(URL, 2)
    assert [tweet.tweet_id for tweet in result["tweets"]] == ["199", "200"]
    assert result["observed_count"] == 5


@pytest.mark.parametrize("limit", [0, 51, True, "2"])
async def test_invalid_limit_never_navigates(app, limit):
    with pytest.raises(BrowserActionError, match="invalid_input"):
        await app.adapter.get_thread(URL, limit)
    assert not app.requests


async def test_challenge_during_scroll_propagates_browser_policy_error(app):
    script = "window.addEventListener('wheel', () => document.body.insertAdjacentHTML('beforeend', '<p>Verify you are human</p>'));"
    app.document("/alice/status/200", document(post("200"), script))
    with pytest.raises(BrowserBlocked, match="challenge"):
        await app.adapter.get_thread(URL, 5)


async def test_initial_challenge_propagates_without_reading(app):
    app.document("/alice/status/200", document(post("200"), outside="<p>Verify you are human</p>"))
    with pytest.raises(BrowserBlocked, match="challenge"):
        await app.adapter.get_thread(URL)


async def test_continuous_growth_still_stops_at_scroll_bound(app):
    template = post("__ID__", "bob")
    script = "let identifier=201; window.addEventListener('wheel', () => document.querySelector('#timeline').insertAdjacentHTML('beforeend', " + json.dumps(template) + ".replace('__ID__', String(identifier++))));"
    app.document("/alice/status/200", document(post("200"), script))
    result = await app.adapter.get_thread(URL, 50)
    assert result["stop_reason"] == "scroll_bound" and result["partial"]
    assert result["observed_count"] == len(result["tweets"]) == 10
    assert [tweet.tweet_id for tweet in result["tweets"]] == [str(identifier) for identifier in range(200, 210)]


async def test_displayed_reply_handles_and_untrusted_parent_attributes_are_not_edges(app):
    context = post("201", "bob", before='<div>Replying to <a href="/alice">@alice</a></div>', attrs='data-parent-tweet-id="200"')
    app.document("/alice/status/200", document(post("200") + context))
    result = await app.adapter.get_thread(URL, 2)
    assert result["entries"][1]["relation"] == "visible_context"
    assert result["entries"][1]["parent_tweet_id"] is None


async def test_navigation_during_scroll_cannot_supply_context_from_another_post(app):
    script = "window.addEventListener('wheel', () => history.replaceState({}, '', '/bob/status/201'));"
    app.document("/alice/status/200", document(post("200"), script))
    with pytest.raises(BrowserActionError, match="tweet_not_found"):
        await app.adapter.get_thread(URL, 5)
