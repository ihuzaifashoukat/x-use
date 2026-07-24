"""MetricsRecorder robustness: atomic summary flushes and corrupt-file
recovery.

Two pinned behaviors:
1. _flush_summary writes via a temp file + os.replace, so a crash mid-flush
   can never leave a truncated summary that silently zeroes the counters.
2. _load_summary validates the shape (a dict whose "counters" is a dict of
   ints); wrong-shape-but-valid JSON resets to defaults with a warning
   instead of crashing increment()/mark_run_* with KeyError/TypeError.

PROJECT_ROOT is monkeypatched to tmp_path — the real data/ and logs/ dirs
are never touched.
"""
import json
import logging
import os

import pytest

from xuse.utils.metrics import MetricsRecorder


@pytest.fixture
def metrics_root(tmp_path, monkeypatch):
    monkeypatch.setattr("xuse.utils.metrics.PROJECT_ROOT", tmp_path)
    return tmp_path


def _recorder(metrics_root, make_config_loader, account_id="acc1") -> MetricsRecorder:
    return MetricsRecorder(account_id=account_id, config_loader=make_config_loader())


def _write_summary(metrics_root, account_id: str, raw: str) -> None:
    metrics_dir = metrics_root / "data" / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    (metrics_dir / f"{account_id}.json").write_text(raw, encoding="utf-8")


# ---------------------------------------------------------------------------
# Shape validation on load
# ---------------------------------------------------------------------------


def test_corrupt_but_valid_json_resets_to_defaults_with_warning(
        metrics_root, make_config_loader, caplog):
    _write_summary(metrics_root, "acc1", '["not", "a", "dict"]')

    with caplog.at_level(logging.WARNING, logger="xuse.utils.metrics"):
        recorder = _recorder(metrics_root, make_config_loader)

    assert recorder.summary["account_id"] == "acc1"
    assert recorder.summary["counters"]["posts"] == 0
    assert any(record.levelno >= logging.WARNING for record in caplog.records)
    # And the recorder is fully usable afterwards — no KeyError/TypeError.
    recorder.increment("posts")
    assert recorder.summary["counters"]["posts"] == 1


def test_missing_counters_key_resets_to_defaults_with_warning(
        metrics_root, make_config_loader, caplog):
    _write_summary(metrics_root, "acc1", "{}")

    with caplog.at_level(logging.WARNING, logger="xuse.utils.metrics"):
        recorder = _recorder(metrics_root, make_config_loader)

    assert recorder.summary["counters"]["posts"] == 0
    assert any(record.levelno >= logging.WARNING for record in caplog.records)
    recorder.increment("errors")  # must not raise KeyError: 'counters'
    assert recorder.summary["counters"]["errors"] == 1


def test_non_integer_counter_values_reset_to_defaults_with_warning(
        metrics_root, make_config_loader, caplog):
    _write_summary(
        metrics_root, "acc1",
        json.dumps({"account_id": "acc1", "counters": {"posts": "lots"}}))

    with caplog.at_level(logging.WARNING, logger="xuse.utils.metrics"):
        recorder = _recorder(metrics_root, make_config_loader)

    assert recorder.summary["counters"]["posts"] == 0
    assert any(record.levelno >= logging.WARNING for record in caplog.records)


def test_truncated_json_recovers_to_defaults_with_warning(
        metrics_root, make_config_loader, caplog):
    _write_summary(metrics_root, "acc1", '{"counters": {"posts": 7')

    with caplog.at_level(logging.WARNING, logger="xuse.utils.metrics"):
        recorder = _recorder(metrics_root, make_config_loader)

    assert recorder.summary["counters"]["posts"] == 0
    assert any(record.levelno >= logging.WARNING for record in caplog.records)


def test_valid_summary_is_preserved(metrics_root, make_config_loader):
    recorder = _recorder(metrics_root, make_config_loader)
    recorder.increment("posts", by=3)
    recorder.increment("likes", by=2)

    reloaded = _recorder(metrics_root, make_config_loader)
    assert reloaded.summary["counters"]["posts"] == 3
    assert reloaded.summary["counters"]["likes"] == 2


# ---------------------------------------------------------------------------
# Atomic flush
# ---------------------------------------------------------------------------


def test_flush_goes_through_temp_file_and_os_replace(metrics_root, make_config_loader, monkeypatch):
    recorder = _recorder(metrics_root, make_config_loader)
    replacements = []
    real_replace = os.replace

    def spy_replace(src, dst):
        replacements.append((str(src), str(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr("xuse.utils.metrics.os.replace", spy_replace)

    recorder.increment("posts")

    assert replacements, "summary flush must use os.replace for atomicity"
    src, dst = replacements[-1]
    assert src.endswith(".tmp")
    assert dst.endswith("acc1.json")


def test_no_temp_file_is_left_behind_after_flush(metrics_root, make_config_loader):
    recorder = _recorder(metrics_root, make_config_loader)
    recorder.increment("posts")
    recorder.mark_run_start()
    recorder.mark_run_finish()

    metrics_dir = metrics_root / "data" / "metrics"
    assert list(metrics_dir.glob("*.tmp")) == []
    summary = json.loads((metrics_dir / "acc1.json").read_text(encoding="utf-8"))
    assert summary["counters"]["posts"] == 1
    assert summary["last_run_started_at"] is not None
    assert summary["last_run_finished_at"] is not None
