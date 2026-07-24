"""Cross-day dedup: processed action keys must never expire at midnight UTC.

The MCP server restarts on every client launch and reloads this set; a
same-day filter silently dropped every key written before today, while the
tool messages promise permanent dedup. Timestamps are still parsed
tolerantly (naive values normalize to UTC — no naive/aware mixing), but a
timestamp is never a reason to drop a key.
"""
from datetime import datetime, timedelta, timezone

import pytest

from xuse.utils.file_handler import FileHandler


@pytest.fixture
def file_handler(make_config_loader, tmp_path):
    handler = FileHandler(make_config_loader())
    handler.processed_tweets_file_path = tmp_path / "processed_tweets_log.csv"
    return handler


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


class TestCrossDayDedup:
    def test_keys_from_previous_days_still_load(self, file_handler):
        file_handler.save_processed_action_key(
            "reply_acc1_three_days_ago", timestamp=(_now_utc() - timedelta(days=3)).isoformat())
        file_handler.save_processed_action_key(
            "reply_acc1_today", timestamp=_now_utc().isoformat())

        assert file_handler.load_processed_action_keys() == {
            "reply_acc1_three_days_ago",
            "reply_acc1_today",
        }

    def test_yesterdays_key_survives_a_restart_reload(self, file_handler):
        """The midnight-UTC regression: a key written 'yesterday' must still
        dedup after the server restarts (fresh load)."""
        yesterday = (_now_utc() - timedelta(days=1)).isoformat()
        file_handler.save_processed_action_key("reply_acc1_123", timestamp=yesterday)

        assert "reply_acc1_123" in file_handler.load_processed_action_keys()

    def test_mixed_naive_and_aware_rows_all_load(self, file_handler):
        """The orchestrator writes naive-local timestamps, the MCP executor
        aware-UTC ones; neither shape may drop keys (hunter Minor #23)."""
        naive_yesterday = (_now_utc() - timedelta(days=1)).replace(tzinfo=None).isoformat()
        aware_two_days_ago = (_now_utc() - timedelta(days=2)).isoformat()
        file_handler.save_processed_action_key("like_acc1_naive", timestamp=naive_yesterday)
        file_handler.save_processed_action_key("like_acc1_aware", timestamp=aware_two_days_ago)

        assert file_handler.load_processed_action_keys() == {"like_acc1_naive", "like_acc1_aware"}

    def test_unparseable_timestamp_keeps_the_key(self, file_handler):
        """A row whose timestamp cannot be parsed is still a processed
        action — dropping it would reopen a duplicate-write window."""
        file_handler.save_processed_action_key("repost_acc1_bad_ts", timestamp="not-a-date")

        assert "repost_acc1_bad_ts" in file_handler.load_processed_action_keys()

    def test_rows_shorter_than_the_timestamp_column_still_load(self, file_handler):
        path = file_handler.processed_tweets_file_path
        path.write_text(
            "action_key,timestamp\n"
            "short_row_key\n"
            f"full_row_key,{_now_utc().isoformat()}\n",
            encoding="utf-8",
        )

        assert file_handler.load_processed_action_keys() == {"short_row_key", "full_row_key"}

    def test_todays_keys_still_load(self, file_handler):
        """Guard: widening the filter must not regress same-day behavior."""
        file_handler.save_processed_action_key("reply_acc1_now", timestamp=_now_utc().isoformat())

        assert file_handler.load_processed_action_keys() == {"reply_acc1_now"}
