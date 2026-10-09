import os
import time
import logging
import mimetypes
import random
import re
import tempfile
from typing import Optional, Tuple, Dict, Any
from urllib.parse import urlparse, unquote
from urllib.parse import urlsplit

import requests

try:
    # Optional import to avoid hard dependency for non-Selenium contexts
    from xuse.core.browser_manager import BrowserManager  # type: ignore
except Exception:  # pragma: no cover - optional path
    BrowserManager = None  # type: ignore

logger = logging.getLogger(__name__)


DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://x.com/",
}


ALLOWED_CONTENT_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
}

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_UNSAFE_FILENAME_CHARS = re.compile(r'[\x00-\x1f<>:"/\\|?*]')
_WINDOWS_DEVICE_NAME = re.compile(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', re.IGNORECASE)


def _safe_filename(filename: str) -> str:
    """Keep only a safe filename component from an untrusted response header."""
    # Treat both separator styles as paths on every platform. This prevents a
    # Windows path from being interpreted as a filename on POSIX and vice versa.
    filename = unquote(str(filename)).replace("\\", "/").rsplit("/", 1)[-1]
    filename = _UNSAFE_FILENAME_CHARS.sub("_", filename).strip().rstrip(" .")
    if not filename or filename in (".", ".."):
        return ""
    if _WINDOWS_DEVICE_NAME.match(filename):
        filename = "_" + filename
    return filename


def _path_is_within(path: str, directory: str) -> bool:
    """Check the resolved path, including existing symlink targets."""
    try:
        return os.path.commonpath((os.path.realpath(directory), os.path.realpath(path))) == os.path.realpath(directory)
    except (OSError, ValueError):
        return False


def _build_requests_context(
    browser_manager: Optional["BrowserManager"],
    extra_headers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Compose public-media request kwargs without account credentials.

    Media URLs can point at arbitrary hosts or redirect there. Never forward
    Selenium/account cookies to these endpoints. The configured account proxy
    remains the only account-derived request setting.
    """
    headers = dict(DEFAULT_HEADERS)
    if extra_headers:
        headers.update({k: v for k, v in extra_headers.items() if v})

    kwargs: Dict[str, Any] = {"headers": headers}

    # Proxies
    if browser_manager and getattr(browser_manager, "effective_proxy", None):
        proxy = browser_manager.effective_proxy
        kwargs["proxies"] = {"http": proxy, "https": proxy}

    return kwargs


def _new_requests_session() -> requests.Session:
    """Create a request session that cannot acquire ambient netrc credentials."""
    session = requests.Session()
    # requests otherwise consults environment proxy/netrc configuration. The
    # caller supplies the configured proxy explicitly through _build_requests_context.
    session.trust_env = False
    return session


def _safe_url_host(url: str) -> str:
    """Return a log-safe host identifier without URL credentials or query data."""
    try:
        return urlsplit(url).hostname or "unknown-host"
    except (TypeError, ValueError):
        return "unknown-host"


def _derive_filename(url: str, response: requests.Response) -> str:
    parsed = urlparse(url)
    base_name = os.path.basename(parsed.path)
    if base_name:
        base_name = unquote(base_name)
    # Prefer Content-Disposition filename when present
    cd = response.headers.get("content-disposition")
    if cd and "filename=" in cd:
        try:
            disp = cd.split("filename=")[-1].strip('"; ')
            if disp:
                base_name = disp
        except Exception:
            pass
    base_name = _safe_filename(base_name)
    if not base_name or "." not in base_name:
        ctype = response.headers.get("content-type", "").split(";")[0].strip()
        ext = ALLOWED_CONTENT_TYPES.get(ctype) or mimetypes.guess_extension(ctype) or ".bin"
        base_name = f"media_{int(time.time())}{ext}"
    return base_name


def _ensure_unique_path(dirpath: str, filename: str) -> str:
    os.makedirs(dirpath, exist_ok=True)
    directory = os.path.realpath(dirpath)
    filename = _safe_filename(filename)
    if not filename:
        filename = "media"
    file_path = os.path.join(directory, filename)
    if not _path_is_within(file_path, directory):
        raise ValueError("Media destination escapes the output directory.")
    if not os.path.exists(file_path):
        return file_path
    name, ext = os.path.splitext(file_path)
    counter = 1
    while os.path.exists(file_path):
        file_path = f"{name}_{counter}{ext}"
        if not _path_is_within(file_path, directory):
            raise ValueError("Media destination escapes the output directory.")
        counter += 1
    return file_path


def _validate_content_type(response: requests.Response) -> Tuple[bool, Optional[str]]:
    ctype = response.headers.get("content-type", "").split(";")[0].strip()
    if not ctype:
        return True, None  # unknown, allow
    if ctype in ALLOWED_CONTENT_TYPES:
        return True, None
    # Allow some generic types too
    if ctype.startswith("image/") or ctype.startswith("video/"):
        return True, None
    if ctype in ("application/octet-stream",):
        return True, None
    return False, ctype


def _should_retry(status_code: int) -> bool:
    return status_code in RETRYABLE_STATUS


class _DisallowedContentType(Exception):
    """The server answered 200 with a non-media body (e.g. a text/html error
    page). Saving it would attach an HTML document to a tweet as an image."""


def download_with_retries(
    url: str,
    out_dir: str,
    timeout: int = 30,
    max_retries: int = 2,
    browser_manager: Optional["BrowserManager"] = None,
) -> Optional[str]:
    if not url:
        return None
    backoff = 1.0
    last_error = None
    req_ctx = _build_requests_context(browser_manager)
    session = _new_requests_session()
    try:
        for attempt in range(max_retries + 1):
            try:
                logger.info("Downloading public media from %s (attempt %s)", _safe_url_host(url), attempt + 1)
                # Retain the metadata probe, but never use its length to validate
                # the GET body: redirects/variants can change it between requests.
                head_resp = None
                try:
                    head_resp = session.head(url, allow_redirects=True, timeout=timeout, **req_ctx)
                except Exception:
                    pass
                finally:
                    if head_resp is not None:
                        head_resp.close()

                with session.get(url, stream=True, timeout=timeout, allow_redirects=True, **req_ctx) as resp:
                    if not resp.ok:
                        # Carry the response so retry policy distinguishes 404/403 from 5xx.
                        raise requests.HTTPError(f"HTTP {resp.status_code} for {_safe_url_host(url)}", response=resp)
                    ok, bad_type = _validate_content_type(resp)
                    if not ok:
                        raise _DisallowedContentType(
                            f"Refusing to save disallowed content-type '{bad_type}' for {_safe_url_host(url)}"
                        )
                    filename = _derive_filename(url, resp)
                    file_path = _ensure_unique_path(out_dir, filename)

                    bytes_written = 0
                    directory = os.path.realpath(out_dir)
                    if not _path_is_within(file_path, directory):
                        raise ValueError("Media destination escapes the output directory.")
                    fd, tmp_path = tempfile.mkstemp(prefix=".xuse-media-", suffix=".part", dir=directory)
                    try:
                        with os.fdopen(fd, "wb") as f:
                            for chunk in resp.iter_content(chunk_size=1024 * 128):
                                if chunk:
                                    f.write(chunk)
                                    bytes_written += len(chunk)
                        # requests transparently decompresses supported encodings,
                        # so compare only an unencoded GET response's own length.
                        content_encoding = resp.headers.get("content-encoding", "").strip().lower()
                        content_length = resp.headers.get("content-length", "")
                        if (content_encoding in ("", "identity") and content_length.isdigit()
                                and bytes_written != int(content_length)):
                            raise IOError(
                                f"Content length mismatch for {_safe_url_host(url)}: "
                                f"expected {content_length}, got {bytes_written}"
                            )
                        os.replace(tmp_path, file_path)
                    finally:
                        try:
                            os.unlink(tmp_path)
                        except FileNotFoundError:
                            pass
                    logger.info("Media downloaded successfully to: %s", file_path)
                    return file_path
            except _DisallowedContentType as e:
                last_error = e
                logger.error("Refusing media response with disallowed content type: %s", type(e).__name__)
                break
            except requests.HTTPError as e:
                last_error = e
                status = getattr(getattr(e, "response", None), "status_code", None)
                if status is not None and not _should_retry(status):
                    logger.error("Non-retryable HTTP %s from %s; not retrying.", status, _safe_url_host(url))
                    break
                logger.warning("Download request failed for %s (%s).", _safe_url_host(url), type(e).__name__)
                if attempt < max_retries:
                    sleep_for = backoff + random.uniform(0, 0.5)
                    time.sleep(sleep_for)
                    backoff = min(backoff * 2, 8.0)
                    continue
            except requests.exceptions.RequestException as e:
                last_error = e
                logger.warning("Download request failed for %s (%s).", _safe_url_host(url), type(e).__name__)
                if attempt < max_retries:
                    sleep_for = backoff + random.uniform(0, 0.5)
                    time.sleep(sleep_for)
                    backoff = min(backoff * 2, 8.0)
                    continue
            except Exception as e:
                last_error = e
                logger.error("Unexpected download failure for %s (%s).", _safe_url_host(url), type(e).__name__)
                if attempt < max_retries:
                    sleep_for = backoff + random.uniform(0, 0.5)
                    time.sleep(sleep_for)
                    backoff = min(backoff * 2, 8.0)
                    continue
                break
        logger.error("Failed to download media from %s after %s attempts (%s).",
                     _safe_url_host(url), max_retries + 1,
                     type(last_error).__name__ if last_error is not None else "unknown error")
        return None
    finally:
        session.close()
