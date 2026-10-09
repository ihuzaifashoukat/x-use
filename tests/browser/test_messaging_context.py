"""Bounded inbox/context regressions using synthetic locator fixtures."""
import pytest

from xuse.browser.errors import BrowserActionError
from test_messaging import Browser, Node, Page


@pytest.mark.asyncio
async def test_receipt_does_not_prove_legacy_message_authorship():
    receipt = Node("Read", attrs={"aria-label": "Read"})
    entry = Node("Incoming?", attrs={"data-message-id": "one"},
                 children={'[aria-label="Read"]': [receipt]})
    scope = Node(children={'[data-testid="messageEntry"]': [entry]})
    assert (await Browser()._message_snapshot(scope))[0]["direction"] == "unknown"


@pytest.mark.asyncio
async def test_conflicting_direction_is_unknown():
    entry = Node("body", attrs={"data-message-id": "one", "data-direction": "outgoing", "data-is-outgoing": "false"})
    scope = Node(children={'[data-testid="messageEntry"]': [entry]})
    assert (await Browser()._message_snapshot(scope))[0]["direction"] == "unknown"


def inbox_browser():
    rows = [Node("Read", {"href": "/messages/10-20", "data-unread": "false"}),
            Node("Unknown", {"href": "/messages/10-30"}),
            Node("Unread", {"href": "/messages/10-40", "data-unread": "true"})]
    root = Node(children={'[data-testid="conversation"]': rows})
    return Browser(Page(children={'[data-testid="DMInbox"]': [root]})), rows


@pytest.mark.asyncio
async def test_unread_filter_and_priority_apply_before_response_limit():
    browser, rows = inbox_browser()
    result = await browser.get_inbox(1, inbox_filter="unread")
    assert [row["conversation_id"] for row in result["conversations"]] == ["10-40"]
    assert result["unread_unknown_count"] == 1 and result["partial"]
    prioritized = await browser.get_inbox(1, unread_first=True)
    assert prioritized["conversations"] == result["conversations"]
    assert all(row.clicked == 0 for row in rows)
    assert browser.navigated == []


@pytest.mark.asyncio
async def test_read_and_all_filters_preserve_unknown_state_and_dom_order():
    browser, _ = inbox_browser()
    assert [row["unread"] for row in (await browser.get_inbox())["conversations"]] == [False, None, True]
    read = await browser.get_inbox(inbox_filter="read")
    assert [row["unread"] for row in read["conversations"]] == [False]
    prioritized = await browser.get_inbox(unread_first=True)
    assert [row["unread"] for row in prioritized["conversations"]] == [True, False, None]


@pytest.mark.asyncio
async def test_filtered_empty_has_partial_unknown_coverage():
    browser, rows = inbox_browser()
    rows[-1].attrs.pop("data-unread")
    result = await browser.get_inbox(inbox_filter="unread")
    assert result["empty"] and result["unread_unknown_count"] == 2
    assert result["partial"] and result["empty_scope"] == "matching_visible_conversation_summaries"


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [{"inbox_filter": "Unread"}, {"inbox_filter": None},
                                    {"unread_first": 1}, {"limit": True}, {"limit": 0}])
async def test_invalid_inbox_options_do_not_open_inbox(kwargs):
    browser = Browser()
    with pytest.raises(ValueError):
        await browser.get_inbox(**kwargs)
    assert browser.navigated == []


@pytest.mark.asyncio
async def test_search_filters_before_limit_without_opening_chats():
    browser, rows = inbox_browser()
    for row in rows:
        row.text = "Matching preview"
    result = await browser.search_conversations("matching", 1, inbox_filter="unread", unread_first=True)
    assert result["conversations"][0]["conversation_id"] == "10-40"
    assert result["unread_unknown_count"] == 1
    assert not any(row.clicked for row in rows)


@pytest.mark.asyncio
async def test_scan_limit_does_not_claim_unread_absence_beyond_window():
    rows = [Node(str(i), {"href": f"/messages/10-{i}", "data-unread": "false"}) for i in range(200)]
    rows.append(Node("Unread beyond bound", {"href": "/messages/10-999", "data-unread": "true"}))
    root = Node(children={'[data-testid="conversation"]': rows})
    browser = Browser(Page(children={'[data-testid="DMInbox"]': [root]}))
    result = await browser.get_inbox(inbox_filter="unread")
    assert result["empty"] and result["scan_truncated"] and result["partial"]
    assert result["scanned_count"] == 200 and result["truncated"]


def context_browser(ids=("a", "b", "c", "d"), header="@Alice"):
    entries = [Node(f"Message {value}", {"data-message-id": value, "data-direction": "incoming"}) for value in ids]
    root = Node(children={'[data-testid="messageEntry"]': entries,
                          '[data-testid="DMConversationHeader"]': [Node(header)]})
    browser = Browser(Page(children={'[data-testid="DMConversationView"]': [root]}))
    browser.page.url = "https://x.com/messages/10-20"
    return browser, entries


@pytest.mark.asyncio
async def test_context_cursor_retains_identity_participants_and_dom_order():
    browser, _ = context_browser()
    first = await browser.get_conversation("10-20", 2)
    assert [row["message_id"] for row in first["messages"]] == ["c", "d"]
    assert first["participants"] == ["alice"] and first["participants_verified"]
    assert first["next_before_message_id"] == "c" and first["truncated"]
    second = await browser.get_conversation("10-20", 2, before_message_id="c")
    assert [row["message_id"] for row in second["messages"]] == ["a", "b"]
    assert second["next_before_message_id"] is None
    assert second["partial"] and second["history_coverage"] == "visible_only"
    assert browser.navigated == []


@pytest.mark.asyncio
async def test_duplicate_context_ids_do_not_produce_ambiguous_cursor():
    browser, _ = context_browser(("a", "b", "b", "d"))
    result = await browser.get_conversation("10-20", 2)
    assert result["messages"][0]["message_id"] is None
    assert result["next_before_message_id"] is None and result["missing_message_id_count"] == 1
    with pytest.raises(ValueError, match="absent or ambiguous"):
        await browser.get_conversation("10-20", before_message_id="b")


@pytest.mark.asyncio
async def test_missing_cursor_never_falls_back_to_another_window():
    browser, _ = context_browser()
    with pytest.raises(ValueError, match="absent or ambiguous"):
        await browser.get_conversation("10-20", before_message_id="missing")


@pytest.mark.asyncio
async def test_collapsed_header_is_explicitly_unverified_for_read_context():
    browser, _ = context_browser(header="@Alice\n3 participants")
    result = await browser.get_conversation("10-20")
    assert result["participants"] == [] and not result["participants_verified"]


@pytest.mark.asyncio
async def test_context_scan_is_bounded_and_never_claims_complete_history():
    browser, _ = context_browser(tuple(str(i) for i in range(210)))
    result = await browser.get_conversation("10-20", 2)
    assert [row["message_id"] for row in result["messages"]] == ["208", "209"]
    assert result["snapshot_count"] == 200 and result["scan_truncated"] and result["partial"]


@pytest.mark.asyncio
async def test_duplicate_inbox_conflicts_remain_unknown():
    browser, rows = inbox_browser()
    root = browser.page.children['[data-testid="DMInbox"]'][0]
    root.children['[data-testid="conversation"]'].append(Node("Conflicting", {"href": "/messages/10-40", "data-unread": "false"}))
    result = await browser.get_inbox(inbox_filter="unread")
    assert result["empty"] and result["unread_unknown_count"] == 2
