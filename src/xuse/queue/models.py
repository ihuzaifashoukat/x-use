"""Data models for the action queue: item, config, status types."""
import logging
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

QueueStatus = Literal["pending", "processing", "done", "failed", "cancelled"]
QueueActionType = Literal["post", "reply", "like", "retweet"]

ALL_STATUSES = ("pending", "processing", "done", "failed", "cancelled")


class QueuedAction(BaseModel):
    """One scheduled write action. Payloads must never contain secrets."""

    queue_id: str
    account: str
    action: QueueActionType
    payload: Dict[str, Any]
    dedup_key: str
    status: QueueStatus = "pending"
    created_at: str
    not_before: Optional[str] = None
    attempts: int = 0
    last_error: Optional[str] = None
    completed_at: Optional[str] = None


class AutoDrainConfig(BaseModel):
    enabled: bool = False
    interval_seconds: int = 900
    max_actions_per_account: int = 3


class QueueConfig(BaseModel):
    store_file: str = "data/engagement_queue.jsonl"
    max_actions_per_run: int = 5
    min_delay_seconds: int = 90
    max_delay_seconds: int = 240
    max_attempts: int = 2
    daily_caps: Dict[str, int] = Field(
        default_factory=lambda: {"post": 5, "reply": 15, "like": 30, "retweet": 10}
    )
    auto_drain: AutoDrainConfig = Field(default_factory=AutoDrainConfig)

    @classmethod
    def from_settings(cls, settings: Any) -> "QueueConfig":
        """Build from the top-level "queue" block of settings.json (tolerant)."""
        if not isinstance(settings, dict):
            return cls()
        try:
            return cls(**settings)
        except Exception:
            logger.warning("Invalid 'queue' settings block; using defaults.", exc_info=True)
            return cls()
