"""Write-path robustness tests for the MCP executors.

Pinned behaviors:
- On a successful write, the dedup key is marked BEFORE any metrics I/O, and
  metrics failures are logged and swallowed — a metrics I/O error must never
  turn a successful X write into a reported failure or skip dedup (which
  would cause duplicate posts on retry).
- The post dedup key discriminates on text + media + community:
  post_<account>_<sha1((text or '') + '|' + str(media or []) + '|' + str(community))[:12]>
  (byte-identical with queue_tools.queue_post).
- mark_processed logs a warning (non-fatal) when the dedup append fails.
- approve_draft self-heals the crash window: a dedup-duplicate rejection
  means the action already ran, so the draft is marked "executed", not
  "failed".
- get_metrics guards the summary shape: valid-but-non-dict JSON yields the
  default summary plus a warning field.

All tests run against injected fakes — no browser, no network.
"""
import hashlib
import json
import logging
from unittest.mock import MagicMock

import pytest

import xuse.mcp.actions as actions
import xuse.mcp.executor as ex
from xuse.mcp.executor import ToolError

from helpers import (  # noqa: F401 — imported fixtures register for this module
    FakeMetrics,
    accounts,
    assert_error_envelope,
    browser_factory,
    call_tool,
    config_loader,
    draft_store,
    drafts_path,
    make_account,
    make_fake_publisher,
    mcp_server,
    mcp_settings,
    queue_store,
    session_pool,
)


def expected_post_key(account_id: str, text, media, community) -> str:
    """The contract both actions.exec_post and queue_tools.queue_post must
    agree on, verbatim."""
    material = (text or "") + "|" + str(media or []) + "|" + str(community)
    return f"post_{account_id}_{hashlib.sha1(material.encode('utf-8')).hexdigest()[:12]}"


class ExplodingMetrics:
    """MetricsRecorder stand-in whose every I/O method raises."""

    def __init__(self, account_id: str) -> None:
        self.account_id = account_id

    def log_event(self, *args, **kwargs) -> None:
        raise PermissionError("simulated metrics disk failure")

    def increment(self, *args, **kwargs) -> None:
        raise PermissionError("simulated metrics disk failure")


@pytest.fixture
def publisher_instances() -> list:
    return []


@pytest.fixture
def stubbed_ctx(mcp_server, monkeypatch, publisher_instances):
    """Same seam-stubbing pattern as test_drafts: real execution path, no
    browser, no real files, no LLM."""
    ctx = mcp_server.xuse_ctx
    ctx.processed_keys = set()
    ctx.file_handler = MagicMock(name="file_handler")
    ctx.llm_service = MagicMock(name="llm_service")
    metrics = {}
    ctx.metrics_factory = lambda account_id: metrics.setdefault(account_id, FakeMetrics(account_id))
    monkeypatch.setattr("xuse.mcp.actions.TweetPublisher", make_fake_publisher(publisher_instances))
    return ctx


# ---------------------------------------------------------------------------
# Post dedup key: text + media + community
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_post_marks_the_exact_dedup_key(stubbed_ctx, publisher_instances):
    result = await actions.exec_post(
        stubbed_ctx, "acc1", text="ship it", media=["chart.png"], community="1234567890")
    assert result["success"] is True
    assert ex.is_processed(
        stubbed_ctx, expected_post_key("acc1", "ship it", ["chart.png"], "1234567890"))
    await stubbed_ctx.session_pool.close_all()


@pytest.mark.asyncio
async def test_same_text_different_media_is_not_a_duplicate(stubbed_ctx, publisher_instances):
    first = await actions.exec_post(stubbed_ctx, "acc1", text="same text", media=["a.png"])
    second = await actions.exec_post(stubbed_ctx, "acc1", text="same text", media=["b.png"])
    assert first["success"] is True and second["success"] is True
    await stubbed_ctx.session_pool.close_all()


@pytest.mark.asyncio
async def test_same_text_with_community_is_not_a_duplicate(stubbed_ctx, publisher_instances):
    first = await actions.exec_post(stubbed_ctx, "acc1", text="same text")
    second = await actions.exec_post(stubbed_ctx, "acc1", text="same text", community="1234567890")
    assert first["success"] is True and second["success"] is True
    await stubbed_ctx.session_pool.close_all()


@pytest.mark.asyncio
async def test_identical_post_is_still_rejected_as_duplicate(stubbed_ctx, publisher_instances):
    await actions.exec_post(stubbed_ctx, "acc1", text="same text", media=["a.png"])
    with pytest.raises(ToolError, match="dedup"):
        await actions.exec_post(stubbed_ctx, "acc1", text="same text", media=["a.png"])
    await stubbed_ctx.session_pool.close_all()


# ---------------------------------------------------------------------------
# Write -> dedup -> metrics ordering; metrics can never fail a write
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metrics_io_failure_does_not_fail_a_successful_write(
        stubbed_ctx, publisher_instances):
    stubbed_ctx.metrics_factory = lambda account_id: ExplodingMetrics(account_id)

    result = await actions.exec_post(stubbed_ctx, "acc1", text="metrics explode")

    assert result["success"] is True
    # Dedup was still marked — a retry is blocked even though metrics blew up.
    assert ex.is_processed(stubbed_ctx, expected_post_key("acc1", "metrics explode", None, None))
    await stubbed_ctx.session_pool.close_all()


@pytest.mark.asyncio
async def test_metrics_io_failure_on_failed_write_still_reports_the_write_failure(
        stubbed_ctx, publisher_instances, monkeypatch):
    class FailingPublisher:
        def __init__(self, browser_manager, llm_service, account_config) -> None:
            pass

        async def post_new_tweet(self, content, llm_settings=None) -> bool:
            return False

    monkeypatch.setattr("xuse.mcp.actions.TweetPublisher", FailingPublisher)
    stubbed_ctx.metrics_factory = lambda account_id: ExplodingMetrics(account_id)

    with pytest.raises(ToolError, match="Post failed"):
        await actions.exec_post(stubbed_ctx, "acc1", text="double explosion")

    # A failed write must NOT be dedup-marked (retry stays possible, paced).
    assert not ex.is_processed(stubbed_ctx, expected_post_key("acc1", "double explosion", None, None))
    await stubbed_ctx.session_pool.close_all()


@pytest.mark.asyncio
async def test_dedup_is_marked_before_metrics_run(stubbed_ctx, publisher_instances):
    """Order pin: mark_processed must land before the first metrics call, so
    a metrics crash can never strand a successful write without its dedup key."""
    order = []
    file_handler = MagicMock(name="file_handler")
    file_handler.save_processed_action_key.side_effect = (
        lambda *args, **kwargs: order.append("dedup") or True)
    stubbed_ctx.file_handler = file_handler

    class RecordingMetrics:
        def __init__(self, account_id: str) -> None:
            self.account_id = account_id

        def log_event(self, *args, **kwargs) -> None:
            order.append("metrics")

        def increment(self, *args, **kwargs) -> None:
            order.append("metrics")

    stubbed_ctx.metrics_factory = lambda account_id: RecordingMetrics(account_id)

    result = await actions.exec_post(stubbed_ctx, "acc1", text="order matters")

    assert result["success"] is True
    assert order and order[0] == "dedup"
    await stubbed_ctx.session_pool.close_all()


@pytest.mark.asyncio
async def test_metrics_failure_does_not_fail_a_successful_like(stubbed_ctx, monkeypatch):
    """The metrics guard wraps every exec_* path, not just posts."""

    class FakeEngagement:
        def __init__(self, browser_manager, account_config) -> None:
            pass

        async def like_tweet(self, tweet_id=None, tweet_url=None) -> bool:
            return True

    monkeypatch.setattr("xuse.mcp.actions.TweetEngagement", FakeEngagement)
    stubbed_ctx.metrics_factory = lambda account_id: ExplodingMetrics(account_id)

    result = await actions.exec_like(stubbed_ctx, "acc1", "999", "https://x.com/u/status/999")

    assert result["success"] is True
    assert ex.is_processed(stubbed_ctx, "like_acc1_999")
    await stubbed_ctx.session_pool.close_all()


# ---------------------------------------------------------------------------
# mark_processed: a failed dedup persist is loud, not silent
# ---------------------------------------------------------------------------


def test_mark_processed_warns_when_the_save_fails(stubbed_ctx, caplog):
    file_handler = MagicMock(name="file_handler")
    file_handler.save_processed_action_key.return_value = False
    stubbed_ctx.file_handler = file_handler

    with caplog.at_level(logging.WARNING, logger="xuse.mcp.executor"):
        ex.mark_processed(stubbed_ctx, "like_acc1_123")

    # Non-fatal: the in-memory set still tracks the key for this process...
    assert "like_acc1_123" in stubbed_ctx.processed_keys
    # ...but the dropped persist is surfaced, not silently lost.
    assert any(
        record.levelno >= logging.WARNING and "like_acc1_123" in record.getMessage()
        for record in caplog.records
    )


# ---------------------------------------------------------------------------
# approve_draft crash-window self-heal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dedup_rejection_on_reapproval_marks_draft_executed_not_failed(
        mcp_server, stubbed_ctx, publisher_instances):
    """Crash window: the post went out and the dedup key persisted, but the
    server died before the draft's "executed" append. After a restart the
    draft reloads as pending; re-approving hits dedup — the draft already
    ran, so it must be labeled "executed", never "failed"."""
    draft = await call_tool(mcp_server, "post_tweet", {"account": "acc1", "text": "crash window"})
    assert draft["ok"] is True

    # Simulate the pre-crash execution: the dedup key is already processed.
    stubbed_ctx.processed_keys.add(expected_post_key("acc1", "crash window", [], None))

    result = await call_tool(mcp_server, "approve_draft", {"draft_id": draft["draft_id"]})

    error = assert_error_envelope(result, "dedup")
    assert error["type"] == "ToolError"
    assert stubbed_ctx.draft_store.get(draft["draft_id"]).status == "executed"
    # Nothing executed a second time.
    assert publisher_instances == []


@pytest.mark.asyncio
async def test_genuine_execution_failure_still_marks_draft_failed(
        mcp_server, stubbed_ctx, monkeypatch):
    """Regression guard: only dedup-duplicate rejections relabel the draft;
    real failures keep the "failed" label."""

    class FailingPublisher:
        def __init__(self, browser_manager, llm_service, account_config) -> None:
            pass

        async def post_new_tweet(self, content, llm_settings=None) -> bool:
            return False

    monkeypatch.setattr("xuse.mcp.actions.TweetPublisher", FailingPublisher)

    draft = await call_tool(mcp_server, "post_tweet", {"account": "acc1", "text": "will fail"})
    result = await call_tool(mcp_server, "approve_draft", {"draft_id": draft["draft_id"]})

    assert_error_envelope(result, "Post failed")
    assert stubbed_ctx.draft_store.get(draft["draft_id"]).status == "failed"
    await stubbed_ctx.session_pool.close_all()


# ---------------------------------------------------------------------------
# get_metrics summary shape guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_metrics_wrong_shape_returns_default_summary_and_warning(
        mcp_server, monkeypatch, tmp_path):
    metrics_dir = tmp_path / "data" / "metrics"
    metrics_dir.mkdir(parents=True)
    (metrics_dir / "acc1.json").write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    monkeypatch.setattr("xuse.mcp.tools.PROJECT_ROOT", tmp_path)

    result = await call_tool(mcp_server, "get_metrics", {"account": "acc1"})

    assert result["ok"] is True
    assert isinstance(result["summary"], dict)
    assert result["summary"]["counters"] == {
        "posts": 0, "replies": 0, "retweets": 0, "quote_tweets": 0, "likes": 0, "errors": 0,
    }
    assert "warning" in result


@pytest.mark.asyncio
async def test_get_metrics_valid_summary_is_returned_verbatim(mcp_server, monkeypatch, tmp_path):
    metrics_dir = tmp_path / "data" / "metrics"
    metrics_dir.mkdir(parents=True)
    summary = {
        "account_id": "acc1",
        "counters": {"posts": 3, "replies": 1, "retweets": 0, "quote_tweets": 0, "likes": 9, "errors": 0},
        "last_run_started_at": None,
        "last_run_finished_at": None,
    }
    (metrics_dir / "acc1.json").write_text(json.dumps(summary), encoding="utf-8")
    monkeypatch.setattr("xuse.mcp.tools.PROJECT_ROOT", tmp_path)

    result = await call_tool(mcp_server, "get_metrics", {"account": "acc1"})

    assert result["ok"] is True
    assert result["summary"] == summary
    assert "warning" not in result
