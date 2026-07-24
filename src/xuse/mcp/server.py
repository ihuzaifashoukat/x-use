"""x-use MCP server — official MCP Python SDK v1.x ``FastMCP`` over stdio.

Exposes the engine as 32 tools (see ``tools.py``): read-only/status tools,
draft-gated immediate writes, the scheduled-action queue, and account
management. Run directly::

    python -m xuse.mcp.server

or via the CLI: ``x-use mcp``.

Config (all optional, additive — ``config/settings.json``)::

    "mcp": {
        "draft_mode": true,                    // default ON
        "session_idle_timeout_seconds": 600,   // warm-session reap threshold
        "cold_start_timeout_seconds": 180,     // browser start + cookie login
        "drafts_file": "data/drafts.jsonl"     // draft persistence
    },
    "queue": {
        "store_file": "data/engagement_queue.jsonl",
        "max_actions_per_run": 5,
        "min_delay_seconds": 90,
        "max_delay_seconds": 240,
        "max_attempts": 2,
        "daily_caps": {"post": 5, "reply": 15, "like": 30, "retweet": 10},
        "auto_drain": {"enabled": false, "interval_seconds": 900,
                       "max_actions_per_account": 3}
    }

SDK note: pinned to ``mcp>=1,<2``. The v2 alpha renames FastMCP to
``MCPServer`` (``mcp.server.mcpserver``) — do not migrate until v2 is stable.
"""
import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional, Union

from mcp.server.fastmcp import FastMCP

from xuse.core.config_loader import ConfigLoader, PROJECT_ROOT
from xuse.queue import AutoDrainScheduler, QueueConfig, QueueRunner, QueueStore

from . import tools as _tools
from .drafts import DraftStore
from .executor import Ctx, is_processed
from .sessions import SessionPool

logger = logging.getLogger(__name__)


class _StderrProxy:
    """Forward stray text writes (print, progress bars) to stderr while
    exposing the real stdout ``.buffer`` — the MCP stdio transport wraps that
    buffer directly when ``server.run()`` starts, so protocol bytes still go
    to genuine stdout and everything else lands on stderr."""

    def __init__(self, real_stdout, stderr):
        self._stderr = stderr
        self.buffer = real_stdout.buffer

    def write(self, data):
        return self._stderr.write(data)

    def flush(self):
        return self._stderr.flush()

    def __getattr__(self, name):
        return getattr(self._stderr, name)


def _enforce_stdio_stdout_hygiene() -> None:
    """Keep stdout reserved for JSON-RPC. The engine's root logger is
    configured (at import time, by xuse.orchestrator/publisher) with a
    StreamHandler bound to the real sys.stdout, and utils.progress.Progress
    writes bars via sys.stdout.write — both would corrupt the stdio
    transport. Re-point handlers at stderr and proxy remaining sys.stdout
    writes there too."""
    real_stdout = sys.stdout
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.StreamHandler) and getattr(handler, "stream", None) is real_stdout:
            handler.setStream(sys.stderr)
    sys.stdout = _StderrProxy(real_stdout, sys.stderr)  # type: ignore[assignment]

SERVER_NAME = "x-use"
SERVER_INSTRUCTIONS = (
    "Browser-native automation for X (Twitter): post, reply, search, engage, and "
    "schedule across multiple accounts. Two safety gates: WRITE tools run in "
    "draft mode by default (review the draft, then approve_draft), and QUEUE "
    "tools only store work — queued items execute solely through an explicit "
    "process_queue call, or the auto_drain worker if the operator enabled it. "
    "Account tools mutate config/accounts.json (validated, backed up); cookie "
    "secrets are imported by server-side file path only. Read-only tools "
    "(list_accounts, get_account, get_metrics, list_queue, list_drafts, "
    "get_draft, get_run_status, get_account_health) never start a browser. "
    "search_tweets, get_tweet, and prepare_reply are read-only (they never "
    "post) but reuse the account's browser session."
)


def _queue_store_path(queue_cfg: QueueConfig) -> Path:
    path = Path(queue_cfg.store_file)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _is_active_account(config_loader: ConfigLoader, account_id: str) -> bool:
    for raw in config_loader.get_accounts_config():
        if isinstance(raw, dict) and raw.get("account_id") == account_id:
            return bool(raw.get("is_active", True))
    return False


@asynccontextmanager
async def _lifespan(server: FastMCP):
    """FastMCP lifespan: start the opt-in auto-drain worker on boot and stop
    it before shutdown (which then closes the warm browser sessions)."""
    scheduler = getattr(server, "xuse_auto_drain", None)
    if scheduler is not None:
        await scheduler.start()
    try:
        yield {}
    finally:
        if scheduler is not None:
            await scheduler.stop()


def create_server(
    config_loader: Optional[ConfigLoader] = None,
    *,
    draft_mode: Optional[bool] = None,
    session_pool: Optional[SessionPool] = None,
    draft_store: Optional[DraftStore] = None,
    queue_store: Optional[QueueStore] = None,
    queue_runner: Optional[QueueRunner] = None,
) -> FastMCP:
    """Build the FastMCP server. All dependencies are injectable so contract
    tests can supply a custom config, a fake-browser pool, tmp stores, and a
    fake-sleep queue runner without touching real browsers or data files."""
    config_loader = config_loader or ConfigLoader()
    mcp_cfg = config_loader.get_setting("mcp", {}) or {}
    if not isinstance(mcp_cfg, dict):
        mcp_cfg = {}
    if draft_mode is None:
        draft_mode = bool(mcp_cfg.get("draft_mode", True))  # default ON
    # NB: explicit `is not None` — DraftStore defines __len__, so an empty
    # store is falsy and `or` would silently discard an injected one.
    pool = session_pool if session_pool is not None else SessionPool(
        config_loader,
        idle_timeout_seconds=float(mcp_cfg.get("session_idle_timeout_seconds", 600)),
        cold_start_timeout_seconds=float(mcp_cfg.get("cold_start_timeout_seconds", 180)),
    )
    store = draft_store if draft_store is not None else DraftStore(
        Path(mcp_cfg["drafts_file"]) if mcp_cfg.get("drafts_file") else PROJECT_ROOT / "data" / "drafts.jsonl"
    )
    queue_cfg = QueueConfig.from_settings(config_loader.get_setting("queue", {}))
    q_store = queue_store if queue_store is not None else QueueStore(_queue_store_path(queue_cfg))
    ctx = Ctx(
        config_loader=config_loader,
        session_pool=pool,
        draft_store=store,
        draft_mode=draft_mode,
        queue_store=q_store,
        queue_config=queue_cfg,
    )
    if queue_runner is not None:
        ctx.queue_runner = queue_runner
    else:
        # Function-level import: queue_tools pulls in the tool layer, which
        # pulls in the scraper stack; keeping it here mirrors tools.py's own
        # deferred imports and avoids import cycles during server bring-up.
        from .queue_tools import build_executor

        ctx.queue_runner = QueueRunner(
            q_store,
            queue_cfg,
            executor=build_executor(ctx),
            already_done=lambda key: is_processed(ctx, key),
            account_filter=lambda aid: _is_active_account(config_loader, aid),
        )
    scheduler = None
    if queue_cfg.auto_drain.enabled:
        scheduler = AutoDrainScheduler(
            ctx.queue_runner,
            account_ids_fn=lambda: [
                a["account_id"]
                for a in config_loader.get_accounts_config()
                if isinstance(a, dict) and a.get("is_active", True) and a.get("account_id")
            ],
            interval_seconds=queue_cfg.auto_drain.interval_seconds,
            max_actions_per_account=queue_cfg.auto_drain.max_actions_per_account,
        )
    server = FastMCP(SERVER_NAME, instructions=SERVER_INSTRUCTIONS, lifespan=_lifespan)
    _tools.register_tools(server, ctx)
    server.xuse_ctx = ctx  # reachable for shutdown hooks and tests
    if scheduler is not None:
        server.xuse_auto_drain = scheduler
    logger.info(
        "x-use MCP server created (draft_mode=%s, idle_timeout=%ss, auto_drain=%s).",
        ctx.draft_mode,
        pool.idle_timeout_seconds,
        queue_cfg.auto_drain.enabled,
    )
    return server


async def shutdown(server: FastMCP) -> None:
    """Close all warm browser sessions (idempotent, never raises)."""
    ctx: Union[Ctx, None] = getattr(server, "xuse_ctx", None)
    if ctx is None:
        return
    try:
        await ctx.session_pool.close_all()
    except Exception:
        logger.exception("Error while closing MCP session pool.")


def main() -> None:
    """Console entry point: run the stdio server until the client disconnects."""
    _enforce_stdio_stdout_hygiene()
    server = create_server()
    try:
        server.run()  # stdio transport; blocks for the server lifecycle
    finally:
        # run() closes its event loop on exit; close browsers on a fresh one.
        try:
            asyncio.run(shutdown(server))
        except Exception:
            logger.exception("MCP server shutdown cleanup failed.")


if __name__ == "__main__":
    main()
