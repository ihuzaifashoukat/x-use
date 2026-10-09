"""Synthetic metrics/support state must be bounded and object shaped."""
import json
from pathlib import Path

import pytest

from helpers import (  # noqa: F401
    accounts, browser_factory, call_tool, config_loader, draft_store, drafts_path,
    mcp_server, mcp_settings, queue_store, session_pool,
)


@pytest.mark.asyncio
async def test_metrics_tail_does_not_read_whole_file(mcp_server, tmp_path, monkeypatch):
    import xuse.mcp.tools as tools
    monkeypatch.setattr(tools, "PROJECT_ROOT", tmp_path)
    events = tmp_path / "logs/accounts/acc1.jsonl"
    events.parent.mkdir(parents=True)
    with events.open("w", encoding="utf-8") as stream:
        for index in range(20000):
            stream.write(json.dumps({"index": index, "synthetic": "x" * 80}) + "\n")

    def deny_read_text(*args, **kwargs):
        raise AssertionError("whole-file text reads are forbidden")

    monkeypatch.setattr(Path, "read_text", deny_read_text)
    result = await call_tool(mcp_server, "get_metrics", {"account": "acc1"})
    assert result["ok"]
    assert [event["index"] for event in result["recent_events"]] == list(range(19980, 20000))
    assert result["events_truncated"] is True


@pytest.mark.asyncio
async def test_health_rejects_non_object_metrics(mcp_server, tmp_path, monkeypatch):
    import xuse.mcp.support_tools as support
    monkeypatch.setattr(support, "PROJECT_ROOT", tmp_path)
    summary = tmp_path / "data/metrics/acc1.json"
    summary.parent.mkdir(parents=True)
    summary.write_text('["synthetic"]', encoding="utf-8")
    result = await call_tool(mcp_server, "get_account_health", {"account": "acc1"})
    assert result["ok"]
    assert result["metrics"] == {"unreadable": True}
