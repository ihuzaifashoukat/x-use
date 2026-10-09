"""Only observed owner analytics evidence is accepted by the parser."""
from datetime import datetime

import pytest

from xuse.browser.analytics import parse_account_analytics_snapshot
from xuse.browser.errors import BrowserActionError


def evidence(**changes):
    return {"url": "https://x.com/i/jf/creators/analytics_paywall", "owner_handle": "owner",
            "primary_available": True, "analytics_heading": True, "premium_title": True,
            "premium_explanation": True,
            "upgrade_urls": ["https://x.com/i/premium_sign_up?referring_page=analytics"], **changes}


def test_observed_paywall_returns_unavailable_and_never_zero_metrics():
    result = parse_account_analytics_snapshot(evidence())
    assert result["status"] == "premium_required" and result["available"] is False
    assert result["metrics"] == [] and result["period"] is None
    assert result["owner"]["handle"] == "owner" and result["analytics_scope"] == "signed_in_owner"
    assert result["subscription"] == {"status": "required", "visible_text": "Advanced analytics with X Premium"}
    assert datetime.fromisoformat(result["observed_at"]).tzinfo is not None
    assert result["partial"] and result["coverage"] == "visible_only"


@pytest.mark.parametrize("changes", [
    {"url": "https://x.com/owner/status/123/analytics"},
    {"url": "https://x.com/i/jf/creators/analytics"},
    {"primary_available": False}, {"analytics_heading": False}, {"premium_title": False},
    {"premium_explanation": False}, {"upgrade_urls": []},
    {"upgrade_urls": ["https://evil.test/i/premium_sign_up?referring_page=analytics"]},
    {"upgrade_urls": ["https://x.com/i/premium_sign_up?referring_page=analytics"] * 2},
])
def test_incomplete_or_other_layout_never_fabricates_analytics_or_subscription(changes):
    result = parse_account_analytics_snapshot(evidence(**changes))
    assert result["status"] == "unsupported_dom" and result["metrics"] == []
    assert result["subscription"] == {"status": "unknown", "visible_text": None}


@pytest.mark.parametrize("owner", [None, "@owner", "home", "owner/other", "OWNER", "", "x" * 16])
def test_owner_identity_must_be_exact_valid_handle(owner):
    with pytest.raises(BrowserActionError) as exc:
        parse_account_analytics_snapshot(evidence(owner_handle=owner))
    assert exc.value.reason == "profile_mismatch"


@pytest.mark.parametrize("limit", [0, True, 51, 1.5])
def test_parser_limit_validates(limit):
    with pytest.raises(ValueError):
        parse_account_analytics_snapshot(evidence(), limit)


def test_unrelated_local_or_public_counts_are_ignored():
    result = parse_account_analytics_snapshot(evidence(metrics=[{"label": "Views", "value": "999"}],
                                                      local_metrics={"posts": 123}, public_tweet_counts={"views": 99}))
    assert result["metrics"] == [] and not result["available"]
