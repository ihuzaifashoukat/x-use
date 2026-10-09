"""A native SPA route update must wait for the selected visible conversation."""
import json

import pytest

from xuse.browser.errors import BrowserActionError
from test_messaging_dom import app, native_browser  # noqa: F401 - intercepted browser fixtures


pytestmark = pytest.mark.asyncio(loop_scope="module")

PREVIOUS_PATH = "/i/chat/10-20"
SELECTED_URL = "https://x.com/i/chat/10-30"
PREVIOUS_ID = "11111111-1111-4111-8111-111111111111"
SELECTED_ID = "22222222-2222-4222-8222-222222222222"


def transition_document(*, header_delay=80, message_delay=240, retain_old=False, empty=False):
    settings = json.dumps({"headerDelay": header_delay, "messageDelay": message_delay,
                           "retainOld": retain_old, "empty": empty,
                           "previous": PREVIOUS_ID, "selected": SELECTED_ID})
    return '''<main><section data-testid="dm-inbox-panel">
      <a href="/i/chat/10-30" id="select">Selected fixture</a></section>
      <section data-testid="dm-conversation-panel">
        <header data-testid="dm-conversation-header"><a href="/previous">Previous fixture</a></header>
        <svg aria-hidden="true" width="16" height="16"><path d="M0 0L16 16"/></svg>
        <div data-testid="dm-conversation-content" id="history"></div>
        <textarea data-testid="dm-composer-textarea"></textarea>
        <button data-testid="dm-composer-send-button">Send</button>
      </section></main><script>
      const settings=SETTINGS;
      const historyPanel=document.querySelector('#history');
      const row=(id,text)=>{
        const wrapper=document.createElement('div');wrapper.dataset.dmItemKey=id;
        const item=document.createElement('div');item.dataset.dmMessageRow=id;item.dataset.testid='message-'+id;
        const focus=document.createElement('div');focus.setAttribute('data-dm-message-focus-target','');
        const body=document.createElement('div');body.dataset.testid='message-text-'+id;
        const authored=document.createElement('span');authored.dir='auto';authored.className='whitespace-pre-wrap';authored.textContent=text;
        body.append(authored);focus.append(body);item.append(focus);wrapper.append(item);return wrapper;
      };
      historyPanel.append(row(settings.previous,'Previous fixture body'));
      document.querySelector('#select').addEventListener('click',event=>{
        event.preventDefault();history.pushState(null,'','/i/chat/10-30');
        document.documentElement.dataset.rowClicks=String(Number(document.documentElement.dataset.rowClicks||0)+1);
        if(settings.headerDelay!==null)setTimeout(()=>{
          document.querySelector('[data-testid="dm-conversation-header"]').innerHTML='<a href="/selected">Selected fixture</a>';
          document.documentElement.dataset.headerHydrated='true';
        },settings.headerDelay);
        if(settings.messageDelay!==null)setTimeout(()=>{
          if(!settings.retainOld)historyPanel.replaceChildren();
          if(settings.empty)historyPanel.textContent='No messages yet';
          else historyPanel.append(row(settings.selected,'Selected fixture body'));
          document.documentElement.dataset.messagesHydrated='true';
        },settings.messageDelay);
      });
      document.querySelector('[data-testid="dm-composer-send-button"]').addEventListener('click',()=>{
        document.documentElement.dataset.sendClicks=String(Number(document.documentElement.dataset.sendClicks||0)+1);
      });
      </script>'''.replace("SETTINGS", settings)


async def load(app, **options):
    app.document(PREVIOUS_PATH, transition_document(**options))
    await app.page.goto("https://x.com" + PREVIOUS_PATH)
    app.adapter.messaging_timeout_ms = 700


async def test_hidden_previous_detail_cannot_bypass_transition_baseline(app):
    await load(app)
    await app.page.locator('[data-testid="dm-conversation-panel"]').evaluate("panel => panel.hidden = true")
    with pytest.raises(BrowserActionError, match="conversation_mismatch"):
        await app.adapter.get_conversation(SELECTED_URL)
    assert app.page.url == "https://x.com" + PREVIOUS_PATH
    assert await app.page.locator('html').get_attribute('data-row-clicks') is None


async def assert_spa_only(app, clicks=1):
    assert [path for _, path, kind in app.requests if kind == "document"] == [PREVIOUS_PATH]
    assert await app.page.locator("html").get_attribute("data-row-clicks") == str(clicks)


@pytest.mark.parametrize("header_delay,message_delay", [(80, 240), (240, 80), (0, 0)])
async def test_native_url_first_selection_waits_for_header_and_messages(app, header_delay, message_delay):
    await load(app, header_delay=header_delay, message_delay=message_delay)
    result = await app.adapter.get_conversation(SELECTED_URL)
    assert result["conversation_id"] == "10-30"
    assert result["messages"][0]["message_id"] == SELECTED_ID
    assert result["messages"][0]["text"] == "Selected fixture body"
    assert result["participants"] == ["selected"]
    assert await app.page.locator("html").get_attribute("data-header-hydrated") == "true"
    assert await app.page.locator("html").get_attribute("data-messages-hydrated") == "true"
    # A second read reuses the completed selected panel and the unlocked SPA.
    assert (await app.adapter.get_conversation(SELECTED_URL))["messages"] == result["messages"]
    await assert_spa_only(app)


@pytest.mark.parametrize("header_delay,message_delay,retain_old", [
    (None, None, False), (0, None, False), (None, 0, False), (0, 0, True),
])
async def test_native_unhydrated_or_mixed_conversation_fails_closed(app, header_delay, message_delay, retain_old):
    await load(app, header_delay=header_delay, message_delay=message_delay, retain_old=retain_old)
    with pytest.raises(BrowserActionError) as failure:
        await app.adapter.get_conversation(SELECTED_URL)
    assert failure.value.reason == "conversation_mismatch"
    # Retrying the now-selected URL must not bypass the unresolved transition.
    with pytest.raises(BrowserActionError):
        await app.adapter.get_conversation(SELECTED_URL)
    await assert_spa_only(app)


async def test_native_unhydrated_selection_never_fills_or_sends(app):
    await load(app, header_delay=0, message_delay=None)
    with pytest.raises(BrowserActionError) as failure:
        await app.adapter.send_message(SELECTED_URL, "Synthetic draft")
    assert failure.value.reason == "conversation_mismatch"
    assert await app.page.locator('[data-testid="dm-composer-textarea"]').input_value() == ""
    assert await app.clicks() == 0
    await assert_spa_only(app)


async def test_native_selected_empty_conversation_waits_for_new_header(app):
    await load(app, header_delay=240, message_delay=80, empty=True)
    result = await app.adapter.get_conversation(SELECTED_URL)
    assert result["messages"] == [] and result["participants"] == ["selected"]
    await assert_spa_only(app)


async def test_native_accepted_request_main_route_alias_reuses_same_panel(app):
    path = "/i/chat/requests/other/10-30"
    body = transition_document(header_delay=None, message_delay=None).replace(
        "settings.previous,'Previous fixture body'", "settings.selected,'Selected fixture body'")
    app.document(path, body)
    await app.page.goto("https://x.com" + path)
    result = await app.adapter.get_conversation(SELECTED_URL)
    assert result["conversation_id"] == "10-30"
    assert result["messages"][0]["message_id"] == SELECTED_ID
    assert result["messages"][0]["text"] == "Selected fixture body"
    assert result["participants"] == ["previous"]
    assert [requested for _, requested, kind in app.requests if kind == "document"] == [path]
    assert await app.page.locator("html").get_attribute("data-row-clicks") == "1"


async def test_native_header_status_churn_cannot_replace_profile_identity_proof(app):
    body = transition_document(header_delay=0, message_delay=0).replace(
        '<a href="/selected">Selected fixture</a>',
        '<a href="https://x.com/Previous">Previous fixture</a><span>Online now</span>')
    app.document(PREVIOUS_PATH, body)
    await app.page.goto("https://x.com" + PREVIOUS_PATH)
    with pytest.raises(BrowserActionError) as failure:
        await app.adapter.get_conversation(SELECTED_URL)
    assert failure.value.reason == "conversation_mismatch"
    assert await app.page.locator('[data-testid="dm-conversation-header"]').inner_text() == "Previous fixtureOnline now"
    assert await app.page.locator('[data-dm-message-row]').get_attribute("data-dm-message-row") == SELECTED_ID
    await assert_spa_only(app)
