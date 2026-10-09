"""Public, credential-safe errors for the browser backend.

Never interpolate browser exceptions, page text, URLs, cookie values, or PINs
into these messages. Playwright exceptions can contain all of those.
"""


class SessionError(Exception):
    """The browser session cannot safely be used."""

    def __init__(self, reason: str = "session_unavailable"):
        self.reason = reason if reason in _REASONS else "session_unavailable"
        super().__init__("Browser session unavailable ({0}).".format(
            reason if reason in _REASONS else "session_unavailable"))


class BrowserBlocked(SessionError):
    """Stop work and request manual recovery; never solve or bypass a challenge."""


class BrowserActionError(SessionError):
    """A read/action could not be safely completed or its outcome is unknown."""


_REASONS = frozenset({
    "session_unavailable", "pool_closed", "unknown_account", "inactive_account",
    "account_in_use", "session_limit", "invalid_cookies", "missing_cookies",
    "expired_credentials", "invalid_proxy", "browser_unavailable", "startup_timeout",
    "startup_failed", "navigation_failed", "invalid_url", "external_redirect",
    "login_required", "challenge", "rate_limited", "pin_required", "account_restricted",
    "unsupported_dom", "invalid_input", "tweet_not_found", "outcome_unknown",
    "read_failed", "media_invalid", "community_unverified",
    "account_locked", "session_expired", "conversation_mismatch", "recipient_mismatch",
    "send_disabled", "composer_not_empty", "send_unconfirmed", "unlock_failed",
    "recipient_blocked", "recipient_unverified", "profile_mismatch",
})
