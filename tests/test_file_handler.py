"""Tests for processed-action-key dedup in xuse.utils.file_handler.FileHandler."""

import csv
from datetime import datetime, timedelta, timezone

import pytest

from xuse.core.config_loader import PROJECT_ROOT
from xuse.utils.file_handler import FileHandler


@pytest.fixture
def file_handler(make_config_loader, tmp_path):
    handler = FileHandler(make_config_loader())
    handler.processed_tweets_file_path = tmp_path / "processed_tweets_log.csv"
    return handler


class TestConfiguredPath:
    """FileHandler must honor twitter_automation.processed_tweets_file."""

    def test_configured_relative_path_resolves_under_project_root(self, make_config_loader):
        handler = FileHandler(make_config_loader(
            settings={"twitter_automation": {"processed_tweets_file": "data/custom_log.csv"}}
        ))
        assert handler.processed_tweets_file_path == PROJECT_ROOT / "data/custom_log.csv"

    def test_default_path_when_not_configured(self, make_config_loader):
        handler = FileHandler(make_config_loader(settings={"twitter_automation": {}}))
        assert handler.processed_tweets_file_path == PROJECT_ROOT / "processed_tweets_log.csv"

    def test_non_dict_twitter_automation_block_falls_back(self, make_config_loader):
        handler = FileHandler(make_config_loader(settings={"twitter_automation": "bogus"}))
        assert handler.processed_tweets_file_path == PROJECT_ROOT / "processed_tweets_log.csv"


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _days_ago_iso(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _read_rows(path):
    with path.open(mode="r", newline="", encoding="utf-8") as f:
        return list(csv.reader(f))


class TestLoad:
    def test_missing_file_returns_empty_set(self, file_handler):
        assert file_handler.load_processed_action_keys() == set()

    def test_header_only_file_returns_empty_set(self, file_handler):
        file_handler.processed_tweets_file_path.write_text(
            "action_key,timestamp\n", encoding="utf-8"
        )
        assert file_handler.load_processed_action_keys() == set()

    def test_keys_from_previous_days_still_load(self, file_handler):
        """Dedup is permanent: keys must not expire at midnight UTC (the MCP
        server restarts per client launch and reloads this set)."""
        file_handler.save_processed_action_key("reply_acc1_today", timestamp=_now_utc_iso())
        # 25+ hours back is never "today", regardless of when the test runs.
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        file_handler.save_processed_action_key("reply_acc1_yesterday", timestamp=old_ts)

        assert file_handler.load_processed_action_keys() == {
            "reply_acc1_today",
            "reply_acc1_yesterday",
        }

    def test_naive_timestamp_assumed_utc(self, file_handler):
        naive_utc_now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        file_handler.save_processed_action_key("like_acc1_naive", timestamp=naive_utc_now)
        assert file_handler.load_processed_action_keys() == {"like_acc1_naive"}

    def test_unparseable_timestamp_keeps_the_key(self, file_handler):
        """A malformed timestamp never drops a key — permanent dedup errs on
        the side of keeping a processed action."""
        file_handler.save_processed_action_key("repost_acc1_bad", timestamp="not-a-date")
        file_handler.save_processed_action_key("repost_acc1_good", timestamp=_now_utc_iso())
        assert file_handler.load_processed_action_keys() == {"repost_acc1_bad", "repost_acc1_good"}

    def test_rows_shorter_than_timestamp_column_still_load(self, file_handler):
        path = file_handler.processed_tweets_file_path
        path.write_text(
            "action_key,timestamp\n"
            f"short_row_key\n"
            f"full_row_key,{_now_utc_iso()}\n",
            encoding="utf-8",
        )
        assert file_handler.load_processed_action_keys() == {"short_row_key", "full_row_key"}

    def test_no_timestamp_column_loads_all_keys(self, file_handler):
        path = file_handler.processed_tweets_file_path
        path.write_text(
            "action_key,note\n"
            "like_acc1_111,some note\n"
            "reply_acc2_222,another note\n",
            encoding="utf-8",
        )
        # Without a timestamp column there is no daily scoping: everything loads.
        assert file_handler.load_processed_action_keys() == {
            "like_acc1_111",
            "reply_acc2_222",
        }


class TestSave:
    def test_save_creates_file_with_header_and_returns_true(self, file_handler):
        assert file_handler.save_processed_action_key("reply_acc1_123", timestamp=_now_utc_iso()) is True
        rows = _read_rows(file_handler.processed_tweets_file_path)
        assert rows[0] == ["action_key", "timestamp"]
        assert len(rows) == 2
        assert rows[1][0] == "reply_acc1_123"

    def test_header_written_once_across_appends(self, file_handler):
        file_handler.save_processed_action_key("like_acc1_1", timestamp=_now_utc_iso())
        file_handler.save_processed_action_key("like_acc1_2", timestamp=_now_utc_iso())
        rows = _read_rows(file_handler.processed_tweets_file_path)
        assert rows[0] == ["action_key", "timestamp"]
        assert [r[0] for r in rows[1:]] == ["like_acc1_1", "like_acc1_2"]

    def test_save_without_timestamp_omits_column(self, file_handler):
        file_handler.save_processed_action_key("skip_own_acc1_555")
        rows = _read_rows(file_handler.processed_tweets_file_path)
        assert rows[0] == ["action_key"]
        assert rows[1] == ["skip_own_acc1_555"]

    def test_extra_data_extends_header_and_daily_scoping_still_works(self, file_handler):
        file_handler.save_processed_action_key(
            "reply_acc1_999", timestamp=_now_utc_iso(), source="unit_test"
        )
        rows = _read_rows(file_handler.processed_tweets_file_path)
        assert rows[0] == ["action_key", "timestamp", "source"]
        assert rows[1][2] == "unit_test"
        assert file_handler.load_processed_action_keys() == {"reply_acc1_999"}


class TestDedupRoundTrip:
    """The orchestrator dedups with keys shaped f"{action}_{account_id}_{tweet_id}" —
    e.g. "reply_acc1_123". These round-trips pin that contract."""

    @pytest.mark.parametrize(
        "action_key",
        [
            "reply_acc1_1234567890",
            "like_acc1_1234567890",
            "repost_acc2_9876543210",
            "community_retweet_acc1_13579",
            "community_reply_acc2_24680",
            "skip_own_acc1_11111",
        ],
    )
    def test_action_key_format_round_trips(self, file_handler, action_key):
        assert file_handler.save_processed_action_key(action_key, timestamp=_now_utc_iso())
        assert action_key in file_handler.load_processed_action_keys()

    def test_in_memory_set_matches_reloaded_disk_state(self, file_handler):
        """Mirror of the orchestrator's usage: keys saved during a run are added
        to an in-memory set; a fresh load (next run, same day) must agree."""
        in_memory = set()
        for key in ("reply_acc1_1", "like_acc1_2", "repost_acc1_3"):
            file_handler.save_processed_action_key(key, timestamp=_now_utc_iso())
            in_memory.add(key)

        assert file_handler.load_processed_action_keys() == in_memory

    def test_duplicate_saves_stay_a_single_dedup_entry(self, file_handler):
        key = "reply_acc1_123"
        file_handler.save_processed_action_key(key, timestamp=_now_utc_iso())
        file_handler.save_processed_action_key(key, timestamp=_now_utc_iso())
        # On disk twice (append-only log) but dedups to one set member.
        assert file_handler.load_processed_action_keys() == {key}
