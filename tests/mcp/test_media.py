"""Image pipeline tests: mocked HTTP, tiny in-test images via Pillow. No network."""
import base64
import io
import json

from xuse.mcp import media as m


def make_png_bytes(width=20, height=10, color=(200, 30, 30)) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="PNG")
    return buf.getvalue()


class FakeResponse:
    def __init__(self, content: bytes, status: int = 200):
        self.content = content
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


class FakeTweet:
    def __init__(self, items):
        self.media = items


class FakeItem:
    def __init__(self, type, url, alt_text=None):
        self.type, self.url, self.alt_text = type, url, alt_text


def test_fetch_image_returns_jpeg_image_content(monkeypatch):
    monkeypatch.setattr(m.requests, "get",
                        lambda url, timeout: FakeResponse(make_png_bytes(3000, 2000)))
    block = m.fetch_image("https://pbs.twimg.com/media/a.jpg")
    assert block is not None and block.type == "image" and block.mimeType == "image/jpeg"
    from PIL import Image
    decoded = Image.open(io.BytesIO(base64.b64decode(block.data)))
    assert max(decoded.size) <= m.MAX_DIMENSION  # 3000px shrunk to 1024


def test_fetch_image_failure_returns_none(monkeypatch):
    monkeypatch.setattr(m.requests, "get",
                        lambda url, timeout: FakeResponse(b"nope", status=404))
    assert m.fetch_image("https://pbs.twimg.com/media/gone.jpg") is None


def test_fetch_image_rejects_non_twimg_url_without_fetching(monkeypatch):
    def _boom(url, timeout):
        raise AssertionError("requests.get must not be called for non-twimg URLs")
    monkeypatch.setattr(m.requests, "get", _boom)
    assert m.fetch_image("https://evil.example.com/media/a.jpg") is None


def test_fetch_image_rejects_oversized_body(monkeypatch):
    monkeypatch.setattr(m.requests, "get",
                        lambda url, timeout: FakeResponse(b"x" * 8_000_001))
    assert m.fetch_image("https://pbs.twimg.com/media/huge.jpg") is None


def test_images_for_tweet_skips_video_and_failed_fetches(monkeypatch):
    monkeypatch.setattr(m.requests, "get",
                        lambda url, timeout: FakeResponse(make_png_bytes()))
    tweet = FakeTweet([
        FakeItem("video", "https://pbs.twimg.com/media/poster.jpg"),
        FakeItem("image", "https://pbs.twimg.com/media/a.jpg"),
        FakeItem("image", "https://pbs.twimg.com/media/b.jpg"),
    ])
    assert len(m.images_for_tweet(tweet)) == 2  # video skipped


def test_images_for_tweet_respects_limit(monkeypatch):
    monkeypatch.setattr(m.requests, "get",
                        lambda url, timeout: FakeResponse(make_png_bytes()))
    tweet = FakeTweet([FakeItem("image", f"https://pbs.twimg.com/media/{i}.jpg") for i in range(6)])
    assert len(m.images_for_tweet(tweet, limit=3)) == 3


def test_images_for_tweet_filters_before_limiting(monkeypatch):
    """Regression: a video-first tweet must still yield its image — the limit
    applies to fetched images, not to raw media items."""
    monkeypatch.setattr(m.requests, "get",
                        lambda url, timeout: FakeResponse(make_png_bytes()))
    tweet = FakeTweet([
        FakeItem("video", "https://pbs.twimg.com/media/poster.jpg"),
        FakeItem("image", "https://pbs.twimg.com/media/a.jpg"),
        FakeItem("image", "https://pbs.twimg.com/media/b.jpg"),
    ])
    assert len(m.images_for_tweet(tweet, limit=1)) == 1  # video skipped before limiting


def test_media_envelope_serializes_all_items_with_alt():
    tweet = FakeTweet([
        FakeItem("image", "https://pbs.twimg.com/media/a.jpg", "chart"),
        FakeItem("video", "https://pbs.twimg.com/media/v.jpg"),
    ])
    assert m.media_envelope(tweet) == [
        {"type": "image", "url": "https://pbs.twimg.com/media/a.jpg", "alt_text": "chart"},
        {"type": "video", "url": "https://pbs.twimg.com/media/v.jpg", "alt_text": None},
    ]


def test_with_images_no_images_returns_plain_envelope():
    envelope = {"ok": True, "x": 1}
    assert m.with_images(envelope, []) is envelope


def test_with_images_builds_call_tool_result(monkeypatch):
    from mcp.types import CallToolResult, ImageContent, TextContent
    monkeypatch.setattr(m.requests, "get",
                        lambda url, timeout: FakeResponse(make_png_bytes()))
    image = m.fetch_image("https://pbs.twimg.com/media/a.jpg")
    envelope = {"ok": True, "media": []}
    result = m.with_images(envelope, [image])
    assert isinstance(result, CallToolResult)
    assert result.structuredContent == envelope
    assert isinstance(result.content[0], TextContent)
    assert json.loads(result.content[0].text) == envelope
    assert any(isinstance(c, ImageContent) for c in result.content[1:])
