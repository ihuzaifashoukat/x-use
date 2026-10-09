"""An inbox shell cannot prove readiness while a native PIN gate hydrates."""
import pytest

from xuse.browser.errors import BrowserActionError, BrowserBlocked
from test_messaging_dom import app, native_browser, segmented_passcode_screen  # noqa: F401

pytestmark = pytest.mark.asyncio(loop_scope="module")


def staged_gate(*, route_delay=0, prompt_delay=0, partial=True, missing_last=False,
                challenge=False, invalid_label=False, valid=True):
    body = '<section data-testid="dm-inbox-panel">Your inbox is empty</section>'
    body += segmented_passcode_screen(valid=valid)
    return body + f'''<script>
      const gate=document.querySelector('#embed').shadowRoot;
      const main=gate.querySelector('main');
      const prompt=gate.querySelector('h1');
      const fields=[...gate.querySelectorAll('input')];
      for(const field of fields)field.remove();
      if({prompt_delay})prompt.remove();
      if({str(invalid_label).lower()})fields[0].setAttribute('aria-label','Digit 7 of 4');
      const moveRoute=()=>history.replaceState(null,'','/i/chat/pin/recovery');
      if({route_delay})setTimeout(moveRoute,{route_delay});else moveRoute();
      if({prompt_delay})setTimeout(()=>main.prepend(prompt),{prompt_delay});
      fields.forEach((field,index)=>{{
        if({str(missing_last).lower()}&&index===3)return;
        setTimeout(()=>main.append(field),180+({str(partial).lower()}?index*70:0));
      }});
      gate.addEventListener('input',event=>{{
        if(!event.target.matches('input')||!event.target.value)return;
        const ready=location.pathname==='/i/chat/pin/recovery'&&gate.querySelector('h1')&&
          [...gate.querySelectorAll('input')].length===4;
        if(!ready)document.documentElement.dataset.earlyTyping='1';
      }},true);
      if({str(challenge).lower()})setTimeout(()=>{{
        const notice=document.createElement('p');notice.textContent='Verify you are human';document.body.append(notice);
      }},130);
      </script>'''


@pytest.mark.parametrize("options", [
    {"route_delay": 500}, {"prompt_delay": 100}, {"partial": False},
])
async def test_one_unlock_waits_for_exact_complete_gate_despite_ready_outer_inbox(app, options):
    app.adapter.messaging_timeout_ms = 1200
    app.document("/i/chat", staged_gate(**options))
    result = await app.adapter.unlock_messages("1234")
    assert result["status"] == "confirmed"
    assert await app.page.locator("html").get_attribute("data-unlock-clicks") == "1"
    assert await app.page.locator("html").get_attribute("data-early-typing") is None
    assert await app.page.locator("#unrelated").input_value() == "Human search"


@pytest.mark.parametrize("options,reason", [
    ({"missing_last": True}, "unsupported_dom"),
    ({"invalid_label": True}, "unsupported_dom"),
    ({"challenge": True}, "challenge"),
])
async def test_partial_or_unrecognized_gate_never_types_or_bypasses_challenge(app, options, reason):
    app.document("/i/chat", staged_gate(**options))
    with pytest.raises((BrowserActionError, BrowserBlocked)) as error:
        await app.adapter.unlock_messages("1234")
    assert error.value.reason == reason
    assert await app.page.locator("html").get_attribute("data-unlock-clicks") == "0"
    assert await app.page.locator('#embed input').evaluate_all("inputs=>inputs.every(input=>input.value==='')")


async def test_hydrated_gate_wrong_pin_is_attempted_once_and_cleared(app):
    app.adapter.messaging_timeout_ms = 1200
    app.document("/i/chat", staged_gate(route_delay=120, valid=False))
    with pytest.raises(BrowserActionError) as error:
        await app.adapter.unlock_messages("1234")
    assert error.value.reason == "unlock_failed"
    assert await app.page.locator("html").get_attribute("data-unlock-clicks") == "1"
    assert await app.page.locator('#embed input').evaluate_all("inputs=>inputs.every(input=>input.value==='')")


async def test_empty_inbox_shell_waits_for_actual_ready_state(app):
    app.document("/i/chat", '''<main><div data-testid="DMInbox" id="mount">Loading inbox</div></main>
      <script>setTimeout(()=>document.querySelector('#mount').textContent='Your inbox is empty',200);</script>''')
    result = await app.adapter.unlock_messages("1234")
    assert result["status"] == "already_unlocked"
    assert await app.page.locator("#mount").inner_text() == "Your inbox is empty"


async def test_challenge_after_first_digit_stops_before_pin_submission(app):
    body = segmented_passcode_screen()
    body += '''<script>
      document.querySelector('#embed').shadowRoot.addEventListener('input',event=>{
        if(event.target.value){const notice=document.createElement('p');
          notice.textContent='Verify you are human';document.body.append(notice);}
      });</script>'''
    app.document("/i/chat/pin/recovery", body)
    await app.page.goto("https://x.com/i/chat/pin/recovery")
    with pytest.raises(BrowserBlocked, match="challenge"):
        await app.adapter.unlock_messages("1234")
    assert await app.page.locator("html").get_attribute("data-unlock-clicks") == "0"
    assert await app.page.locator('#embed input').evaluate_all("inputs=>inputs.every(input=>input.value==='')")
