"""Durable thread claims across callers and restarts; no network or config."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading

import pytest

from xuse.mcp.drafts import DraftStore
from xuse.mcp.executor import ToolError
from xuse.mcp.thread_store import ThreadStore


@pytest.fixture
def prepared(tmp_path):
    store = ThreadStore(tmp_path / "threads.sqlite3")
    draft = DraftStore().create("test", "publish_thread", {
        "run_id": "run-1", "reply_to": None,
        "posts": [{"text": "one", "media": []}, {"text": "two", "media": []}]}, "review")
    store.create(draft)
    store.authorize("run-1")
    return store, draft


def test_claim_survives_restart_and_is_never_resent(prepared):
    store, _ = prepared
    with store.execution_lock("run-1"):
        store.start("run-1", 0, None)
    restored = ThreadStore(store.path)
    with restored.execution_lock("run-1"):
        run = restored.get("run-1")
        assert run["state"] == "uncertain"
        assert run["segments"][0]["state"] == "uncertain"
        with pytest.raises(ToolError):
            restored.start("run-1", 0, None)


def test_new_store_read_does_not_reset_active_claim(prepared):
    store, _ = prepared
    with store.execution_lock("run-1"):
        store.start("run-1", 0, None)
        other = ThreadStore(store.path)
        assert other.get("run-1")["segments"][0]["state"] == "in_flight"
        with pytest.raises(ToolError, match="Another caller"):
            with other.execution_lock("run-1"):
                pytest.fail("Must not acquire live execution ownership")
        store.finish("run-1", 0, state="confirmed", published_url="https://x.com/test/status/1")
    assert other.get("run-1")["segments"][0]["published_url"].endswith("/1")


def test_payload_copies_and_draft_tampering_fail_closed(prepared):
    store, draft = prepared
    snapshot = store.get("run-1")
    snapshot["payload"]["posts"][0]["text"] = "tampered"
    assert store.get("run-1")["payload"]["posts"][0]["text"] == "one"
    draft.payload["posts"][0]["text"] = "tampered"
    with pytest.raises(ToolError, match="changed"):
        store.validate_draft("run-1", draft)
    with store._db() as db:
        db.execute("UPDATE thread_runs SET payload=? WHERE run_id='run-1'", (json.dumps(snapshot["payload"]),))
    with pytest.raises(ToolError, match="integrity"):
        store.get("run-1")


def test_atomic_claim_only_one_concurrent_caller(prepared):
    store, _ = prepared
    barrier = threading.Barrier(4)

    def claim(_):
        peer = ThreadStore(store.path)
        barrier.wait()
        try:
            peer.start("run-1", 0, None)
            return True
        except ToolError:
            return False

    with ThreadPoolExecutor(max_workers=4) as workers:
        assert sum(workers.map(claim, range(4))) == 1


def test_parent_must_be_preceding_confirmed_url(prepared):
    store, _ = prepared
    with pytest.raises(ToolError, match="Previous"):
        store.start("run-1", 1, "https://x.com/test/status/4")
    with pytest.raises(ToolError, match="initial"):
        store.start("run-1", 0, "https://x.com/test/status/4")
    store.start("run-1", 0, None)
    store.finish("run-1", 0, state="confirmed", published_url="https://x.com/test/status/1")
    with pytest.raises(ToolError, match="preceding"):
        store.start("run-1", 1, "https://x.com/test/status/4")
    store.start("run-1", 1, "https://x.com/test/status/1")
    store.finish("run-1", 1, state="confirmed", published_url="https://x.com/test/status/2")
    assert ThreadStore(store.path).get("run-1")["state"] == "complete"


def test_missing_segment_fails_closed(prepared):
    store, _ = prepared
    with store._db() as db:
        db.execute("DELETE FROM thread_segments WHERE run_id='run-1' AND position=1")
    with pytest.raises(ToolError, match="progress integrity"):
        store.get("run-1")
