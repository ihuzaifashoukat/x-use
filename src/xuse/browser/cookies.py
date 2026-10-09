"""Strict cookie import for Playwright. No credentials are persisted or logged."""
import json
import math
import re
import time
from pathlib import Path
from typing import Any, Dict, List

from xuse.core.config_loader import PROJECT_ROOT

from .errors import SessionError

MAX_COOKIE_FILE_BYTES = 1_048_576
MAX_COOKIES = 256
_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


def normalize_cookies(payload: Any, *, now: float = None) -> List[Dict[str, Any]]:
    """Accept X exports only; malformed entries invalidate the entire import.

    Expired non-authentication cookies can be dropped. Authentication cookies
    must exist, be nonempty, be secure, and not be expired. Domains are checked
    exactly, never with a substring match or a twitter.com-to-x.com rewrite.
    """
    now = time.time() if now is None else now
    if not isinstance(payload, list) or not payload or len(payload) > MAX_COOKIES:
        raise SessionError("invalid_cookies")
    result = []
    seen = set()
    for raw in payload:
        if not isinstance(raw, dict):
            raise SessionError("invalid_cookies")
        name, value = raw.get("name"), raw.get("value")
        if (not isinstance(name, str) or not _NAME.fullmatch(name)
                or not isinstance(value, str) or len(value) > 16384
                or any(ord(c) < 32 or ord(c) == 127 for c in value)):
            raise SessionError("invalid_cookies")
        domain = raw.get("domain", ".x.com")
        if domain not in ("x.com", ".x.com"):
            raise SessionError("invalid_cookies")
        path = raw.get("path", "/")
        if not isinstance(path, str) or not path.startswith("/") or any(ord(c) < 32 for c in path):
            raise SessionError("invalid_cookies")
        key = (name, domain, path)
        if key in seen:
            raise SessionError("invalid_cookies")
        seen.add(key)
        cookie = {"name": name, "value": value, "domain": domain, "path": path}
        for flag in ("secure", "httpOnly", "session", "hostOnly"):
            if flag in raw and not isinstance(raw[flag], bool):
                raise SessionError("invalid_cookies")
        cookie["secure"] = raw.get("secure", False)
        cookie["httpOnly"] = raw.get("httpOnly", False)
        same_site = raw.get("sameSite")
        if same_site is not None:
            if not isinstance(same_site, str):
                raise SessionError("invalid_cookies")
            mapped = {"strict": "Strict", "lax": "Lax", "none": "None",
                      "no_restriction": "None", "no-restriction": "None"}.get(same_site.lower())
            if not mapped or (mapped == "None" and not cookie["secure"]):
                raise SessionError("invalid_cookies")
            cookie["sameSite"] = mapped
        expiration = raw.get("expires", raw.get("expirationDate", raw.get("expiry")))
        if expiration is not None:
            try:
                finite = math.isfinite(expiration)
            except (OverflowError, TypeError, ValueError):
                finite = False
            if isinstance(expiration, bool) or not isinstance(expiration, (int, float)) or not finite:
                raise SessionError("invalid_cookies")
            if expiration != -1:
                if expiration <= now:
                    if name in ("auth_token", "ct0"):
                        raise SessionError("expired_credentials")
                    continue
                if not raw.get("session", False):
                    cookie["expires"] = float(expiration)
        if name in ("auth_token", "ct0") and (not value or not cookie["secure"] or path != "/"):
            raise SessionError("invalid_cookies")
        result.append(cookie)
    credentials = {c["name"] for c in result}
    if not {"auth_token", "ct0"}.issubset(credentials):
        raise SessionError("missing_cookies")
    # Two different cookies for a credential name have ambiguous precedence.
    if any(sum(c["name"] == name for c in result) != 1 for name in ("auth_token", "ct0")):
        raise SessionError("invalid_cookies")
    return result


def load_account_cookies(account: Dict[str, Any], config_loader: Any) -> List[Dict[str, Any]]:
    """Load inline cookies or the canonical AccountConfig cookie_file_path."""
    inline = account.get("cookies")
    if inline is not None:
        return normalize_cookies(inline)
    candidate = account.get("cookie_file_path")
    if not isinstance(candidate, str) or not candidate:
        raise SessionError("missing_cookies")
    config_dir = Path(getattr(config_loader, "accounts_file", PROJECT_ROOT / "config/accounts.json")).parent
    requested = Path(candidate)
    candidates = [requested] if requested.is_absolute() else [config_dir / requested, PROJECT_ROOT / requested]
    for path in candidates:
        try:
            if not path.is_file():
                continue
            with path.open("rb") as stream:
                data = stream.read(MAX_COOKIE_FILE_BYTES + 1)
            if len(data) > MAX_COOKIE_FILE_BYTES:
                raise SessionError("invalid_cookies")
            return normalize_cookies(json.loads(data.decode("utf-8-sig")))
        except SessionError:
            raise
        except (OSError, UnicodeError, ValueError):
            raise SessionError("invalid_cookies") from None
    raise SessionError("missing_cookies")
