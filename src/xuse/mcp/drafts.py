"""Draft store for MCP draft mode (the brand-safety gate).

When draft mode is on (the default), write tools build a fully-rendered
payload — including LLM-generated text where applicable — and return a
:class:`Draft` for human review instead of touching the browser. Nothing
executes until ``approve_draft(draft_id)`` is called.

Drafts are plain JSON (Pydantic models) and optionally persisted as JSONL
so they survive server restarts. Payloads must never contain secrets
(cookies, API keys, proxy credentials) — only the content to be published.
"""
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel
from xuse.core.local_state import private_state_file

logger = logging.getLogger(__name__)

DraftStatus = Literal["pending", "approved", "partial", "executed", "failed", "rejected", "uncertain"]


class Draft(BaseModel):
    """A pending write action awaiting human approval."""

    draft_id: str
    account: str
    action: str  # e.g. "post_tweet", "reply_to_tweet", "engage_like"
    payload: Dict[str, Any]  # everything needed to execute, secret-free
    preview: str  # human-readable rendering of exactly what will happen
    created_at: str
    status: DraftStatus = "pending"


class DraftStore:
    """In-memory draft registry with optional JSONL persistence.

    Persistence is append-only: every create/status change appends the full
    draft as one JSON line; on load, the last line per draft_id wins.
    """

    def __init__(self, persistence_path: Optional[Path] = None):
        self._drafts: Dict[str, Draft] = {}
        self._path: Optional[Path] = Path(persistence_path) if persistence_path else None
        if self._path:
            self._load()

    def create(self, account: str, action: str, payload: Dict[str, Any], preview: str) -> Draft:
        draft = Draft(
            draft_id=uuid4().hex,
            account=account,
            action=action,
            payload=payload,
            preview=preview,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self._append(draft)
        self._drafts[draft.draft_id] = draft
        logger.info("Created draft %s (%s on account '%s').", draft.draft_id, action, account)
        return draft

    def get(self, draft_id: str) -> Draft:
        """Return the draft or raise KeyError for unknown ids."""
        try:
            return self._drafts[draft_id]
        except KeyError:
            raise KeyError(f"Unknown draft_id '{draft_id}'.") from None

    def set_status(self, draft_id: str, status: DraftStatus) -> Draft:
        draft = self.get(draft_id)
        # Persist a candidate first: approval must fail closed if the journal
        # cannot record it, and existing references must retain the old state.
        candidate = draft.model_copy(update={"status": status})
        self._append(candidate)
        draft.status = status
        return draft

    def list(self, status: Optional[DraftStatus] = None) -> List[Draft]:
        drafts = list(self._drafts.values())
        if status is not None:
            drafts = [d for d in drafts if d.status == status]
        return drafts

    def __len__(self) -> int:
        return len(self._drafts)

    # -- persistence ------------------------------------------------------

    def _append(self, draft: Draft) -> None:
        if not self._path:
            return
        try:
            private_state_file(self._path)
            with self._path.open("a", encoding="utf-8") as f:
                f.write(draft.model_dump_json() + "\n")
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            logger.exception("Failed to persist draft %s to %s", draft.draft_id, self._path)
            raise

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
                        draft = Draft.model_validate_json(line)
                    except Exception:
                        # Skipping a damaged approval/outcome could revive an
                        # older pending record and authorize a duplicate write.
                        raise ValueError("Draft journal contains an invalid record; inspect and repair local state before startup.") from None
                    if draft.status == "approved":
                        # A restart cannot prove whether an approved action
                        # submitted. Preserve uncertainty until reconciliation.
                        draft.status = "uncertain"
                    self._drafts[draft.draft_id] = draft  # last write wins
            logger.info("Loaded %d draft(s) from %s", len(self._drafts), self._path)
        except Exception:
            logger.exception("Failed to load drafts from %s", self._path)
            raise
