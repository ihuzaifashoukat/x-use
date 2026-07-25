"""Userinfo masking must survive passwords containing '/', ' ' and '@'.

Regression tests for the leak introduced with the whole-userinfo masking work:
the regex `(://)[^@/\\s]+@` cannot match userinfo containing any of those
characters, so `http://user:pa/ss@host` passed through completely unmasked and
`http://user:p@ss@host` leaked its tail. validate_proxy_url accepts all of
these forms, so the leak was reachable from list_proxies/get_account/errors.
"""
import pytest

from xuse.mcp.proxy_tools import mask_proxy
from xuse.utils.proxy_manager import mask_proxy_url, validate_proxy_url
from xuse.utils.sanitize import mask_url_userinfo, sanitize_text

# Every masker in the codebase. They used to be three independent copies of the
# same regex, so a fix to one left the others leaking; they now delegate to one
# implementation and this parametrisation keeps them from drifting apart again.
MASKERS = [mask_url_userinfo, mask_proxy_url, mask_proxy]

LEAKY_URLS = [
    ("http://user:pa/ss@host:8080", "pa/ss"),
    ("http://user:p@ss@host:8080", "p@ss"),
    ("http://user:pass word@host:8080", "pass word"),
    ("http://user:pass@host:8080", "pass"),
    ("socks5://customer-id-12345@host:1080", "customer-id-12345"),
    ("http://user:p:a:s:s@host:8080", "p:a:s:s"),
]


@pytest.mark.parametrize("masker", MASKERS, ids=lambda f: f.__module__)
@pytest.mark.parametrize("url,secret", LEAKY_URLS, ids=[u for u, _ in LEAKY_URLS])
def test_no_masker_leaks_any_part_of_the_userinfo(masker, url, secret):
    masked = masker(url)
    assert secret not in masked, f"{masker.__module__} leaked {secret!r}: {masked}"
    assert masked.startswith(("http://***@", "socks5://***@"))
    assert masked.endswith(("host:8080", "host:1080"))


@pytest.mark.parametrize("masker", MASKERS, ids=lambda f: f.__module__)
@pytest.mark.parametrize("safe", [
    "http://host:8080",
    "socks5://host",
    "https://api.ipify.org?format=json",
    "pool:residential",
    "http://host:8080/path?q=1",
])
def test_urls_without_userinfo_are_untouched(masker, safe):
    assert masker(safe) == safe


@pytest.mark.parametrize("masker", MASKERS, ids=lambda f: f.__module__)
def test_falsy_values_pass_through(masker):
    assert masker(None) is None
    assert masker("") == ""


def test_path_and_query_survive_masking():
    assert (mask_url_userinfo("http://user:pass@host:8080/p?q=1")
            == "http://***@host:8080/p?q=1")


def test_sanitize_text_masks_credentials_embedded_in_error_prose():
    text = "proxy http://user:pa/ss@host:8080 refused the connection"
    cleaned = sanitize_text(text)
    assert "pa/ss" not in cleaned
    assert "***@host:8080" in cleaned
    assert cleaned.startswith("proxy ") and cleaned.endswith("refused the connection")


def test_sanitize_text_leaves_text_without_urls_alone():
    assert sanitize_text("no credentials here") == "no credentials here"


@pytest.mark.parametrize("url,_secret", LEAKY_URLS)
def test_validator_error_text_never_echoes_the_password(url, _secret):
    """validate_proxy_url embeds the URL in its ValueError, so an unsupported
    scheme must not turn the error message into the leak path."""
    with pytest.raises(ValueError) as exc:
        validate_proxy_url(url.replace("http://", "ftp://").replace("socks5://", "ftp://"))
    assert _secret not in str(exc.value)
