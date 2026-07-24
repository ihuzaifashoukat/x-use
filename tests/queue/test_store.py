"""QueueStore: persistence, crash recovery, due/cap queries. tmp files only."""
from datetime import datetime, timedelta, timezone

import pytest

from xuse.queue import QueueStore

NOW = datetime(2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc)


def make_item(store, account="acc1", action="like", dedup_key=None, not_before=None):
    return store.add(account=account, action=action, payload={"tweet_id": "1"},
                     dedup_key=dedup_key or f"{action}_{account}_1", not_before=not_before)


def test_add_assigns_ids_and_defaults(tmp_path):
    store = QueueStore(tmp_path / "q.jsonl")
    item = make_item(store)
    assert item.queue_id and item.status == "pending" and item.attempts == 0
    assert item.created_at and item.completed_at is None


def test_persistence_round_trip(tmp_path):
    path = tmp_path / "q.jsonl"
    store = QueueStore(path)
    item = make_item(store)
    store.set_status(item.queue_id, "done", completed_at=NOW.isoformat())
    assert QueueStore(path).get(item.queue_id).status == "done"


def test_last_write_wins(tmp_path):
    path = tmp_path / "q.jsonl"
    store = QueueStore(path)
    item = make_item(store)
    store.set_status(item.queue_id, "processing")
    store.set_status(item.queue_id, "done", completed_at=NOW.isoformat())
    reloaded = QueueStore(path)
    assert reloaded.get(item.queue_id).status == "done"
    assert len([i for i in reloaded.list() if i.queue_id == item.queue_id]) == 1


def test_corrupt_lines_are_skipped(tmp_path):
    path = tmp_path / "q.jsonl"
    store = QueueStore(path)
    item = make_item(store)
    with path.open("a", encoding="utf-8") as f:
        f.write("this is not json\n")
    reloaded = QueueStore(path)
    assert len(reloaded) == 1 and reloaded.get(item.queue_id).queue_id == item.queue_id


def test_processing_resets_to_pending_on_load(tmp_path):
    path = tmp_path / "q.jsonl"
    store = QueueStore(path)
    item = make_item(store)
    store.set_status(item.queue_id, "processing")
    assert QueueStore(path).get(item.queue_id).status == "pending"


def test_get_unknown_raises_keyerror(tmp_path):
    store = QueueStore(tmp_path / "q.jsonl")
    with pytest.raises(KeyError):
        store.get("nope")


def test_record_attempt_increments_and_stores_error(tmp_path):
    store = QueueStore(tmp_path / "q.jsonl")
    item = make_item(store)
    updated = store.record_attempt(item.queue_id, "boom")
    assert updated.attempts == 1 and updated.last_error == "boom"


def test_due_items_fifo_and_not_before(tmp_path):
    store = QueueStore(tmp_path / "q.jsonl")
    future = (NOW + timedelta(hours=1)).isoformat()
    past = (NOW - timedelta(hours=1)).isoformat()
    first = make_item(store, dedup_key="like_acc1_a")
    second = make_item(store, dedup_key="like_acc1_b", not_before=past)
    make_item(store, dedup_key="like_acc1_c", not_before=future)
    due = store.due_items("acc1", NOW)
    assert [i.queue_id for i in due] == [first.queue_id, second.queue_id]


def test_due_items_accepts_z_suffix(tmp_path):
    store = QueueStore(tmp_path / "q.jsonl")
    item = make_item(store, not_before="2026-07-24T11:00:00Z")
    assert [i.queue_id for i in store.due_items("acc1", NOW)] == [item.queue_id]


def test_due_accounts_lists_accounts_with_due_items(tmp_path):
    store = QueueStore(tmp_path / "q.jsonl")
    make_item(store, account="a", dedup_key="like_a_1")
    make_item(store, account="b", dedup_key="like_b_1",
              not_before=(NOW + timedelta(hours=2)).isoformat())
    make_item(store, account="c", dedup_key="like_c_1")
    assert store.due_accounts(NOW) == ["a", "c"]


def test_count_done_today_only_counts_today(tmp_path):
    store = QueueStore(tmp_path / "q.jsonl")
    today = make_item(store, dedup_key="like_acc1_t")
    yesterday = make_item(store, dedup_key="like_acc1_y")
    other_action = make_item(store, action="reply", dedup_key="reply_acc1_1")
    store.set_status(today.queue_id, "done", completed_at=NOW.isoformat())
    store.set_status(yesterday.queue_id, "done",
                     completed_at=(NOW - timedelta(days=1)).isoformat())
    store.set_status(other_action.queue_id, "done", completed_at=NOW.isoformat())
    assert store.count_done_today("acc1", "like", NOW) == 1


def test_find_pending_by_dedup_key(tmp_path):
    store = QueueStore(tmp_path / "q.jsonl")
    item = make_item(store, dedup_key="like_acc1_1")
    assert store.find_pending_by_dedup_key("like_acc1_1").queue_id == item.queue_id
    store.set_status(item.queue_id, "done", completed_at=NOW.isoformat())
    assert store.find_pending_by_dedup_key("like_acc1_1") is None


def test_counts_by_status_and_account(tmp_path):
    store = QueueStore(tmp_path / "q.jsonl")
    a = make_item(store, account="a", dedup_key="like_a_1")
    make_item(store, account="b", dedup_key="like_b_1")
    store.set_status(a.queue_id, "cancelled")
    assert store.counts() == {"cancelled": 1, "pending": 1}
    assert store.counts(account="b") == {"pending": 1}
