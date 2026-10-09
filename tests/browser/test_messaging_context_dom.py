"""Inbox filters/context windows preserve the observed DOM without writes."""
import pytest

from test_messaging_dom import app, native_browser, conversation, entry  # noqa: F401


pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest.mark.parametrize("inbox_filter,unread_first,ids", [
    ("all", False, ["10-20", "10-30", "10-40"]),
    ("all", True, ["10-40", "10-20", "10-30"]),
    ("read", False, ["10-20"]),
    ("unread", False, ["10-40"]),
])
async def test_native_filter_and_priority_do_not_open_or_mark_conversations(app, inbox_filter, unread_first, ids):
    app.document("/messages", '''<section data-testid="DMInbox">
      <a data-testid="conversation" href="/messages/10-20" data-unread="false">Alice hello</a>
      <a data-testid="conversation" href="/messages/10-30">Bob hello</a>
      <a data-testid="conversation" href="/messages/10-40" data-unread="true">Carol hello</a>
      </section><script>document.documentElement.dataset.rowClicks='0';
      document.querySelectorAll('a').forEach(a=>a.onclick=e=>{
        e.preventDefault();a.dataset.unread='false';document.documentElement.dataset.rowClicks='1';});</script>''')
    await app.page.goto("https://x.com/messages")
    before = await app.page.locator('[data-testid="DMInbox"]').inner_html()
    result = await app.adapter.get_inbox(3, inbox_filter=inbox_filter, unread_first=unread_first)
    assert [row["conversation_id"] for row in result["conversations"]] == ids
    assert result["unread_unknown_count"] == 1 and result["partial"]
    limited = await app.adapter.search_conversations("hello", 1, inbox_filter="unread")
    assert limited["conversations"][0]["conversation_id"] == "10-40"
    assert await app.page.locator('[data-testid="DMInbox"]').inner_html() == before
    assert await app.page.locator("html").get_attribute("data-row-clicks") == "0"
    assert app.page.url == "https://x.com/messages"
    assert [r[1] for r in app.requests if r[2] == "document"] == ["/messages"]


async def test_native_visible_window_pages_preserve_ids_context_and_same_chat(app):
    existing = ''.join(entry(str(i), str(i), direction="incoming") for i in range(4))
    app.document("/messages/10-20", conversation(existing=existing))
    await app.page.goto("https://x.com/messages/10-20")
    first = await app.adapter.get_conversation("10-20", 2)
    assert [row["message_id"] for row in first["messages"]] == ["2", "3"]
    second = await app.adapter.get_conversation("10-20", 2, before_message_id=first["next_before_message_id"])
    assert [row["message_id"] for row in second["messages"]] == ["0", "1"]
    assert first["participants"] == second["participants"] == ["alice"]
    assert first["truncated"] and not second["truncated"] and second["partial"]
    assert second["history_coverage"] == "visible_only"
    assert len([r for r in app.requests if r[2] == "document"]) == 1
    assert await app.clicks() == 0


async def test_native_receipts_conflicts_and_duplicate_ids_remain_unknown(app):
    existing = entry("Received", "duplicate", direction=None).replace('</div>', '<span aria-label="Read">Read</span></div>')
    existing += entry("Contradiction", "duplicate").replace('data-direction="outgoing"', 'data-direction="outgoing" data-is-outgoing="false"')
    app.document("/messages/10-20", conversation(existing=existing))
    await app.page.goto("https://x.com/messages/10-20")
    result = await app.adapter.get_conversation("10-20")
    assert [row["direction"] for row in result["messages"]] == ["unknown", "unknown"]
    assert [row["message_id"] for row in result["messages"]] == [None, None]
    assert result["missing_message_id_count"] == result["unknown_direction_count"] == 2


async def test_native_inbox_bound_and_conflicting_duplicate_never_claim_complete_unread_coverage(app):
    rows = ''.join(f'<a data-testid="conversation" href="/messages/10-{i}" data-unread="false">Read</a>' for i in range(200))
    rows += '<a data-testid="conversation" href="/messages/10-999" data-unread="true">Unread outside bound</a>'
    app.document("/messages", '<section data-testid="DMInbox">' + rows + '</section>')
    await app.page.goto("https://x.com/messages")
    result = await app.adapter.get_inbox(inbox_filter="unread")
    assert result["empty"] and result["scan_truncated"] and result["partial"]
    assert result["scanned_count"] == 200
    await app.page.locator('[data-testid="DMInbox"]').evaluate('root => root.innerHTML = \'<a data-testid="conversation" href="/messages/10-20" data-unread="true">Unread</a><a data-testid="conversation" href="/messages/10-20" data-unread="false">Read conflict</a>\'')
    result = await app.adapter.get_inbox(inbox_filter="unread")
    assert result["empty"] and result["unread_unknown_count"] == 1


async def test_native_message_window_is_consistent_while_virtualized_rows_change(app):
    existing = ''.join(entry(str(i), str(i), direction="incoming") for i in range(5))
    script = '''<script>let epoch=0;setInterval(()=>{
      epoch++;const rows=[...document.querySelectorAll('[data-testid="messageEntry"]')];
      rows.forEach((row,i)=>{const id=epoch+'-'+i;row.dataset.messageId=id;
        row.querySelector('[data-testid="messageEntryText"]').textContent=id;});},1);</script>'''
    app.document("/messages/10-20", conversation(existing=existing) + script)
    await app.page.goto("https://x.com/messages/10-20")
    result = await app.adapter.get_conversation("10-20")
    assert all(row["message_id"] == row["text"] for row in result["messages"])
    assert len({row["message_id"].split('-')[0] for row in result["messages"]}) == 1


async def test_native_message_scan_ignores_malformed_rows_outside_tail_window(app):
    malformed = '<div data-dm-item-key="11111111-1111-4111-8111-111111111111"><div data-dm-message-row="11111111-1111-4111-8111-111111111111" data-testid="message-11111111-1111-4111-8111-111111111111">Missing body</div></div>'
    existing = malformed + ''.join(entry(str(i), str(i), direction="incoming") for i in range(200))
    app.document("/messages/10-20", conversation(existing=existing))
    await app.page.goto("https://x.com/messages/10-20")
    result = await app.adapter.get_conversation("10-20", 2)
    assert [row["message_id"] for row in result["messages"]] == ["198", "199"]
    assert result["snapshot_count"] == 200 and result["scan_truncated"]
