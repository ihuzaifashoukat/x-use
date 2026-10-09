"""Real-browser inbox scenarios using synthetic, fully intercepted documents.

These exercise native browser-driver visibility, accessibility, typing, click,
history and hydration behavior. They do not authenticate to X or demonstrate
that any particular live chat UI implements these fixture selectors.
"""

import html
import importlib
import importlib.util
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import pytest_asyncio

from xuse.browser.errors import BrowserActionError, BrowserBlocked
from xuse.browser.page import XBrowser


pytestmark = pytest.mark.asyncio(loop_scope="module")


_DRIVERS = os.environ.get(
    "XUSE_TEST_BROWSER_DRIVERS", os.environ.get("XUSE_TEST_BROWSER_DRIVER", "patchright,playwright")
).split(",")


@pytest_asyncio.fixture(scope="module", loop_scope="module", params=_DRIVERS)
async def native_browser(request):
    driver = request.param
    if driver not in {"patchright", "playwright"}:
        pytest.fail("Unsupported offline browser-test driver.")
    try:
        api = importlib.import_module(f"{driver}.async_api")
    except ModuleNotFoundError:
        if driver == "patchright" and os.environ.get("XUSE_REQUIRE_BROWSER_TESTS") == "1":
            pytest.fail("Required primary inbox-test driver is unavailable.", pytrace=False)
        pytest.skip(f"Optional {driver} driver is unavailable for offline DOM scenarios.")
    channel = os.environ.get("XUSE_TEST_BROWSER_CHANNEL", "chrome")
    if channel not in {"chrome", "msedge", "chromium"}:
        pytest.fail("Unsupported offline browser-test channel.")
    async with api.async_playwright() as runtime:
        options = {"headless": True}
        if channel != "chromium":
            options["channel"] = channel
        try:
            browser = await runtime.chromium.launch(**options)
        except Exception:
            if os.environ.get("XUSE_REQUIRE_BROWSER_TESTS") == "1":
                pytest.fail("Required local browser runtime is unavailable.", pytrace=False)
            pytest.skip("Local browser runtime unavailable for offline DOM scenarios.")
        try:
            yield browser
        finally:
            await browser.close()


class FixtureApp:
    def __init__(self, page):
        self.page = page
        self.documents = {}
        self.requests = []
        self.adapter = XBrowser(page)
        self.adapter.messaging_timeout_ms = 600
        self.adapter.messaging_confirmation_timeout_ms = 600

    async def intercept(self, route):
        request = route.request
        parsed = urlsplit(request.url)
        self.requests.append((parsed.hostname, parsed.path, request.resource_type))
        if parsed.hostname == "x.com" and request.resource_type == "document" and parsed.path in self.documents:
            document = self.documents[parsed.path]
            if isinstance(document, tuple):
                status, headers, body = document
                await route.fulfill(status=status, headers=headers, body=body)
            else:
                await route.fulfill(status=200, content_type="text/html", body=document)
        else:
            await route.abort()

    def document(self, path, body):
        self.documents[path] = (
            '<!doctype html><meta charset="utf-8"><title>Offline inbox fixture</title>'
            '<button data-testid="SideNav_AccountSwitcher_Button">Account</button>'
            '<style>[hidden]{display:none!important} textarea{display:block;width:350px;height:90px} '
            '[contenteditable]{display:block;white-space:pre-wrap;min-height:90px;width:350px} '
            '.message-body{white-space:pre-wrap}</style>' + body
        )
        if path == "/messages":
            # Existing legacy fixture bodies remain available through the
            # primary inbox route, matching X's current chat entry point.
            self.documents["/i/chat"] = self.documents[path]

    async def clicks(self):
        return int(await self.page.locator("html").get_attribute("data-send-clicks") or "0")


@pytest_asyncio.fixture(loop_scope="module")
async def app(native_browser):
    context = await native_browser.new_context(service_workers="block")
    page = await context.new_page()
    page.set_default_timeout(1200)
    fixture = FixtureApp(page)
    await context.route("**/*", fixture.intercept)
    try:
        yield fixture
        assert all(host == "x.com" for host, _, _ in fixture.requests)
    finally:
        await context.close()


def entry(text="hello", message_id="old", *, direction="outgoing", status="sent", modern=False):
    identifier = f' data-message-id="{html.escape(message_id, quote=True)}"' if message_id is not None else ""
    direction_attr = f' data-direction="{direction}"' if direction else ""
    entry_testid = "chatMessage" if modern else "messageEntry"
    text_testid = "chatMessageText" if modern else "messageEntryText"
    return f'<div data-testid="{entry_testid}"{identifier}{direction_attr} data-status="{status}"><span class="message-body" data-testid="{text_testid}">{html.escape(text)}</span><time>Earlier</time></div>'


def conversation(*, existing=None, modern=False, editable=False, behavior="append", header="@alice", draft="", duplicate_composer=False):
    root = "chatConversation" if modern else "DMConversationView"
    header_id = "chatConversationHeader" if modern else "DMConversationHeader"
    composer_id = "chatComposerTextInput" if modern else "dmComposerTextInput"
    send_id = "chatComposerSendButton" if modern else "dmComposerSendButton"
    if existing is None:
        existing = entry("Earlier incoming", direction="incoming", modern=modern)
    if editable:
        composer = f'<div role="textbox" contenteditable="true" data-testid="{composer_id}">{html.escape(draft)}</div>'
    else:
        composer = f'<textarea data-testid="{composer_id}">{html.escape(draft)}</textarea>'
    extra = composer if duplicate_composer else ""
    return f'''<main><section data-testid="{root}">
      <header data-testid="{header_id}">{header}</header>
      <div id="history">{existing}</div>{composer}{extra}
      <button data-testid="{send_id}">Send</button>
      </section></main><script>
      window.sendClicks=0; document.documentElement.dataset.sendClicks='0';
      const composer=document.querySelector('[data-testid="{composer_id}"]');
      document.querySelector('[data-testid="{send_id}"]').addEventListener('click', () => {{
        window.sendClicks++; document.documentElement.dataset.sendClicks=String(window.sendClicks);
        const value={str(editable).lower()} ? composer.innerText : composer.value;
        const bubble=document.createElement('div');
        bubble.dataset.testid='{ "chatMessage" if modern else "messageEntry" }';
        if ('{behavior}' !== 'no_id') bubble.dataset.messageId='new1';
        if ('{behavior}' !== 'unknown_direction') bubble.dataset.direction='outgoing';
        bubble.dataset.status='{ "pending" if behavior == "pending" else "failed" if behavior == "failed" else "sent" }';
        const body=document.createElement('span');
        body.className='message-body'; body.dataset.testid='{ "chatMessageText" if modern else "messageEntryText" }'; body.textContent=value;
        bubble.appendChild(body);
        const history=document.querySelector('#history');
        if ('{behavior}' === 'prepend') history.prepend(bubble);
        else if ('{behavior}' === 'replace') history.replaceChildren(bubble);
        else if ('{behavior}' !== 'no_change') history.appendChild(bubble);
        if ('{behavior}' !== 'uncleared') {{
          if ({str(editable).lower()}) composer.textContent=''; else composer.value='';
        }}
      }});
      </script>'''


async def open_legacy_inbox(app):
    """Numeric IDs use legacy routing only after that inbox is observed."""
    app.document("/messages", '''<main><section data-testid="DMInbox">
      <a href="/messages/10-20">Fixture conversation</a></section></main>''')
    await app.page.goto("https://x.com/messages")


async def test_native_inbox_limits_visibility_dedupe_and_unread_states(app):
    app.document("/messages", '''<main><div data-testid="DMInbox">
      <a data-testid="conversation" href="/messages/10-20" data-unread="true">Alice</a>
      <a data-testid="conversation" href="/messages/10-20" data-unread="true">Duplicate</a>
      <a data-testid="conversation" href="/messages/10-30" data-unread="false">Bob</a>
      <a data-testid="conversation" href="/messages/10-40">Unknown read state</a>
      <a data-testid="conversation" href="/messages/10-50" hidden>Hidden</a>
      <a data-testid="conversation" href="https://evil.example/messages/10-60">External</a>
      </div></main>''')
    result = await app.adapter.get_inbox(3)
    assert [row["conversation_id"] for row in result["conversations"]] == ["10-20", "10-30", "10-40"]
    assert [row["unread"] for row in result["conversations"]] == [True, False, None]
    assert result["partial"] and result["pagination"] == "visible_only"
    limited = await app.adapter.get_inbox(1)
    assert limited["count"] == 1


async def test_native_unread_badge_hidden_badge_and_preview_word_are_distinguished(app):
    app.document("/messages", '''<div data-testid="DMInbox">
      <a data-testid="conversation" href="/messages/10-20">Alice<span data-testid="unreadBadge">1</span></a>
      <a data-testid="conversation" href="/messages/10-30">Bob<span data-testid="unreadBadge" hidden>1</span></a>
      <a data-testid="conversation" href="/messages/10-40">How do I mark unread?</a></div>''')
    rows = (await app.adapter.get_inbox())["conversations"]
    assert [row["unread"] for row in rows] == [True, None, None]


async def test_native_explicit_unread_accessibility_and_conflicting_flags_are_distinguished(app):
    app.document("/messages", '''<div data-testid="DMInbox">
      <a data-testid="conversation" href="/messages/10-20" aria-label="Unread conversation">Alice</a>
      <a data-testid="conversation" href="/messages/10-30" data-unread="false"><span data-testid="unreadBadge">1</span>Bob</a>
      </div>''')
    rows = (await app.adapter.get_inbox())["conversations"]
    assert [row["unread"] for row in rows] == [True, None]


async def test_native_search_is_local_case_insensitive_and_no_match_is_partial(app):
    app.document("/messages", '<div data-testid="DMInbox"><a data-testid="conversation" href="/messages/10-20">Alice: Hello there</a></div>')
    result = await app.adapter.search_conversations(" HELLO ")
    assert result["count"] == 1 and result["search_scope"] == "visible_conversation_summaries"
    absent = await app.adapter.search_conversations("not present")
    assert absent["empty"] and absent["partial"]


async def test_native_modern_chat_route_uses_allowlisted_visible_links(app):
    app.document("/messages", '<script>history.replaceState(null,"","/i/chat");</script><main><a href="/i/chat/chat_A-1">Alice</a></main>')
    result = await app.adapter.get_inbox()
    assert result["conversations"][0]["url"] == "https://x.com/i/chat/chat_A-1"
    assert app.page.url == "https://x.com/i/chat"


async def test_native_delayed_inbox_hydration_waits_for_rows_not_empty_shell(app):
    app.document("/messages", '''<main><div data-testid="DMInbox" id="inbox"></div></main>
      <script>setTimeout(()=>{document.querySelector('#inbox').innerHTML='<a data-testid="conversation" href="/messages/10-20">Alice</a>';},80);</script>''')
    assert (await app.adapter.get_inbox())["count"] == 1


@pytest.mark.parametrize("body,reason", [
    ('<main><div data-testid="DMInbox"></div></main>', "unsupported_dom"),
    ('<main><div role="progressbar">Loading chat</div></main>', "unsupported_dom"),
    ('<main><p>Unknown chat layout</p></main>', "unsupported_dom"),
])
async def test_native_unknown_or_loading_chat_is_not_reported_as_empty(app, body, reason):
    app.document("/messages", body)
    with pytest.raises(BrowserActionError) as exc:
        await app.adapter.get_inbox()
    assert exc.value.reason == reason


async def test_native_empty_inbox_requires_explicit_empty_state(app):
    app.document("/messages", '<main><div data-testid="DMInbox">Your inbox is empty</div></main>')
    assert (await app.adapter.get_inbox())["empty"] is True


async def test_native_multiple_inbox_panels_do_not_merge_conversation_summaries(app):
    app.document("/messages", '<main><div data-testid="DMInbox"><a data-testid="conversation" href="/messages/10-20">Alice</a></div><div data-testid="DMInbox"><a data-testid="conversation" href="/messages/10-30">Bob</a></div></main>')
    with pytest.raises(BrowserActionError) as exc:
        await app.adapter.get_inbox()
    assert exc.value.reason == "unsupported_dom"


@pytest.mark.parametrize("modern", [False, True])
async def test_native_conversation_read_preserves_order_unicode_and_scoped_body(app, modern):
    messages = entry("Oldest", "a", direction="incoming", modern=modern)
    messages += entry("Line one\nLine two 🐦 اردو", "b", modern=modern)
    messages += entry("Latest", "c", direction="incoming", modern=modern)
    path = "/i/chat/chat_A" if modern else "/messages/10-20"
    app.document(path, conversation(existing=messages, modern=modern))
    result = await app.adapter.get_conversation("https://x.com" + path, 2)
    assert [message["message_id"] for message in result["messages"]] == ["b", "c"]
    assert result["messages"][0]["text"] == "Line one\nLine two 🐦 اردو"
    assert "Earlier" not in result["messages"][0]["text"]
    assert result["partial"] and result["count"] == 2


async def test_native_multiple_visible_conversations_do_not_merge_history(app):
    app.document("/messages/10-20", '<main><section data-testid="DMConversationView">' + entry("Alice", "a") + '</section><section data-testid="DMConversationView">' + entry("Bob", "b") + '</section></main>')
    await open_legacy_inbox(app)
    with pytest.raises(BrowserActionError) as exc:
        await app.adapter.get_conversation("10-20")
    assert exc.value.reason == "unsupported_dom"


async def test_native_explicit_incoming_direction_overrides_ambiguous_receipt_labels(app):
    message = entry("Incoming", "a", direction="incoming").replace("<time>", '<span aria-label="Read">Read</span><time>')
    app.document("/messages/10-20", conversation(existing=message))
    await open_legacy_inbox(app)
    result = await app.adapter.get_conversation("10-20")
    assert result["messages"][0]["direction"] == "incoming"


async def test_native_empty_conversation_and_unknown_history_are_distinguished(app):
    app.document("/messages/10-20", conversation(existing="No messages yet"))
    await open_legacy_inbox(app)
    assert (await app.adapter.get_conversation("10-20"))["empty"]
    app.document("/messages/10-20", conversation(existing=""))
    await app.page.goto("https://x.com/messages/10-20")
    with pytest.raises(BrowserActionError) as exc:
        await app.adapter.get_conversation("10-20")
    assert exc.value.reason == "unsupported_dom"


async def test_native_conversation_read_waits_for_history_after_composer_hydrates(app):
    body = conversation(existing="") + '<script>setTimeout(()=>document.querySelector("#history").innerHTML=' + json.dumps(entry("Delayed history", "late", direction="incoming")) + ',100);</script>'
    app.document("/messages/10-20", body)
    await open_legacy_inbox(app)
    assert (await app.adapter.get_conversation("10-20"))["messages"][0]["message_id"] == "late"


@pytest.mark.parametrize("status,pending", [("sent", False), ("pending", True), ("failed", True)])
async def test_native_observed_shadow_message_rows_exclude_header_date_and_receipt_text(app, status, pending):
    contents = f'''<section data-testid="dm-conversation-panel">
      <header data-testid="dm-conversation-header">@alice</header><div data-testid="dm-message-list">
      <div data-dm-item-key="header"><div data-testid="dm-conversation-header-item">Participant profile</div></div>
      <div data-dm-item-key="date">Today</div>
      <div data-dm-item-key="message_fixture"><div data-dm-message-row data-send-status="{status}">
      <div role="button" data-dm-message-focus-target style="white-space:pre-wrap"><div id="fixture-body"><span>Visible body\nSecond line</span></div></div>
      <span>10:42</span><span aria-label="Read">Read</span></div></div>
      <div data-dm-item-key="spacer" data-testid="dm-message-list-composer-spacer"></div>
      </div><textarea data-testid="dm-composer-textarea"></textarea></section>'''
    app.document("/i/chat/fixture_A", '<div id="embed"></div><script>document.querySelector("#embed").attachShadow({mode:"open"}).innerHTML=' + json.dumps(contents) + ';</script>')
    result = await app.adapter.get_conversation("https://x.com/i/chat/fixture_A")
    assert result["count"] == 1
    message = result["messages"][0]
    assert message["text"] == "Visible body\nSecond line"
    assert message["pending_or_failed"] is pending
    # A visible receipt alone cannot identify modern message authorship; this
    # fixture intentionally has no outgoing/incoming identity evidence.
    assert message["direction"] == "unknown"
    assert message["message_id"] is None


@pytest.mark.parametrize("mutation,expected", [
    ("valid", "12345678-1234-1234-1234-123456789abc"),
    ("row", None), ("testid", None), ("key", None), ("non_uuid", None),
])
async def test_native_modern_message_id_requires_exact_observed_uuid_equality(app, mutation, expected):
    identifier = "12345678-1234-1234-1234-123456789abc"
    other = "87654321-1234-1234-1234-123456789abc"
    row = other if mutation == "row" else "temporary-local-id" if mutation == "non_uuid" else identifier
    testid = other if mutation == "testid" else identifier
    key = other if mutation == "key" else identifier
    contents = f'''<section data-testid="dm-conversation-panel"><header data-testid="dm-conversation-header"><a href="https://x.com/Alice">Alice</a></header>
      <div data-dm-item-key="{key}"><div data-dm-message-row="{row}" data-testid="message-{testid}" data-send-status="sent">
      <div data-dm-message-focus-target><div data-testid="message-text-{row}">Visible body</div></div></div></div>
      <textarea data-testid="dm-composer-textarea"></textarea></section>'''
    app.document("/i/chat/fixture_A", '<div id="embed"></div><script>document.querySelector("#embed").attachShadow({mode:"open"}).innerHTML=' + json.dumps(contents) + ';</script>')
    message = (await app.adapter.get_conversation("https://x.com/i/chat/fixture_A"))["messages"][0]
    assert message["message_id"] == expected
    assert message["direction"] == "unknown"


@pytest.mark.parametrize("modern,editable", [(False, False), (True, True)])
async def test_native_send_unicode_multiline_and_whitespace_uses_real_typing(app, modern, editable):
    path = "/i/chat/chat_A" if modern else "/messages/10-20"
    app.document(path, conversation(modern=modern, editable=editable))
    result = await app.adapter.send_message("https://x.com" + path, "  Hello 🐦\nاردو line  ")
    assert result["success"] and result["message_id"] == "new1"
    assert await app.clicks() == 1


@pytest.mark.parametrize("behavior", ["no_change", "no_id", "unknown_direction", "pending", "failed", "uncleared", "prepend", "replace"])
async def test_native_send_ambiguous_evidence_never_confirms_or_retries(app, behavior):
    app.document("/messages/10-20", conversation(existing=entry("hello"), behavior=behavior))
    await open_legacy_inbox(app)
    with pytest.raises(BrowserActionError) as exc:
        await app.adapter.send_message("10-20", "hello")
    assert exc.value.reason == "send_unconfirmed"
    assert await app.clicks() == 1


async def test_native_existing_duplicate_plus_new_identical_stable_id_confirms(app):
    app.document("/messages/10-20", conversation(existing=entry("hello")))
    await open_legacy_inbox(app)
    result = await app.adapter.send_message("10-20", "hello")
    assert result["success"] and result["message_id"] == "new1"


@pytest.mark.parametrize("draft,ambiguous", [("Human draft", False), ("", True)])
async def test_native_human_draft_and_multiple_composers_never_send(app, draft, ambiguous):
    app.document("/messages/10-20", conversation(draft=draft, duplicate_composer=ambiguous))
    await open_legacy_inbox(app)
    with pytest.raises(BrowserActionError) as exc:
        await app.adapter.send_message("10-20", "hello")
    assert exc.value.reason == ("unsupported_dom" if ambiguous else "composer_not_empty")
    assert await app.clicks() == 0


async def test_native_delayed_confirmation_waits_for_settled_status(app):
    body = conversation(behavior="pending")
    body += '<script>document.querySelector("[data-testid=dmComposerSendButton]").addEventListener("click",()=>setTimeout(()=>document.querySelector("[data-message-id=new1]").dataset.status="sent",100));</script>'
    app.document("/messages/10-20", body)
    await open_legacy_inbox(app)
    assert (await app.adapter.send_message("10-20", "hello"))["success"]
    assert await app.clicks() == 1


async def test_native_last_moment_policy_pause_and_recipient_suppression_prevent_click(app):
    app.document("/messages/10-20", conversation(header="<span>@alice</span><span>@bob</span>"))
    await open_legacy_inbox(app)
    seen = []
    def check(handle):
        seen.append(handle)
        if handle == "bob":
            raise ValueError("Synthetic private lead information")
    app.adapter.recipient_validator = check
    with pytest.raises(BrowserActionError) as exc:
        await app.adapter.send_message("10-20", "hello")
    assert exc.value.reason == "recipient_blocked" and "private" not in str(exc.value)
    assert seen == ["alice", "bob"] and await app.clicks() == 0
    assert await app.page.locator("textarea").input_value() == "hello"
    await app.page.locator("textarea").fill("")
    app.adapter.recipient_validator = lambda handle: None
    def pause():
        raise RuntimeError("Synthetic paused account policy")
    app.adapter.action_validator = pause
    with pytest.raises(BrowserActionError) as exc:
        await app.adapter.send_message("10-20", "hello")
    assert exc.value.reason == "outcome_unknown"
    assert await app.clicks() == 0


async def test_native_route_redirect_on_input_never_clicks_wrong_conversation(app):
    body = conversation() + '<script>document.querySelector("textarea").addEventListener("input",()=>history.replaceState(null,"","/messages/10-30"));</script>'
    app.document("/messages/10-20", body)
    await open_legacy_inbox(app)
    with pytest.raises(BrowserActionError) as exc:
        await app.adapter.send_message("10-20", "hello")
    assert exc.value.reason == "conversation_mismatch" and await app.clicks() == 0


def recipient_modal(*, wrong_header=False, duplicate=False):
    header = "@bob" if wrong_header else "@alice"
    duplicate_row = '<div data-testid="UserCell">@alice</div>' if duplicate else ""
    chat_js = json.dumps(conversation(header=header)).replace("</script>", r"<\/script>")
    # The fixture's JavaScript implements local modal state, not adapter code.
    return f'''<main><div data-testid="DMInbox">Your inbox is empty</div>
      <button data-testid="NewDM_Button">New message</button><div id="modal"></div></main>
      <script>document.querySelector('[data-testid="NewDM_Button"]').onclick=()=>setTimeout(()=>{{
        document.querySelector('#modal').innerHTML='<div role="dialog"><input data-testid="searchPeople" placeholder="Search people"><div data-testid="UserCell" id="exact">@alice</div><div data-testid="UserCell">@alice_other</div>{duplicate_row}<button id="next" disabled>Next</button></div>';
        document.querySelector('#exact').onclick=()=>document.querySelector('#next').disabled=false;
        document.querySelector('#next').onclick=()=>{{
          history.replaceState(null,'','/messages/10-20');
          document.body.innerHTML={chat_js};
          for(const script of document.body.querySelectorAll('script')) {{ const replacement=document.createElement('script');replacement.textContent=script.textContent;script.replaceWith(replacement); }}
        }};
      }},80);</script>'''


async def test_native_new_recipient_modal_waits_and_selects_exact_handle(app):
    app.document("/messages", recipient_modal())
    result = await app.adapter.send_message("@alice", "hello")
    assert result["success"] and result["recipient"] == "@alice"
    assert await app.clicks() == 1


async def test_native_profile_recipient_reuses_exact_current_chat_without_reload_or_new_modal(app):
    app.document("/messages/10-20", conversation(header='<a href="/Alice">Alice</a>'))
    await app.page.goto("https://x.com/messages/10-20")
    app.adapter.recipient_validator = lambda handle: None
    result = await app.adapter.send_message("https://x.com/ALICE", "hello")
    assert result["success"] and result["recipient"] == "@alice"
    assert await app.clicks() == 1
    assert [path for _, path, kind in app.requests if kind == "document"] == ["/messages/10-20"]


async def test_native_handle_recipient_does_not_reuse_a_group_containing_that_handle(app):
    app.document("/messages/10-20", conversation(header="<span>@alice</span><span>@bob</span>"))
    app.document("/i/chat", '<main><div data-testid="DMInbox">Your inbox is empty</div></main>')
    app.document("/alice", '<div data-testid="UserName">@bob</div><button data-testid="sendDMFromProfile" aria-label="Message">Message</button>')
    await app.page.goto("https://x.com/messages/10-20")
    with pytest.raises(BrowserActionError) as exc:
        await app.adapter.send_message("@alice", "hello")
    assert exc.value.reason == "recipient_mismatch" and await app.clicks() == 0
    assert app.page.url == "https://x.com/alice"


async def test_native_handle_identity_change_after_fill_blocks_even_without_policy_callback(app):
    body = conversation() + '<script>document.querySelector("textarea").addEventListener("input",()=>document.querySelector("[data-testid=DMConversationHeader]").textContent="@bob");</script>'
    app.document("/messages/10-20", body)
    await app.page.goto("https://x.com/messages/10-20")
    with pytest.raises(BrowserActionError) as exc:
        await app.adapter.send_message("@alice", "hello")
    assert exc.value.reason == "recipient_mismatch"
    assert await app.clicks() == 0


@pytest.mark.parametrize("wrong,duplicate", [(True, False), (False, True)])
async def test_native_wrong_or_ambiguous_new_recipient_never_sends(app, wrong, duplicate):
    app.document("/messages", recipient_modal(wrong_header=wrong, duplicate=duplicate))
    with pytest.raises(BrowserActionError) as exc:
        await app.adapter.send_message("@alice", "hello")
    assert exc.value.reason == "recipient_mismatch"
    assert await app.clicks() == 0


def pin_screen(*, valid=True, delay=False, disabled=False, challenge=False):
    # Dummy fixture PIN only; real supplied credentials are never read by tests.
    input_html = '<input name="pin" type="password" inputmode="numeric" aria-label="PIN">'
    button = '<button id="unlock"' + (" disabled" if disabled else "") + '>Unlock</button>'
    contents = input_html + button
    if delay:
        body = '<main>Enter your PIN<div id="mount"></div></main>'
        mount = f"setTimeout(()=>{{document.querySelector('#mount').innerHTML={repr(contents)};attach();}},80);"
    else:
        body = '<main>Enter your PIN' + contents + '</main>'
        mount = "attach();"
    target = '<main>Verify you are human</main>' if challenge else '<main><div data-testid="DMInbox">Your inbox is empty</div></main>'
    return body + f'''<script>window.unlockClicks=0;document.documentElement.dataset.unlockClicks='0';function attach(){{
      document.querySelector('#unlock').onclick=()=>{{window.unlockClicks++;document.documentElement.dataset.unlockClicks=String(window.unlockClicks);
        if ({str(valid).lower()} && document.querySelector('input').value==='1234') setTimeout(()=>document.body.innerHTML={repr(target)},80);
      }};}}{mount}</script>'''


@pytest.mark.parametrize("delay", [False, True])
async def test_native_pin_unlock_waits_for_prompt_and_verified_inbox(app, delay):
    app.document("/messages", pin_screen(delay=delay))
    result = await app.adapter.unlock_messages("1234")
    assert result["success"] and result["status"] == "confirmed"
    assert "1234" not in repr(result)
    assert await app.page.locator("html").get_attribute("data-unlock-clicks") == "1"


@pytest.mark.parametrize("valid,disabled,challenge,reason", [
    (False, False, False, "unlock_failed"), (True, True, False, "unlock_failed"),
    (True, False, True, "challenge"),
])
async def test_native_pin_wrong_disabled_and_challenge_states_never_claim_success(app, valid, disabled, challenge, reason):
    app.document("/messages", pin_screen(valid=valid, disabled=disabled, challenge=challenge))
    with pytest.raises((BrowserActionError, BrowserBlocked)) as exc:
        await app.adapter.unlock_messages("1234")
    assert exc.value.reason == reason
    if await app.page.locator("input").count():
        assert await app.page.locator("input").input_value() == ""
    assert await app.page.locator("html").get_attribute("data-unlock-clicks") == ("0" if disabled else "1")


async def test_native_segmented_pin_fails_closed_without_entering_any_digits(app):
    app.document("/messages", '<main>Enter your PIN' + ''.join(f'<input aria-label="PIN digit {index}" type="password" inputmode="numeric" maxlength="1">' for index in range(4)) + '<button>Unlock</button></main>')
    with pytest.raises(BrowserActionError) as exc:
        await app.adapter.unlock_messages("1234")
    assert exc.value.reason == "unsupported_dom"
    assert await app.page.locator("input").evaluate_all("inputs=>inputs.every(input=>input.value==='')")


def segmented_passcode_screen(*, valid=True, disabled=False, wrong_focus=False, duplicate=False, redirect=False, unknown_after=False, unlocked_html=None):
    """Sanitized mirror of the observed four-input open-shadow passcode gate."""
    fields = ''.join(f'<input type="text" inputmode="numeric" pattern="[0-9]*" maxlength="1" aria-label="Digit {index} of 4"' + (' disabled' if disabled and index == 2 else '') + '>' for index in range(1, 5))
    if duplicate:
        fields += '<input type="text" inputmode="numeric" pattern="[0-9]*" maxlength="1" aria-label="Digit 1 of 4">'
    content = '<main><h1>Enter Passcode</h1><p>Your passcode is required to recover your encryption keys.</p>' + fields + '<button id="forgot">Forgot passcode</button><p role="alert" id="error"></p></main>'
    unlocked = unlocked_html or ('<main>Unknown unlocked UI</main>' if unknown_after else '<main><div data-testid="DMInbox">Your inbox is empty</div></main>')
    return '<input id="unrelated" aria-label="Search" value="Human search"><div data-testid="xchatEmbedRoute" id="embed"></div>' + f'''<script>
      const shadow=document.querySelector('#embed').attachShadow({{mode:'open'}});
      shadow.innerHTML={json.dumps(content)};
      const inputs=[...shadow.querySelectorAll('input')];
      document.documentElement.dataset.unlockClicks='0';
      document.documentElement.dataset.forgotClicks='0';
      shadow.querySelector('#forgot').onclick=()=>document.documentElement.dataset.forgotClicks='1';
      let submitted=false;
      for (const input of inputs) input.addEventListener('input',()=>{{
        if ({str(wrong_focus).lower()}) document.querySelector('#unrelated').focus();
        if ({str(redirect).lower()} && input.value) history.replaceState(null,'','/i/chat/wrong-route');
        if (!submitted && inputs.length===4 && inputs.every(node=>node.value.length===1)) {{
          submitted=true;
          document.documentElement.dataset.unlockClicks='1';
          if ({str(valid).lower()} && inputs.map(node=>node.value).join('')==='1234') setTimeout(()=>{{shadow.innerHTML={json.dumps(unlocked)};history.replaceState(null,'','/i/chat');}},80);
          else shadow.querySelector('#error').textContent='Incorrect passcode';
        }}
      }});
      document.querySelector('#unrelated').focus();
      </script>'''


@pytest.mark.parametrize("wrong_focus", [False, True])
async def test_native_observed_shadow_passcode_auto_submits_once_despite_focus_changes(app, wrong_focus):
    app.document("/i/chat/pin/recovery", segmented_passcode_screen(wrong_focus=wrong_focus))
    await app.page.goto("https://x.com/i/chat/pin/recovery?from=%2Fi%2Fchat")
    result = await app.adapter.unlock_messages("1234")
    assert result["success"] and result["status"] == "confirmed"
    assert await app.page.locator("html").get_attribute("data-unlock-clicks") == "1"
    assert await app.page.locator("html").get_attribute("data-forgot-clicks") == "0"
    assert await app.page.locator("#unrelated").input_value() == "Human search"


@pytest.mark.parametrize("options,reason,attempts", [
    ({"valid": False}, "unlock_failed", "1"),
    ({"disabled": True}, "unlock_failed", "0"),
    ({"duplicate": True}, "unsupported_dom", "0"),
    ({"redirect": True}, "unlock_failed", "0"),
    ({"unknown_after": True}, "unlock_failed", "1"),
])
async def test_native_observed_shadow_passcode_errors_do_not_retry_or_claim_success(app, options, reason, attempts):
    app.document("/i/chat/pin/recovery", segmented_passcode_screen(**options))
    await app.page.goto("https://x.com/i/chat/pin/recovery?from=%2Fi%2Fchat")
    with pytest.raises(BrowserActionError) as exc:
        await app.adapter.unlock_messages("1234")
    assert exc.value.reason == reason
    assert await app.page.locator("html").get_attribute("data-unlock-clicks") == attempts
    assert await app.page.locator("html").get_attribute("data-forgot-clicks") == "0"
    assert await app.page.locator("#unrelated").input_value() == "Human search"
    assert await app.page.locator('#embed input').evaluate_all("inputs=>inputs.every(input=>input.value==='')")


async def test_native_observed_shadow_passcode_requires_exact_length_and_existing_gate(app):
    app.document("/i/chat/pin/recovery", segmented_passcode_screen())
    await app.page.goto("https://x.com/i/chat/pin/recovery")
    with pytest.raises(BrowserActionError) as exc:
        await app.adapter.unlock_messages("123456")
    assert exc.value.reason == "unsupported_dom"
    assert await app.page.locator("html").get_attribute("data-unlock-clicks") == "0"
    assert await app.page.locator('#embed input').evaluate_all("inputs=>inputs.every(input=>input.value==='')")


async def test_native_chat_unlock_inbox_search_and_repeated_thread_reads_preserve_spa_without_reload(app):
    unlocked = '''<main><section data-testid="dm-inbox-panel">
      <a href="/i/chat/fixture_A">Alice</a><a href="/i/chat/fixture_B">Bob</a>
      </section><section data-testid="dm-conversation-panel">Start a conversation</section></main>'''
    thread = '<header data-testid="dm-conversation-header">@alice</header>' + entry("Visible history", "first", direction="incoming", modern=True) + '<textarea data-testid="dm-composer-textarea"></textarea>'
    body = segmented_passcode_screen(unlocked_html=unlocked)
    body += '<script>history.replaceState(null,"","/i/chat/pin/recovery?from=%2Fi%2Fchat");document.querySelector("#embed").shadowRoot.addEventListener("click",event=>{const row=event.target.closest("a[href^=\\\"/i/chat/\\\"]");if(row){event.preventDefault();history.pushState(null,"",row.getAttribute("href"));document.querySelector("#embed").shadowRoot.querySelector("[data-testid=dm-conversation-panel]").innerHTML=' + json.dumps(thread) + ';}});</script>'
    app.document("/i/chat", body)
    with pytest.raises(BrowserBlocked) as exc:
        await app.adapter.get_inbox()
    assert exc.value.reason == "pin_required"
    assert (await app.adapter.unlock_messages("1234"))["success"]
    assert (await app.adapter.get_inbox())["count"] == 2
    assert (await app.adapter.search_conversations("Alice"))["count"] == 1
    assert (await app.adapter.get_conversation("https://x.com/i/chat/fixture_A"))["count"] == 1
    assert (await app.adapter.get_conversation("https://x.com/i/chat/fixture_A"))["count"] == 1
    assert (await app.adapter.get_inbox())["count"] == 2
    documents = [path for host, path, kind in app.requests if kind == "document"]
    assert documents == ["/i/chat"]
    assert await app.page.locator("html").get_attribute("data-unlock-clicks") == "1"


async def test_native_already_unlocked_inbox_is_idempotent_with_observed_ready_state(app):
    app.document("/messages", '<main><div data-testid="DMInbox">Your inbox is empty</div></main>')
    result = await app.adapter.unlock_messages("1234")
    assert result["success"] and result["status"] == "already_unlocked"


@pytest.mark.parametrize("body,reason", [
    ('<main>Verify you are human</main>', "challenge"),
    ('<main>Rate limit exceeded</main>', "rate_limited"),
    ('<main><button data-testid="loginButton">Log in</button></main>', "login_required"),
    ('<main>Enter your PIN<input type="password" inputmode="numeric"></main>', "pin_required"),
])
async def test_native_inbox_read_respects_authentication_rate_limit_and_pin_gates(app, body, reason):
    app.document("/messages", body)
    with pytest.raises(BrowserBlocked) as exc:
        await app.adapter.get_inbox()
    assert exc.value.reason == reason


async def test_probe_metadata_does_not_export_bodies_pin_values_or_dynamic_identifiers(app):
    probe_path = Path(__file__).resolve().parents[2] / "scripts" / "inspect_x_session.py"
    spec = importlib.util.spec_from_file_location("offline_inbox_probe", probe_path)
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    private_marker = "synthetic-private-contact-and-message"
    app.document("/i/chat/PrivateFixtureThread", f'''<main>
      <h1>{private_marker}</h1><button aria-label="{private_marker}">{private_marker}</button>
      <input type="password" name="pin" inputmode="numeric" value="946321">
      <div data-testid="{private_marker}" role="{private_marker}">Private body</div>
      </main>''')
    await app.page.goto("https://x.com/i/chat/PrivateFixtureThread")
    metadata = await probe._metadata(app.page)
    serialized = json.dumps(metadata)
    assert private_marker not in serialized
    assert "946321" not in serialized
    assert "PrivateFixtureThread" not in serialized
    assert "Private body" not in serialized
    assert metadata["route_family"] == "/i/chat/:conversation"
    assert metadata["numeric_pin_input_count"] == 1
    assert "synthetic-private-error" not in json.dumps(probe._failure(ValueError("synthetic-private-error")))
