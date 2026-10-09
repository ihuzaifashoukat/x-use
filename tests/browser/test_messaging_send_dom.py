"""Native profile-to-chat and modern send proofs against intercepted fixtures.

Selectors and UUID equality mirror observed DOM. No account, real recipient,
network endpoint, or live message is used by these tests.
"""

import json

import pytest

from xuse.browser.errors import BrowserActionError, BrowserBlocked
from test_messaging_dom import app, native_browser  # noqa: F401


pytestmark = pytest.mark.asyncio(loop_scope="module")

OLD_ID = "11111111-1111-4111-8111-111111111111"
NEW_ID = "22222222-2222-4222-8222-222222222222"
CHAT_URL = "https://x.com/i/chat/10-20"
PAYLOAD = "Hello\nSynthetic test 🐦 اردو"


def modern_chat_script(behavior="complete", *, header="alice", existing_text="Earlier", draft="", identity_delay_ms=0):
    settings = json.dumps({"behavior": behavior, "header": header, "oldText": existing_text, "draft": draft, "old": OLD_ID, "new": NEW_ID, "identityDelay": identity_delay_ms})
    return """<div data-testid="xchatEmbedRoute" id="chat-host"></div><script>
      const settings=SETTINGS;
      const shadow=document.querySelector('#chat-host').attachShadow({mode:'open'});
      window.openFixtureChat=()=>{
        shadow.innerHTML='<style>.body{white-space:pre-wrap} textarea{display:block;width:350px;height:80px}</style>'+
          '<section data-testid="dm-conversation-panel"><header data-testid="dm-conversation-header"></header>'+
          '<div id="history"></div><textarea data-testid="dm-composer-textarea" aria-label="Message"></textarea>'+
          '<div id="controls"></div></section>';
        const header=shadow.querySelector('header');
        const addProfiles=()=>{for(const handle of settings.header.split(',')){
          const profile=document.createElement('a');profile.href='https://x.com/'+handle;profile.textContent=handle;header.append(profile);
        }};
        if(settings.identityDelay)setTimeout(addProfiles,settings.identityDelay);else addProfiles();
        const makeRow=(id,text,status)=>{
          const wrapper=document.createElement('div');wrapper.dataset.dmItemKey=id;
          const row=document.createElement('div');row.dataset.dmMessageRow=id;row.dataset.testid='message-'+id;row.dataset.sendStatus=status;
          const focus=document.createElement('div');focus.setAttribute('data-dm-message-focus-target','');
          const body=document.createElement('div');body.dataset.testid='message-text-'+id;body.className='body';body.textContent=text;
          const time=document.createElement('time');time.textContent='Earlier';focus.append(body,time);row.append(focus);wrapper.append(row);
          return {wrapper,row,body};
        };
        const history=shadow.querySelector('#history');history.append(makeRow(settings.old,settings.oldText,'sent').wrapper);
        const composer=shadow.querySelector('textarea');composer.value=settings.draft;
        const controls=shadow.querySelector('#controls');
        const send=()=>{
          document.documentElement.dataset.sendClicks=String(Number(document.documentElement.dataset.sendClicks||0)+1);
          const behavior=settings.behavior;
          const id=behavior==='old_id'?settings.old:settings.new;
          const text=behavior==='wrong_body'?'Other body':composer.value;
          const created=makeRow(id,text,behavior==='skip_pending'?'sent':'pending');
          if(behavior==='duplicate_body')created.body.after(created.body.cloneNode(true));
          if(behavior==='wrong_body_id')created.body.dataset.testid='message-text-'+settings.old;
          if(behavior==='bad_key')created.wrapper.dataset.dmItemKey='33333333-3333-4333-8333-333333333333';
          if(behavior==='bad_testid')created.row.dataset.testid='message-33333333-3333-4333-8333-333333333333';
          if(behavior==='prepend')history.prepend(created.wrapper);
          else if(behavior==='replace')history.replaceChildren(created.wrapper);
          else if(behavior!=='no_change')history.append(created.wrapper);
          if(behavior==='duplicate_id')history.append(makeRow(id,text,'sent').wrapper);
          const finish=()=>{
            if(behavior==='failed_then_sent')created.row.dataset.sendStatus='failed';
            if(behavior!=='pending')created.row.dataset.sendStatus=behavior==='failed'?'failed':'sent';
            if(behavior!=='uncleared')composer.value='';
          };
          if(behavior==='fast'||behavior==='failed_then_sent')finish();else setTimeout(finish,30);
          if(behavior==='redirect')window.history.pushState(null,'','/i/chat/10-30');
        };
        const updateControls=()=>{
          controls.replaceChildren();const control=document.createElement('button');
          if(composer.value){control.dataset.testid='dm-composer-send-button';control.setAttribute('aria-label','Send');control.textContent='Send';control.addEventListener('click',send);}
          else{control.dataset.testid='dm-composer-voice-button';control.setAttribute('aria-label','Voice message');control.textContent='Voice';}
          if(settings.behavior==='disabled')control.disabled=true;
          controls.append(control);
          if(settings.behavior==='ambiguous_send'&&composer.value)controls.append(control.cloneNode(true));
          if(settings.behavior==='header_change'&&composer.value){header.querySelector('a').href='https://x.com/bob';}
          if(settings.behavior==='fill_redirect'&&composer.value)window.history.pushState(null,'','/i/chat/10-30');
          if(settings.behavior==='pin_after_fill'&&composer.value){const pin=document.createElement('input');pin.type='password';pin.inputMode='numeric';shadow.append(pin);}
        };
        composer.addEventListener('input',updateControls);updateControls();
      };
      window.openFixtureChat();
      </script>""".replace("SETTINGS", settings)


async def load_chat(app, behavior="complete", **options):
    app.document("/i/chat/10-20", modern_chat_script(behavior, **options))
    await app.page.goto(CHAT_URL)
    assert await app.page.locator('[data-testid="dm-composer-voice-button"]').count() == (0 if options.get("draft") else 1)
    assert await app.page.locator('[data-testid="dm-composer-send-button"]').count() == (1 if options.get("draft") else 0)


async def assert_disposed(app):
    assert await app.page.evaluate("Object.keys(window).filter(key=>key.startsWith('__xuse_send_')).length") == 0


@pytest.mark.parametrize("behavior", ["complete", "fast"])
async def test_native_modern_send_requires_causal_transition_and_lazy_control(app, behavior):
    await load_chat(app, behavior, existing_text=PAYLOAD)
    before = len(app.requests)
    result = await app.adapter.send_message("https://x.com/alice", PAYLOAD)
    assert result["success"] and result["message_id"] == NEW_ID
    assert result["confirmation"] == "new_local_pending_to_sent_message_and_cleared_composer"
    assert result["recipient"] == "@alice"
    assert await app.clicks() == 1 and len(app.requests) == before
    snapshot = await app.adapter._message_snapshot(await app.adapter._conversation_scope())
    assert snapshot[-1]["text"] == PAYLOAD
    assert snapshot[-1]["direction"] == "unknown"  # Receipt alone never proves authorship on later reads.
    await assert_disposed(app)


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "outside_focus", "wrong_id", "duplicate_focus"])
async def test_native_verified_modern_read_rejects_missing_or_ambiguous_body(app, mutation):
    await load_chat(app, existing_text=PAYLOAD)
    await app.page.locator('[data-dm-message-row]').evaluate("""(row,args)=>{
      const focus=row.querySelector('[data-dm-message-focus-target]');const body=focus.querySelector('.body');
      if(args.mutation==='missing')body.remove();
      if(args.mutation==='duplicate')focus.append(body.cloneNode(true));
      if(args.mutation==='outside_focus')row.append(body);
      if(args.mutation==='wrong_id')body.dataset.testid='message-text-'+args.other;
      if(args.mutation==='duplicate_focus')row.append(focus.cloneNode(true));
    }""", {"mutation": mutation, "other": NEW_ID})
    with pytest.raises(BrowserActionError) as failure:
        await app.adapter.get_conversation(CHAT_URL)
    assert failure.value.reason == "unsupported_dom" and await app.clicks() == 0


async def test_native_verified_modern_read_ignores_hidden_duplicate_body(app):
    await load_chat(app, existing_text=PAYLOAD)
    await app.page.locator('[data-dm-message-row]').evaluate("""row=>{
      const body=row.querySelector('.body');const duplicate=body.cloneNode(true);duplicate.hidden=true;body.after(duplicate);
    }""")
    messages=(await app.adapter.get_conversation(CHAT_URL))["messages"]
    assert messages[0]["text"] == PAYLOAD and messages[0]["direction"] == "unknown"
    assert messages[0]["message_id"] == OLD_ID and await app.clicks() == 0


async def test_native_timestamp_matching_payload_cannot_confirm_wrong_message_body(app):
    await load_chat(app, "wrong_body")
    with pytest.raises(BrowserActionError) as failure:
        await app.adapter.send_message("@alice", "Earlier")
    assert failure.value.reason == "send_unconfirmed" and await app.clicks() == 1
    await assert_disposed(app)


@pytest.mark.parametrize("behavior", ["duplicate_body", "wrong_body_id"])
async def test_native_modern_confirmation_rejects_ambiguous_or_wrong_uuid_body(app, behavior):
    await load_chat(app, behavior)
    with pytest.raises(BrowserActionError) as failure:
        await app.adapter.send_message("@alice", PAYLOAD)
    assert failure.value.reason == "send_unconfirmed" and await app.clicks() == 1
    await assert_disposed(app)


@pytest.mark.parametrize("behavior", [
    "skip_pending", "pending", "failed", "failed_then_sent", "wrong_body",
    "old_id", "prepend", "replace", "uncleared", "no_change", "bad_key", "bad_testid", "duplicate_id",
])
async def test_native_modern_send_uncertain_evidence_never_confirms_or_retries(app, behavior):
    await load_chat(app, behavior, existing_text=PAYLOAD)
    with pytest.raises(BrowserActionError) as failure:
        await app.adapter.send_message("@alice", PAYLOAD)
    assert failure.value.reason == "send_unconfirmed"
    assert await app.clicks() == 1
    await assert_disposed(app)


@pytest.mark.parametrize("behavior,reason", [
    ("disabled", "send_disabled"), ("ambiguous_send", "unsupported_dom"),
    ("header_change", "recipient_mismatch"), ("fill_redirect", "conversation_mismatch"),
])
async def test_native_modern_fill_time_recipient_control_and_route_checks_prevent_send(app, behavior, reason):
    await load_chat(app, behavior)
    with pytest.raises(BrowserActionError) as failure:
        await app.adapter.send_message("@alice", PAYLOAD)
    assert failure.value.reason == reason and await app.clicks() == 0
    await assert_disposed(app)


async def test_native_modern_pin_gate_after_fill_prevents_send(app):
    await load_chat(app, "pin_after_fill")
    with pytest.raises(BrowserBlocked) as failure:
        await app.adapter.send_message("@alice", PAYLOAD)
    assert failure.value.reason == "pin_required" and await app.clicks() == 0
    await assert_disposed(app)


async def test_native_modern_redirect_after_click_is_uncertain(app):
    await load_chat(app, "redirect")
    with pytest.raises(BrowserActionError) as failure:
        await app.adapter.send_message("@alice", PAYLOAD)
    assert failure.value.reason == "send_unconfirmed" and await app.clicks() == 1
    await assert_disposed(app)


async def test_native_modern_existing_human_draft_is_preserved(app):
    await load_chat(app, draft="Human draft")
    with pytest.raises(BrowserActionError) as failure:
        await app.adapter.send_message("@alice", PAYLOAD)
    assert failure.value.reason == "composer_not_empty" and await app.clicks() == 0
    assert await app.page.locator('textarea').input_value() == "Human draft"


@pytest.mark.parametrize("recipient", ["@alice", CHAT_URL])
async def test_native_modern_final_policy_refusal_disposes_observer_without_click(app, recipient):
    await load_chat(app)
    calls = []
    async def policy(handle):
        calls.append(handle)
        if await app.page.evaluate("Object.keys(window).some(key=>key.startsWith('__xuse_send_'))"):
            raise ValueError("Private policy data must not escape")
    app.adapter.recipient_validator = policy
    with pytest.raises(BrowserActionError) as failure:
        await app.adapter.send_message(recipient, PAYLOAD)
    assert failure.value.reason == "recipient_blocked" and await app.clicks() == 0
    assert calls == ["alice", "alice"] and "Private" not in str(failure.value)
    await assert_disposed(app)


@pytest.mark.parametrize("header,reason", [("", "recipient_unverified"), ("alice,bob", "recipient_blocked")])
async def test_native_modern_id_cannot_bypass_unknown_or_suppressed_group_participants(app, header, reason):
    await load_chat(app, header=header)
    checked = []
    def policy(handle):
        checked.append(handle)
        if handle == "bob":
            raise ValueError("Synthetic suppression")
    app.adapter.recipient_validator = policy
    with pytest.raises(BrowserActionError) as failure:
        await app.adapter.send_message(CHAT_URL, PAYLOAD)
    assert failure.value.reason == reason and await app.clicks() == 0
    assert checked == (["alice", "bob"] if header else [])
    await assert_disposed(app)


async def test_native_modern_unproven_history_anchor_prevents_send(app):
    await load_chat(app)
    await app.page.locator('[data-dm-message-row]').evaluate("row=>row.dataset.testid='unknown-row'")
    with pytest.raises(BrowserActionError) as failure:
        await app.adapter.send_message("@alice", PAYLOAD)
    assert failure.value.reason == "unsupported_dom" and await app.clicks() == 0
    await assert_disposed(app)


async def test_native_modern_preclick_arrival_cannot_supply_causal_confirmation(app):
    await load_chat(app, "no_change")
    policy_errors = []
    async def policy(_handle):
        try:
            if await app.page.evaluate("Object.keys(window).some(key=>key.startsWith('__xuse_send_'))"):
                await app.page.locator('[data-testid="dm-conversation-panel"]').evaluate("""(scope,args)=>{
                  const original=scope.querySelector('[data-dm-item-key]');const wrapper=original.cloneNode(true);
                  wrapper.dataset.dmItemKey=args.id;const row=wrapper.querySelector('[data-dm-message-row]');
                  row.dataset.dmMessageRow=args.id;row.dataset.testid='message-'+args.id;row.dataset.sendStatus='pending';
                  const body=row.querySelector('.body');body.dataset.testid='message-text-'+args.id;body.textContent=args.text;
                  scope.querySelector('#history').append(wrapper);
                  setTimeout(()=>{row.dataset.sendStatus='sent';scope.querySelector('textarea').value='';},60);
                }""", {"id": NEW_ID, "text": PAYLOAD})
        except Exception as error:
            policy_errors.append(str(error))
            raise
    app.adapter.recipient_validator = policy
    with pytest.raises(BrowserActionError) as failure:
        await app.adapter.send_message("@alice", PAYLOAD)
    assert not policy_errors
    assert failure.value.reason == "send_unconfirmed" and await app.clicks() == 1
    await assert_disposed(app)


def profile_document(*, wrong_profile=False, disabled=False, ambiguous=False, wrong_chat=False, identity_delay_ms=0, missing_identity=False):
    script = modern_chat_script(header="" if missing_identity else "bob" if wrong_chat else "alice", identity_delay_ms=identity_delay_ms)
    # Keep chat inactive until the native profile Message action.
    script = script.replace("window.openFixtureChat();", "")
    return '<div data-testid="UserName">' + ("@bob" if wrong_profile else "@alice") + '</div>' + (
        '<button data-testid="sendDMFromProfile" aria-label="Message" ' + ("disabled" if disabled else "") + '>Message</button>'
    ) * (2 if ambiguous else 1) + script + '''<script>
      document.querySelectorAll('[data-testid="sendDMFromProfile"]').forEach(button=>button.addEventListener('click',()=>{
        document.documentElement.dataset.profileClicks=String(Number(document.documentElement.dataset.profileClicks||0)+1);
        history.pushState(null,'','/i/chat/10-20');
        document.querySelector('[data-testid="UserName"]').remove();
        document.querySelectorAll('[data-testid="sendDMFromProfile"]').forEach(node=>node.remove());
        window.openFixtureChat();
      }));</script>'''


async def test_native_exact_profile_message_opens_chat_and_sends_without_reload(app):
    app.document("/alice", profile_document())
    await app.page.goto("https://x.com/alice")
    before = len(app.requests)
    result = await app.adapter.send_message("https://x.com/alice", PAYLOAD)
    assert result["success"] and app.page.url == CHAT_URL
    assert await app.page.locator("html").get_attribute("data-profile-clicks") == "1"
    assert await app.clicks() == 1 and len(app.requests) == before
    await assert_disposed(app)


async def test_native_profile_waits_for_delayed_header_identity_before_filling_or_sending(app):
    app.adapter.messaging_timeout_ms = 2400
    app.document("/alice", profile_document(identity_delay_ms=1500))
    await app.page.goto("https://x.com/alice")
    result = await app.adapter.send_message("@alice", PAYLOAD)
    assert result["success"] and await app.clicks() == 1
    assert [path for _, path, kind in app.requests if kind == "document"] == ["/alice"]
    await assert_disposed(app)


async def test_native_selected_chat_waits_for_delayed_identity_without_profile_reload(app):
    app.adapter.messaging_timeout_ms = 2400
    await load_chat(app, identity_delay_ms=1500)
    result = await app.adapter.send_message("@alice", PAYLOAD)
    assert result["success"] and await app.clicks() == 1
    assert [path for _, path, kind in app.requests if kind == "document"] == ["/i/chat/10-20"]
    await assert_disposed(app)


async def test_native_profile_delayed_wrong_header_still_blocks_before_fill(app):
    app.adapter.messaging_timeout_ms = 2400
    app.document("/alice", profile_document(wrong_chat=True, identity_delay_ms=1500))
    await app.page.goto("https://x.com/alice")
    with pytest.raises(BrowserActionError) as failure:
        await app.adapter.send_message("@alice", PAYLOAD)
    assert failure.value.reason == "recipient_mismatch" and await app.clicks() == 0
    assert await app.page.locator('textarea').input_value() == ""
    await assert_disposed(app)


async def test_native_profile_permanent_missing_identity_times_out_without_fill_or_reload(app):
    app.adapter.messaging_timeout_ms = 150
    app.document("/alice", profile_document(missing_identity=True))
    await app.page.goto("https://x.com/alice")
    with pytest.raises(BrowserActionError) as failure:
        await app.adapter.send_message("@alice", PAYLOAD)
    assert failure.value.reason == "recipient_unverified" and await app.clicks() == 0
    assert await app.page.locator('textarea').input_value() == ""
    assert [path for _, path, kind in app.requests if kind == "document"] == ["/alice"]
    await assert_disposed(app)


@pytest.mark.parametrize("options,reason,profile_clicks", [
    ({"wrong_profile": True}, "recipient_mismatch", 0),
    ({"disabled": True}, "send_disabled", 0),
    ({"ambiguous": True}, "unsupported_dom", 0),
    ({"wrong_chat": True}, "recipient_mismatch", 1),
])
async def test_native_profile_identity_and_message_control_fail_closed(app, options, reason, profile_clicks):
    app.document("/alice", profile_document(**options))
    await app.page.goto("https://x.com/alice")
    with pytest.raises(BrowserActionError) as failure:
        await app.adapter.send_message("@alice", PAYLOAD)
    assert failure.value.reason == reason and await app.clicks() == 0
    assert int(await app.page.locator("html").get_attribute("data-profile-clicks") or 0) == profile_clicks
    await assert_disposed(app)
