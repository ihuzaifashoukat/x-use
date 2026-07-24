"""Shared infrastructure for the x-use MCP tools.

Context object, error sanitizing, account/config resolution, per-account
pacing (NFR-4), dedup keys, and metrics handles. The actual browser/LLM
operations built on top of this live in ``actions.py`` — no Selenium logic
in either module, only orchestration of the existing engine modules.
"""
import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Set, Tuple

from xuse.core.config_loader import ConfigLoader
from xuse.core.llm_service import LLMService
from xuse.models import AccountConfig, ActionConfig, LLMSettings
from xuse.utils.file_handler import FileHandler
from xuse.utils.metrics import MetricsRecorder

from .drafts import DraftStore
from xuse.queue import QueueConfig, QueueRunner, QueueStore
from .sessions import SessionPool

logger = logging.getLogger(__name__)

MAX_REPLY_CHARS = 270


class ToolError(Exception):
    """Expected, user-facing failure (bad input, unknown account, action
    rejected). Converted into a structured error envelope by the tool layer."""


@dataclass
class Ctx:
    """Shared state for all MCP tools. Injectable seams keep contract tests
    free of real browsers, real metrics files, and real dedup stores."""

    config_loader: ConfigLoader
    session_pool: SessionPool
    draft_store: DraftStore
    draft_mode: bool = True
    llm_service: Optional[LLMService] = None
    file_handler: Optional[FileHandler] = None
    processed_keys: Optional[Set[str]] = None
    metrics_factory: Optional[Callable[[str], Any]] = None
    last_action_at: Dict[str, float] = field(default_factory=dict)
    runs: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    queue_store: Optional[QueueStore] = None
    queue_config: Optional[QueueConfig] = None
    queue_runner: Optional[QueueRunner] = None


# ---------------------------------------------------------------------------
# Error sanitizing (NFR-2: never echo cookies, API keys, proxy credentials)
# ---------------------------------------------------------------------------

_URL_CREDENTIALS_RE = re.compile(r"(://[^:/\s]+):([^@\s]+)@")


def sanitize_text(text: Any) -> str:
    return _URL_CREDENTIALS_RE.sub(r"\1:***@", str(text))


def error_envelope(exc_type: str, message: Any) -> Dict[str, Any]:
    return {"ok": False, "error": {"type": exc_type, "message": sanitize_text(message)}}


# ---------------------------------------------------------------------------
# Account / config resolution
# ---------------------------------------------------------------------------


def default_account_id(ctx: Ctx) -> str:
    accounts = ctx.config_loader.get_accounts_config()
    for raw in accounts:
        if isinstance(raw, dict) and raw.get("is_active", True) and raw.get("account_id"):
            return raw["account_id"]
    for raw in accounts:
        if isinstance(raw, dict) and raw.get("account_id"):
            return raw["account_id"]
    raise ToolError("No accounts configured in config/accounts.json.")


def resolve_account(ctx: Ctx, account: Optional[str]) -> Tuple[str, Dict[str, Any], AccountConfig]:
    """Return (account_id, normalized raw dict, validated AccountConfig)."""
    account_id = account or default_account_id(ctx)
    raw = ctx.session_pool.find_account_dict(account_id)  # raises SessionError if unknown
    try:
        model = AccountConfig.model_validate(raw)
    except Exception as e:
        raise ToolError(f"Account '{account_id}' failed config validation: {sanitize_text(e)}") from e
    return account_id, raw, model


def current_action_config(ctx: Ctx, account: AccountConfig) -> ActionConfig:
    """Per-account action_config wins over the global default (NFR-7)."""
    if account.action_config is not None:
        return account.action_config
    global_ac = ctx.config_loader.get_setting("twitter_automation.action_config", {}) or {}
    try:
        return ActionConfig(**global_ac)
    except Exception:
        logger.warning("Invalid global action_config; falling back to defaults.", exc_info=True)
        return ActionConfig()


def llm_settings_for(account: AccountConfig, action_config: ActionConfig, kind: str) -> LLMSettings:
    """Account-level LLM override wins over the action-specific settings."""
    return account.llm_settings_override or getattr(action_config, f"llm_settings_for_{kind}")


def get_llm(ctx: Ctx) -> LLMService:
    if ctx.llm_service is None:
        ctx.llm_service = LLMService(config_loader=ctx.config_loader)
    return ctx.llm_service


def require_llm(ctx: Ctx) -> LLMService:
    service = get_llm(ctx)
    # clients is a dict of provider-name -> client-or-None; placeholder keys
    # leave every value None (see llm_service.clients.initialize_clients).
    clients = getattr(service, "clients", None) or {}
    if not any(clients.values()):
        raise ToolError(
            "No LLM provider is configured (all API keys missing or placeholders). "
            "Set a real key in config/settings.json api_keys or the environment."
        )
    return service


# ---------------------------------------------------------------------------
# Pacing (NFR-4): per-account minimum spacing between write actions.
# No "no delay" fast path is exposed anywhere.
# ---------------------------------------------------------------------------


async def pace(ctx: Ctx, account_id: str, action_config: ActionConfig) -> None:
    min_delay = max(0, int(action_config.min_delay_between_actions_seconds))
    last = ctx.last_action_at.get(account_id)
    if last is not None:
        wait = min_delay - (time.monotonic() - last)
        if wait > 0:
            logger.info("[mcp] pacing account '%s': waiting %.1fs before next write action.", account_id, wait)
            await asyncio.sleep(wait)


def mark_action_now(ctx: Ctx, account_id: str) -> None:
    ctx.last_action_at[account_id] = time.monotonic()


# ---------------------------------------------------------------------------
# Dedup keys + metrics — same mechanisms as batch runs
# ---------------------------------------------------------------------------


def _file_handler(ctx: Ctx) -> FileHandler:
    if ctx.file_handler is None:
        ctx.file_handler = FileHandler(ctx.config_loader)
    return ctx.file_handler


def _processed(ctx: Ctx) -> Set[str]:
    if ctx.processed_keys is None:
        ctx.processed_keys = _file_handler(ctx).load_processed_action_keys()
    return ctx.processed_keys


def is_processed(ctx: Ctx, key: str) -> bool:
    return key in _processed(ctx)


def mark_processed(ctx: Ctx, key: str) -> None:
    _file_handler(ctx).save_processed_action_key(key, timestamp=datetime.now(timezone.utc).isoformat())
    _processed(ctx).add(key)


def metrics_for(ctx: Ctx, account_id: str) -> Any:
    if ctx.metrics_factory is not None:
        return ctx.metrics_factory(account_id)
    return MetricsRecorder(account_id=account_id, config_loader=ctx.config_loader)


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------

_TWEET_ID_RE = re.compile(r"/status(?:es)?/(\d+)")


def tweet_id_from_url(url: str) -> Optional[str]:
    match = _TWEET_ID_RE.search(url or "")
    return match.group(1) if match else None


def mask_account(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Account config safe to return to an MCP client: no cookies, no
    password, proxy credentials masked."""
    masked = {k: v for k, v in raw.items() if k not in ("cookies", "password")}
    if isinstance(masked.get("proxy"), str):
        masked["proxy"] = sanitize_text(masked["proxy"])
    return masked
