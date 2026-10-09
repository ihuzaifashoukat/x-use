"""Durable SQLite lead/campaign storage with permanent opt-out suppression.

All reads and mutations require an account. A campaign only reserves local
drafts: each external delivery still needs the existing draft approval gate.
Private notes are never used in messages, exceptions, or logging.
"""
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from string import Formatter
from typing import Any, Dict, List, Optional, Union
from uuid import uuid4
from xuse.core.local_state import private_sqlite_file

from .models import (
    CAMPAIGN_STATUSES, ELIGIBLE_LEAD_STATUSES, LEAD_STATUSES,
    MAX_PREPARED_MESSAGES, Campaign, CampaignMember, Lead, OutreachError,
)

_ACCOUNT_RE = re.compile(r"[A-Za-z0-9_-]+")
_HANDLE_RE = re.compile(r"[A-Za-z0-9_]{1,15}")
_TEMPLATE_FIELDS = frozenset({"handle", "name", "display_name", "company"})
_PERSON_FIELDS = frozenset({"handle", "name", "display_name"})
_CAMPAIGN_TRANSITIONS = {
    "draft": {"ready", "paused", "completed"},
    "ready": {"paused", "completed"},
    "paused": {"draft", "ready", "completed"},
    "completed": set(),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _account(value: str) -> str:
    if not isinstance(value, str) or not _ACCOUNT_RE.fullmatch(value):
        raise OutreachError("A valid account id is required.")
    return value


def normalize_handle(value: str) -> str:
    if not isinstance(value, str):
        raise OutreachError("handle must be an X handle, with an optional @ prefix.")
    value = value.strip().removeprefix("@")
    if not _HANDLE_RE.fullmatch(value):
        raise OutreachError("handle must contain 1-15 letters, digits, or underscores.")
    return value.lower()


def _text(value: str, field: str, maximum: int, *, required: bool = False) -> str:
    if not isinstance(value, str):
        raise OutreachError(f"{field} must be text.")
    value = value.strip()
    if required and not value:
        raise OutreachError(f"{field} must not be empty.")
    if len(value) > maximum:
        raise OutreachError(f"{field} exceeds the {maximum} character limit.")
    return value


def validate_template(value: str) -> str:
    template = _text(value, "message_template", 10000, required=True)
    fields = set()
    try:
        for _, field, spec, conversion in Formatter().parse(template):
            if field is None:
                continue
            if field not in _TEMPLATE_FIELDS:
                raise OutreachError(
                    "Unknown template placeholder. Allowed: handle, name, display_name, company.")
            if spec or conversion:
                raise OutreachError("Template placeholders cannot use formatting or conversions.")
            fields.add(field)
    except ValueError as exc:
        if isinstance(exc, OutreachError):
            raise
        raise OutreachError("message_template contains malformed braces.") from None
    if not fields.intersection(_PERSON_FIELDS):
        raise OutreachError("message_template must personalize each recipient with {handle} or {name}.")
    return template


class OutreachStore:
    """SQLite-backed local records, safe for concurrent store instances.

    Each mutation uses BEGIN IMMEDIATE. Draft creation and membership
    reservation happen in one such transaction, preventing duplicate
    preparation. SQLite and the separate draft JSONL cannot commit together;
    an interrupted reservation therefore stays blocked for safe review.
    """

    def __init__(self, path: Union[str, Path]):
        self.path = Path(path)
        private_sqlite_file(self.path)
        with self._connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS leads (
                    lead_id TEXT PRIMARY KEY,
                    account TEXT NOT NULL,
                    handle TEXT NOT NULL,
                    display_name TEXT NOT NULL DEFAULT '',
                    company TEXT NOT NULL DEFAULT '',
                    notes TEXT NOT NULL DEFAULT '',
                    tags TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL CHECK(status IN
                        ('new','qualified','contacted','replied','closed','opted_out')),
                    opt_out INTEGER NOT NULL DEFAULT 0 CHECK(opt_out IN (0,1)),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(account, handle), UNIQUE(account, lead_id)
                );
                CREATE TABLE IF NOT EXISTS suppressions (
                    account TEXT NOT NULL, handle TEXT NOT NULL,
                    opted_out_at TEXT NOT NULL,
                    PRIMARY KEY(account, handle)
                );
                CREATE TABLE IF NOT EXISTS campaigns (
                    campaign_id TEXT PRIMARY KEY,
                    account TEXT NOT NULL,
                    name TEXT NOT NULL,
                    message_template TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('draft','ready','paused','completed')),
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(account, campaign_id)
                );
                CREATE TABLE IF NOT EXISTS campaign_leads (
                    account TEXT NOT NULL, campaign_id TEXT NOT NULL, lead_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'unprepared' CHECK(status IN
                        ('unprepared','pending','delivered','failed','rejected')),
                    draft_id TEXT, prepared_at TEXT, delivered_at TEXT,
                    PRIMARY KEY(account, campaign_id, lead_id),
                    FOREIGN KEY(account, campaign_id) REFERENCES campaigns(account, campaign_id),
                    FOREIGN KEY(account, lead_id) REFERENCES leads(account, lead_id)
                );
                CREATE INDEX IF NOT EXISTS leads_account_status ON leads(account, status);
                CREATE INDEX IF NOT EXISTS campaigns_account_status ON campaigns(account, status);
                CREATE INDEX IF NOT EXISTS campaign_member_reservations
                    ON campaign_leads(account, lead_id, status, campaign_id);
            """)

    @contextmanager
    def _connection(self, *, write: bool = False):
        private_sqlite_file(self.path)
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            if write:
                conn.commit()
        except BaseException:
            if write:
                conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _lead(row) -> Lead:
        values = dict(row)
        values["tags"] = json.loads(values["tags"])
        values["opt_out"] = bool(values["opt_out"])
        return Lead(**values)

    @staticmethod
    def _lead_row(conn, account: str, lead_id: str):
        row = conn.execute("SELECT * FROM leads WHERE account=? AND lead_id=?",
                           (account, lead_id)).fetchone()
        if row is None:
            raise OutreachError("Unknown lead for this account.")
        return row

    @staticmethod
    def _campaign_row(conn, account: str, campaign_id: str):
        row = conn.execute("SELECT * FROM campaigns WHERE account=? AND campaign_id=?",
                           (account, campaign_id)).fetchone()
        if row is None:
            raise OutreachError("Unknown campaign for this account.")
        return row

    @staticmethod
    def _suppressed(conn, account: str, handle: str) -> bool:
        return conn.execute("SELECT 1 FROM suppressions WHERE account=? AND handle=?",
                            (account, handle)).fetchone() is not None

    def upsert_lead(self, account: str, handle: str, *, display_name: Optional[str] = None,
                    company: Optional[str] = None, notes: Optional[str] = None,
                    tags: Optional[List[str]] = None, status: Optional[str] = None) -> Lead:
        account, handle = _account(account), normalize_handle(handle)
        updates = {}
        for field, value, limit in (("display_name", display_name, 200),
                                    ("company", company, 200), ("notes", notes, 8000)):
            if value is not None:
                updates[field] = _text(value, field, limit)
        if tags is not None:
            if not isinstance(tags, list) or len(tags) > 30:
                raise OutreachError("tags must be a list of at most 30 labels.")
            labels = [_text(label, "tag", 100, required=True) for label in tags]
            updates["tags"] = json.dumps(list(dict.fromkeys(labels)), ensure_ascii=False)
        if status is not None and status not in LEAD_STATUSES:
            raise OutreachError(f"status must be one of {', '.join(LEAD_STATUSES)}.")
        with self._connection(write=True) as conn:
            row = conn.execute("SELECT * FROM leads WHERE account=? AND handle=?",
                               (account, handle)).fetchone()
            now = _now()
            suppressed = self._suppressed(conn, account, handle)
            if status == "opted_out":
                conn.execute("INSERT OR IGNORE INTO suppressions VALUES (?,?,?)",
                             (account, handle, now))
                suppressed = True
            if suppressed:
                updates.update(status="opted_out", opt_out=1)
            elif status is not None:
                updates["status"] = status
            if row is None:
                lead_id = uuid4().hex
                values = dict(display_name="", company="", notes="", tags="[]",
                              status="new", opt_out=0)
                values.update(updates)
                conn.execute("""INSERT INTO leads VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                             (lead_id, account, handle, values["display_name"], values["company"],
                              values["notes"], values["tags"], values["status"], values["opt_out"],
                              now, now))
            else:
                lead_id = row["lead_id"]
                updates = {field: value for field, value in updates.items() if row[field] != value}
                if updates:
                    updates["updated_at"] = now
                    columns = ", ".join(f"{field}=?" for field in updates)
                    conn.execute(f"UPDATE leads SET {columns} WHERE account=? AND lead_id=?",
                                 (*updates.values(), account, lead_id))
            return self._lead(self._lead_row(conn, account, lead_id))

    def get_lead(self, account: str, lead_id: str) -> Lead:
        with self._connection() as conn:
            return self._lead(self._lead_row(conn, _account(account), lead_id))

    def is_suppressed(self, account: str, handle: str) -> bool:
        """Check permanent account/handle opt-out, including non-campaign sends."""
        account, handle = _account(account), normalize_handle(handle)
        with self._connection() as conn:
            return self._suppressed(conn, account, handle)

    def validate_recipient(self, account: str, handle: str) -> str:
        """Normalize a direct-message/follow recipient and enforce opt-out."""
        handle = normalize_handle(handle)
        if self.is_suppressed(account, handle):
            raise OutreachError("This lead opted out; outreach to this recipient is suppressed.")
        return handle

    def list_leads(self, account: str, *, status: Optional[str] = None,
                   tag: Optional[str] = None, limit: int = 50, offset: int = 0) -> List[Lead]:
        account = _account(account)
        self._page(limit, offset)
        if status is not None and status not in LEAD_STATUSES:
            raise OutreachError("Unknown lead status.")
        with self._connection() as conn:
            sql, args = "SELECT * FROM leads WHERE account=?", [account]
            if status is not None:
                sql += " AND status=?"
                args.append(status)
            if tag is not None:
                # json_each uses exact tag equality; no substring or SQL wildcard matching.
                sql += " AND EXISTS (SELECT 1 FROM json_each(leads.tags) WHERE value=?)"
                args.append(_text(tag, "tag", 100, required=True))
            sql += " ORDER BY created_at, lead_id LIMIT ? OFFSET ?"
            return [self._lead(row) for row in conn.execute(sql, (*args, limit, offset))]

    @staticmethod
    def _page(limit: int, offset: int) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise OutreachError("limit must be an integer between 1 and 100.")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise OutreachError("offset must be a non-negative integer.")

    def update_lead_status(self, account: str, lead_id: str, status: str) -> Lead:
        if status not in LEAD_STATUSES:
            raise OutreachError("Unknown lead status.")
        account = _account(account)
        with self._connection(write=True) as conn:
            row = self._lead_row(conn, account, lead_id)
            if status == "opted_out":
                return self._opt_out(conn, account, row)
            if row["opt_out"] or self._suppressed(conn, account, row["handle"]):
                raise OutreachError("This lead opted out; status edits cannot remove suppression.")
            if row["status"] != status:
                conn.execute("UPDATE leads SET status=?, updated_at=? WHERE account=? AND lead_id=?",
                             (status, _now(), account, lead_id))
            return self._lead(self._lead_row(conn, account, lead_id))

    def _opt_out(self, conn, account: str, row) -> Lead:
        now = _now()
        conn.execute("INSERT OR IGNORE INTO suppressions VALUES (?,?,?)",
                     (account, row["handle"], now))
        if not row["opt_out"] or row["status"] != "opted_out":
            conn.execute("UPDATE leads SET opt_out=1, status='opted_out', updated_at=? "
                         "WHERE account=? AND lead_id=?", (now, account, row["lead_id"]))
        return self._lead(self._lead_row(conn, account, row["lead_id"]))

    def opt_out_lead(self, account: str, lead_id: str) -> Lead:
        account = _account(account)
        with self._connection(write=True) as conn:
            return self._opt_out(conn, account, self._lead_row(conn, account, lead_id))

    def create_campaign(self, account: str, name: str, message_template: str) -> Campaign:
        account = _account(account)
        name = _text(name, "name", 200, required=True)
        template = validate_template(message_template)
        campaign_id, now = uuid4().hex, _now()
        with self._connection(write=True) as conn:
            conn.execute("INSERT INTO campaigns VALUES (?,?,?,?,?,?,?)",
                         (campaign_id, account, name, template, "draft", now, now))
            return Campaign(**dict(self._campaign_row(conn, account, campaign_id)))

    def _campaign(self, conn, account: str, campaign_id: str, *, limit: int = 50, offset: int = 0) -> Campaign:
        campaign = Campaign(**dict(self._campaign_row(conn, account, campaign_id)))
        campaign.members_total = conn.execute(
            "SELECT count(*) FROM campaign_leads WHERE account=? AND campaign_id=?",
            (account, campaign_id)).fetchone()[0]
        campaign.members_offset = offset
        rows = conn.execute("""SELECT m.lead_id,l.handle,m.status,m.draft_id,
                                      m.prepared_at,m.delivered_at FROM campaign_leads m
                               JOIN leads l ON l.account=m.account AND l.lead_id=m.lead_id
                               WHERE m.account=? AND m.campaign_id=? ORDER BY l.created_at,l.lead_id
                               LIMIT ? OFFSET ?""",
                            (account, campaign_id, limit, offset))
        campaign.members = [CampaignMember(**dict(row)) for row in rows]
        if offset + len(campaign.members) < campaign.members_total:
            campaign.members_next_offset = offset + len(campaign.members)
        return campaign

    def get_campaign(self, account: str, campaign_id: str, *, limit: int = 50, offset: int = 0) -> Campaign:
        self._page(limit, offset)
        with self._connection() as conn:
            return self._campaign(conn, _account(account), campaign_id, limit=limit, offset=offset)

    def list_campaigns(self, account: str, *, status: Optional[str] = None,
                       limit: int = 50, offset: int = 0) -> List[Campaign]:
        account = _account(account)
        self._page(limit, offset)
        if status is not None and status not in CAMPAIGN_STATUSES:
            raise OutreachError("Unknown campaign status.")
        with self._connection() as conn:
            sql, args = "SELECT * FROM campaigns WHERE account=?", [account]
            if status is not None:
                sql += " AND status=?"
                args.append(status)
            rows = conn.execute(sql + " ORDER BY created_at,campaign_id LIMIT ? OFFSET ?",
                                (*args, limit, offset)).fetchall()
            # Listings omit members; get_campaign pages through membership.
            return [Campaign(**dict(row)) for row in rows]

    def add_campaign_leads(self, account: str, campaign_id: str,
                           lead_ids: List[str]) -> Campaign:
        account = _account(account)
        if not isinstance(lead_ids, list) or not 1 <= len(lead_ids) <= 100:
            raise OutreachError("lead_ids must contain between 1 and 100 lead ids.")
        if any(not isinstance(value, str) or not value for value in lead_ids):
            raise OutreachError("Each lead_id must be non-empty text.")
        with self._connection(write=True) as conn:
            campaign = self._campaign_row(conn, account, campaign_id)
            if campaign["status"] == "completed":
                raise OutreachError("A completed campaign cannot accept leads.")
            # Validate the entire batch before any mutation, then insert idempotently.
            for lead_id in set(lead_ids):
                self._lead_row(conn, account, lead_id)
            added = False
            for lead_id in dict.fromkeys(lead_ids):
                cursor = conn.execute("INSERT OR IGNORE INTO campaign_leads(account,campaign_id,lead_id) "
                                      "VALUES (?,?,?)", (account, campaign_id, lead_id))
                added = added or cursor.rowcount > 0
            if added:
                conn.execute("UPDATE campaigns SET updated_at=? WHERE account=? AND campaign_id=?",
                             (_now(), account, campaign_id))
            return self._campaign(conn, account, campaign_id)

    def set_campaign_status(self, account: str, campaign_id: str, status: str) -> Campaign:
        account = _account(account)
        if status not in CAMPAIGN_STATUSES:
            raise OutreachError("Unknown campaign status.")
        with self._connection(write=True) as conn:
            campaign = self._campaign_row(conn, account, campaign_id)
            previous = campaign["status"]
            if status != previous and status not in _CAMPAIGN_TRANSITIONS[previous]:
                raise OutreachError(f"Cannot transition campaign from {previous} to {status}.")
            if status != previous:
                conn.execute("UPDATE campaigns SET status=?,updated_at=? "
                             "WHERE account=? AND campaign_id=?",
                             (status, _now(), account, campaign_id))
            return self._campaign(conn, account, campaign_id)

    @staticmethod
    def _render(template: str, lead: Lead) -> str:
        values = {"handle": "@" + lead.handle, "name": lead.display_name or "@" + lead.handle,
                  "display_name": lead.display_name or "@" + lead.handle, "company": lead.company}
        fields = [field for _, field, _, _ in Formatter().parse(template) if field is not None]
        if any(not values[field] for field in fields):
            raise OutreachError("A lead is missing a value required by the message template.")
        message = template.format_map(values).strip()
        if not message or len(message) > 10000:
            raise OutreachError("Rendered message must contain between 1 and 10000 characters.")
        return message

    def prepare_campaign_messages(self, account: str, campaign_id: str, draft_store,
                                  max_messages: int = 5) -> List[Any]:
        """Reserve at most 20 personalized drafts; never execute an X action.

        Pending/delivered membership is excluded, including across restarts.
        Rejected drafts may be prepared again after status reconciliation.
        Failed or missing draft records remain pending conservatively: a failed
        browser action may have reached X before confirmation was lost.
        """
        account = _account(account)
        if (isinstance(max_messages, bool) or not isinstance(max_messages, int)
                or not 1 <= max_messages <= MAX_PREPARED_MESSAGES):
            raise OutreachError("max_messages must be an integer between 1 and 20.")
        drafts = []
        try:
            with self._connection(write=True) as conn:
                campaign = self._campaign_row(conn, account, campaign_id)
                if campaign["status"] in ("paused", "completed"):
                    raise OutreachError("A paused or completed campaign cannot prepare messages.")
                template = validate_template(campaign["message_template"])
                rows = conn.execute("""SELECT l.*,m.status AS member_status,m.draft_id
                                       FROM campaign_leads m JOIN leads l
                                       ON l.account=m.account AND l.lead_id=m.lead_id
                                       WHERE m.account=? AND m.campaign_id=?
                                       ORDER BY l.created_at,l.lead_id""",
                                    (account, campaign_id)).fetchall()
                candidates = []
                for row in rows:
                    member_status = row["member_status"]
                    if member_status == "pending" and row["draft_id"]:
                        try:
                            previous = draft_store.get(row["draft_id"])
                        except KeyError:
                            previous = None
                        if previous is not None and previous.status in ("rejected", "executed"):
                            member_status = "delivered" if previous.status == "executed" else previous.status
                            conn.execute("UPDATE campaign_leads SET status=? WHERE account=? "
                                         "AND campaign_id=? AND lead_id=?",
                                         (member_status, account, campaign_id, row["lead_id"]))
                    if member_status in ("pending", "delivered"):
                        continue
                    if conn.execute("SELECT 1 FROM campaign_leads WHERE account=? AND lead_id=? "
                                    "AND campaign_id<>? AND status IN ('pending','delivered') LIMIT 1",
                                    (account, row["lead_id"], campaign_id)).fetchone():
                        continue
                    if (row["status"] not in ELIGIBLE_LEAD_STATUSES or row["opt_out"]
                            or self._suppressed(conn, account, row["handle"])):
                        continue
                    lead_values = {key: row[key] for key in Lead.model_fields}
                    lead = self._lead(lead_values)
                    candidates.append((lead, self._render(template, lead)))
                    if len(candidates) == max_messages:
                        break
                # Rendering the selected batch before creation avoids partial drafts on bad input.
                for lead, message in candidates:
                    draft = draft_store.create(
                        account=account, action="send_message",
                        payload={"recipient": "@" + lead.handle, "text": message,
                                 "campaign_id": campaign_id, "lead_id": lead.lead_id},
                        preview=f"Send a private message to @{lead.handle}:\n{message}",
                    )
                    drafts.append(draft)
                    conn.execute("UPDATE campaign_leads SET status='pending',draft_id=?,prepared_at=? "
                                 "WHERE account=? AND campaign_id=? AND lead_id=?",
                                 (draft.draft_id, _now(), account, campaign_id, lead.lead_id))
                if drafts:
                    conn.execute("UPDATE campaigns SET status='ready',updated_at=? "
                                 "WHERE account=? AND campaign_id=?", (_now(), account, campaign_id))
            return drafts
        except BaseException:
            # Rollback cannot undo the separate draft store. Fail closed for any created orphans.
            for draft in drafts:
                draft_store.set_status(draft.draft_id, "rejected")
            raise

    def validate_delivery(self, account: str, lead_id: str, campaign_id: str, *,
                          expected_draft_id: Optional[str] = None) -> Lead:
        """Recheck opt-out/pause/eligibility immediately before approving a DM.

        The caller must also compare the draft's recipient with this returned
        lead's handle and enforce account activation and individual approval.
        A supplied draft ID must still own the current reservation.
        """
        account = _account(account)
        with self._connection(write=True) as conn:
            campaign = self._campaign_row(conn, account, campaign_id)
            if campaign["status"] != "ready":
                raise OutreachError("The campaign must be ready before a message can be delivered.")
            row = self._lead_row(conn, account, lead_id)
            if row["opt_out"] or self._suppressed(conn, account, row["handle"]):
                raise OutreachError("This lead opted out; delivery is suppressed.")
            if row["status"] not in ELIGIBLE_LEAD_STATUSES:
                raise OutreachError("The lead's current status is not eligible for outreach.")
            member = conn.execute("SELECT status,draft_id FROM campaign_leads WHERE account=? "
                                  "AND campaign_id=? AND lead_id=?",
                                  (account, campaign_id, lead_id)).fetchone()
            if member is None or member["status"] != "pending":
                raise OutreachError("No pending reviewed draft reservation exists for this campaign lead.")
            if expected_draft_id is not None and member["draft_id"] != expected_draft_id:
                raise OutreachError("The campaign draft reservation has changed; inspect the current draft before retrying.")
            return self._lead(row)

    def record_delivery(self, account: str, lead_id: str, campaign_id: str,
                        outcome: str, *, expected_draft_id: Optional[str] = None) -> CampaignMember:
        """Record an outcome only for the supplied current draft reservation.

        Supplying the draft ID prevents old reconciliation from changing a
        replacement reservation. Omitted IDs retain the legacy caller API.
        """
        account = _account(account)
        outcome = {"success": "delivered", "executed": "delivered", "done": "delivered"}.get(outcome, outcome)
        if outcome not in ("delivered", "failed", "rejected"):
            raise OutreachError("outcome must be delivered, failed, or rejected.")
        with self._connection(write=True) as conn:
            self._campaign_row(conn, account, campaign_id)
            lead = self._lead_row(conn, account, lead_id)
            row = conn.execute("SELECT * FROM campaign_leads WHERE account=? AND campaign_id=? AND lead_id=?",
                               (account, campaign_id, lead_id)).fetchone()
            if row is None:
                raise OutreachError("The lead does not belong to this account's campaign.")
            if expected_draft_id is not None and row["draft_id"] != expected_draft_id:
                raise OutreachError("The campaign draft reservation has changed; inspect the current draft before retrying.")
            if row["status"] == "delivered":
                if outcome != "delivered":
                    raise OutreachError("A confirmed delivery cannot be changed to a failed or rejected draft.")
            elif row["status"] != "pending":
                if row["status"] != outcome:
                    raise OutreachError("Only a pending draft may record a delivery outcome.")
            else:
                now = _now()
                conn.execute("UPDATE campaign_leads SET status=?,delivered_at=? WHERE account=? "
                             "AND campaign_id=? AND lead_id=?",
                             (outcome, now if outcome == "delivered" else None, account, campaign_id, lead_id))
                if outcome == "delivered" and not lead["opt_out"] and not self._suppressed(conn, account, lead["handle"]):
                    conn.execute("UPDATE leads SET status='contacted',updated_at=? "
                                 "WHERE account=? AND lead_id=?", (now, account, lead_id))
                conn.execute("UPDATE campaigns SET updated_at=? WHERE account=? AND campaign_id=?",
                             (now, account, campaign_id))
            member = conn.execute("""SELECT m.lead_id,l.handle,m.status,m.draft_id,
                                            m.prepared_at,m.delivered_at
                                     FROM campaign_leads m JOIN leads l
                                     ON l.account=m.account AND l.lead_id=m.lead_id
                                     WHERE m.account=? AND m.campaign_id=? AND m.lead_id=?""",
                                  (account, campaign_id, lead_id)).fetchone()
            return CampaignMember(**dict(member))

    def get_campaign_summary(self, account: str, campaign_id: str) -> Dict[str, Any]:
        account = _account(account)
        with self._connection() as conn:
            campaign = self._campaign_row(conn, account, campaign_id)
            counts = {key: 0 for key in ("unprepared", "pending", "delivered", "failed", "rejected")}
            # Aggregate in SQLite with indexed existence checks. Response size
            # and Python memory stay constant as campaign membership grows.
            rows = conn.execute("""SELECT m.status, count(*) AS total,
                sum(CASE WHEN l.opt_out OR s.handle IS NOT NULL THEN 1 ELSE 0 END) AS suppressed,
                sum(CASE WHEN NOT l.opt_out AND s.handle IS NULL
                    AND l.status IN ('new','qualified') AND m.status NOT IN ('pending','delivered')
                    AND NOT EXISTS (SELECT 1 FROM campaign_leads other
                        WHERE other.account=m.account AND other.lead_id=m.lead_id
                        AND other.campaign_id<>m.campaign_id AND other.status IN ('pending','delivered'))
                    THEN 1 ELSE 0 END) AS eligible
                FROM campaign_leads m JOIN leads l ON l.account=m.account AND l.lead_id=m.lead_id
                LEFT JOIN suppressions s ON s.account=l.account AND s.handle=l.handle
                WHERE m.account=? AND m.campaign_id=? GROUP BY m.status""",
                (account, campaign_id))
            total, eligible, suppressed = 0, 0, 0
            for row in rows:
                counts[row["status"]] = row["total"]
                total += row["total"]
                suppressed += row["suppressed"]
                eligible += row["eligible"]
            return {"account": account, "campaign_id": campaign_id, "status": campaign["status"],
                    "total_leads": total, "counts": counts,
                    "eligible_to_prepare": eligible, "suppressed_leads": suppressed}
