"""Selected chat reads must not reload or reselect the existing thread."""
import pytest

from xuse.browser.errors import BrowserActionError
from test_messaging_dom import app, native_browser, entry  # noqa: F401 - intercepted browser fixtures


pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest.mark.parametrize("sidebar_visible", [False, True])
async def test_selected_chat_repeated_reads_do_not_navigate_or_click(app, sidebar_visible):
    path = "/i/chat/SelectedFixtureThread"
    sidebar = '''<div data-testid="dm-inbox-panel">
      <a href="/i/chat/SelectedFixtureThread" onclick="event.preventDefault();
        document.documentElement.dataset.rowClicks=String(Number(document.documentElement.dataset.rowClicks||0)+1)">
        Fixture person
      </a></div>''' if sidebar_visible else ""
    app.document(path, sidebar + '''<main><div data-testid="chatConversation">
      <div data-testid="chatConversationHeader">@fixture</div>
      <div data-testid="chatMessage" data-message-id="fixture-message" data-direction="incoming">
        <span data-testid="chatMessageText">Fixture message</span>
      </div>
      <textarea data-testid="chatComposerTextInput"></textarea>
    </div></main>''')
    await app.page.goto("https://x.com" + path)
    for _ in range(2):
        result = await app.adapter.get_conversation("https://x.com" + path)
        assert result["messages"][0]["text"] == "Fixture message"
    documents = [requested for _, requested, kind in app.requests if kind == "document"]
    assert documents == [path]
    assert int(await app.page.locator("html").get_attribute("data-row-clicks") or "0") == 0


def inbox_with_route(route="/i/chat/10-20", duplicate=False):
    row = f'<a href="{route}" class="row">Fixture person</a>'
    message = entry("Fixture history", "fixture-message", direction="incoming", modern=True)
    return '''<main><section data-testid="dm-inbox-panel">''' + row * (2 if duplicate else 1) + '''</section>
      <section data-testid="chatConversation">Start a conversation</section></main><script>
      document.querySelectorAll('.row').forEach(row=>row.addEventListener('click',event=>{
        event.preventDefault();history.pushState(null,'',row.getAttribute('href'));
        document.documentElement.dataset.rowClicks=String(Number(document.documentElement.dataset.rowClicks||0)+1);
        document.querySelector('[data-testid=chatConversation]').innerHTML=''' + repr(message) + ''';
      }));</script>'''


@pytest.mark.parametrize("reference", ["10-20", "https://x.com/i/chat/10-20"])
async def test_numeric_id_uses_exact_native_chat_row_and_reuses_selected_chat(app, reference):
    app.document("/i/chat", inbox_with_route())
    await app.page.goto("https://x.com/i/chat")
    for _ in range(2):
        result = await app.adapter.get_conversation(reference)
        assert result["messages"][0]["text"] == "Fixture history"
        assert result["url"] == "https://x.com/i/chat/10-20"
    documents = [path for _, path, kind in app.requests if kind == "document"]
    assert documents == ["/i/chat"]
    assert await app.page.locator("html").get_attribute("data-row-clicks") == "1"


async def test_numeric_id_preserves_observed_legacy_inbox_row(app):
    app.document("/messages", inbox_with_route("/messages/10-20"))
    await app.page.goto("https://x.com/messages")
    result = await app.adapter.get_conversation("10-20")
    assert result["url"] == "https://x.com/messages/10-20"
    assert [path for _, path, kind in app.requests if kind == "document"] == ["/messages"]


async def test_numeric_id_reuses_selected_native_chat_without_sidebar(app):
    app.document("/i/chat/10-20", '<main><section data-testid="chatConversation">' +
                 entry("Selected history", "selected", direction="incoming", modern=True) + '</section></main>')
    await app.page.goto("https://x.com/i/chat/10-20")
    for _ in range(2):
        assert (await app.adapter.get_conversation("10-20"))["messages"][0]["text"] == "Selected history"
    assert [path for _, path, kind in app.requests if kind == "document"] == ["/i/chat/10-20"]


async def test_numeric_id_prefers_native_row_over_legacy_alias(app):
    body = inbox_with_route().replace('</section>', '<a href="/messages/10-20">Legacy alias</a></section>', 1)
    app.document("/i/chat", body)
    await app.page.goto("https://x.com/i/chat")
    assert (await app.adapter.get_conversation("10-20"))["url"] == "https://x.com/i/chat/10-20"
    assert [path for _, path, kind in app.requests if kind == "document"] == ["/i/chat"]


async def test_numeric_id_from_home_resolves_in_the_native_inbox(app):
    app.document("/home", '<main>Home feed</main>')
    app.document("/i/chat", inbox_with_route())
    await app.page.goto("https://x.com/home")
    assert (await app.adapter.get_conversation("10-20"))["url"] == "https://x.com/i/chat/10-20"
    assert [path for _, path, kind in app.requests if kind == "document"] == ["/home", "/i/chat"]


@pytest.mark.parametrize("duplicate", [False, True])
async def test_native_id_missing_or_ambiguous_row_cannot_detour_to_legacy(app, duplicate):
    app.document("/i/chat", inbox_with_route(duplicate=duplicate))
    await app.page.goto("https://x.com/i/chat")
    with pytest.raises(BrowserActionError):
        await app.adapter.get_conversation("10-20" if duplicate else "10-21")
    assert [path for _, path, kind in app.requests if kind == "document"] == ["/i/chat"]
    assert await app.page.locator("html").get_attribute("data-row-clicks") is None
