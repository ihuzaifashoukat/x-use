"""Writer hardening (bug-hunt cluster: config persistence).

- Backup failures must surface as ConfigWriteError (never a raw OSError) and
  must block the atomic write: the guarantee is "backup before replace".
- Unknown keys inside action_config must be rejected, naming the key —
  pydantic ignores them by default, so typos would otherwise persist and be
  silently ignored forever.
- The proxy field must be validated at the write gate with the same rules as
  the proxy pool tools (http/https/socks4/socks5 + host, ${VAR} structural),
  plus pool:<name> references which only accounts may carry.

tmp paths only; no browser, no network.
"""
import json

import pytest

from xuse.core.config_writer import AccountsConfigWriter, ConfigWriteError


def write_accounts(path, accounts):
    path.write_text(json.dumps(accounts), encoding="utf-8")


def read_accounts(path):
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Backup failures: wrapped in ConfigWriteError, original untouched.
# ---------------------------------------------------------------------------

def test_backup_failure_raises_config_write_error_and_skips_write(tmp_path):
    target = tmp_path / "accounts.json"
    original = [{"account_id": "acc1"}]
    write_accounts(target, original)
    # backups_dir exists as a FILE -> mkdir/copy2 fails with an OSError.
    (tmp_path / "backups").write_text("not a dir", encoding="utf-8")
    writer = AccountsConfigWriter(target)

    with pytest.raises(ConfigWriteError, match="[Bb]ackup"):
        writer.mutate(lambda accs: [*accs, {"account_id": "acc2"}])

    # The failed backup must block the atomic write entirely.
    assert read_accounts(target) == original


def test_backup_failure_never_escapes_as_raw_oserror(tmp_path):
    target = tmp_path / "accounts.json"
    write_accounts(target, [{"account_id": "acc1"}])
    (tmp_path / "backups").write_text("not a dir", encoding="utf-8")
    writer = AccountsConfigWriter(target)

    try:
        writer.mutate(lambda accs: accs)
    except ConfigWriteError:
        pass  # the documented contract
    except OSError as e:
        pytest.fail(f"backup failure escaped as raw OSError: {e!r}")


def test_working_backup_still_allows_mutation(tmp_path):
    target = tmp_path / "accounts.json"
    write_accounts(target, [{"account_id": "acc1"}])
    writer = AccountsConfigWriter(target)
    updated = writer.mutate(lambda accs: [*accs, {"account_id": "acc2"}])
    assert [a["account_id"] for a in read_accounts(target)] == ["acc1", "acc2"]
    assert len(list((tmp_path / "backups").glob("accounts-*.json"))) == 1


# ---------------------------------------------------------------------------
# action_config: unknown keys rejected (writer-side check; model unchanged).
# ---------------------------------------------------------------------------

def test_unknown_action_config_key_rejected_and_named(tmp_path):
    target = tmp_path / "accounts.json"
    original = [{"account_id": "acc1"}]
    write_accounts(target, original)
    writer = AccountsConfigWriter(target)

    with pytest.raises(ConfigWriteError, match="bogus_knob"):
        writer.mutate(lambda accs: [*accs, {
            "account_id": "acc2",
            "action_config": {"max_likes_per_run": 3, "bogus_knob": True},
        }])

    assert read_accounts(target) == original


def test_typo_action_config_key_rejected(tmp_path):
    """The hunt scenario: relevance_threshold_liks (typo) must not persist."""
    target = tmp_path / "accounts.json"
    write_accounts(target, [])
    writer = AccountsConfigWriter(target)

    with pytest.raises(ConfigWriteError, match="relevance_threshold_liks"):
        writer.mutate(lambda accs: [{
            "account_id": "acc1",
            "action_config": {
                "max_likes_per_run": 3,
                "min_delay_between_actions_seconds": 300,
                "relevance_threshold_liks": 0.9,
            },
        }])

    assert read_accounts(target) == []


def test_known_action_config_keys_accepted(tmp_path):
    target = tmp_path / "accounts.json"
    write_accounts(target, [])
    writer = AccountsConfigWriter(target)

    updated = writer.mutate(lambda accs: [{
        "account_id": "acc1",
        "action_config": {
            "max_likes_per_run": 3,
            "min_delay_between_actions_seconds": 0,
            "relevance_threshold_likes": 0.9,
            "llm_settings_for_post": {"max_tokens": 500},
        },
    }])

    assert updated[0]["action_config"]["max_likes_per_run"] == 3
    assert read_accounts(target)[0]["action_config"]["relevance_threshold_likes"] == 0.9


# ---------------------------------------------------------------------------
# proxy: same validation as the MCP proxy tools, at the write gate.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_proxy", [
    "definitely not a url",   # no scheme at all
    "ftp://1.2.3.4:21",       # unsupported scheme
    "http://",                # hostless
])
def test_malformed_proxy_rejected(tmp_path, bad_proxy):
    target = tmp_path / "accounts.json"
    write_accounts(target, [])
    writer = AccountsConfigWriter(target)

    with pytest.raises(ConfigWriteError):
        writer.mutate(lambda accs: [{"account_id": "acc1", "proxy": bad_proxy}])

    assert read_accounts(target) == []


@pytest.mark.parametrize("good_proxy", [
    "http://user:pass@1.2.3.4:8080",
    "https://1.2.3.4:8443",
    "socks4://9.9.9.9:1080",
    "socks5://9.9.9.9:1080",
    "http://u:${RESI_PASS}@h:8080",  # ${VAR} passes a structural check only
    "pool:resi",                      # pool references are account-only, valid
])
def test_valid_proxy_accepted(tmp_path, good_proxy):
    target = tmp_path / "accounts.json"
    write_accounts(target, [])
    writer = AccountsConfigWriter(target)

    updated = writer.mutate(lambda accs: [{"account_id": "acc1", "proxy": good_proxy}])

    assert updated[0]["proxy"] == good_proxy
    assert read_accounts(target)[0]["proxy"] == good_proxy


def test_empty_pool_reference_rejected(tmp_path):
    target = tmp_path / "accounts.json"
    write_accounts(target, [])
    writer = AccountsConfigWriter(target)

    with pytest.raises(ConfigWriteError, match="pool"):
        writer.mutate(lambda accs: [{"account_id": "acc1", "proxy": "pool:"}])

    assert read_accounts(target) == []
