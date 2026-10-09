"""Bounded media networking and source attribution for conversation analysis."""
import base64
import io
import threading
import time
import asyncio
from types import SimpleNamespace

import pytest
from mcp.types import ImageContent
from PIL import Image

from xuse.models import MediaItem, ScrapedTweet
from xuse.mcp import media as m
from test_media_hardening import StreamResponse, make_png_bytes


@pytest.mark.parametrize("url", [
    "http://pbs.twimg.com/media/a.jpg", "https://user:secret@pbs.twimg.com/a.jpg",
    "https://pbs.twimg.com:444/a.jpg", "https://pbs.twimg.com.attacker.invalid/a.jpg",
    "https://pbs.twimg.com:bad/a.jpg", "https://pbs.twimg.com/\na.jpg",
])
def test_disallowed_media_urls_never_make_a_request(monkeypatch, url):
    monkeypatch.setattr(m.requests, "get", lambda *a, **kw: pytest.fail("unexpected request"))
    assert m.fetch_image(url) is None


def test_redirect_body_is_never_read_or_followed(monkeypatch):
    response = StreamResponse([make_png_bytes()], status=302, headers={"Location": "http://127.0.0.1/secret"})
    calls = []

    def request(url, **options):
        calls.append(options)
        return response

    monkeypatch.setattr(m.requests, "get", request)
    assert m.fetch_image("https://pbs.twimg.com/media/a.jpg") is None
    assert calls[0]["allow_redirects"] is False and response.body_reads == 0 and response.closed


def test_failed_images_do_not_expand_per_post_attempt_budget(monkeypatch):
    calls = []
    monkeypatch.setattr(m, "fetch_image", lambda url: calls.append(url))
    tweet = ScrapedTweet(tweet_id="1", text_content="", media=[
        MediaItem(type="image", url=f"https://pbs.twimg.com/media/{i}.jpg") for i in range(20)])
    assert m.images_for_tweet(tweet, limit=2) == [] and len(calls) == 2


def test_high_entropy_jpeg_respects_output_byte_cap(monkeypatch):
    import random
    pixels = random.Random(7).randbytes(1024 * 1024 * 3)
    image = Image.frombytes("RGB", (1024, 1024), pixels)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    monkeypatch.setattr(m.requests, "get", lambda *a, **kw: StreamResponse([buffer.getvalue()]))
    result = m.fetch_image("https://pbs.twimg.com/media/noise.png")
    assert result is not None and len(base64.b64decode(result.data)) <= m.MAX_BYTES


@pytest.mark.asyncio
async def test_thread_photos_and_posters_are_bounded_concurrent_and_attributed(monkeypatch):
    active = maximum = 0
    lock = threading.Lock()
    calls = []

    def fetch(url):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
            calls.append(url)
        time.sleep(0.02)
        with lock:
            active -= 1
        if url.endswith("/1.jpg"):
            return None
        return ImageContent(type="image", data="eA==", mimeType="image/jpeg")

    monkeypatch.setattr(m, "fetch_image", fetch)
    tweets = [ScrapedTweet(tweet_id=str(i), text_content="", media=[
        MediaItem(type="image", url=f"https://pbs.twimg.com/media/{i}.jpg")]) for i in range(8)]
    tweets[0].media.insert(0, MediaItem(type="video", url="https://video.twimg.com/a.mp4",
                                      poster_url="https://pbs.twimg.com/media/poster.jpg"))
    images, references, coverage = await m.thread_images(tweets)
    assert len(calls) == 5 and 1 < maximum <= 3
    assert references[0]["kind"] == "video_poster" and references[0]["tweet_id"] == "0"
    assert len(images) == 4 and coverage["omitted"] == 4
    assert [r["content_index"] for r in references if r["attached"]] == [1, 2, 3, 4]
    assert not any(".mp4" in url for url in calls)


@pytest.mark.asyncio
async def test_media_duplicates_do_not_duplicate_fetches(monkeypatch):
    calls = []
    monkeypatch.setattr(m, "fetch_image", lambda url: calls.append(url))
    tweet = ScrapedTweet(tweet_id="1", text_content="", media=[
        MediaItem(type="image", url="https://pbs.twimg.com/media/same.jpg") for _ in range(4)])
    _, refs, coverage = await m.thread_images([tweet, tweet])
    assert len(calls) == len(refs) == 1 and coverage["attached"] == 0


def routing_context():
    accounts = {"a": {"account_id": "a", "proxy": "http://user:p%40ss@proxy-a.test:8080"},
                "b": {"account_id": "b", "proxy": "http://proxy-b.test:8080"},
                "socks": {"account_id": "socks", "proxy": "socks5://proxy-s.test:1080"},
                "direct": {"account_id": "direct"}}
    return SimpleNamespace(session_pool=SimpleNamespace(find_account_dict=accounts.__getitem__),
                           config_loader=SimpleNamespace(get_setting=lambda key, default=None: default))


@pytest.mark.asyncio
async def test_concurrent_account_images_keep_separate_proxy_routes(monkeypatch):
    ctx = routing_context()
    calls = {}

    def request(url, **options):
        calls[url] = options
        return StreamResponse([make_png_bytes()])

    monkeypatch.setattr(m.requests, "get", request)

    async def fetch(account):
        with m.image_fetch_scope(ctx, account):
            await asyncio.sleep(0)
            return await asyncio.to_thread(m.fetch_image, f"https://pbs.twimg.com/media/{account}.jpg")

    results = await asyncio.gather(*(fetch(account) for account in ("a", "b", "direct", "socks")))
    assert all(results[:3]) and results[3] is None
    assert len(calls) == 3
    assert calls["https://pbs.twimg.com/media/a.jpg"]["proxies"]["https"] == "http://user:p%40ss@proxy-a.test:8080"
    assert calls["https://pbs.twimg.com/media/b.jpg"]["proxies"]["https"] == "http://proxy-b.test:8080"
    assert calls["https://pbs.twimg.com/media/direct.jpg"]["proxies"] == {"http": "", "https": ""}
    assert m._MEDIA_ROUTE.get() is None


def test_failed_proxy_image_has_no_direct_fallback_or_credential_log(monkeypatch, caplog):
    calls = []

    def request(url, **options):
        calls.append(options)
        raise RuntimeError("PRIVATE_PROXY_PASSWORD_SENTINEL")

    monkeypatch.setattr(m.requests, "get", request)
    with caplog.at_level("INFO"), m.image_fetch_scope(routing_context(), "a"):
        assert m.fetch_image("https://pbs.twimg.com/media/a.jpg") is None
    assert len(calls) == 1 and "PRIVATE_PROXY_PASSWORD_SENTINEL" not in caplog.text


def test_public_media_auth_strips_authorization():
    request = SimpleNamespace(headers={"Authorization": "private"})
    assert m._PublicMediaAuth()(request).headers == {}
