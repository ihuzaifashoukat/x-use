"""Synthetic cookie exports only; never reads browser or account credentials."""
import copy
import json

import pytest

from xuse.browser import cookies
from xuse.browser.errors import SessionError


NOW = 1_800_000_000


def credentials():
    return [
        {"name": "auth_token", "value": "synthetic-auth", "domain": ".x.com", "secure": True},
        {"name": "ct0", "value": "synthetic-csrf", "domain": ".x.com", "secure": True},
    ]


@pytest.mark.parametrize("with_origins", [False, True])
def test_export_envelope_imports_cookies_only_without_mutating_input(with_origins):
    payload = {"cookies": credentials()}
    if with_origins:
        payload["origins"] = [{"origin": "https://elsewhere.invalid", "localStorage": [
            {"name": "private", "value": "do-not-import"}]}]
    before = copy.deepcopy(payload)
    result = cookies.normalize_cookies(payload, now=NOW)
    assert [item["name"] for item in result] == ["auth_token", "ct0"]
    assert "do-not-import" not in json.dumps(result)
    assert payload == before


@pytest.mark.parametrize("domain,host_only,expected", [
    (".x.com", True, "x.com"), ("x.com", True, "x.com"),
    ("x.com", False, ".x.com"), (".x.com", False, ".x.com"),
])
def test_explicit_host_only_scope_survives_normalization(domain, host_only, expected):
    payload = credentials()
    payload[0].update(domain=domain, hostOnly=host_only)
    assert cookies.normalize_cookies(payload, now=NOW)[0]["domain"] == expected


@pytest.mark.parametrize("domain", ["x.com", ".x.com"])
def test_missing_host_only_metadata_preserves_exported_scope(domain):
    payload = credentials()
    payload[0]["domain"] = domain
    assert cookies.normalize_cookies(payload, now=NOW)[0]["domain"] == domain


@pytest.mark.parametrize("metadata", [
    {"partitionKey": "https://x.com"}, {"partitionKey": {"topLevelSite": "https://x.com"}},
    {"partitionKey": {}}, {"partitioned": True}, {"partitioned": "false"},
    {"firstPartyDomain": "elsewhere.invalid"},
])
def test_partitioned_cookies_are_never_silently_promoted_to_unpartitioned(metadata):
    payload = credentials()
    payload[0].update(metadata)
    with pytest.raises(SessionError, match="invalid_cookies"):
        cookies.normalize_cookies(payload, now=NOW)


def test_explicit_unpartitioned_export_metadata_is_supported():
    payload = credentials()
    payload[0].update(partitionKey=None, partitioned=False, firstPartyDomain="")
    assert "partitionKey" not in cookies.normalize_cookies(payload, now=NOW)[0]


@pytest.mark.parametrize("expiration", [
    {"expires": NOW + 100, "expirationDate": NOW + 200},
    {"expires": NOW + 100, "expiry": NOW - 1},
    {"expires": -1, "expirationDate": NOW + 100},
    {"expires": None, "expirationDate": NOW + 100},
    {"expires": NOW + 100, "expiry": True},
])
def test_conflicting_expiration_fields_reject_the_entire_import(expiration):
    payload = credentials()
    payload[0].update(expiration)
    with pytest.raises(SessionError, match="invalid_cookies"):
        cookies.normalize_cookies(payload, now=NOW)


def test_equivalent_expiration_aliases_and_session_sentinel_are_accepted():
    payload = credentials()
    payload[0].update(expires=NOW + 100, expirationDate=float(NOW + 100), expiry=NOW + 100)
    payload[1]["expires"] = -1
    result = cookies.normalize_cookies(payload, now=NOW)
    assert result[0]["expires"] == float(NOW + 100)
    assert "expires" not in result[1]


def test_expired_credential_cannot_hide_behind_session_flag():
    payload = credentials()
    payload[0].update(session=True, expirationDate=NOW - 1)
    with pytest.raises(SessionError, match="expired_credentials"):
        cookies.normalize_cookies(payload, now=NOW)


def test_unspecified_samesite_preserves_browser_default_without_forcing_none():
    payload = credentials()
    payload[0]["sameSite"] = "unspecified"
    result = cookies.normalize_cookies(payload, now=NOW)
    assert "sameSite" not in result[0]


@pytest.mark.parametrize("extra", [
    {"domain": "x.com"}, {"domain": ".x.com", "hostOnly": True},
])
def test_duplicate_effective_cookie_scope_skips_every_copy_of_an_optional_cookie(extra):
    payload = credentials() + [{"name": "preference", "value": "one", "domain": ".x.com"},
                               {"name": "preference", "value": "two", **extra}]
    report = cookies.inspect_cookies(payload, now=NOW)
    assert [c["name"] for c in report.cookies] == ["auth_token", "ct0"]
    assert report.skipped == [("preference", "duplicate")]


def test_expired_optional_cookie_does_not_hide_invalid_later_entry():
    payload = credentials() + [{"name": "old", "value": "one", "expires": NOW - 1},
                               {"name": "foreign", "value": "two", "domain": "evilx.com"}]
    report = cookies.inspect_cookies(payload, now=NOW)
    assert report.skipped == [("foreign", "not_x_domain")]
    assert [c["name"] for c in report.cookies] == ["auth_token", "ct0"]


@pytest.mark.parametrize("path", ["/safe\x7fpath", "/safe\npath"])
def test_invalid_cookie_path_is_skipped_for_optional_cookies(path):
    payload = credentials() + [{"name": "preference", "value": "one", "path": path}]
    assert cookies.inspect_cookies(payload, now=NOW).skipped == [("preference", "invalid_path")]


@pytest.mark.parametrize("path", ["/safe\x7fpath", "/safe\npath"])
def test_invalid_credential_path_is_rejected(path):
    payload = credentials()
    payload[0]["path"] = path
    with pytest.raises(SessionError, match="invalid_cookies"):
        cookies.normalize_cookies(payload, now=NOW)


@pytest.mark.parametrize("envelope", [
    {"cookies": credentials(), "origins": {}},
    {"cookies": credentials(), "cookie": []},
    {"cookies": []}, {"cookies": "invalid"},
])
def test_malformed_or_unknown_envelope_is_rejected(envelope):
    with pytest.raises(SessionError, match="invalid_cookies"):
        cookies.normalize_cookies(envelope, now=NOW)


def test_byte_parser_accepts_bom_and_storage_state_and_discards_origins():
    data = {"cookies": credentials(), "origins": [{"origin": "https://x.com", "localStorage": []}]}
    result = cookies.parse_cookie_export(json.dumps(data).encode("utf-8-sig"), now=NOW)
    assert result == cookies.normalize_cookies(credentials(), now=NOW)


@pytest.mark.parametrize("data", [
    b'[{"name":"auth_token","name":"ct0","value":"sensitive"}]',
    b'{"cookies":[],"cookies":[]}', b'{"broken":"sensitive"', b'\xff',
    b"[" * 2000 + b"]" * 2000,
], ids=["duplicate-cookie-field", "duplicate-envelope", "malformed-json", "invalid-utf8", "excessive-nesting"])
def test_byte_parser_failures_are_credential_safe(data):
    with pytest.raises(SessionError, match="invalid_cookies") as caught:
        cookies.parse_cookie_export(data, now=NOW)
    assert "sensitive" not in str(caught.value)
    assert caught.value.__cause__ is None


def test_byte_parser_enforces_size_limit():
    data = b" " * (cookies.MAX_COOKIE_FILE_BYTES + 1)
    with pytest.raises(SessionError, match="invalid_cookies"):
        cookies.parse_cookie_export(data, now=NOW)


def test_file_import_rejects_duplicate_json_fields_without_path_or_value_leak(make_config_loader, tmp_path):
    payload = json.dumps(credentials()).replace('"synthetic-auth"', '"discarded-secret", "value": "hidden-secret"')
    path = tmp_path / "private-export.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(SessionError, match="invalid_cookies") as caught:
        cookies.load_account_cookies({"cookie_file_path": str(path)}, make_config_loader())
    assert str(path) not in str(caught.value)
    assert "secret" not in str(caught.value)


def test_file_import_accepts_storage_state_without_importing_local_storage(make_config_loader, tmp_path):
    payload = {"cookies": credentials(), "origins": [{"origin": "https://x.com", "localStorage": [
        {"name": "key", "value": "private-local-storage"}]}]}
    path = tmp_path / "storage-state.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = cookies.load_account_cookies({"cookie_file_path": str(path)}, make_config_loader())
    assert len(result) == 2
    assert "private-local-storage" not in json.dumps(result)
