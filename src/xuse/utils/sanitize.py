"""Shared secret masking for URLs and text (NFR-2).

Masks the ENTIRE userinfo — `http://user:pass@host`, `http://TOKEN@host`,
`socks5://user@host` all become `scheme://***@host`. Usernames and token-only
userinfo are secrets too (proxy usernames are often customer IDs); the old
`user:***@` shape leaked both them and, for token-style URLs, everything.
"""
import re
from typing import Any

_URL_USERINFO_RE = re.compile(r"(://)[^@/\s]+@")


def sanitize_text(text: Any) -> str:
    """Return str(text) with every URL userinfo replaced by ***."""
    return _URL_USERINFO_RE.sub(r"\1***@", str(text))
