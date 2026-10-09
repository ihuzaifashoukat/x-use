"""Offline DOM fixtures for messaging; no account or network is used."""

import re

import pytest

from xuse.browser.errors import BrowserActionError, BrowserBlocked
from xuse.browser.messaging import MessagingMixin, conversation_url, normalize_recipient


class Locator:
    def __init__(self, nodes=()):
        self.nodes = list(nodes)

    async def count(self):
        return len(self.nodes)

    def nth(self, index):
        return self.nodes[index]

    @property
    def first(self):
        return self.nodes[0] if self.nodes else Node(visible=False)


class Node:
    """Small Playwright locator fixture with only the APIs the adapter uses."""

    def __init__(self, text="", attrs=None, children=None, visible=True, enabled=True, on_click=None):
        self.text = text
        self.attrs = attrs or {}
        self.children = children or {}
        self.visible = visible
        self.enabled = enabled
        self.on_click = on_click
        self.value = ""
        self.clicked = 0
        self.fill_error = False

    def locator(self, selector):
        found = []
        for component in selector.split(", "):
            for owner in self.descendants():
                for node in owner.children.get(component.strip(), []):
                    if node not in found:
                        found.append(node)
        return Locator(found)

    def descendants(self):
        nodes = [self]
        for children in self.children.values():
            for child in children:
                for node in child.descendants():
                    if node not in nodes:
                        nodes.append(node)
        return nodes

    def get_by_text(self, text, exact=False):
        return Locator(node for node in self.descendants() if (
            bool(text.search(node.text)) if hasattr(text, "search")
            else node.text == text if exact else text in node.text
        ))

    def get_by_role(self, role, name=None):
        def matches(node):
            value = node.attrs.get("aria-label", node.text)
            if node.attrs.get("role") != role:
                return False
            if name is None:
                return True
            return bool(name.search(value)) if hasattr(name, "search") else value == name
        return Locator(node for node in self.descendants() if matches(node))

    async def is_visible(self):
        return self.visible

    async def is_enabled(self):
        return self.enabled

    async def get_attribute(self, name):
        return self.attrs.get(name)

    async def inner_text(self):
        return self.text

    async def input_value(self):
        return self.value

    async def evaluate(self, script):
        assert script == "element => element.tagName.toLowerCase()"
        return "textarea"

    async def fill(self, value):
        if self.fill_error:
            raise RuntimeError(f"Call log: fill({value!r})")
        self.value = value

    async def press(self, key):
        assert key == "Enter"

    async def click(self):
        self.clicked += 1
        if self.on_click:
            self.on_click()

    async def wait_for(self, state, timeout):
        if self.visible != (state == "visible"):
            raise RuntimeError("fixture timeout")


class Page(Node):
    url = "https://x.com/messages"


class Browser(MessagingMixin):
    messaging_timeout_ms = 0
    messaging_confirmation_timeout_ms = 0

    def __init__(self, page=None):
        self.page = page or Page()
        self.navigated = []
        self.blocked = None

    async def navigate(self, url):
        self.navigated.append(url)
        self.page.url = url
        await self.check_blocked()

    async def check_blocked(self):
        if self.blocked:
            raise BrowserBlocked(self.blocked)


def message(text="hello", message_id="old", direction="outgoing", pending=False):
    return {"text": text, "message_id": message_id, "direction": direction, "pending_or_failed": pending}


class SendBrowser(Browser):
    def __init__(self, before, after, *, clears=True, redirects=None, enabled=True):
        super().__init__()
        self.page.url = "https://x.com/messages/10-20"
        self.snapshots = [before, after]
        self.field = Node()
        def click():
            if clears:
                self.field.value = ""
            if redirects:
                self.page.url = redirects
        self.send = Node(enabled=enabled, on_click=click)

    async def _open_conversation(self, value):
        url, identifier = conversation_url(value)
        assert url == self.page.url
        return self.page, url, identifier

    async def _composer(self, scope):
        return self.field, self.send

    async def _message_snapshot(self, scope):
        return self.snapshots.pop(0) if len(self.snapshots) > 1 else self.snapshots[0]


@pytest.mark.parametrize("value, expected", [
    ("10-20", ("https://x.com/messages/10-20", "10-20")),
    ("https://x.com/messages/10-20", ("https://x.com/messages/10-20", "10-20")),
    ("https://x.com/i/chat/chat_a-1", ("https://x.com/i/chat/chat_a-1", "chat_a-1")),
    ("https://twitter.com/messages/10-20/", ("https://x.com/messages/10-20", "10-20")),
])
def test_conversation_url_allowlist(value, expected):
    assert conversation_url(value) == expected


@pytest.mark.parametrize("value", [
    "https://evil.example/messages/10-20", "https://x.com.evil.example/messages/10-20",
    "https://x.com@evil.example/messages/10-20", "https://evil@x.com/messages/10-20",
    "http://x.com/messages/10-20", "https://x.com:443/messages/10-20",
    "https://x.com/messages/10-20?redirect=evil", "https://x.com/messages/10-20#fragment",
    "https://x.com/messages/%31%30-20", "https://x.com/i/chat/../settings",
    "https://x.com/i/chat/new", "10-20/evil", "chat_a-1", " 10-20", None,
    "https://x.com/messages/10-\n20", "https://x.com/mes\tsages/10-20",
])
def test_conversation_urls_reject_external_encoded_and_ambiguous_ids(value):
    with pytest.raises(ValueError):
        conversation_url(value)


@pytest.mark.asyncio
async def test_invalid_conversation_never_navigates():
    browser = Browser()
    with pytest.raises(ValueError):
        await browser.get_conversation("https://evil.example/messages/10-20")
    assert browser.navigated == []


@pytest.mark.asyncio
async def test_inbox_rows_are_visible_partial_and_deduplicated():
    row = Node("Person\n@person\nhello", {"href": "/messages/10-20"})
    hidden = Node("Hidden", {"href": "/messages/30-40"}, visible=False)
    evil = Node("Bad URL", {"href": "https://evil.example/messages/50-60"})
    root = Node(children={'[data-testid="conversation"]': [row, row, hidden, evil]})
    browser = Browser(Page(children={'[data-testid="DMInbox"]': [root]}))
    result = await browser.get_inbox()
    assert result["count"] == 1
    assert result["partial"] is True
    assert result["pagination"] == "visible_only"
    assert result["conversations"][0]["conversation_id"] == "10-20"


@pytest.mark.asyncio
async def test_anchor_only_inbox_is_supported_without_guessing_chat_ids():
    row = Node("Person\nhello", {"href": "/i/chat/chat_a1"})
    browser = Browser(Page(children={'a[href^="/i/chat/"]': [row]}))
    result = await browser.get_inbox()
    assert result["conversations"][0]["url"] == "https://x.com/i/chat/chat_a1"


@pytest.mark.asyncio
async def test_empty_inbox_requires_explicit_empty_state():
    empty = Node("Your inbox is empty")
    browser = Browser(Page(children={"empty": [empty]}))
    result = await browser.get_inbox()
    assert result["empty"] is True
    assert result["conversations"] == []
    drift = Browser(Page(children={'[data-testid="DMInbox"]': [Node()]}))
    with pytest.raises(BrowserActionError) as exc:
        await drift.get_inbox()
    assert exc.value.reason == "unsupported_dom"


@pytest.mark.asyncio
async def test_search_matches_only_rendered_conversation_summaries():
    rows = [Node("Alice\nHello there", {"href": "/messages/10-20"}),
            Node("Bob\nOther message", {"href": "/messages/10-30"})]
    root = Node(children={'[data-testid="conversation"]': rows})
    browser = Browser(Page(children={'[data-testid="DMInbox"]': [root]}))
    result = await browser.search_conversations("HELLO")
    assert [row["conversation_id"] for row in result["conversations"]] == ["10-20"]
    assert result["search_scope"] == "visible_conversation_summaries"
    assert result["partial"] is True


@pytest.mark.asyncio
async def test_message_snapshot_marks_direction_unknown_instead_of_guessing():
    body = Node("hello")
    entry = Node(attrs={"data-message-id": "1"}, children={'[data-testid="messageEntryText"]': [body]})
    root = Node(children={'[data-testid="messageEntry"]': [entry]})
    result = await Browser()._message_snapshot(root)
    assert result == [message(message_id="1", direction="unknown")]


@pytest.mark.asyncio
async def test_preexisting_identical_outgoing_message_does_not_confirm_send():
    duplicate = message()
    browser = SendBrowser([duplicate], [duplicate])
    with pytest.raises(BrowserActionError) as exc:
        await browser.send_message("10-20", "hello")
    assert exc.value.reason == "send_unconfirmed"
    assert browser.send.clicked == 1


@pytest.mark.asyncio
async def test_new_identical_message_with_new_id_confirms_once():
    browser = SendBrowser([message()], [message(), message(message_id="new")])
    result = await browser.send_message("10-20", "hello")
    assert result["status"] == "confirmed"
    assert result["message_id"] == "new"
    assert browser.send.clicked == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("after,clears", [
    ([message(message_id="new", direction="incoming")], True),
    ([message(message_id="new", direction="unknown")], True),
    ([message(message_id="new", pending=True)], True),
    ([message(message_id="new")], False),
])
async def test_confirmation_requires_outgoing_settled_bubble_and_cleared_composer(after, clears):
    browser = SendBrowser([], after, clears=clears)
    with pytest.raises(BrowserActionError) as exc:
        await browser.send_message("10-20", "hello")
    assert exc.value.reason == "send_unconfirmed"


@pytest.mark.asyncio
async def test_redirected_conversation_never_confirms():
    browser = SendBrowser([], [message(message_id="new")], redirects="https://x.com/messages/10-30")
    with pytest.raises(BrowserActionError) as exc:
        await browser.send_message("10-20", "hello")
    assert exc.value.reason == "send_unconfirmed"


@pytest.mark.asyncio
async def test_disabled_send_never_clicks():
    browser = SendBrowser([], [], enabled=False)
    with pytest.raises(BrowserActionError) as exc:
        await browser.send_message("10-20", "hello")
    assert exc.value.reason == "send_disabled"
    assert browser.send.clicked == 0


@pytest.mark.asyncio
async def test_existing_human_draft_is_preserved():
    browser = SendBrowser([], [])
    browser.field.value = "human draft"
    with pytest.raises(BrowserActionError) as exc:
        await browser.send_message("10-20", "hello")
    assert exc.value.reason == "composer_not_empty"
    assert browser.field.value == "human draft"
    assert browser.send.clicked == 0


@pytest.mark.asyncio
async def test_send_playwright_call_log_does_not_expose_private_message():
    browser = SendBrowser([], [])
    browser.field.fill_error = True
    with pytest.raises(BrowserActionError) as exc:
        await browser.send_message("10-20", "private message")
    assert exc.value.reason == "outcome_unknown"
    assert "private message" not in str(exc.value)
    assert exc.value.__suppress_context__ is True
    assert browser.send.clicked == 0


@pytest.mark.asyncio
async def test_conversation_id_send_with_policy_requires_identified_header():
    browser = SendBrowser([], [message(message_id="new")])
    validated = []
    browser.recipient_validator = validated.append
    with pytest.raises(BrowserActionError) as exc:
        await browser.send_message("10-20", "hello")
    assert exc.value.reason == "recipient_unverified"
    assert validated == []
    assert browser.field.value == ""
    assert browser.send.clicked == 0


@pytest.mark.asyncio
async def test_suppressed_conversation_participant_cannot_bypass_policy():
    browser = SendBrowser([], [message(message_id="new")])
    browser.page.children['[data-testid="DMConversationHeader"]'] = [Node("@Alice")]
    def suppressed(handle):
        assert handle == "alice"
        raise ValueError("Private opted-out lead and contact information")
    browser.recipient_validator = suppressed
    with pytest.raises(BrowserActionError) as exc:
        await browser.send_message("10-20", "hello")
    assert exc.value.reason == "recipient_blocked"
    assert "Private" not in str(exc.value)
    assert exc.value.__suppress_context__ is True
    assert browser.send.clicked == 0


@pytest.mark.asyncio
async def test_every_group_participant_is_validated_before_send():
    browser = SendBrowser([], [message(message_id="new")])
    browser.page.children['[data-testid="DMConversationHeader"]'] = [Node("@Alice\n@Bob")]
    validated = []
    def validate(handle):
        validated.append((handle, browser.field.value, browser.send.clicked))
        if handle == "bob":
            return False
    browser.recipient_validator = validate
    with pytest.raises(BrowserActionError) as exc:
        await browser.send_message("10-20", "hello")
    assert exc.value.reason == "recipient_blocked"
    assert validated == [("alice", "hello", 0), ("bob", "hello", 0)]
    assert browser.send.clicked == 0


@pytest.mark.asyncio
async def test_identified_participant_profile_link_allows_policy_checked_send():
    browser = SendBrowser([], [message(message_id="new")])
    header = Node("Alice", children={"a[href]": [Node(attrs={"href": "https://x.com/Alice"})]})
    browser.page.children['[data-testid="DMConversationHeader"]'] = [header]
    validated = []
    browser.recipient_validator = validated.append
    result = await browser.send_message("10-20", "hello")
    assert result["success"] is True
    assert validated == ["alice", "alice"]
    assert browser.send.clicked == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("href", [
    "https://evil.example/alice", "https://x.com@evil.example/alice",
    "https://private@x.com/alice", "https://x.com:443/alice", "/home", "/alice?other=1",
])
async def test_untrusted_or_non_profile_header_link_never_identifies_participant(href):
    browser = SendBrowser([], [])
    header = Node("Alice", children={"a[href]": [Node(attrs={"href": href})]})
    browser.page.children['[data-testid="DMConversationHeader"]'] = [header]
    browser.recipient_validator = lambda handle: None
    with pytest.raises(BrowserActionError) as exc:
        await browser.send_message("10-20", "hello")
    assert exc.value.reason == "recipient_unverified"
    assert browser.send.clicked == 0


@pytest.mark.asyncio
async def test_collapsed_group_with_unidentified_members_fails_closed():
    browser = SendBrowser([], [])
    header = Node("@Alice\n3 participants", {"data-participant-count": "3"})
    browser.page.children['[data-testid="DMConversationHeader"]'] = [header]
    browser.recipient_validator = lambda handle: None
    with pytest.raises(BrowserActionError) as exc:
        await browser.send_message("10-20", "hello")
    assert exc.value.reason == "recipient_unverified"
    assert browser.send.clicked == 0


@pytest.mark.asyncio
async def test_handle_send_calls_policy_immediately_before_send():
    browser = SendBrowser([], [message(message_id="new")])
    browser.page.children['[data-testid="DMConversationHeader"]'] = [Node("@alice")]
    async def new_conversation(handle):
        assert handle == "alice"
        return browser.page
    browser._new_conversation = new_conversation
    validated = []
    browser.recipient_validator = lambda handle: validated.append((handle, browser.field.value, browser.send.clicked))
    result = await browser.send_message("@Alice", "hello")
    assert result["success"] is True
    assert validated == [("alice", "hello", 0), ("alice", "hello", 0)]


@pytest.mark.asyncio
async def test_redirect_between_composer_fill_and_send_never_clicks():
    browser = SendBrowser([], [message(message_id="new")])
    async def fill(value):
        browser.field.value = value
        browser.page.url = "https://x.com/messages/10-30"
    browser.field.fill = fill
    with pytest.raises(BrowserActionError) as exc:
        await browser.send_message("10-20", "hello")
    assert exc.value.reason == "conversation_mismatch"
    assert browser.send.clicked == 0


@pytest.mark.asyncio
async def test_redirect_during_policy_validation_never_clicks():
    browser = SendBrowser([], [message(message_id="new")])
    browser.page.children['[data-testid="DMConversationHeader"]'] = [Node("@alice")]
    def validate(handle):
        browser.page.url = "https://x.com/messages/10-30"
    browser.recipient_validator = validate
    with pytest.raises(BrowserActionError) as exc:
        await browser.send_message("10-20", "hello")
    assert exc.value.reason == "conversation_mismatch"
    assert browser.send.clicked == 0


@pytest.mark.asyncio
async def test_participants_change_during_policy_validation_never_clicks():
    browser = SendBrowser([], [message(message_id="new")])
    header = Node("@alice")
    browser.page.children['[data-testid="DMConversationHeader"]'] = [header]
    def validate(handle):
        header.text = "@bob"
    browser.recipient_validator = validate
    with pytest.raises(BrowserActionError) as exc:
        await browser.send_message("10-20", "hello")
    assert exc.value.reason == "recipient_mismatch"
    assert browser.send.clicked == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("policy_change", ["optout", "campaign_pause"])
async def test_policy_changes_during_final_header_inspection_prevent_click(policy_change):
    browser = SendBrowser([], [message(message_id="new")])
    browser.page.children['[data-testid="DMConversationHeader"]'] = [Node("@alice")]
    original = browser._participant_handles
    inspections = 0
    blocked = False
    async def inspect_header(scope, expected_handle=None):
        nonlocal inspections, blocked
        inspections += 1
        result = await original(scope, expected_handle)
        if inspections == 3:
            blocked = True
        return result
    def validate(handle):
        if blocked:
            raise ValueError(policy_change)
    browser._participant_handles = inspect_header
    browser.recipient_validator = validate
    with pytest.raises(BrowserActionError) as exc:
        await browser.send_message("10-20", "hello")
    assert exc.value.reason == "recipient_blocked"
    assert browser.send.clicked == 0


@pytest.mark.asyncio
async def test_exact_handle_does_not_accept_display_name_or_partial_handle():
    browser = Browser()
    wrong = Node("Alice", children={"text": [Node("@alice_other")]})
    assert await browser._exact_handle(wrong, "alice") is False
    correct = Node(children={"text": [Node("@alice")]})
    assert await browser._exact_handle(correct, "alice") is True
    external = Node(children={"a[href]": [Node(attrs={"href": "https://evil.example/alice"})]})
    assert await browser._exact_handle(external, "alice") is False


@pytest.mark.asyncio
async def test_wrong_header_recipient_aborts_before_composer():
    search = Node()
    row = Node(children={"text": [Node("@alice")]})
    next_button = Node("Next", {"role": "button"})
    dialog = Node(attrs={"role": "dialog"}, children={
        '[data-testid="searchPeople"]': [search], '[data-testid="UserCell"]': [row], "next": [next_button],
    })
    wrong_header = Node(children={"text": [Node("@bob")]})
    scope = Node(children={'[data-testid="DMConversationHeader"]': [wrong_header],
                           '[data-testid="dmComposerTextInput"]': [Node()]})
    browser = Browser(Page(children={
        '[data-testid="NewDM_Button"]': [Node()], "dialog": [dialog],
        '[data-testid="DMConversationView"]': [scope],
    }))
    async def open_inbox():
        return None
    browser._open_inbox = open_inbox
    with pytest.raises(BrowserActionError) as exc:
        await browser.send_message("@alice", "hello")
    assert exc.value.reason == "recipient_mismatch"


@pytest.mark.asyncio
async def test_pin_gate_and_login_challenge_block_reads_and_writes():
    pin = Node(attrs={"type": "password", "inputmode": "numeric"})
    browser = Browser(Page(children={'input[type="password"][inputmode="numeric"]': [pin]}))
    with pytest.raises(BrowserBlocked) as exc:
        await browser.get_inbox()
    assert exc.value.reason == "pin_required"
    sending = SendBrowser([], [])
    sending.blocked = "challenge"
    with pytest.raises(BrowserBlocked) as exc:
        await sending.send_message("10-20", "hello")
    assert exc.value.reason == "challenge"
    assert sending.send.clicked == 0


@pytest.mark.asyncio
async def test_unlock_success_requires_pin_gate_disappear_and_actual_inbox():
    pin = Node()
    inbox = Node("Your inbox is empty", visible=False)
    def unlock():
        pin.visible = False
        inbox.visible = True
    button = Node("Unlock", {"role": "button"}, on_click=unlock)
    browser = Browser(Page(children={
        'input[type="password"][inputmode="numeric"]': [pin], "button": [button],
        '[data-testid="DMInbox"]': [inbox],
    }))
    result = await browser.unlock_messages("1234")
    assert result == {"success": True, "action": "unlock_messages", "status": "confirmed", "source": "browser_dom"}
    assert "1234" not in repr(result)
    assert not hasattr(browser, "pin")


@pytest.mark.asyncio
async def test_unlock_failure_suppresses_playwright_secret_call_log_and_clears_field():
    pin = Node()
    pin.fill_error = True
    browser = Browser(Page(children={'input[type="password"][inputmode="numeric"]': [pin]}))
    with pytest.raises(BrowserActionError) as exc:
        await browser.unlock_messages("1234")
    assert exc.value.reason == "unlock_failed"
    assert "1234" not in str(exc.value)
    assert exc.value.__suppress_context__ is True


@pytest.mark.asyncio
async def test_wrong_pin_is_not_retried_and_field_is_cleared():
    pin = Node()
    button = Node("Unlock", {"role": "button"})
    browser = Browser(Page(children={
        'input[type="password"][inputmode="numeric"]': [pin], "button": [button],
    }))
    with pytest.raises(BrowserActionError) as exc:
        await browser.unlock_messages("1234")
    assert exc.value.reason == "unlock_failed"
    assert button.clicked == 1
    assert pin.value == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, -1, 201, True, "20"])
async def test_read_limits_validated_before_navigation(limit):
    browser = Browser()
    with pytest.raises(ValueError):
        await browser.get_inbox(limit)
    assert browser.navigated == []


@pytest.mark.parametrize("value,expected", [
    ("@ALICE", "@alice"), ("alice", "@alice"), ("10-20", "10-20"),
    ("https://x.com/ALICE", "@alice"), ("https://twitter.com/Alice/", "@alice"),
    ("https://x.com/i/chat/abc_DEF", "https://x.com/i/chat/abc_DEF"),
])
def test_recipient_normalization(value, expected):
    assert normalize_recipient(value) == expected


@pytest.mark.parametrize("value", [
    "https://evil.example/alice", "https://x.com@evil.example/alice",
    "https://private@x.com/alice", "https://x.com:443/alice",
    "https://x.com/home", "https://x.com/alice?other=1", "https://x.com/alice#message",
    "https://x.com/%61lice", "https://x.com/alice/status/123", "x-use",
])
def test_profile_recipient_rejects_untrusted_urls_routes_and_display_labels(value):
    with pytest.raises(ValueError):
        normalize_recipient(value)
