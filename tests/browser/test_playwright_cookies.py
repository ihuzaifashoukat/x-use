import json
import time

import pytest

from xuse.browser.cookies import load_account_cookies, normalize_cookies
from xuse.browser.errors import SessionError


def credentials():
    return [{"name": "auth_token", "value": "test-auth-only", "domain": ".x.com", "secure": True,
             "httpOnly": True, "sameSite": "no_restriction", "expirationDate": time.time() + 1000},
            {"name": "ct0", "value": "test-csrf-only", "domain": ".x.com", "secure": True,
             "sameSite": "lax", "path": "/"}]


def test_export_normalization_drops_metadata_without_altering_credentials():
    payload = credentials()
    payload[0].update(hostOnly=False, storeId=None, session=False)
    normalized = normalize_cookies(payload)
    assert normalized[0]["value"] == "test-auth-only"
    assert normalized[0]["sameSite"] == "None"
    assert normalized[1]["sameSite"] == "Lax"
    assert "expires" in normalized[0]
    assert set(normalized[0]) <= {"name", "value", "domain", "path", "secure", "httpOnly", "sameSite", "expires"}


@pytest.mark.parametrize("domain", ["evilx.com", "x.com.evil.test", ".twitter.com", "sub.x.com", None])
def test_domain_must_be_exact_x_domain(domain):
    payload = credentials()
    payload[0]["domain"] = domain
    with pytest.raises(SessionError, match="invalid_cookies"):
        normalize_cookies(payload)


@pytest.mark.parametrize("change", [
    {"secure": "true"}, {"sameSite": "unknown"}, {"expirationDate": float("nan")},
    {"value": "secret\ncredential"}, {"path": "x/"}, {"name": "auth token"},
    {"secure": False}, {"value": ""},
])
def test_malformed_credential_fails_entire_import(change):
    payload = credentials()
    payload[0].update(change)
    with pytest.raises(SessionError):
        normalize_cookies(payload)


def test_expired_auth_is_rejected_but_expired_analytics_is_dropped():
    payload = credentials()
    payload.append({"name": "analytics", "value": "old", "expirationDate": 1})
    assert len(normalize_cookies(payload)) == 2
    payload[0]["expirationDate"] = 1
    with pytest.raises(SessionError, match="expired_credentials"):
        normalize_cookies(payload)


@pytest.mark.parametrize("value", [10 ** 400, -(10 ** 400)])
def test_oversized_integer_expiration_is_a_safe_cookie_validation_error(value):
    payload = credentials()
    payload[0]["expirationDate"] = value
    with pytest.raises(SessionError, match="invalid_cookies"):
        normalize_cookies(payload)


def test_missing_and_duplicate_credentials_fail_closed():
    with pytest.raises(SessionError, match="missing_cookies"):
        normalize_cookies(credentials()[:1])
    with pytest.raises(SessionError, match="invalid_cookies"):
        normalize_cookies(credentials() + credentials()[:1])
    payload = credentials()
    payload.append(dict(payload[0], domain="x.com"))
    with pytest.raises(SessionError, match="invalid_cookies"):
        normalize_cookies(payload)


def test_canonical_cookie_path_resolves_relative_to_account_configuration(make_config_loader, tmp_path):
    loader = make_config_loader(accounts=[{"account_id": "a"}])
    (tmp_path / "test-cookies.json").write_text(json.dumps(credentials()), encoding="utf-8")
    assert len(load_account_cookies({"cookie_file_path": "test-cookies.json"}, loader)) == 2


def test_corrupt_file_errors_never_expose_credentials_or_path(make_config_loader, tmp_path):
    loader = make_config_loader()
    path = tmp_path / "private-credential-file.json"
    path.write_text('secret-auth-value-not-json', encoding="utf-8")
    with pytest.raises(SessionError) as error:
        load_account_cookies({"cookie_file_path": str(path)}, loader)
    assert "secret-auth" not in str(error.value)
    assert str(path) not in str(error.value)


def test_inline_malformed_cookies_cannot_fall_back_to_valid_file(make_config_loader, tmp_path):
    loader = make_config_loader()
    path = tmp_path / "valid.json"
    path.write_text(json.dumps(credentials()), encoding="utf-8")
    with pytest.raises(SessionError):
        load_account_cookies({"cookies": [], "cookie_file_path": str(path)}, loader)
