"""Request acceptance state is observed independently of the route."""
import pytest

from test_messaging_dom import app, native_browser, conversation, entry  # noqa: F401
from xuse.browser.errors import BrowserActionError


pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest.mark.parametrize("pending", [True, False])
async def test_request_route_reports_prompt_or_enabled_composer_without_accepting(app, pending):
    route = "/i/chat/requests/other/10-20"
    document = conversation(existing=entry("Synthetic request", "stable", direction="incoming"))
    if pending:
        document += '''<script>
        document.querySelector('[data-testid="dmComposerTextInput"]').remove();
        const prompt=document.createElement('section');prompt.dataset.testid='dm-message-request-prompt';
        prompt.innerHTML='<button data-testid="dm-message-request-accept-button">Accept</button>';
        prompt.querySelector('button').onclick=()=>document.documentElement.dataset.acceptClicks='1';
        document.querySelector('[data-testid="DMConversationView"]').append(prompt);
        </script>'''
    app.document(route, document)
    await app.page.goto("https://x.com" + route)
    result = await app.adapter.get_conversation("https://x.com" + route)
    assert result["count"] == 1 and result["acceptance_required"] is pending
    assert result["can_reply"] is (not pending)
    assert result["reply_state_source"] == ("visible_request_prompt" if pending else "visible_composer")
    assert await app.page.locator("html").get_attribute("data-accept-clicks") is None
    assert len([r for r in app.requests if r[2] == "document"]) == 1


async def test_conflicting_prompt_and_composer_fail_closed(app):
    route = "/i/chat/requests/other/10-20"
    document = conversation(existing=entry("Synthetic request", "stable", direction="incoming"))
    document += '''<script>const p=document.createElement('section');p.dataset.testid='dm-message-request-prompt';
    p.innerHTML='<button data-testid="dm-message-request-accept-button">Accept</button>';
    document.querySelector('[data-testid="DMConversationView"]').append(p);</script>'''
    app.document(route, document)
    await app.page.goto("https://x.com" + route)
    with pytest.raises(BrowserActionError, match="unsupported_dom"):
        await app.adapter.get_conversation("https://x.com" + route)


@pytest.mark.parametrize("attribute", ["disabled", "readonly", 'aria-disabled="true"'])
async def test_unwritable_composer_does_not_claim_reply_available(app, attribute):
    route = "/i/chat/10-20"
    document = conversation(existing=entry("Synthetic history", "stable", direction="incoming"))
    document = document.replace('<textarea ', '<textarea ' + attribute + ' ')
    app.document(route, document)
    await app.page.goto("https://x.com" + route)
    result = await app.adapter.get_conversation("https://x.com" + route)
    assert result["acceptance_required"] is False and result["can_reply"] is False
