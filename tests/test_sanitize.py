"""Whole-userinfo secret masking (hunt Criticals: token-style creds leaked;
usernames survived). Shared module + executor re-export + runner last_error."""
import pytest

from xuse.utils.sanitize import sanitize_text
from xuse.mcp.executor import mask_account
from xuse.mcp.executor import sanitize_text as ex_sanitize
from xuse.queue import QueueConfig, QueueRunner, QueueStore

NOW_MSG = "connect http://alice:pw123@proxy.example:8080 failed"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("http://alice:secret@host:8080", "http://***@host:8080"),
        ("http://TOKEN@host:8080", "http://***@host:8080"),  # token-only userinfo
        ("socks5://bob@host:1080", "socks5://***@host:1080"),
        ("http://host:8080/path", "http://host:8080/path"),  # no userinfo untouched
        ("no urls here", "no urls here"),
    ],
)
def test_userinfo_masking(raw, expected):
    assert sanitize_text(raw) == expected


def test_executor_reexport_and_mask_account():
    assert ex_sanitize("socks5://u:p@h:1") == "socks5://***@h:1"
    masked = mask_account({"account_id": "a", "proxy": "http://bob:pw@h:1"})
    assert masked["proxy"] == "http://***@h:1"


class _FailExecutor:
    async def __call__(self, item):
        raise RuntimeError(NOW_MSG)


@pytest.mark.asyncio
async def test_runner_sanitizes_last_error_everywhere(tmp_path):
    """Executor errors with proxy creds must never reach the store, the
    DrainReport, or list_queue responses."""
    from datetime import datetime, timezone
    store = QueueStore(tmp_path / "q.jsonl")
    item = store.add(account="acc1", action="like", payload={}, dedup_key="k1")
    runner = QueueRunner(
        store, QueueConfig(max_attempts=1), _FailExecutor(),
        sleep_fn=lambda s: _noop(s), jitter_fn=lambda a, b: a,
        now_fn=lambda: datetime(2026, 7, 24, tzinfo=timezone.utc))
    report = await runner.drain("acc1")
    assert report.failed == 1
    stored = store.get(item.queue_id)
    assert "pw123" not in (stored.last_error or "")
    assert "alice" not in (stored.last_error or "")
    assert "***@" in stored.last_error
    assert "pw123" not in str(report.executed)

    # and the raw file on disk
    assert "pw123" not in (tmp_path / "q.jsonl").read_text()


async def _noop(seconds):
    return None
