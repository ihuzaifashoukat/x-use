"""Queue models: defaults and tolerant config parsing."""
from xuse.queue.models import AutoDrainConfig, QueueConfig, QueuedAction


def test_queued_action_defaults():
    item = QueuedAction(queue_id="q1", account="acc1", action="like",
                        payload={}, dedup_key="like_acc1_1", created_at="2026-07-24T00:00:00+00:00")
    assert item.status == "pending" and item.attempts == 0
    assert item.not_before is None and item.completed_at is None


def test_queue_config_defaults_are_conservative():
    cfg = QueueConfig()
    assert cfg.max_actions_per_run == 5
    assert (cfg.min_delay_seconds, cfg.max_delay_seconds) == (90, 240)
    assert cfg.max_attempts == 2
    assert cfg.daily_caps == {"post": 5, "reply": 15, "like": 30, "retweet": 10}
    assert cfg.auto_drain.enabled is False
    assert cfg.auto_drain.interval_seconds == 900
    assert cfg.auto_drain.max_actions_per_account == 3


def test_from_settings_parses_full_block():
    cfg = QueueConfig.from_settings({
        "max_actions_per_run": 3,
        "daily_caps": {"like": 10},
        "auto_drain": {"enabled": True, "interval_seconds": 300},
    })
    assert cfg.max_actions_per_run == 3
    assert cfg.daily_caps == {"like": 10}
    assert cfg.auto_drain.enabled is True and cfg.auto_drain.interval_seconds == 300


def test_from_settings_tolerates_garbage():
    assert QueueConfig.from_settings(None) == QueueConfig()
    assert QueueConfig.from_settings("nope") == QueueConfig()
    assert QueueConfig.from_settings({"min_delay_seconds": "not-a-number"}) == QueueConfig()


def test_auto_drain_config_is_independent_per_instance():
    a, b = QueueConfig(), QueueConfig()
    a.daily_caps["like"] = 1
    a.auto_drain.enabled = True
    assert b.daily_caps["like"] == 30 and b.auto_drain.enabled is False
