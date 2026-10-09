"""Post confirmation and reply-composer scoping in intercepted documents."""
import html
import json

import pytest

from xuse.browser.errors import BrowserActionError

from test_messaging_dom import app, native_browser  # noqa: F401 - reuse intercepted browser fixtures


pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest.mark.parametrize("fresh_toast", [False, True])
async def test_old_toast_is_not_current_submission_evidence(app, fresh_toast):
    update = "document.querySelector('#sent-link').href='/person/status/124';" if fresh_toast else ""
    app.document("/compose/tweet", '''
      <div data-testid="toast">Your post was sent
        <a id="sent-link" href="/person/status/123">View</a>
      </div>
      <div role="dialog">
        <div contenteditable="true" data-testid="tweetTextarea_0"></div>
        <button data-testid="tweetButton" onclick="
          document.documentElement.dataset.sendClicks='1';
          document.querySelector('[role=dialog]').remove();
          ''' + update + '''">Post</button>
      </div>
    ''')
    app.adapter.action_confirmation_timeout_ms = 300
    if fresh_toast:
        result = await app.adapter.post("Exact reviewed text")
        assert result["success"] and result["evidence"]["tweet_id"] == "124"
    else:
        with pytest.raises(BrowserActionError, match="outcome_unknown"):
            await app.adapter.post("Exact reviewed text")
    assert await app.clicks() == 1


SOURCE_TEXT = "The exact original post"
MEDIA_SUFFIX = " \nhttps://\npic.x.com/abc123"


def nested_reply_document(original_id="123", *, hidden_outer=True, ambiguous=None,
                          media=False, preview_text=SOURCE_TEXT, preview_author="person",
                          modal_route=None, preexisting=False, route_after_fill=None,
                          change_preview_after_fill=False, author_layout="name_links",
                          avatar_author="person", avatar_links=None, visible_author=True,
                          extra_user_handle=None, user_link_author=None, name_link_count=1,
                          composer_markup=""):
    outer_style = ' style="visibility:hidden"' if hidden_outer else ""
    inner_style = ' style="visibility:visible"' if hidden_outer else ""
    hidden = "" if preexisting else " hidden"
    permalink = (f'<a href="/person/status/{original_id}"><time>Earlier</time></a>'
                 if original_id is not None else "")
    own_media = ('<div data-testid="tweetPhoto"><img src="https://x.com/media/original.jpg"></div>'
                 if media else "")
    route_change = f"history.pushState({{}}, '', {json.dumps(modal_route)});" if modal_route else ""
    input_route_change = f"history.pushState({{}}, '', {json.dumps(route_after_fill)});" if route_after_fill else ""
    input_preview_change = ("document.querySelector('#preview-body').textContent='A different post';"
                            if change_preview_after_fill else "")
    handle_style = "" if visible_author else ' style="display:none"'
    handle_text = '<span' + handle_style + '>@' + html.escape(preview_author) + '</span>'
    if author_layout == "name_links":
        author_name = ''.join('<a href="/' + html.escape(user_link_author or preview_author, quote=True) + '">' + handle_text + '</a>'
                              for _ in range(name_link_count))
        avatar = ""
    else:
        author_name = '<span>Sterling</span>' + handle_text + '<span>·</span><span>Oct7</span>'
        if user_link_author:
            author_name += '<a href="/' + html.escape(user_link_author, quote=True) + '">Profile</a>'
        if extra_user_handle:
            author_name += '<span>@' + html.escape(extra_user_handle) + '</span>'
        if avatar_links is None:
            avatar_links = ["/person"]
        avatar = ('<div data-testid="UserAvatar-Container-' + html.escape(avatar_author, quote=True) + '">'
                  + ''.join('<a href="' + html.escape(href, quote=True) + '">Avatar</a>' for href in avatar_links) + '</div>')
    duplicate = {
        "dialog": "document.body.appendChild(document.querySelector('#reply-modal').cloneNode(true));",
        "textbox": "document.querySelector('#reply-text').parentElement.appendChild(document.querySelector('#reply-text').cloneNode(true));",
        "button": "document.querySelector('[data-testid=toolBar]').appendChild(document.querySelector('[data-testid=tweetButton]').cloneNode(true));",
        "preview": "document.querySelector('#reply-text').parentElement.appendChild(document.querySelector('#reply-modal article').cloneNode(true));",
    }.get(ambiguous, "")
    return '''
      <main data-testid="primaryColumn">
        <article data-testid="tweet">
          <a href="/person/status/123"><time>Earlier</time></a>
          <div data-testid="User-Name"><span>@person</span></div>
          <div data-testid="tweetText">''' + SOURCE_TEXT + '''</div>''' + own_media + '''
          <button data-testid="reply" id="open-reply">Reply</button>
        </article>
        <div contenteditable="true" data-testid="tweetTextarea_0" id="background-text"></div>
        <button data-testid="tweetButtonInline" disabled>Reply</button>
      </main>
      <div role="dialog" id="reply-modal"''' + hidden + outer_style + '''>
        <div role="dialog"''' + inner_style + '''>
          <button data-testid="app-bar-close">Close</button><button data-testid="unsentButton">Drafts</button>
          <article data-testid="tweet">
            ''' + permalink + '''
            ''' + avatar + '''<div data-testid="User-Name">''' + author_name + '''</div>
            <div data-testid="tweetText" id="preview-body">''' + html.escape(preview_text) + '''</div>
          </article>
          <div contenteditable="true" data-testid="tweetTextarea_0" aria-label="Post text" id="reply-text">''' + composer_markup + '''</div>
          <div data-testid="toolBar"><button data-testid="tweetButton" disabled>Reply</button></div>
        </div>
      </div>
      <script>
        document.querySelector('#open-reply').addEventListener('click', () => {
          document.documentElement.dataset.replyClicks=String(Number(document.documentElement.dataset.replyClicks || 0) + 1);
          document.querySelector('#reply-modal').hidden=false;
          ''' + duplicate + route_change + '''
        });
        document.querySelector('#reply-text').addEventListener('input', () => {
          document.querySelector('[data-testid="tweetButton"]').disabled=false;
          ''' + input_route_change + input_preview_change + '''
        });
        document.querySelector('[data-testid="tweetButton"]').addEventListener('click', () => {
          document.documentElement.dataset.sendClicks='1';
          document.documentElement.dataset.submittedText=document.querySelector('#reply-text').innerText;
          document.querySelector('#reply-modal').remove();
          const toast=document.createElement('div'); toast.dataset.testid='toast';
          toast.innerHTML='Your reply was sent <a href="/person/status/124">View</a>';
          document.body.appendChild(toast);
        });
      </script>
    '''


@pytest.mark.parametrize("hidden_outer", [False, True])
async def test_nested_reply_dialog_scopes_typing_and_submit_away_from_inline_background(app, hidden_outer):
    app.document("/person/status/123", nested_reply_document(hidden_outer=hidden_outer))
    app.adapter.action_confirmation_timeout_ms = 500
    text = "A reply grounded in the exact original post."
    result = await app.adapter.reply("https://x.com/person/status/123", text)
    assert result["success"] and result["action"] == "reply"
    assert result["evidence"]["tweet_id"] == "124" and result["evidence"]["reply_to"] == "123"
    assert await app.page.locator("html").get_attribute("data-submitted-text") == text
    assert await app.page.locator("#background-text").inner_text() == ""
    assert await app.clicks() == 1


async def test_nested_reply_dialog_requires_exact_original_before_typing_or_submit(app):
    app.document("/person/status/123", nested_reply_document(original_id="1234"))
    with pytest.raises(BrowserActionError, match="tweet_not_found"):
        await app.adapter.reply("https://x.com/person/status/123", "Reviewed reply")
    assert await app.page.locator("#reply-text").inner_text() == ""
    assert await app.page.locator("#background-text").inner_text() == ""
    assert await app.clicks() == 0


@pytest.mark.parametrize("ambiguous", ["dialog", "textbox", "button"])
async def test_ambiguous_reply_controls_fail_before_typing_or_submit(app, ambiguous):
    app.document("/person/status/123", nested_reply_document(ambiguous=ambiguous))
    with pytest.raises(BrowserActionError, match="unsupported_dom"):
        await app.adapter.reply("https://x.com/person/status/123", "Reviewed reply")
    textboxes = app.page.get_by_test_id("tweetTextarea_0")
    for index in range(await textboxes.count()):
        assert await textboxes.nth(index).inner_text() == ""
    assert await app.clicks() == 0


async def test_reply_keeps_the_final_policy_check_before_submit(app):
    app.document("/person/status/123", nested_reply_document())

    def stop_write():
        raise BrowserActionError("account_locked")

    app.adapter.action_validator = stop_write
    with pytest.raises(BrowserActionError, match="account_locked"):
        await app.adapter.reply("https://x.com/person/status/123", "Reviewed reply")
    assert await app.page.locator("#reply-text").inner_text() == "Reviewed reply"
    assert await app.clicks() == 0


@pytest.mark.parametrize("media,preview_text", [
    (False, SOURCE_TEXT), (False, "The\texact\noriginal post"),
    (True, SOURCE_TEXT + MEDIA_SUFFIX),
])
@pytest.mark.parametrize("author_layout", ["name_links", "avatar_link"])
async def test_new_native_reply_preview_proves_target_without_permalink(app, media, preview_text, author_layout):
    app.document("/person/status/123", nested_reply_document(original_id=None, media=media,
                 preview_text=preview_text, modal_route="/compose/post", author_layout=author_layout))
    result = await app.adapter.reply("https://x.com/person/status/123", "Reviewed reply")
    assert result["success"] and result["evidence"]["reply_to"] == "123"
    assert await app.page.locator("html").get_attribute("data-submitted-text") == "Reviewed reply"
    assert await app.page.locator("#background-text").inner_text() == ""
    assert await app.clicks() == 1


@pytest.mark.parametrize("changes", [
    {"preview_author": "other"}, {"preview_author": ""},
    {"preview_text": "The exact original..."}, {"preview_text": SOURCE_TEXT + " extra words", "media": True},
    {"preview_text": SOURCE_TEXT + MEDIA_SUFFIX},
    {"preview_text": SOURCE_TEXT + " https://pic.x.com.evil.test/abc", "media": True},
    {"ambiguous": "preview"}, {"modal_route": "/home"},
    {"original_id": "1234", "media": True, "preview_text": SOURCE_TEXT + MEDIA_SUFFIX},
])
async def test_native_preview_fallback_rejects_unproven_target_before_typing(app, changes):
    params = {"original_id": None, "modal_route": "/compose/post", **changes}
    app.document("/person/status/123", nested_reply_document(**params))
    with pytest.raises(BrowserActionError, match="tweet_not_found"):
        await app.adapter.reply("https://x.com/person/status/123", "Reviewed reply")
    assert await app.page.locator("#reply-text").inner_text() == ""
    assert await app.clicks() == 0


async def test_preexisting_modal_is_not_accepted_as_the_new_verified_reply_composer(app):
    app.document("/person/status/123", nested_reply_document(original_id=None, preexisting=True))
    with pytest.raises(BrowserActionError, match="composer_not_empty"):
        await app.adapter.reply("https://x.com/person/status/123", "Reviewed reply")
    assert await app.page.locator("html").get_attribute("data-reply-clicks") is None
    assert await app.page.locator("#reply-text").inner_text() == ""
    assert await app.clicks() == 0


@pytest.mark.parametrize("changes", [{"route_after_fill": "/home"}, {"change_preview_after_fill": True}])
async def test_reply_target_and_route_are_rechecked_immediately_before_submit(app, changes):
    app.document("/person/status/123", nested_reply_document(original_id=None, modal_route="/compose/post", **changes))
    with pytest.raises(BrowserActionError, match="tweet_not_found"):
        await app.adapter.reply("https://x.com/person/status/123", "Reviewed reply")
    assert await app.page.locator("#reply-text").inner_text() == "Reviewed reply"
    assert await app.clicks() == 0


async def test_navigation_during_final_preview_proof_stops_before_policy_and_submit(app):
    app.document("/person/status/123", nested_reply_document(original_id=None, modal_route="/compose/post"))
    verify_target = app.adapter._verify_reply_target
    verifications = 0
    policy_calls = []

    async def navigate_after_final_proof(*args, **kwargs):
        nonlocal verifications
        await verify_target(*args, **kwargs)
        verifications += 1
        if verifications == 2:
            await app.page.evaluate("history.pushState({}, '', '/home')")

    app.adapter._verify_reply_target = navigate_after_final_proof
    app.adapter.action_validator = lambda: policy_calls.append(True)
    with pytest.raises(BrowserActionError, match="tweet_not_found"):
        await app.adapter.reply("https://x.com/person/status/123", "Reviewed reply")
    assert verifications == 2 and policy_calls == []
    assert await app.clicks() == 0


@pytest.mark.parametrize("changes", [
    {"avatar_links": []}, {"avatar_links": ["/person", "/person"]},
    {"avatar_links": ["/other"]}, {"avatar_links": ["https://x.com.evil.test/person"]},
    {"avatar_links": ["/person?redirect=other"]}, {"avatar_author": "other"},
    {"preview_author": "other"}, {"preview_author": ""}, {"visible_author": False},
    {"extra_user_handle": "other"}, {"user_link_author": "other"},
])
async def test_native_avatar_author_proof_rejects_missing_conflicting_or_deceptive_identity(app, changes):
    app.document("/person/status/123", nested_reply_document(original_id=None, author_layout="avatar_link",
                 modal_route="/compose/post", media=True, preview_text=SOURCE_TEXT + MEDIA_SUFFIX, **changes))
    with pytest.raises(BrowserActionError, match="tweet_not_found"):
        await app.adapter.reply("https://x.com/person/status/123", "Reviewed reply")
    assert await app.page.locator("#reply-text").inner_text() == ""
    assert await app.clicks() == 0


async def test_reply_author_proof_rejects_link_overflow_instead_of_ignoring_extra_identity(app):
    app.document("/person/status/123", nested_reply_document(original_id=None, name_link_count=9,
                 modal_route="/compose/post"))
    with pytest.raises(BrowserActionError, match="tweet_not_found"):
        await app.adapter.reply("https://x.com/person/status/123", "Reviewed reply")
    assert await app.page.locator("#reply-text").inner_text() == ""
    assert await app.clicks() == 0


async def test_native_blank_contenteditable_structure_allows_reviewed_reply(app):
    app.document("/person/status/123", nested_reply_document(original_id=None, author_layout="avatar_link",
                 modal_route="/compose/post", media=True, preview_text=SOURCE_TEXT + MEDIA_SUFFIX,
                 composer_markup="<div><br></div>"))
    result = await app.adapter.reply("https://x.com/person/status/123", "Reviewed reply")
    assert result["success"]
    assert await app.page.locator("html").get_attribute("data-submitted-text") == "Reviewed reply"
    assert await app.clicks() == 1


async def test_native_nonblank_contenteditable_draft_is_preserved_without_submit(app):
    app.document("/person/status/123", nested_reply_document(original_id=None, author_layout="avatar_link",
                 modal_route="/compose/post", media=True, preview_text=SOURCE_TEXT + MEDIA_SUFFIX,
                 composer_markup="<div>Existing draft</div>"))
    with pytest.raises(BrowserActionError, match="composer_not_empty"):
        await app.adapter.reply("https://x.com/person/status/123", "Reviewed reply")
    assert await app.page.locator("#reply-text").inner_text() == "Existing draft"
    assert await app.clicks() == 0
