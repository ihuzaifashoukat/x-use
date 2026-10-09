"""Tweet image pipeline for MCP tools: fetch from pbs.twimg.com (public, no
auth), re-encode to bounded JPEG, and attach as MCP ImageContent blocks so
vision-capable clients see the actual picture. Every failure degrades to
URL-only — media problems never error a tool call.

`with_images` keeps the JSON envelope as the structured result (the tool
contract is unchanged) and appends images as extra content blocks; clients
that drop image blocks still have the envelope's `media` URLs + alt text.
"""
import asyncio
import base64
import io
import json
import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlparse

import requests
from mcp.types import CallToolResult, ImageContent, TextContent

logger = logging.getLogger(__name__)

MAX_IMAGES_PER_TWEET = 4
MAX_IMAGES_PER_SEARCH = 5
FETCH_TIMEOUT_SECONDS = 5
MAX_DIMENSION = 1024
MAX_BYTES = 200_000
MAX_DOWNLOAD_BYTES = 8_000_000
# Decoded-pixel gate (decompression-bomb class): a small-on-the-wire image can
# still cost hundreds of MB of process memory to decode, so dimensions are
# checked from the header before any bitmap is materialized.
MAX_IMAGE_PIXELS = 40_000_000
_MEDIA_ROUTE = ContextVar("xuse_media_route", default=None)


class _PublicMediaAuth(requests.auth.AuthBase):
    """Do not allow requests to import unrelated credentials from .netrc."""

    def __call__(self, request):
        request.headers.pop("Authorization", None)
        return request


@contextmanager
def image_fetch_scope(ctx, account_id):
    """Carry the selected account route through async tasks and worker threads.

    Requests has no bundled SOCKS transport. Those accounts retain media URLs
    instead of silently fetching images through a direct connection.
    """
    from xuse.browser.sessions import resolve_account_proxy

    account = ctx.session_pool.find_account_dict(account_id)
    proxy = resolve_account_proxy(ctx.config_loader, account)
    route, mode = None, "direct"
    if proxy:
        server = proxy["server"]
        if server.startswith("socks5:"):
            route, mode = False, "url_only_socks_proxy"
        else:
            parsed = urlparse(server)
            userinfo = ""
            if proxy.get("username"):
                userinfo = quote(proxy["username"], safe="") + ":" + quote(proxy.get("password", ""), safe="") + "@"
            route = parsed.scheme + "://" + userinfo + parsed.netloc
            mode = "account_proxy"
    token = _MEDIA_ROUTE.set(route)
    try:
        yield {"mode": mode, "cookies_sent": False}
    finally:
        _MEDIA_ROUTE.reset(token)


def _is_allowed_image_url(url: str) -> bool:
    """Fetch public HTTPS CDN images without credentials or alternate ports."""
    if not isinstance(url, str) or any(ord(c) < 32 for c in url):
        return False
    try:
        parsed = urlparse(url)
        if parsed.port not in (None, 443) or parsed.username is not None or parsed.password is not None:
            return False
    except Exception:
        return False
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https":
        return False
    return host == "twimg.com" or host.endswith(".twimg.com")


def _download(url: str) -> Optional[bytes]:
    """Stream the body with a hard cap: reject oversized Content-Length up
    front and abort the stream past MAX_DOWNLOAD_BYTES, so the wire cap limits
    what is transferred, not just what is kept."""
    if not _is_allowed_image_url(url):
        return None
    route = _MEDIA_ROUTE.get()
    if route is False:
        return None
    # Explicit empty proxy entries prevent unrelated environment proxy settings
    # from changing the browser account's chosen direct route.
    options = {"proxies": {"http": route or "", "https": route or ""},
               "auth": _PublicMediaAuth()}
    started = time.monotonic()
    # Never follow a CDN redirect into a local service or a different host.
    resp = requests.get(url, timeout=FETCH_TIMEOUT_SECONDS, stream=True, allow_redirects=False, **options)
    try:
        resp.raise_for_status()
        if resp.status_code != 200:
            return None
        declared = resp.headers.get("Content-Length")
        if declared is not None:
            try:
                if int(declared) > MAX_DOWNLOAD_BYTES:
                    return None
            except (TypeError, ValueError):
                pass  # unparseable header — fall through to the streaming cap
        chunks: List[bytes] = []
        downloaded = 0
        for chunk in resp.iter_content(chunk_size=65536):
            if time.monotonic() - started > FETCH_TIMEOUT_SECONDS * 2:
                return None
            downloaded += len(chunk)
            if downloaded > MAX_DOWNLOAD_BYTES:
                return None
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        resp.close()


def fetch_image(url: str) -> Optional[ImageContent]:
    """Download one image and return a bounded JPEG ImageContent, or None."""
    if not _is_allowed_image_url(url):
        return None
    try:
        raw = _download(url)
    except Exception:
        # Proxy transport exceptions can contain its credentials. Never log them.
        logger.info("Public image fetch failed; retaining media metadata.")
        return None
    if raw is None:
        logger.info("Image exceeds the %d-byte download cap: %s", MAX_DOWNLOAD_BYTES, url)
        return None
    try:
        from PIL import Image
    except Exception:  # pragma: no cover - Pillow is a hard dependency in practice
        logger.info("Pillow unavailable; skipping image: %s", url)
        return None
    try:
        img = Image.open(io.BytesIO(raw))  # lazy — reads headers only
        if img.size[0] * img.size[1] > MAX_IMAGE_PIXELS:
            logger.info("Image exceeds the pixel cap (%dx%d): %s", img.size[0], img.size[1], url)
            return None
        img = img.convert("RGB")
        img.thumbnail((MAX_DIMENSION, MAX_DIMENSION))
        quality = 85
        while True:
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality)
            data = buf.getvalue()
            if len(data) <= MAX_BYTES:
                break
            if quality > 40:
                quality -= 15
            elif max(img.size) > 64:
                img.thumbnail((max(1, img.width // 2), max(1, img.height // 2)))
            else:
                return None
        return ImageContent(
            type="image", data=base64.b64encode(data).decode("ascii"), mimeType="image/jpeg"
        )
    except Image.DecompressionBombError:
        logger.info("Decompression-bomb-class image rejected: %s", url)
        return None
    except Exception:
        logger.info("Image re-encode failed: %s", url, exc_info=True)
        return None


def images_for_tweet(tweet, limit: int = MAX_IMAGES_PER_TWEET) -> List[ImageContent]:
    """Fetch up to `limit` photo images for a tweet (videos stay URL-only).
    Synchronous — call via asyncio.to_thread from async tools."""
    images: List[ImageContent] = []
    attempted = set()
    limit = max(0, min(limit, MAX_IMAGES_PER_TWEET))
    for item in (getattr(tweet, "media", None) or []):
        if len(attempted) >= limit:
            break
        if item.type != "image":
            continue
        url = str(item.url)
        if url in attempted:
            continue
        attempted.add(url)
        block = fetch_image(url)
        if block is not None:
            images.append(block)
    return images


def media_envelope(tweet) -> List[Dict[str, Any]]:
    """Text fallback for every client: typed media URLs + alt text."""
    rows = []
    for item in (getattr(tweet, "media", None) or []):
        row = {"type": item.type, "url": str(item.url), "alt_text": item.alt_text}
        for key in ("poster_url", "source_url"):
            if getattr(item, key, None):
                row[key] = str(getattr(item, key))
        rows.append(row)
    return rows


async def thread_images(tweets, limit: int = MAX_IMAGES_PER_SEARCH):
    """Bound download attempts and concurrency; map images to source posts.

    Video posters are explicitly labelled still images, never video analysis.
    No remote URL is retrieved unless it passes the public CDN restriction.
    """
    candidates = []
    seen = set()
    limit = max(0, min(limit, MAX_IMAGES_PER_SEARCH))
    for tweet in tweets:
        for index, item in enumerate(getattr(tweet, "media", None) or []):
            url = str(item.url) if item.type == "image" else str(getattr(item, "poster_url", None) or "")
            if not url or url in seen or not _is_allowed_image_url(url):
                continue
            seen.add(url)
            if len(candidates) < limit:
                candidates.append({"tweet_id": tweet.tweet_id, "media_index": index,
                                   "kind": "photo" if item.type == "image" else "video_poster",
                                   "url": url, "alt_text": item.alt_text})
    semaphore = asyncio.Semaphore(3)

    async def fetch(candidate):
        async with semaphore:
            return await asyncio.to_thread(fetch_image, candidate["url"])

    fetched = await asyncio.gather(*(fetch(candidate) for candidate in candidates), return_exceptions=True)
    images, references = [], []
    for candidate, block in zip(candidates, fetched):
        reference = dict(candidate)
        reference["attached"] = isinstance(block, ImageContent)
        if reference["attached"]:
            # content[0] is the JSON envelope; subsequent blocks are images.
            reference["content_index"] = len(images) + 1
            images.append(block)
        references.append(reference)
    return images, references, {"attempted": len(candidates), "attached": len(images),
                                 "omitted": max(0, len(seen) - len(candidates)),
                                 "video_analysis": "posters_only; audio and motion are not inspected"}


def with_images(envelope: Dict[str, Any], images: List[ImageContent]) -> Any:
    """Plain envelope when no images; otherwise a CallToolResult carrying the
    same envelope as structuredContent + JSON text, with images appended."""
    if not images:
        return envelope
    return CallToolResult(
        content=[
            TextContent(type="text", text=json.dumps(envelope, indent=2, default=str)),
            *images,
        ],
        structuredContent=envelope,
    )
