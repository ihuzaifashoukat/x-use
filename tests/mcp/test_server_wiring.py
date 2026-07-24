"""Server wiring: queue store/config on ctx, auto_drain scheduler lifecycle."""
import pytest

from xuse.mcp.drafts import DraftStore
from xuse.mcp.server import create_server
from xuse.mcp.sessions import SessionPool
from xuse.queue import QueueConfig, QueueRunner, QueueStore

from helpers import (  # noqa: F401 — imported fixtures register for this module
    accounts,
    browser_factory,
    config_loader,
    draft_store,
    drafts_path,
    mcp_server,
    mcp_settings,
    queue_store,
    session_pool,
)


@pytest.mark.asyncio
async def test_ctx_carries_queue_store_and_defaults(mcp_server, queue_store):
    ctx = mcp_server.xuse_ctx
    assert ctx.queue_store is queue_store
    assert isinstance(ctx.queue_config, QueueConfig)
    assert ctx.queue_config.auto_drain.enabled is False
    assert isinstance(ctx.queue_runner, QueueRunner)


@pytest.mark.asyncio
async def test_default_runner_uses_injected_store(mcp_server, queue_store):
    # helpers.mcp_server injects only the store; the server builds the runner.
    assert mcp_server.xuse_ctx.queue_runner.store is queue_store


@pytest.mark.asyncio
async def test_auto_drain_off_by_default(mcp_server):
    assert getattr(mcp_server, "xuse_auto_drain", None) is None


@pytest.mark.asyncio
async def test_auto_drain_scheduler_created_when_enabled(make_config_loader, drafts_path,
                                                         tmp_path, browser_factory):
    loader = make_config_loader(
        settings={"queue": {"auto_drain": {"enabled": True, "interval_seconds": 5}}},
        accounts=[{"account_id": "acc1", "is_active": True}],
    )
    pool = SessionPool(loader, browser_factory=browser_factory)
    server = create_server(config_loader=loader, session_pool=pool,
                           draft_store=DraftStore(drafts_path),
                           queue_store=QueueStore(tmp_path / "q.jsonl"))
    scheduler = getattr(server, "xuse_auto_drain", None)
    assert scheduler is not None
    assert scheduler.interval_seconds == 5
    assert scheduler.account_ids_fn() == ["acc1"]
