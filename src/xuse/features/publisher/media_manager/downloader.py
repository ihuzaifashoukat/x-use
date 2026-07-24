import os
import time
import logging
import mimetypes
import random
from typing import Optional, Tuple, Dict, Any
from urllib.parse import urlparse, unquote

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


def _build_requests_context(
    browser_manager: Optional["BrowserManager"],
    extra_headers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Compose kwargs for requests calls (cookies, headers, proxies) from BrowserManager.

    - Uses live Selenium driver cookies if available, else account-config cookies if preloaded.
    - Applies proxy from BrowserManager if configured.
    """
    headers = dict(DEFAULT_HEADERS)
    if extra_headers:
        headers.update({k: v for k, v in extra_headers.items() if v})

    kwargs: Dict[str, Any] = {"headers": headers}

    # Proxies
    if browser_manager and getattr(browser_manager, "effective_proxy", None):
        proxy = browser_manager.effective_proxy
        kwargs["proxies"] = {"http": proxy, "https": proxy}

    # Cookies
    cookies_dict: Dict[str, str] = {}
    try:
        if browser_manager and getattr(browser_manager, "driver", None):
            for c in (browser_manager.driver.get_cookies() or []):
                if c.get("name") and c.get("value"):
                    cookies_dict[c["name"]] = c["value"]
        elif browser_manager and getattr(browser_manager, "cookies_data", None):
            for c in (browser_manager.cookies_data or []):
                if c.get("name") and c.get("value"):
                    cookies_dict[c["name"]] = c["value"]
    except Exception as e:
        logger.debug("Failed to extract cookies from BrowserManager: %s", e)

    if cookies_dict:
        kwargs["cookies"] = cookies_dict

    return kwargs


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
                base_name = unquote(disp)
        except Exception:
            pass
    if not base_name or "." not in base_name:
        ctype = response.headers.get("content-type", "").split(";")[0].strip()
        ext = ALLOWED_CONTENT_TYPES.get(ctype) or mimetypes.guess_extension(ctype) or ".bin"
        base_name = f"media_{int(time.time())}{ext}"
    return base_name


def _ensure_unique_path(dirpath: str, filename: str) -> str:
    os.makedirs(dirpath, exist_ok=True)
    file_path = os.path.join(dirpath, filename)
    if not os.path.exists(file_path):
        return file_path
    name, ext = os.path.splitext(file_path)
    counter = 1
    while os.path.exists(file_path):
        file_path = f"{name}_{counter}{ext}"
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
    content_length_expected: Optional[int] = None
    for attempt in range(max_retries + 1):
        try:
            logger.info("Downloading media from: %s (attempt %s)", url, attempt + 1)
            # HEAD to get metadata if available
            try:
                head_resp = requests.head(url, allow_redirects=True, timeout=timeout, **req_ctx)
                if head_resp.ok:
                    cl = head_resp.headers.get("content-length")
                    if cl and cl.isdigit():
                        content_length_expected = int(cl)
            except Exception:
                pass

            with requests.get(url, stream=True, timeout=timeout, allow_redirects=True, **req_ctx) as resp:
                if not resp.ok:
                    # Carry the response so the retry policy below can tell
                    # retryable statuses (429/5xx) from hard failures (404…).
                    raise requests.HTTPError(f"HTTP {resp.status_code} for {url}", response=resp)
                ok, bad_type = _validate_content_type(resp)
                if not ok:
                    raise _DisallowedContentType(
                        f"Refusing to save disallowed content-type '{bad_type}' for {url}"
                    )
                filename = _derive_filename(url, resp)
                file_path = _ensure_unique_path(out_dir, filename)

                bytes_written = 0
                tmp_path = f"{file_path}.part"
                with open(tmp_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=1024 * 128):
                        if chunk:
                            f.write(chunk)
                            bytes_written += len(chunk)
                # Verify size if we know expected length
                if content_length_expected is not None and bytes_written != content_length_expected:
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
                    raise IOError(
                        f"Content length mismatch for {url}: expected {content_length_expected}, got {bytes_written}"
                    )
                os.replace(tmp_path, file_path)
                logger.info("Media downloaded successfully to: %s", file_path)
                return file_path
        except _DisallowedContentType as e:
            last_error = e
            logger.error("%s", e)
            break
        except requests.HTTPError as e:
            last_error = e
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status is not None and not _should_retry(status):
                # 404/403/etc. will not heal with backoff — fail fast.
                logger.error("Non-retryable HTTP %s for %s; not retrying.", status, url)
                break
            logger.warning("Download error for %s: %s", url, e)
            if attempt < max_retries:
                # jittered exponential backoff
                sleep_for = backoff + random.uniform(0, 0.5)
                time.sleep(sleep_for)
                backoff = min(backoff * 2, 8.0)
                continue
        except requests.exceptions.RequestException as e:
            last_error = e
            logger.warning("Download error for %s: %s", url, e)
            if attempt < max_retries:
                # jittered exponential backoff
                sleep_for = backoff + random.uniform(0, 0.5)
                time.sleep(sleep_for)
                backoff = min(backoff * 2, 8.0)
                continue
        except Exception as e:
            last_error = e
            logger.error("Unexpected error during download from %s: %s", url, e)
            if attempt < max_retries:
                sleep_for = backoff + random.uniform(0, 0.5)
                time.sleep(sleep_for)
                backoff = min(backoff * 2, 8.0)
                continue
            break
    logger.error("Failed to download %s after %s attempts: %s", url, max_retries + 1, last_error)
    return None
