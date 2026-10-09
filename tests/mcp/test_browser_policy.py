"""Durable budgets, concurrency and uncertainty without any live account."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from xuse.mcp.safety import PolicyError, SafetyStore


def store(path, clock=lambda: 100000, **settings):
    return SafetyStore(path, {"read_interval_seconds": 0, "write_interval_seconds": 0,
                              "max_actions_per_minute": 100, **settings}, clock=clock)


def test_failed_attempts_and_daily_budget_survive_restart(tmp_path):
    path = tmp_path / "safety.sqlite3"
    policy = store(path, daily_caps={"message": 1})
    action = policy.reserve("account", "message", "hash1")
    policy.finish("account", action, "uncertain")
    restored = store(path, daily_caps={"message": 1})
    with pytest.raises(PolicyError) as blocked:
        restored.reserve("account", "message", "hash2")
    assert blocked.value.reason == "daily_budget"
    assert restored.status("account")["daily_used"]["message"] == 1
    assert restored.reserve("other_account", "message", "hash2")


def test_budget_reservation_is_atomic_between_clients(tmp_path):
    path = tmp_path / "shared.sqlite3"
    # Initialize schema before racing independent clients.
    store(path).status("account")

    def reserve(index):
        try:
            return store(path, daily_caps={"post": 1}).reserve("account", "post", str(index))
        except PolicyError:
            return None

    with ThreadPoolExecutor(max_workers=8) as workers:
        actions = list(workers.map(reserve, range(8)))
    assert len([a for a in actions if a]) == 1


def test_uncertain_action_requires_explicit_reconciliation(tmp_path):
    policy = store(tmp_path / "s.sqlite3")
    action = policy.reserve("account", "message", "payload-hash",
                            {"draft_id": "draft", "campaign_id": "campaign", "lead_id": "lead"})
    with pytest.raises(PolicyError) as blocked:
        policy.reserve("account", "message", "payload-hash")
    assert blocked.value.action_id == action
    assert policy.reference("account", action)["draft_id"] == "draft"
    policy.reconcile("account", action, "not_sent")
    assert policy.reserve("account", "message", "payload-hash") != action


def test_confirmed_action_can_repair_bookkeeping_but_never_reopen(tmp_path):
    policy = store(tmp_path / "s.sqlite3")
    action = policy.reserve("account", "post", "hash")
    policy.finish("account", action, "succeeded")
    policy.reconcile("account", action, "succeeded")
    policy.reconcile("account", action, "succeeded")
    with pytest.raises(PolicyError):
        policy.reconcile("account", action, "not_sent")
    with pytest.raises(PolicyError):
        policy.reserve("account", "post", "hash")


def test_pause_persists_and_only_pin_unlock_is_allowed(tmp_path):
    path = tmp_path / "s.sqlite3"
    policy = store(path)
    policy.pause("account", "pin_required")
    restored = store(path)
    with pytest.raises(PolicyError):
        restored.reserve("account", "read")
    assert restored.reserve("account", "unlock")
    restored.pause("account", "manual_pause")
    with pytest.raises(PolicyError):
        restored.reserve("account", "unlock")
    restored.resume("account")
    assert restored.reserve("account", "read")


def test_intervals_and_minute_budget_report_retry_after(tmp_path):
    now = [100000]
    policy = store(tmp_path / "s.sqlite3", clock=lambda: now[0], write_interval_seconds=90,
                   max_actions_per_minute=2)
    policy.reserve("account", "post", "one")
    with pytest.raises(PolicyError) as blocked:
        policy.reserve("account", "reply", "two")
    assert blocked.value.reason == "cooldown"
    assert blocked.value.retry_after_seconds == 90
    policy.reserve("account", "read")
    with pytest.raises(PolicyError) as blocked:
        policy.reserve("account", "read")
    assert blocked.value.reason == "minute_budget"
    now[0] += 90
    assert policy.reserve("account", "reply", "two")


def test_recovery_retains_newer_pause_even_with_same_reason_and_timestamp(tmp_path):
    policy = store(tmp_path / "s.sqlite3")
    policy.pause("account", "manual_pause")
    previous = policy.pause_token("account")
    policy.pause("account", "manual_pause")
    assert policy.resume_if_unchanged("account", previous) is False
    assert policy.status("account")["paused"]
    assert policy.resume_if_unchanged("account", policy.pause_token("account")) is True
    assert not policy.status("account")["paused"]


def test_recovery_without_initial_pause_cannot_clear_new_pause(tmp_path):
    policy = store(tmp_path / "s.sqlite3")
    previous = policy.pause_token("account")
    policy.pause("account", "manual_pause")
    assert policy.resume_if_unchanged("account", previous) is False


def test_automatic_challenge_does_not_replace_operator_pause(tmp_path):
    policy = store(tmp_path / "s.sqlite3")
    policy.pause("account", "manual_pause")
    previous = policy.pause_token("account")
    policy.pause("account", "pin_required", preserve_manual=True)
    assert policy.pause_token("account") == previous
    assert policy.status("account")["pause"]["reason"] == "manual_pause"
    with pytest.raises(PolicyError):
        policy.reserve("account", "unlock")


@pytest.mark.parametrize("cfg", [{"write_interval_seconds": float("nan")},
                                  {"read_interval_seconds": -1},
                                  {"daily_caps": {"message": -1}},
                                  {"max_actions_per_minute": False}])
def test_invalid_limits_fail_closed(tmp_path, cfg):
    with pytest.raises(ValueError):
        SafetyStore(tmp_path / "s.sqlite3", cfg)
