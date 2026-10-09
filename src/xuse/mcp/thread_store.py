"""Durable, immutable thread payloads and at-most-once segment progress.

Transactions never span a browser call. An OS-held run lock spans execution;
creating another store or reading progress does not reset a live claim.
"""
import hashlib
import json
import os
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path

from xuse.core.local_state import private_sqlite_file
from .executor import ToolError


def payload_digest(account, payload):
    encoded = json.dumps([account, payload], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class ThreadStore:
    def __init__(self, path):
        self.path = Path(path)
        private_sqlite_file(self.path)
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS thread_runs (
                    run_id TEXT PRIMARY KEY, draft_id TEXT UNIQUE NOT NULL,
                    account TEXT NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL,
                    authorized INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL,
                    error TEXT);
                CREATE TABLE IF NOT EXISTS thread_segments (
                    run_id TEXT NOT NULL, position INTEGER NOT NULL,
                    state TEXT NOT NULL, parent_url TEXT, published_url TEXT,
                    action_id TEXT, PRIMARY KEY(run_id, position));
            """)

    @contextmanager
    def _db(self):
        private_sqlite_file(self.path)
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @contextmanager
    def execution_lock(self, run_id):
        from xuse.browser.sessions import AccountOwnerLock
        from xuse.browser.errors import SessionError
        key = hashlib.sha256((os.path.normcase(str(self.path.resolve())) + "|" + run_id).encode()).hexdigest()
        owner = AccountOwnerLock(key, Path(tempfile.gettempdir()) / "xuse-thread-locks")
        try:
            owner.acquire()
        except SessionError:
            error = ToolError("Another caller is executing this thread run.")
            error.reason = "thread_busy"
            raise error from None
        try:
            # With exclusive ownership, an old in-flight segment is a crash,
            # never an invitation to submit it again.
            with self._db() as db:
                changed = db.execute("UPDATE thread_segments SET state='uncertain' WHERE run_id=? AND state='in_flight'", (run_id,)).rowcount
                if changed:
                    db.execute("UPDATE thread_runs SET state='uncertain',error=? WHERE run_id=?", (json.dumps({"reason": "interrupted_write", "message": "A segment was interrupted; inspect X and the safety ledger. It will not be resent."}), run_id))
            yield
        finally:
            owner.release()

    def create(self, draft):
        payload = json.loads(json.dumps(draft.payload))
        run_id = payload["run_id"]
        with self._db() as db:
            db.execute("INSERT INTO thread_runs(run_id,draft_id,account,payload,digest,state,error) VALUES(?,?,?,?,?,'blocked',?)", (
                run_id, draft.draft_id, draft.account, json.dumps(payload), payload_digest(draft.account, payload),
                json.dumps({"reason": "approval_required", "message": "Review and approve the thread draft before publishing."})))
            db.executemany("INSERT INTO thread_segments(run_id,position,state) VALUES(?,?,'pending')", [(run_id, i) for i in range(len(payload["posts"]))])
        return self.get(run_id)

    def get(self, run_id):
        with self._db() as db:
            row = db.execute("SELECT * FROM thread_runs WHERE run_id=?", (run_id,)).fetchone()
            if not row:
                raise ToolError("Unknown thread run_id.")
            result = dict(row)
            result["payload"] = json.loads(result["payload"])
            if payload_digest(result["account"], result["payload"]) != result.pop("digest"):
                raise ToolError("Thread payload integrity check failed; no segment can be published.")
            result["authorized"] = bool(result["authorized"])
            result["error"] = json.loads(result["error"]) if result["error"] else None
            result["segments"] = [dict(r) for r in db.execute("SELECT position,state,parent_url,published_url,action_id FROM thread_segments WHERE run_id=? ORDER BY position", (run_id,))]
            if (result["payload"].get("run_id") != run_id
                    or [s["position"] for s in result["segments"]] != list(range(len(result["payload"]["posts"])))
                    or any(s["state"] == "confirmed" and not s["published_url"] for s in result["segments"])
                    or result["state"] == "complete" and any(s["state"] != "confirmed" for s in result["segments"])):
                raise ToolError("Thread progress integrity check failed; inspect local state before continuing.")
        return result

    def validate_draft(self, run_id, draft):
        run = self.get(run_id)
        if (draft.action != "publish_thread" or draft.draft_id != run["draft_id"]
                or payload_digest(draft.account, draft.payload) != payload_digest(run["account"], run["payload"])):
            raise ToolError("Thread draft payload changed after preparation; create and review a new draft.")
        return run

    def authorize(self, run_id):
        with self._db() as db:
            changed = db.execute("UPDATE thread_runs SET authorized=1 WHERE run_id=? AND state!='cancelled'", (run_id,)).rowcount
            if not changed:
                raise ToolError("A cancelled thread cannot be authorized again.")

    def start(self, run_id, position, parent_url):
        with self._db() as db:
            row = db.execute("SELECT authorized,state FROM thread_runs WHERE run_id=?", (run_id,)).fetchone()
            if not row or not row["authorized"] or row["state"] in ("complete", "uncertain", "cancelled"):
                raise ToolError("Thread run cannot claim a segment.")
            if db.execute("SELECT count(*) FROM thread_segments WHERE run_id=? AND position<? AND state!='confirmed'", (run_id, position)).fetchone()[0]:
                raise ToolError("Previous thread segment is not confirmed.")
            expected_parent = db.execute("SELECT published_url FROM thread_segments WHERE run_id=? AND position=?", (run_id, position - 1)).fetchone() if position else None
            if expected_parent and parent_url != expected_parent[0]:
                raise ToolError("Thread parent does not match the preceding confirmed post.")
            if not position:
                payload = json.loads(db.execute("SELECT payload FROM thread_runs WHERE run_id=?", (run_id,)).fetchone()[0])
                if parent_url != payload["reply_to"]:
                    raise ToolError("Thread parent does not match the reviewed initial reply target.")
            changed = db.execute("UPDATE thread_segments SET state='in_flight',parent_url=? WHERE run_id=? AND position=? AND state='pending'", (parent_url, run_id, position)).rowcount
            if changed != 1:
                raise ToolError("Thread segment is already submitted or uncertain.")
            db.execute("UPDATE thread_runs SET state='uncertain',error=? WHERE run_id=?", (json.dumps({"reason": "write_in_flight", "message": "A segment is being submitted; it cannot be retried."}), run_id))

    def finish(self, run_id, position, *, state, published_url=None, action_id=None, error=None):
        if state not in ("confirmed", "pending", "uncertain"):
            raise ValueError("Invalid segment outcome.")
        if state == "confirmed" and not published_url:
            raise ValueError("Confirmed thread segments require a new post URL.")
        with self._db() as db:
            changed = db.execute("UPDATE thread_segments SET state=?,published_url=?,action_id=? WHERE run_id=? AND position=? AND state='in_flight'", (state, published_url, action_id, run_id, position)).rowcount
            if changed != 1:
                raise ToolError("Thread segment claim was lost; inspect its outcome.")
            remaining = db.execute("SELECT count(*) FROM thread_segments WHERE run_id=? AND state!='confirmed'", (run_id,)).fetchone()[0]
            run_state = "uncertain" if state == "uncertain" else "complete" if not remaining else "blocked"
            db.execute("UPDATE thread_runs SET state=?,error=? WHERE run_id=?", (run_state, json.dumps(error) if error else None, run_id))

    def block(self, run_id, error):
        with self._db() as db:
            db.execute("UPDATE thread_runs SET state='blocked',error=? WHERE run_id=? AND state NOT IN ('complete','uncertain','cancelled')", (json.dumps(error), run_id))

    def cancel(self, run_id):
        """Revoke future submission without deleting confirmed/uncertain evidence.

        Call under execution_lock. Complete runs are idempotent no-ops.
        """
        with self._db() as db:
            row = db.execute("SELECT state,error FROM thread_runs WHERE run_id=?", (run_id,)).fetchone()
            if not row:
                raise ToolError("Unknown thread run_id.")
            if row["state"] not in ("complete", "cancelled"):
                error = {"reason": "cancelled", "message": "Remaining segments were cancelled locally; published posts and uncertain evidence are retained."}
                if row["error"]:
                    error["previous_error"] = json.loads(row["error"])
                db.execute("UPDATE thread_runs SET authorized=0,state='cancelled',error=? WHERE run_id=?", (json.dumps(error), run_id))
        return self.get(run_id)
