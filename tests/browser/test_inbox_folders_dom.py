"""Observed shadow-DOM inbox folders and native unread selection."""
import pytest

from xuse.browser.errors import BrowserActionError
from xuse.browser.messaging import conversation_url
from test_messaging_dom import app, native_browser  # noqa: F401


pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest.mark.parametrize("inbox_filter,expected_count", [("all", 3), ("unread", 1)])
async def test_selected_native_conversation_keeps_sidebar_readable_without_reload(app, inbox_filter, expected_count):
    document = inbox_document()
    transition = "history.pushState(null,'',paths[folder]);render();}};menu.append(item);"
    assert transition in document
    document = document.replace(transition, "if(value==='requests')" + transition)
    app.document("/i/chat/10-20", document)
    await app.page.goto("https://x.com/i/chat/10-20")
    # Native filters can update the sidebar while retaining the selected chat.
    result = await app.adapter.get_inbox(inbox_filter=inbox_filter)
    assert result["count"] == expected_count
    assert app.page.url == "https://x.com/i/chat/10-20"
    assert len([request for request in app.requests if request[2] == "document"]) == 1


def inbox_document(*, folder="inbox", info=False, selection="all", hidden_icon=False, bad_menu=False):
    settings = {"folder": folder, "info": info, "selection": selection,
                "hiddenIcon": hidden_icon, "badMenu": bad_menu}
    import json
    return '''<div id="chat-host"></div><script>
      const settings=SETTINGS,shadow=document.querySelector('#chat-host').attachShadow({mode:'open'});
      let folder=settings.folder,selection=settings.selection;
      const paths={inbox:'/i/chat/',requests:'/i/chat/requests',other:'/i/chat/requests/other'};
      document.documentElement.dataset.rowClicks='0';document.documentElement.dataset.dismissClicks='0';
      const row=(id,text,unread,request=false)=>{
        const container=document.createElement('div');container.dataset.testid=(request?'dm-message-request-item-':'dm-conversation-item-')+id;
        const a=document.createElement('a');a.href=request?(folder==='other'?'/i/chat/requests/other/':'/i/chat/requests/')+id.replace(':','-'):'/i/chat/'+id.replace(':','-');
        a.textContent=text;
        if(unread){const holder=document.createElement('span');holder.innerHTML='<svg data-icon="icon-circle-fill" role="img" aria-hidden="true" class="text-chat-accent shrink-0 text-[8px]" '+(settings.hiddenIcon?'style="display:none"':'')+'><circle></circle></svg>';a.append(holder);}
        a.onclick=e=>{e.preventDefault();document.documentElement.dataset.rowClicks='1';history.pushState(null,'',a.href);};container.append(a);return container;
      };
      const render=()=>{
        shadow.innerHTML='<style>[hidden]{display:none!important}svg{width:8px;height:8px}</style><section data-testid="dm-inbox-panel"></section>';
        const panel=shadow.querySelector('section');
        if(folder==='inbox'){
          const trigger=document.createElement('button');trigger.dataset.testid='dm-inbox-dropdown-trigger';trigger.textContent=selection==='unread'?'Unread':'All';panel.append(trigger);
          trigger.onclick=()=>{
            const existing=panel.querySelector('[role="menu"]');if(existing){existing.remove();return;}
            const menu=document.createElement('div');menu.dataset.testid='dm-inbox-dropdown-content';menu.setAttribute('role','menu');
            for(const [value,text] of [['all','All'],['unread','Unread'],['oneonone','Direct'],['groups','Groups'],['requests','Requests']]){
              const item=document.createElement('button');item.dataset.testid='dm-inbox-dropdown-'+value;item.setAttribute('role','menuitemradio');
              item.setAttribute('aria-checked',selection===value?'true':'false');item.textContent=text;
              item.onclick=()=>{if(!settings.badMenu){selection=value;if(value==='requests')folder='requests';history.pushState(null,'',paths[folder]);render();}};menu.append(item);
            }panel.append(menu);
          };
          const scroller=document.createElement('div');scroller.dataset.testid='dm-conversation-list';panel.append(scroller);
          if(selection==='all')scroller.append(row('10:20','Read or unknown preview',false));
          scroller.append(row('10:30','Unread preview',true));
          if(selection==='all')scroller.append(row('10:40','Unknown preview',false));
        }else{
          const root=document.createElement('section');root.dataset.testid='dm-message-requests';panel.append(root);
          const back=document.createElement('button');back.dataset.testid='dm-message-requests-back';back.setAttribute('aria-label','Back');back.textContent='Back';
          back.onclick=()=>{folder=folder==='other'?'requests':'inbox';selection='all';history.pushState(null,'',paths[folder]);render();};root.append(back);
          if(folder==='requests'){
            const other=document.createElement('button');other.dataset.testid='dm-message-requests-other-button';other.textContent='Other';
            other.onclick=()=>{folder='other';history.pushState(null,'',paths.other);render();};root.append(other);
          }else{const disclaimer=document.createElement('p');disclaimer.dataset.testid='dm-message-requests-other-disclaimer';disclaimer.textContent='These message requests may be spam or lower priority.';root.append(disclaimer);}
          const scroller=document.createElement('div');scroller.dataset.testid='dm-message-requests-scroller';scroller.append(row('10:50',folder==='other'?'Other request preview':'Request preview',false,true));root.append(scroller);
          // An unrelated main-list row must not enter Requests results.
          panel.append(row('99:99','Unrelated main preview',true));
          if(settings.info&&folder==='requests'){
            const dialog=document.createElement('div');dialog.dataset.testid='dm-message-requests-info-sheet';dialog.setAttribute('role','dialog');
            dialog.innerHTML='<h2>Message Requests</h2><p>Message requests from accounts you don’t follow live here. To reply to their messages, you need to accept the request.</p><button>Dismiss</button><button>Learn more</button>';
            dialog.querySelector('button').onclick=()=>{document.documentElement.dataset.dismissClicks='1';settings.info=false;dialog.remove();};panel.append(dialog);
          }
        }
      };render();</script>'''.replace("SETTINGS", json.dumps(settings))


async def test_observed_requests_route_is_not_reloaded_by_inbox_bootstrap(app):
    app.document("/i/chat/requests", inbox_document(folder="requests"))
    app.document("/i/chat", inbox_document())
    await app.page.goto("https://x.com/i/chat/requests")
    await app.adapter._open_inbox()
    assert app.page.url == "https://x.com/i/chat/requests"
    assert len([r for r in app.requests if r[2] == "document"]) == 1


async def test_observed_native_unread_selection_is_used_without_opening_chat(app):
    app.document("/i/chat", inbox_document(hidden_icon=True))
    await app.page.goto("https://x.com/i/chat")
    result = await app.adapter.get_inbox(inbox_filter="unread")
    assert result["count"] == 1 and result["conversations"][0]["unread"] is True
    assert result["filter_application"] == "native_unread_selection"
    assert await app.page.locator('[data-testid="dm-inbox-dropdown-trigger"]').inner_text() == "Unread"
    assert await app.page.locator("html").get_attribute("data-row-clicks") == "0"


async def test_folder_routes_are_not_conversation_ids():
    for route in ("/i/chat/requests", "/i/chat/requests/other", "/i/chat/requests/invalid"):
        with pytest.raises(ValueError):
            conversation_url("https://x.com" + route)
    for route in ("/i/chat/requests/10-20", "/i/chat/requests/other/10-20"):
        assert conversation_url("https://x.com" + route) == ("https://x.com" + route, "10-20")


@pytest.mark.parametrize("folder", ["requests", "other"])
async def test_selected_request_sidebar_is_classified_and_left_natively(app, folder):
    route = "/i/chat/requests/" + ("other/" if folder == "other" else "") + "10-50"
    app.document(route, inbox_document(folder=folder))
    await app.page.goto("https://x.com" + route)
    result = await app.adapter.get_inbox(folder=folder)
    assert result["count"] == 1 and result["folder"] == folder
    assert result["conversations"][0]["conversation_id"] == "10-50"
    assert result["conversations"][0]["unread"] is None
    assert app.page.url.endswith(route)
    found = await app.adapter.search_conversations("request preview", folder=folder)
    assert found["count"] == 1
    main = await app.adapter.get_inbox(folder="inbox")
    assert main["count"] == 3 and main["folder"] == "inbox"
    assert len([r for r in app.requests if r[2] == "document"]) == 1
    assert await app.page.locator("html").get_attribute("data-row-clicks") == "0"


@pytest.mark.parametrize("apostrophe", ["'", "’"])
async def test_request_info_dismissal_does_not_accept_or_open_a_request(app, apostrophe):
    escaped = "\\'" if apostrophe == "'" else apostrophe
    app.document("/i/chat/requests", inbox_document(folder="requests", info=True).replace("don’t", "don" + escaped + "t"))
    await app.page.goto("https://x.com/i/chat/requests")
    result = await app.adapter.get_inbox(folder="requests")
    assert result["count"] == 1
    assert await app.page.locator("html").get_attribute("data-dismiss-clicks") == "1"
    assert await app.page.locator("html").get_attribute("data-row-clicks") == "0"


async def test_other_filter_returns_partial_unknown_instead_of_fabricating_read_state(app):
    app.document("/i/chat", inbox_document())
    await app.page.goto("https://x.com/i/chat")
    other = await app.adapter.get_inbox(folder="other", inbox_filter="unread")
    assert other["count"] == 0 and other["unread_unknown_count"] == 1
    assert other["partial"] and other["coverage"] == "visible_only"
    requests = await app.adapter.get_inbox(folder="requests")
    assert requests["count"] == 1
    assert len([r for r in app.requests if r[2] == "document"]) == 1
