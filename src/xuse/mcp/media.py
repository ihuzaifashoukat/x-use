"""Tweet image pipeline for MCP tools: fetch from pbs.twimg.com (public, no
auth), re-encode to bounded JPEG, and attach as MCP ImageContent blocks so
vision-capable clients see the actual picture. Every failure degrades to
URL-only — media problems never error a tool call.

`with_images` keeps the JSON envelope as the structured result (the tool
contract is unchanged) and appends images as extra content blocks; clients
that drop image blocks still have the envelope's `media` URLs + alt text.
"""
import base64
import io
import json
import logging
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

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


def _is_allowed_image_url(url: str) -> bool:
    """Only http(s) URLs on twimg.com or a subdomain of it may be fetched."""
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https"):
        return False
    return host == "twimg.com" or host.endswith(".twimg.com")


def _download(url: str) -> Optional[bytes]:
    """Stream the body with a hard cap: reject oversized Content-Length up
    front and abort the stream past MAX_DOWNLOAD_BYTES, so the wire cap limits
    what is transferred, not just what is kept."""
    resp = requests.get(url, timeout=FETCH_TIMEOUT_SECONDS, stream=True)
    try:
        resp.raise_for_status()
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
        logger.info("Image fetch failed: %s", url, exc_info=True)
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
            if len(data) <= MAX_BYTES or quality <= 40:
                break
            quality -= 15
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
    for item in (getattr(tweet, "media", None) or []):
        if len(images) >= limit:
            break
        if item.type != "image":
            continue
        block = fetch_image(str(item.url))
        if block is not None:
            images.append(block)
    return images


def media_envelope(tweet) -> List[Dict[str, Any]]:
    """Text fallback for every client: typed media URLs + alt text."""
    return [
        {"type": item.type, "url": str(item.url), "alt_text": item.alt_text}
        for item in (getattr(tweet, "media", None) or [])
    ]


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
