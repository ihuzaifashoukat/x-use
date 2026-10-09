"""Bounded membership output and constant SQL query count for summaries."""
from contextlib import contextmanager

import pytest

from xuse.mcp.drafts import DraftStore
from xuse.outreach import OutreachStore


def populate(store, count=125):
    campaign = store.create_campaign('a', 'Test', 'Hi {handle}')
    ids = [store.upsert_lead('a', f'user{i}').lead_id for i in range(count)]
    for index in range(0, count, 100):
        store.add_campaign_leads('a', campaign.campaign_id, ids[index:index+100])
    return campaign.campaign_id, ids


def test_membership_pages_cover_all_records_without_duplicates(tmp_path):
    store = OutreachStore(tmp_path / 'outreach.sqlite3')
    cid, ids = populate(store)
    seen, offset = [], 0
    while True:
        page = store.get_campaign('a', cid, limit=30, offset=offset)
        assert len(page.members) <= 30
        assert page.members_total == len(ids)
        seen.extend(m.lead_id for m in page.members)
        if page.members_next_offset is None:
            break
        offset = page.members_next_offset
    assert len(seen) == len(set(seen)) == len(ids)
    assert set(seen) == set(ids)
    assert store.get_campaign('a', cid, offset=1000).members_next_offset is None


def test_summary_uses_two_selects_preserving_suppression_and_other_campaign_rules(tmp_path, monkeypatch):
    store = OutreachStore(tmp_path / 'outreach.sqlite3')
    cid, ids = populate(store)
    other = store.create_campaign('a', 'Other', 'Hi {handle}')
    store.add_campaign_leads('a', other.campaign_id, [ids[1]])
    store.prepare_campaign_messages('a', other.campaign_id, DraftStore())
    store.opt_out_lead('a', ids[0])
    store.update_lead_status('a', ids[2], 'closed')
    statements = []
    original = store._connection
    @contextmanager
    def traced(**kwargs):
        with original(**kwargs) as conn:
            conn.set_trace_callback(statements.append)
            yield conn
    monkeypatch.setattr(store, '_connection', traced)
    result = store.get_campaign_summary('a', cid)
    assert result['total_leads'] == 125
    assert result['suppressed_leads'] == 1
    assert result['eligible_to_prepare'] == 122
    assert result['counts']['unprepared'] == 125
    assert len([q for q in statements if q.lstrip().upper().startswith('SELECT')]) == 2


def test_delivery_lookup_can_find_member_beyond_default_first_page(tmp_path):
    store = OutreachStore(tmp_path / 'outreach.sqlite3')
    cid, ids = populate(store, 60)
    last = store.get_campaign('a', cid, limit=100).members[-1].lead_id
    with store._connection(write=True) as conn:
        conn.execute("UPDATE campaign_leads SET status='pending',draft_id='test' WHERE lead_id=?", (last,))
    assert store.record_delivery('a', last, cid, 'rejected', expected_draft_id='test').lead_id == last
