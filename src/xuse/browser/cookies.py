"""Strict cookie import for Playwright. No credentials are persisted or logged."""
import json
import math
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from xuse.core.config_loader import PROJECT_ROOT

from .errors import SessionError

MAX_COOKIE_FILE_BYTES = 1_048_576
MAX_COOKIES = 256
_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_CREDENTIALS = ("auth_token", "ct0")
_MAX_REPORTED_NAME = 64


class _Rule(Exception):
    """Internal: one cookie broke the named rule. Carries no cookie data."""

    def __init__(self, rule: str):
        super().__init__(rule)
        self.rule = rule


@dataclass(frozen=True)
class CookieReport:
    """Usable cookies plus the optional ones that were skipped, as (name, rule).

    ``cookies`` is excluded from repr so values cannot reach a log by accident.
    """
    cookies: List[Dict[str, Any]] = field(repr=False)
    skipped: List[Tuple[str, str]]


def summarize_skipped(skipped: List[Tuple[str, str]], limit: int) -> str:
    """Text form of a skipped list: first ``limit`` "name (rule)" entries, then ", and N more"."""
    text = ", ".join(f"{name} ({rule})" for name, rule in skipped[:limit])
    if len(skipped) > limit:
        text += f", and {len(skipped) - limit} more"
    return text


def _safe_name(name: Any) -> str:
    """Echo a cookie name only when it is a short, well-formed token."""
    if isinstance(name, str) and len(name) <= _MAX_REPORTED_NAME and _NAME.fullmatch(name):
        return name
    return "<invalid name>"


def _cookie_expiration(raw: Dict[str, Any]) -> Optional[float]:
    """Accept exporter aliases only when they describe the same expiration."""
    values = [raw[key] for key in ("expires", "expirationDate", "expiry") if key in raw]
    for value in values:
        if value is None:
            continue
        try:
            finite = math.isfinite(value)
        except (OverflowError, TypeError, ValueError):
            finite = False
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not finite:
            raise _Rule("invalid_expiry")
    if values and any(value != values[0] for value in values[1:]):
        raise _Rule("invalid_expiry")
    return values[0] if values else None


def _inspect_one(raw: Dict[str, Any], now: float) -> Optional[Dict[str, Any]]:
    """Return a normalized cookie, None when expired, or raise _Rule."""
    name, value = raw.get("name"), raw.get("value")
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise _Rule("invalid_name")
    if (not isinstance(value, str) or len(value) > 16384
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise _Rule("invalid_value")
    domain = raw.get("domain", ".x.com")
    if domain not in ("x.com", ".x.com"):
        raise _Rule("not_x_domain")
    for flag in ("secure", "httpOnly", "session", "hostOnly"):
        if flag in raw and not isinstance(raw[flag], bool):
            raise _Rule("invalid_flags")
    # A leading dot tells Chromium to include subdomains. Exporters can
    # spell a host-only domain with a dot, so explicit metadata takes
    # precedence. Without metadata preserve the browser's exported scope.
    if "hostOnly" in raw:
        domain = "x.com" if raw["hostOnly"] else ".x.com"
    # This importer has no partition-aware account model. Silently
    # discarding a partition key would turn a scoped cookie into a broader
    # unpartitioned cookie. Explicit unpartitioned metadata is harmless.
    if (raw.get("partitionKey") is not None
            or raw.get("firstPartyDomain") not in (None, "")
            or ("partitioned" in raw and raw["partitioned"] is not False)):
        raise _Rule("partitioned")
    path = raw.get("path", "/")
    if (not isinstance(path, str) or not path.startswith("/")
            or any(ord(c) < 32 or ord(c) == 127 for c in path)
            or (name in _CREDENTIALS and path != "/")):
        raise _Rule("invalid_path")
    cookie = {"name": name, "value": value, "domain": domain, "path": path}
    cookie["secure"] = raw.get("secure", False)
    cookie["httpOnly"] = raw.get("httpOnly", False)
    same_site = raw.get("sameSite")
    if same_site is not None:
        if not isinstance(same_site, str):
            raise _Rule("invalid_same_site")
        mapped = {"strict": "Strict", "lax": "Lax", "none": "None",
                  "no_restriction": "None", "no-restriction": "None"}.get(same_site.lower())
        if not mapped and same_site.lower() != "unspecified":
            raise _Rule("invalid_same_site")
        if mapped == "None" and not cookie["secure"]:
            raise _Rule("insecure_same_site_none")
        if mapped:
            cookie["sameSite"] = mapped
    expiration = _cookie_expiration(raw)
    if expiration is not None and expiration != -1:
        if expiration <= now:
            if name in _CREDENTIALS:
                raise _Rule("expired")
            return None
        if not raw.get("session", False):
            cookie["expires"] = float(expiration)
    # Credential-only rules: an optional cookie may be insecure or empty.
    if name in _CREDENTIALS:
        if not cookie["secure"]:
            raise _Rule("insecure")
        if not value:
            raise _Rule("empty_value")
    return cookie


def _credential_error(name: str, rule: str) -> SessionError:
    reason = "expired_credentials" if rule == "expired" else "invalid_cookies"
    return SessionError(reason, detail="{0}: {1}".format(name, rule))


def inspect_cookies(payload: Any, *, now: Optional[float] = None) -> CookieReport:
    """Validate an X export; skip unusable optional cookies and report them.

    File-level problems and any broken ``auth_token``/``ct0`` raise
    SessionError. Any other cookie that breaks a rule is skipped and reported by
    (safe name, rule); values are never reported. Expired optional cookies are
    dropped silently. Domains are checked exactly, never with a substring match
    or a twitter.com-to-x.com rewrite. Cookie arrays and cookies/storage-state
    envelopes are accepted. The latter import cookies only: local storage and
    IndexedDB are never restored.
    """
    now = time.time() if now is None else now
    if isinstance(payload, dict):
        if (set(payload) - {"cookies", "origins"}
                or ("origins" in payload and not isinstance(payload["origins"], list))):
            raise SessionError("invalid_cookies")
        payload = payload.get("cookies")
    if not isinstance(payload, list) or not payload or len(payload) > MAX_COOKIES:
        raise SessionError("invalid_cookies")
    kept: List[Dict[str, Any]] = []
    skipped: List[Tuple[str, str]] = []
    for raw in payload:
        if not isinstance(raw, dict):
            raise SessionError("invalid_cookies")
        name = raw.get("name")
        try:
            cookie = _inspect_one(raw, now)
        except _Rule as broken:
            if isinstance(name, str) and name in _CREDENTIALS:
                raise _credential_error(name, broken.rule) from None
            skipped.append((_safe_name(name), broken.rule))
            continue
        if cookie is not None:
            kept.append(cookie)
    # Host-only and domain variants can collide in the browser cookie store.
    # Neither copy has clear precedence, so an optional duplicate is dropped
    # entirely and a duplicated credential is refused.
    groups: Dict[Tuple[str, str, str], int] = {}
    for cookie in kept:
        key = (cookie["name"], cookie["domain"].lstrip("."), cookie["path"])
        groups[key] = groups.get(key, 0) + 1
    # Credentials are always path "/", so a repeated name is a repeated key
    # unless the domains differ only by host-only scope; refuse both.
    for name in _CREDENTIALS:
        if sum(c["name"] == name for c in kept) > 1:
            raise _credential_error(name, "duplicate")
    missing = [n for n in _CREDENTIALS if not any(c["name"] == n for c in kept)]
    if missing:
        raise SessionError("missing_cookies", detail="missing: " + ", ".join(missing))
    duplicated = {key for key, count in groups.items() if count > 1}
    skipped.extend((_safe_name(key[0]), "duplicate") for key in groups if key in duplicated)
    result = [c for c in kept
              if (c["name"], c["domain"].lstrip("."), c["path"]) not in duplicated]
    return CookieReport(result, skipped)


def normalize_cookies(payload: Any, *, now: Optional[float] = None) -> List[Dict[str, Any]]:
    """Cookies from inspect_cookies, discarding the skipped-cookie report."""
    return inspect_cookies(payload, now=now).cookies


def _unique_json_object(pairs: Any) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def parse_cookie_export_report(data: bytes, *, now: Optional[float] = None) -> CookieReport:
    """Parse bounded JSON bytes into a cookie report without exposing raw input.

    Duplicate fields are rejected before JSON can silently choose a domain,
    value, or expiration. This parser is shared by file loading and explicit
    account import so both boundaries make the same validation decision.
    """
    if not isinstance(data, bytes) or len(data) > MAX_COOKIE_FILE_BYTES:
        raise SessionError("invalid_cookies")
    try:
        payload = json.loads(data.decode("utf-8-sig"), object_pairs_hook=_unique_json_object)
    except (UnicodeError, ValueError, RecursionError):
        raise SessionError("invalid_cookies") from None
    return inspect_cookies(payload, now=now)


def parse_cookie_export(data: bytes, *, now: Optional[float] = None) -> List[Dict[str, Any]]:
    """Cookies from parse_cookie_export_report, discarding the skipped list."""
    return parse_cookie_export_report(data, now=now).cookies


def load_account_cookies_report(account: Dict[str, Any], config_loader: Any) -> CookieReport:
    """Load inline cookies or the canonical AccountConfig cookie_file_path."""
    inline = account.get("cookies")
    if inline is not None:
        return inspect_cookies(inline)
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
            return parse_cookie_export_report(data)
        except SessionError:
            raise
        except (OSError, UnicodeError, ValueError):
            raise SessionError("invalid_cookies") from None
    raise SessionError("missing_cookies")


def load_account_cookies(account: Dict[str, Any], config_loader: Any) -> List[Dict[str, Any]]:
    """Cookies from load_account_cookies_report, discarding the skipped list."""
    return load_account_cookies_report(account, config_loader).cookies
