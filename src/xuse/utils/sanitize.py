"""Shared secret masking for URLs and text (NFR-2).

Masks the ENTIRE userinfo — `http://user:pass@host`, `http://TOKEN@host`,
`socks5://user@host` all become `scheme://***@host`. Usernames and token-only
userinfo are secrets too (proxy usernames are often customer IDs); the old
`user:***@` shape leaked both them and, for token-style URLs, everything.

This module is the single implementation. `utils.proxy_manager.mask_proxy_url`
and `mcp.proxy_tools.mask_proxy` delegate here — they each used to carry their
own copy of the same regex, so a fix to one silently left the other two leaking.

Why not a regex: the previous `(://)[^@/\\s]+@` cannot match userinfo that
contains `/`, whitespace, or a second `@`, because the character class excludes
them. `http://user:pa/ss@host` passed through COMPLETELY UNMASKED, and
`http://user:p@ss@host` masked only up to the first `@`, leaking the tail.
Why not urlsplit alone: it terminates the authority at the first `/`, so for
`http://user:pa/ss@host` it reports no userinfo at all and the credential
survives. Scanning the authority by hand handles both.
"""
from typing import Any, Optional

_AUTHORITY_END = "/?#"


def mask_url_userinfo(url: Optional[str]) -> Optional[str]:
    """Replace a single URL's userinfo with ``***``.

    Returns falsy input unchanged, and leaves URLs without userinfo — and
    non-URL values like ``pool:<name>`` — byte-identical.
    """
    if not url:
        return url
    text = str(url)
    if "://" not in text:
        return text
    scheme, _, rest = text.partition("://")
    ends = [rest.find(c) for c in _AUTHORITY_END]
    boundary = min([i for i in ends if i != -1], default=len(rest))
    authority, tail = rest[:boundary], rest[boundary:]
    if "@" in authority:
        # Well-formed: userinfo is everything before the LAST '@' (RFC 3986).
        return f"{scheme}://***@{authority.rpartition('@')[2]}{tail}"
    if "@" in tail and ":" in authority:
        # Malformed but accepted by validate_proxy_url: a raw '/' or space in
        # the password pushed the '@' past the authority boundary, so what we
        # sliced as `authority` is really a userinfo fragment ("user:pa") and
        # the true host sits after the last '@'. Redact the whole span.
        return f"{scheme}://***@{rest.rpartition('@')[2]}"
    return text


def sanitize_text(text: Any) -> str:
    """Return str(text) with every URL userinfo replaced by ***.

    Applied to free-form error text, so it scans whitespace-separated tokens
    and masks each one that looks like a URL.
    """
    raw = str(text)
    if "://" not in raw:
        return raw
    return " ".join(
        mask_url_userinfo(token) if "://" in token else token
        for token in raw.split(" ")
    )
