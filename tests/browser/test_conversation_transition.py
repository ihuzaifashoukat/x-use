"""Locator-only regressions for URL-first native conversation selection."""
import pytest

from xuse.browser.errors import BrowserActionError
from test_messaging import Browser, Node, Page


def selection_browser(hydrates=False):
    page = Page()
    page.url = "https://x.com/i/chat/PreviousFixture"
    target = "https://x.com/i/chat/SelectedFixture"
    header = Node("@previous")
    entry = Node(attrs={"data-message-id": "previous-message", "data-direction": "incoming"},
                 children={'[data-testid="messageEntryText"]': [Node("Previous fixture body")]})
    scope = Node(children={'[data-testid="messageEntry"]': [entry],
                           '[data-testid="chatConversationHeader"]': [header]})

    def select():
        page.url = target
        if hydrates:
            header.text = "@selected"
            entry.attrs["data-message-id"] = "selected-message"
            entry.children['[data-testid="messageEntryText"]'][0].text = "Selected fixture body"

    link = Node("Selected fixture", attrs={"href": "/i/chat/SelectedFixture"}, on_click=select)
    page.children = {'[data-testid="chatConversation"]': [scope],
                     'a[href^="/i/chat/"]': [link]}
    return Browser(page), target, link


@pytest.mark.asyncio
async def test_previous_conversation_is_not_relabelled_after_url_update():
    browser, target, link = selection_browser()
    for _ in range(2):
        with pytest.raises(BrowserActionError) as failure:
            await browser.get_conversation(target)
        assert failure.value.reason == "conversation_mismatch"
    assert link.clicked == 1 and browser.navigated == []


@pytest.mark.asyncio
async def test_immediate_native_transition_then_same_route_reuses_document():
    browser, target, link = selection_browser(hydrates=True)
    for _ in range(2):
        result = await browser.get_conversation(target)
        assert result["conversation_id"] == "SelectedFixture"
        assert result["messages"][0]["text"] == "Selected fixture body"
    assert link.clicked == 1 and browser.navigated == []
