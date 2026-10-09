"""Durable action budgets and an at-most-once ledger shared by MCP clients.

Only hashes and action metadata are stored; credentials and message bodies
never enter the ledger. An interrupted write stays uncertain until the
operator reconciles its outcome. These are local limits, not X guarantees.
"""
import math
import hashlib
import os
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from .executor import ToolError
from xuse.core.local_state import private_sqlite_file


class PolicyError(ToolError):
    def __init__(self, message, *, reason="policy", retry_after_seconds=None, action_id=None):
        super().__init__(message)
        self.reason = reason
        self.retry_after_seconds = retry_after_seconds
        self.action_id = action_id


class SafetyStore:
    # Inbox encryption has no bearing on these public/owner-visible reads.
    # Unknown future operations remain denied until explicitly reviewed here.
    PIN_SAFE_READS = frozenset({"get_profile", "get_profile_context", "get_profile_posts",
        "get_profile_connections", "get_tweet", "get_thread", "search_tweets", "search_profile",
        "get_home_feed", "get_notifications", "get_account_analytics"})
    DEFAULT_CAPS = {"read": 300, "post": 5, "reply": 15, "like": 30,
                    "retweet": 10, "message": 10, "follow": 10, "unlock": 5}

    def __init__(self, path, settings=None, *, clock=time.time):
        self.path = Path(path)
        cfg = settings or {}
        if not isinstance(cfg, dict):
            raise ValueError("mcp.safety must be an object.")
        self.caps = dict(self.DEFAULT_CAPS)
        for kind, value in cfg.get("daily_caps", {}).items():
            if kind not in self.caps or isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("Safety daily caps must be nonnegative integers for known actions.")
            self.caps[kind] = value
        self.read_interval = self._interval(cfg.get("read_interval_seconds", 2))
        self.write_interval = self._interval(cfg.get("write_interval_seconds", 90))
        self.per_minute = cfg.get("max_actions_per_minute", 6)
        if isinstance(self.per_minute, bool) or not isinstance(self.per_minute, int) or self.per_minute < 1:
            raise ValueError("max_actions_per_minute must be a positive integer.")
        self.clock = clock
        self._ready = False

    @staticmethod
    def _interval(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError("Safety intervals must be finite nonnegative numbers.")
        return float(value)

    @contextmanager
    def operation_lock(self, account):
        """Exclude reconciliation while an action is still running locally.

        The lock also spans distinct MCP processes sharing the policy database.
        It contains no credentials and is released by the OS after a crash.
        """
        from xuse.browser.sessions import AccountOwnerLock
        from xuse.browser.errors import SessionError
        key = hashlib.sha256((os.path.normcase(str(self.path.resolve())) + "|" + account).encode()).hexdigest()
        lock = AccountOwnerLock(key, Path(tempfile.gettempdir()) / "xuse-action-locks")
        try:
            lock.acquire()
        except SessionError:
            raise PolicyError("Another operation is still active for this account. Retry after it finishes.", reason="account_busy") from None
        try:
            yield
        finally:
            lock.release()

    @contextmanager
    def _db(self):
        private_sqlite_file(self.path)
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            if not self._ready:
                connection.executescript("""
                    CREATE TABLE IF NOT EXISTS actions (
                        action_id TEXT PRIMARY KEY, account TEXT NOT NULL,
                        kind TEXT NOT NULL, action_key TEXT, started REAL NOT NULL,
                        status TEXT NOT NULL, finished REAL);
                    CREATE INDEX IF NOT EXISTS action_budget ON actions(account, started);
                    CREATE INDEX IF NOT EXISTS action_dedup ON actions(account, action_key);
                    CREATE TABLE IF NOT EXISTS pauses (
                        account TEXT PRIMARY KEY, reason TEXT NOT NULL, paused REAL NOT NULL);
                    CREATE TABLE IF NOT EXISTS pause_versions (
                        account TEXT PRIMARY KEY, token TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS action_refs (
                        action_id TEXT PRIMARY KEY, account TEXT NOT NULL,
                        draft_id TEXT, campaign_id TEXT, lead_id TEXT);
                """)
                self._ready = True
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def reserve(self, account, kind, action_key=None, context=None, *, recovery=False, read_operation=None):
        if kind not in self.caps:
            raise PolicyError("Unknown action budget.")
        if recovery and kind != "read":
            raise PolicyError("Recovery probes must be read-only.")
        now = self.clock()
        day_start = now - now % 86400  # UTC; survives local DST/time-zone changes.
        with self._db() as db:
            pause = db.execute("SELECT reason FROM pauses WHERE account=?", (account,)).fetchone()
            pin_read = kind == "read" and read_operation in self.PIN_SAFE_READS
            if (pause and not recovery
                    and not (pause["reason"] == "pin_required" and (kind == "unlock" or pin_read))):
                raise PolicyError("Account actions are paused. Resolve the browser challenge, then use resume_account_actions.", reason=pause["reason"])
            if action_key:
                old = db.execute("SELECT action_id,status FROM actions WHERE account=? AND action_key=? AND status IN ('started','succeeded','uncertain') ORDER BY started DESC LIMIT 1", (account, action_key)).fetchone()
                if old:
                    raise PolicyError("This action already succeeded or has an uncertain outcome. Inspect its result before reconciling or retrying.", reason="duplicate_or_uncertain", action_id=old["action_id"])
            used = db.execute("SELECT count(*) FROM actions WHERE account=? AND kind=? AND started>=?", (account, kind, day_start)).fetchone()[0]
            if used >= self.caps[kind]:
                raise PolicyError("Local daily action budget exhausted.", reason="daily_budget", retry_after_seconds=math.ceil(day_start + 86400 - now))
            recent = db.execute("SELECT started FROM actions WHERE account=? AND started>? ORDER BY started", (account, now - 60)).fetchall()
            if len(recent) >= self.per_minute:
                raise PolicyError("Local per-minute action budget exhausted.", reason="minute_budget", retry_after_seconds=max(1, math.ceil(recent[0][0] + 60 - now)))
            group = "kind IN ('read','unlock')" if kind in ("read", "unlock") else "kind NOT IN ('read','unlock')"
            last = db.execute("SELECT max(started) FROM actions WHERE account=? AND " + group, (account,)).fetchone()[0]
            interval = self.read_interval if kind in ("read", "unlock") else self.write_interval
            if last is not None and now - last < interval:
                raise PolicyError("Local action interval has not elapsed.", reason="cooldown", retry_after_seconds=max(1, math.ceil(last + interval - now)))
            action_id = uuid4().hex
            db.execute("INSERT INTO actions VALUES(?,?,?,?,?,'started',NULL)", (action_id, account, kind, action_key, now))
            if context:
                db.execute("INSERT INTO action_refs VALUES(?,?,?,?,?)", (action_id, account,
                           context.get("draft_id"), context.get("campaign_id"), context.get("lead_id")))
        return action_id

    def finish(self, account, action_id, status):
        if status not in ("succeeded", "failed", "uncertain"):
            raise ValueError("Invalid action outcome.")
        with self._db() as db:
            db.execute("UPDATE actions SET status=?,finished=? WHERE account=? AND action_id=?", (status, self.clock(), account, action_id))

    def pause(self, account, reason, *, preserve_manual=False):
        # Store a bounded machine reason, never an exception/HTML/body.
        allowed = {"login_required", "challenge", "rate_limited", "account_locked", "pin_required", "session_expired", "manual_pause"}
        reason = reason if reason in allowed else "challenge"
        with self._db() as db:
            if preserve_manual:
                current = db.execute("SELECT reason FROM pauses WHERE account=?", (account,)).fetchone()
                if current and current[0] == "manual_pause":
                    return
            db.execute("INSERT OR REPLACE INTO pauses VALUES(?,?,?)", (account, reason, self.clock()))
            db.execute("INSERT OR REPLACE INTO pause_versions VALUES(?,?)", (account, uuid4().hex))

    def resume(self, account):
        with self._db() as db:
            db.execute("DELETE FROM pauses WHERE account=?", (account,))
            db.execute("DELETE FROM pause_versions WHERE account=?", (account,))

    def pause_token(self, account):
        """Snapshot a pause generation, including records from older databases."""
        with self._db() as db:
            pause = db.execute("SELECT account FROM pauses WHERE account=?", (account,)).fetchone()
            if not pause:
                return None
            version = db.execute("SELECT token FROM pause_versions WHERE account=?", (account,)).fetchone()
            if version:
                return version[0]
            token = uuid4().hex
            db.execute("INSERT INTO pause_versions VALUES(?,?)", (account, token))
            return token

    def resume_if_unchanged(self, account, expected_token):
        """Clear only the pause inspected before recovery; retain newer pauses."""
        with self._db() as db:
            current = db.execute("SELECT v.token FROM pauses p LEFT JOIN pause_versions v ON p.account=v.account WHERE p.account=?", (account,)).fetchone()
            if current and (current[0] is None or current[0] != expected_token):
                return False
            db.execute("DELETE FROM pauses WHERE account=?", (account,))
            db.execute("DELETE FROM pause_versions WHERE account=?", (account,))
            return True

    def check_pause(self, account, *, read_operation=None):
        with self._db() as db:
            pause = db.execute("SELECT reason FROM pauses WHERE account=?", (account,)).fetchone()
        if pause and not (pause[0] == "pin_required" and read_operation in self.PIN_SAFE_READS):
            raise PolicyError("Account actions are paused.", reason=pause[0])

    def status(self, account):
        now = self.clock()
        with self._db() as db:
            pause = db.execute("SELECT reason,paused FROM pauses WHERE account=?", (account,)).fetchone()
            counts = {row["kind"]: row["used"] for row in db.execute("SELECT kind,count(*) AS used FROM actions WHERE account=? AND started>=? GROUP BY kind", (account, now - now % 86400))}
            uncertain = [dict(row) for row in db.execute("SELECT action_id,kind,started,status FROM actions WHERE account=? AND status IN ('started','uncertain') ORDER BY started DESC LIMIT 50", (account,))]
            recent = [dict(row) for row in db.execute("SELECT a.action_id,a.kind,a.started,a.status,r.draft_id,r.campaign_id,r.lead_id FROM actions a LEFT JOIN action_refs r ON a.action_id=r.action_id WHERE a.account=? ORDER BY a.started DESC LIMIT 50", (account,))]
        return {"paused": bool(pause), "pause": dict(pause) if pause else None,
                "daily_used": counts, "daily_caps": self.caps, "budget_timezone": "UTC",
                "uncertain_actions": uncertain, "recent_actions": recent}

    def reconcile(self, account, action_id, outcome):
        if outcome not in ("succeeded", "not_sent"):
            raise PolicyError("observed_outcome must be succeeded or not_sent.")
        with self._db() as db:
            row = db.execute("SELECT status FROM actions WHERE account=? AND action_id=?", (account, action_id)).fetchone()
            expected = "succeeded" if outcome == "succeeded" else "failed"
            if row and row[0] == expected:
                return {"action_id": action_id, "observed_outcome": outcome}
            if not row or row[0] not in ("started", "uncertain"):
                raise PolicyError("Only an uncertain action belonging to this account can be reconciled.")
            db.execute("UPDATE actions SET status=?,finished=? WHERE account=? AND action_id=?", ("succeeded" if outcome == "succeeded" else "failed", self.clock(), account, action_id))
        return {"action_id": action_id, "observed_outcome": outcome}

    def reference(self, account, action_id):
        with self._db() as db:
            row = db.execute("SELECT a.status,r.draft_id,r.campaign_id,r.lead_id FROM actions a LEFT JOIN action_refs r ON a.action_id=r.action_id WHERE a.account=? AND a.action_id=?", (account, action_id)).fetchone()
        return dict(row) if row else None

    def latest_draft_action(self, account, draft_id):
        """Return the newest reservation for one account's reviewed draft."""
        with self._db() as db:
            row = db.execute("SELECT a.action_id,a.status FROM actions a JOIN action_refs r ON a.action_id=r.action_id WHERE a.account=? AND r.account=? AND r.draft_id=? ORDER BY a.rowid DESC LIMIT 1", (account, account, draft_id)).fetchone()
        return dict(row) if row else None
