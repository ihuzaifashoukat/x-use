"""Persistent store for queued actions.

JSONL, append-only, last-write-wins on load (same pattern as the MCP draft
store). Items stuck in "processing" at load reset to "pending": the server
died mid-action. Payloads must never contain secrets.
"""
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from .models import QueuedAction, QueueStatus

logger = logging.getLogger(__name__)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: str) -> datetime:
    # Python 3.10 fromisoformat has no Z-suffix support.
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def is_due(item: QueuedAction, now: datetime) -> bool:
    if not item.not_before:
        return True
    try:
        not_before = _parse_iso(item.not_before)
    except ValueError:
        return True  # never strand an item on an unparseable timestamp
    if not_before.tzinfo is None:
        not_before = not_before.replace(tzinfo=timezone.utc)
    return now >= not_before


class QueueStore:
    """In-memory registry with append-only JSONL persistence."""

    def __init__(self, path: Optional[Path] = None):
        self._items: Dict[str, QueuedAction] = {}
        self._path: Optional[Path] = Path(path) if path else None
        if self._path:
            self._load()

    # -- writes ----------------------------------------------------------

    def add(self, *, account: str, action: str, payload: Dict[str, Any],
            dedup_key: str, not_before: Optional[str] = None) -> QueuedAction:
        item = QueuedAction(
            queue_id=uuid4().hex,
            account=account,
            action=action,
            payload=payload,
            dedup_key=dedup_key,
            created_at=_utcnow_iso(),
            not_before=not_before,
        )
        self._items[item.queue_id] = item
        self._append(item)
        return item

    def get(self, queue_id: str) -> QueuedAction:
        try:
            return self._items[queue_id]
        except KeyError:
            raise KeyError(f"Unknown queue_id '{queue_id}'.") from None

    def set_status(self, queue_id: str, status: QueueStatus, *,
                   last_error: Optional[str] = None,
                   not_before: Optional[str] = None,
                   completed_at: Optional[str] = None) -> QueuedAction:
        item = self.get(queue_id)
        item.status = status
        if last_error is not None:
            item.last_error = last_error
        if not_before is not None:
            item.not_before = not_before
        if completed_at is not None:
            item.completed_at = completed_at
        self._append(item)
        return item

    def record_attempt(self, queue_id: str, last_error: str) -> QueuedAction:
        item = self.get(queue_id)
        item.attempts += 1
        item.last_error = last_error
        self._append(item)
        return item

    # -- queries ---------------------------------------------------------

    def list(self, account: Optional[str] = None,
             status: Optional[QueueStatus] = None) -> List[QueuedAction]:
        items = list(self._items.values())
        if account is not None:
            items = [i for i in items if i.account == account]
        if status is not None:
            items = [i for i in items if i.status == status]
        return items

    def find_pending_by_dedup_key(self, dedup_key: str) -> Optional[QueuedAction]:
        for item in self._items.values():
            if item.dedup_key == dedup_key and item.status in ("pending", "processing"):
                return item
        return None

    def due_items(self, account_id: str, now: datetime) -> List[QueuedAction]:
        """Pending items for the account whose not_before has passed, FIFO."""
        due = [
            i for i in self._items.values()
            if i.account == account_id and i.status == "pending" and is_due(i, now)
        ]
        due.sort(key=lambda i: i.created_at)
        return due

    def due_accounts(self, now: datetime) -> List[str]:
        seen: List[str] = []
        for item in sorted(self._items.values(), key=lambda i: i.created_at):
            if item.status == "pending" and is_due(item, now) and item.account not in seen:
                seen.append(item.account)
        return seen

    def count_done_today(self, account_id: str, action: str, now: datetime) -> int:
        day = now.date()
        count = 0
        for item in self._items.values():
            if item.account != account_id or item.action != action or item.status != "done":
                continue
            if not item.completed_at:
                continue
            try:
                completed = _parse_iso(item.completed_at)
            except ValueError:
                continue
            if completed.date() == day:
                count += 1
        return count

    def counts(self, account: Optional[str] = None) -> Dict[str, int]:
        result: Dict[str, int] = {}
        for item in self._items.values():
            if account is not None and item.account != account:
                continue
            result[item.status] = result.get(item.status, 0) + 1
        return result

    def __len__(self) -> int:
        return len(self._items)

    # -- persistence -----------------------------------------------------

    def _append(self, item: QueuedAction) -> None:
        if not self._path:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as f:
                f.write(item.model_dump_json() + "\n")
        except Exception:
            logger.exception("Failed to persist queue item %s to %s", item.queue_id, self._path)

    def _load(self) -> None:
        if not self._path or not self._path.exists():
            return
        try:
            with self._path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = QueuedAction.model_validate_json(line)
                    except Exception:
                        logger.warning("Skipping malformed queue line in %s", self._path)
                        continue
                    if item.status == "processing":
                        item.status = "pending"  # crashed mid-action
                    self._items[item.queue_id] = item  # last write wins
            logger.info("Loaded %d queue item(s) from %s", len(self._items), self._path)
        except Exception:
            logger.exception("Failed to load queue from %s", self._path)
