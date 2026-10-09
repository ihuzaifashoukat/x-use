"""Causal send proofs survive body/control hydration without relaxing evidence."""
import pytest

from xuse.browser.errors import BrowserActionError
from test_messaging_dom import app, native_browser  # noqa: F401
from test_messaging_send_dom import CHAT_URL, NEW_ID, PAYLOAD, modern_chat_script


pytestmark = pytest.mark.asyncio(loop_scope="module")


async def load(app, script):
    app.document("/i/chat/10-20", script)
    await app.page.goto(CHAT_URL)
    await app.page.locator('[data-testid="dm-conversation-panel"]').wait_for(state="visible")
    app.adapter.messaging_confirmation_timeout_ms = 1200


async def disposed(app):
    assert await app.page.evaluate("Object.keys(window).filter(key=>key.startsWith('__xuse_send_')).length") == 0


async def test_native_lost_pending_when_body_late_is_retained_for_exact_confirmation(app):
    script = modern_chat_script("fast").replace(
        "if(behavior==='duplicate_body')", "created.body.hidden=true;setTimeout(()=>{created.body.hidden=false;},60);if(behavior==='duplicate_body')")
    await load(app, script)
    result = await app.adapter.send_message("@alice", PAYLOAD)
    assert result["message_id"] == NEW_ID and result["success"]
    assert await app.clicks() == 1
    await disposed(app)


async def test_native_hidden_first_send_does_not_bind_the_wrong_button(app):
    script = modern_chat_script().replace("controls.append(control);", "const hidden=control.cloneNode(true);hidden.hidden=true;controls.append(hidden,control);")
    await load(app, script)
    result = await app.adapter.send_message("@alice", PAYLOAD)
    assert result["success"] and await app.clicks() == 1
    await disposed(app)


async def test_native_remounted_send_before_click_still_binds_exact_trusted_control(app):
    await load(app, modern_chat_script())
    original = app.adapter._participant_handles
    inspections = 0
    async def inspect(scope, expected_handle=None):
        nonlocal inspections
        result = await original(scope, expected_handle)
        inspections += 1
        if inspections == 5:  # The header check immediately after arming.
            # DOM event dispatch crosses driver worlds; do not depend on a
            # fixture window global being shared by Patchright's evaluator.
            await app.page.locator('[data-testid="dm-composer-send-button"]').evaluate("old=>{const fresh=old.cloneNode(true);fresh.addEventListener('click',()=>old.click());old.replaceWith(fresh);}")
        return result
    app.adapter._participant_handles = inspect
    result = await app.adapter.send_message("@alice", PAYLOAD)
    assert result["success"] and await app.clicks() == 1
    await disposed(app)


async def test_native_remounted_composer_after_send_uses_unique_current_empty_composer(app):
    script = modern_chat_script().replace("if(behavior!=='uncleared')composer.value='';", "if(behavior!=='uncleared'){const fresh=composer.cloneNode(true);fresh.value='';composer.replaceWith(fresh);}")
    await load(app, script)
    result = await app.adapter.send_message("@alice", PAYLOAD)
    assert result["success"] and await app.clicks() == 1
    assert await app.page.locator('[data-testid="dm-composer-textarea"]').input_value() == ""
    await disposed(app)


@pytest.mark.parametrize("behavior,pending,failed", [
    ("skip_pending", 0, 0), ("pending", 1, 0), ("failed", 1, 1), ("no_change", 0, 0),
])
async def test_native_uncertain_diagnostics_are_small_and_primitive(app, behavior, pending, failed):
    await load(app, modern_chat_script(behavior))
    with pytest.raises(BrowserActionError) as failure:
        await app.adapter.send_message("@alice", PAYLOAD)
    error = failure.value
    assert error.reason == "send_unconfirmed"
    assert error.diagnostics == {"schema_version": 1, "stage": "confirmation_timeout",
                                 "observer_armed": True, "observer_started": True,
                                 "composer_empty": True, "pending_count": pending, "failed_count": failed,
                                 "scope_connected": True, "composer_connected": True, "send_control_connected": True}
    assert PAYLOAD not in str(error) and NEW_ID not in str(error.diagnostics)
    assert await app.clicks() == 1
    await disposed(app)


async def test_native_route_changed_has_safe_diagnostics_and_remains_uncertain(app):
    await load(app, modern_chat_script("redirect"))
    with pytest.raises(BrowserActionError) as failure:
        await app.adapter.send_message("@alice", PAYLOAD)
    assert failure.value.reason == "send_unconfirmed"
    assert failure.value.diagnostics["stage"] == "route_changed"
    assert failure.value.diagnostics["observer_started"] is True
    assert await app.clicks() == 1
    await disposed(app)


@pytest.mark.parametrize("behavior", ["skip_pending", "wrong_body", "failed_then_sent"])
async def test_native_late_body_never_relaxes_direct_sent_text_or_failure_proof(app, behavior):
    script = modern_chat_script(behavior).replace(
        "if(behavior==='duplicate_body')", "created.body.hidden=true;setTimeout(()=>{created.body.hidden=false;},60);if(behavior==='duplicate_body')")
    await load(app, script)
    with pytest.raises(BrowserActionError) as failure:
        await app.adapter.send_message("@alice", PAYLOAD)
    assert failure.value.reason == "send_unconfirmed"
    assert failure.value.diagnostics["observer_started"] is True
    if behavior == "skip_pending":
        assert failure.value.diagnostics["pending_count"] == 0
    if behavior == "failed_then_sent":
        assert failure.value.diagnostics["failed_count"] == 1
    assert await app.clicks() == 1
    await disposed(app)


async def test_native_hidden_first_empty_composer_does_not_confirm_active_uncleared_composer(app):
    script = modern_chat_script("uncleared").replace("composer.value=settings.draft;", "composer.value=settings.draft;const hidden=composer.cloneNode(true);hidden.style.display='none';composer.before(hidden);")
    await load(app, script)
    with pytest.raises(BrowserActionError) as failure:
        await app.adapter.send_message("@alice", PAYLOAD)
    assert failure.value.reason == "send_unconfirmed"
    assert failure.value.diagnostics["composer_empty"] is False
    assert await app.clicks() == 1
    await disposed(app)


async def test_native_detached_scope_never_reattaches_or_confirms_another_root(app):
    script = modern_chat_script().replace("composer.value='';", "composer.value='';const panel=shadow.querySelector('section');panel.replaceWith(panel.cloneNode(true));")
    await load(app, script)
    with pytest.raises(BrowserActionError) as failure:
        await app.adapter.send_message("@alice", PAYLOAD)
    assert failure.value.reason == "send_unconfirmed"
    assert failure.value.diagnostics["scope_connected"] is False
    assert failure.value.diagnostics["composer_connected"] is False
    assert failure.value.diagnostics["composer_empty"] is None
    assert await app.clicks() == 1
    await disposed(app)
