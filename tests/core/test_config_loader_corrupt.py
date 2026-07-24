"""ConfigLoader corrupt-file surfacing (bug-hunt cluster: config persistence).

Boot semantics are unchanged: a corrupt file still yields empty defaults and
no exception. But "corrupt" is now surfaced distinctly from "missing/empty":
the loader records a human-readable parse error on ``settings_error`` /
``accounts_error`` and logs a loud error. tmp paths only.
"""
import logging

import pytest

from xuse.core.config_loader import ConfigLoader


def make_files(tmp_path, settings_text=None, accounts_text=None):
    settings_file = tmp_path / "settings.json"
    accounts_file = tmp_path / "accounts.json"
    if settings_text is not None:
        settings_file.write_text(settings_text, encoding="utf-8")
    if accounts_text is not None:
        accounts_file.write_text(accounts_text, encoding="utf-8")
    return settings_file, accounts_file


def test_corrupt_settings_sets_error_attr_and_logs_loudly(tmp_path, caplog):
    settings_file, accounts_file = make_files(
        tmp_path, settings_text='{"api_keys": ,}', accounts_text="[]")

    with caplog.at_level(logging.ERROR, logger="xuse.core.config_loader"):
        loader = ConfigLoader(settings_file=settings_file, accounts_file=accounts_file)

    # Boot semantics unchanged: defaults, no exception.
    assert loader.get_settings() == {}
    assert loader.get_accounts_config() == []
    # ...but the corruption is surfaced on the instance...
    assert loader.settings_error is not None
    assert str(settings_file) in loader.settings_error
    assert loader.accounts_error is None  # the valid file carries no error
    # ...and logged loudly (error level, named file, parse detail).
    loud = [r for r in caplog.records if r.levelno >= logging.ERROR
            and "settings.json" in r.getMessage()]
    assert loud, "expected a loud error log for the corrupt settings file"


def test_corrupt_accounts_sets_error_attr(tmp_path, caplog):
    settings_file, accounts_file = make_files(
        tmp_path, settings_text="{}", accounts_text='[{"account_id": "acc1",]')

    with caplog.at_level(logging.ERROR, logger="xuse.core.config_loader"):
        loader = ConfigLoader(settings_file=settings_file, accounts_file=accounts_file)

    assert loader.get_accounts_config() == []
    assert loader.accounts_error is not None
    assert str(accounts_file) in loader.accounts_error
    assert loader.settings_error is None


def test_wrong_type_json_counts_as_corrupt(tmp_path):
    """Valid JSON of the wrong shape (object instead of array and vice
    versa) previously degraded silently — iterated as string keys, filtered
    out by isinstance guards downstream."""
    settings_file, accounts_file = make_files(
        tmp_path, settings_text="[1, 2, 3]", accounts_text='{"account_id": "acc1"}')

    loader = ConfigLoader(settings_file=settings_file, accounts_file=accounts_file)

    assert loader.get_settings() == {}
    assert loader.get_accounts_config() == []
    assert loader.settings_error is not None
    assert loader.accounts_error is not None


def test_missing_files_leave_error_attrs_none(tmp_path):
    loader = ConfigLoader(settings_file=tmp_path / "nope_settings.json",
                          accounts_file=tmp_path / "nope_accounts.json")

    assert loader.get_settings() == {}
    assert loader.get_accounts_config() == []
    assert loader.settings_error is None
    assert loader.accounts_error is None


def test_valid_files_leave_error_attrs_none(make_config_loader):
    loader = make_config_loader(settings={"a": 1}, accounts=[{"account_id": "x"}])

    assert loader.settings_error is None
    assert loader.accounts_error is None
    assert loader.get_settings() == {"a": 1}
    assert loader.get_accounts_config() == [{"account_id": "x"}]
