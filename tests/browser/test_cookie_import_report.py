"""Synthetic exports only: unusable optional cookies are skipped and named (#38)."""
import json

import pytest

from xuse.browser import cookies
from xuse.browser.errors import SessionError

NOW = 1_800_000_000
SECRET = "synthetic-secret-value"


def base():
    return [
        {"name": "auth_token", "value": "synthetic-auth", "domain": ".x.com", "secure": True,
         "httpOnly": True},
        {"name": "ct0", "value": "synthetic-csrf", "domain": ".x.com", "secure": True,
         "sameSite": "lax"},
    ]


def optional(**fields):
    return {"name": "lang", "value": SECRET, "domain": ".x.com", **fields}


def names(report):
    return {c["name"] for c in report.cookies}


@pytest.mark.parametrize("cookie,expected", [
    (optional(sameSite="no_restriction", secure=False), ("lang", "insecure_same_site_none")),
    (optional(partitionKey={"topLevelSite": "https://x.com"}), ("lang", "partitioned")),
    (optional(domain=".twitter.com"), ("lang", "not_x_domain")),
    (optional(domain="api.x.com"), ("lang", "not_x_domain")),
    (optional(name="g_state:x"), ("<invalid name>", "invalid_name")),
    (optional(expirationDate="1900000000"), ("lang", "invalid_expiry")),
    (optional(value="a\tb" + SECRET), ("lang", "invalid_value")),
    (optional(path=""), ("lang", "invalid_path")),
    (optional(firstPartyDomain="x.com"), ("lang", "partitioned")),
    (optional(secure="true"), ("lang", "invalid_flags")),
    (optional(sameSite="bogus"), ("lang", "invalid_same_site")),
], ids=["insecure-none", "partition-key", "twitter", "subdomain", "bad-name", "string-expiry",
        "control-char", "empty-path", "first-party", "string-secure", "bad-samesite"])
def test_unusable_optional_cookie_is_skipped_and_named(cookie, expected):
    report = cookies.inspect_cookies(base() + [cookie], now=NOW)
    assert names(report) == {"auth_token", "ct0"}
    assert report.skipped == [expected]
    assert SECRET not in repr(report)
    assert SECRET not in json.dumps(report.skipped)


def test_host_only_and_domain_variants_are_both_skipped_once():
    payload = base() + [optional(domain="x.com", value="one" + SECRET),
                        optional(domain=".x.com", value="two" + SECRET)]
    report = cookies.inspect_cookies(payload, now=NOW)
    assert names(report) == {"auth_token", "ct0"}
    assert report.skipped == [("lang", "duplicate")]


def test_duplicate_optional_reported_once():
    payload = base() + [optional()] * 3
    report = cookies.inspect_cookies(payload, now=NOW)
    assert report.skipped == [("lang", "duplicate")]
    assert names(report) == {"auth_token", "ct0"}


def test_all_optional_bad_keeps_credentials():
    payload = base() + [optional(domain=".twitter.com"), optional(name="a b"),
                        optional(path="x"), optional(partitioned=True)]
    report = cookies.inspect_cookies(payload, now=NOW)
    assert [c["name"] for c in report.cookies] == ["auth_token", "ct0"]
    assert [rule for _, rule in report.skipped] == [
        "not_x_domain", "invalid_name", "invalid_path", "partitioned"]


def test_unsafe_name_is_not_echoed():
    long_name = "x" * 65
    payload = base() + [optional(name=long_name + SECRET, domain="evil.test"),
                        optional(name=long_name, domain="evil.test"),
                        optional(name=None), optional(name="bad name" + SECRET)]
    report = cookies.inspect_cookies(payload, now=NOW)
    assert report.skipped == [("<invalid name>", "not_x_domain"), ("<invalid name>", "not_x_domain"),
                              ("<invalid name>", "invalid_name"), ("<invalid name>", "invalid_name")]
    assert SECRET not in repr(report)


def test_expired_optional_is_dropped_not_reported():
    report = cookies.inspect_cookies(base() + [optional(expirationDate=NOW - 1)], now=NOW)
    assert report.skipped == []
    assert names(report) == {"auth_token", "ct0"}


def test_wrappers_return_cookies_only():
    payload = base() + [optional(domain=".twitter.com")]
    assert cookies.normalize_cookies(payload, now=NOW) == cookies.inspect_cookies(payload, now=NOW).cookies
    data = json.dumps(payload).encode()
    assert cookies.parse_cookie_export(data, now=NOW) == cookies.parse_cookie_export_report(data, now=NOW).cookies
    assert cookies.parse_cookie_export_report(data, now=NOW).skipped == [("lang", "not_x_domain")]


def test_load_account_cookies_report_inline_and_file(make_config_loader, tmp_path):
    payload = base() + [optional(domain=".twitter.com")]
    loader = make_config_loader()
    inline = cookies.load_account_cookies_report({"cookies": payload}, loader)
    assert inline.skipped == [("lang", "not_x_domain")]
    path = tmp_path / "export.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    from_file = cookies.load_account_cookies_report({"cookie_file_path": str(path)}, loader)
    assert from_file.skipped == [("lang", "not_x_domain")]
    assert len(cookies.load_account_cookies({"cookie_file_path": str(path)}, loader)) == 2


def hard(payload):
    with pytest.raises(SessionError) as caught:
        cookies.inspect_cookies(payload, now=NOW)
    assert SECRET not in str(caught.value) and SECRET not in caught.value.detail
    return caught.value


def test_credential_on_wrong_path_is_hard_failure():
    payload = base()
    payload[1]["path"] = "/api"
    error = hard(payload)
    assert error.reason == "invalid_cookies"
    assert error.detail == "ct0: invalid_path"


def test_insecure_auth_token_detail():
    payload = base()
    payload[0]["secure"] = False
    error = hard(payload)
    assert (error.reason, error.detail) == ("invalid_cookies", "auth_token: insecure")


def test_credential_rule_details():
    cases = [
        ({"value": ""}, "invalid_cookies", "auth_token: empty_value"),
        ({"value": SECRET + "\n"}, "invalid_cookies", "auth_token: invalid_value"),
        ({"domain": "evil.test"}, "invalid_cookies", "auth_token: not_x_domain"),
        ({"partitioned": True}, "invalid_cookies", "auth_token: partitioned"),
        ({"sameSite": "nope"}, "invalid_cookies", "auth_token: invalid_same_site"),
        ({"expirationDate": float("nan")}, "invalid_cookies", "auth_token: invalid_expiry"),
        ({"expirationDate": 1}, "expired_credentials", "auth_token: expired"),
    ]
    for change, reason, detail in cases:
        payload = base()
        payload[0].update(change)
        error = hard(payload)
        assert (error.reason, error.detail) == (reason, detail)


def test_duplicate_credential_is_hard_failure():
    payload = base() + [dict(base()[0], domain="x.com")]
    error = hard(payload)
    assert (error.reason, error.detail) == ("invalid_cookies", "auth_token: duplicate")


def test_missing_credentials_name_only_missing_ones():
    error = hard(base()[:1])
    assert (error.reason, error.detail) == ("missing_cookies", "missing: ct0")
    error = hard([optional()])
    assert error.detail == "missing: auth_token, ct0"


def test_detail_is_not_in_message_and_defaults_empty():
    assert SessionError("invalid_cookies").detail == ""
    error = SessionError("invalid_cookies", detail="ct0: insecure")
    assert str(error) == str(SessionError("invalid_cookies"))
    assert "ct0" not in str(error)


@pytest.mark.parametrize("payload", [
    "text", {}, {"cookies": []}, {"cookies": [1]}, {"cookies": base(), "extra": 1},
    {"cookies": base(), "origins": {}}, [], [dict(base()[0])] * 257,
])
def test_file_level_problems_still_raise(payload):
    assert hard(payload).reason == "invalid_cookies"


@pytest.mark.parametrize("data", [b"\xff", b"{broken", b'{"a":1,"a":2}',
                                  b" " * (cookies.MAX_COOKIE_FILE_BYTES + 1)])
def test_byte_level_problems_still_raise(data):
    with pytest.raises(SessionError, match="invalid_cookies"):
        cookies.parse_cookie_export_report(data, now=NOW)
