"""Media fetch hardening: the 8MB wire cap is enforced BEFORE the body is
buffered (Content-Length pre-check + streamed hard cap), and pixel bombs are
rejected from header dimensions without decoding the bitmap.
"""
import io
import struct
import zlib

from xuse.mcp import media as m


class StreamResponse:
    """Minimal streaming-capable stand-in for requests.Response."""

    def __init__(self, chunks, headers=None, status=200):
        self._chunks = list(chunks)
        self.headers = headers or {}
        self.status_code = status
        self.body_reads = 0
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def iter_content(self, chunk_size=65536):
        self.body_reads += 1
        consumed = 0
        for chunk in self._chunks:
            consumed += len(chunk)
            yield chunk
        self.fully_consumed = True

    def close(self):
        self.closed = True


def make_png_bytes(width=20, height=10, color=(200, 30, 30)) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="PNG")
    return buf.getvalue()


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def header_only_png(width: int, height: int) -> bytes:
    """PNG signature + IHDR + start of an IDAT: enough for Pillow to read the
    declared dimensions, not enough to decode a single pixel."""
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    idat_start = struct.pack(">I", 100) + b"IDAT" + b"\x78\x9c" + b"\x00" * 8
    return sig + ihdr + idat_start


# --- fix 7: size cap enforced before/while streaming the body ----------------


def test_huge_content_length_rejected_without_reading_body(monkeypatch):
    resp = StreamResponse([b"x" * 20_000_000], headers={"Content-Length": "20000000"})
    monkeypatch.setattr(m.requests, "get", lambda url, timeout, stream: resp)
    assert m.fetch_image("https://pbs.twimg.com/media/huge.jpg") is None
    assert resp.body_reads == 0  # body never streamed
    assert resp.closed


def test_stream_aborts_past_the_cap_without_content_length(monkeypatch):
    chunks = [b"y" * 1_000_000 for _ in range(20)]  # 20MB undeclared
    resp = StreamResponse(chunks)
    consumed = []

    original_iter = resp.iter_content

    def counting_iter(chunk_size=65536):
        for chunk in original_iter(chunk_size):
            consumed.append(len(chunk))
            yield chunk

    resp.iter_content = counting_iter
    monkeypatch.setattr(m.requests, "get", lambda url, timeout, stream: resp)
    assert m.fetch_image("https://pbs.twimg.com/media/huge.jpg") is None
    assert sum(consumed) <= m.MAX_DOWNLOAD_BYTES + 1_000_000  # aborted early
    assert not getattr(resp, "fully_consumed", False)
    assert resp.closed


def test_get_is_called_with_streaming_enabled(monkeypatch):
    calls = {}

    def fake_get(url, timeout, stream):
        calls["stream"] = stream
        return StreamResponse([make_png_bytes()])

    monkeypatch.setattr(m.requests, "get", fake_get)
    assert m.fetch_image("https://pbs.twimg.com/media/a.jpg") is not None
    assert calls["stream"] is True


def test_small_streamed_image_still_works(monkeypatch):
    png = make_png_bytes(3000, 2000)
    chunks = [png[i:i + 65536] for i in range(0, len(png), 65536)]
    resp = StreamResponse(chunks, headers={"Content-Length": str(len(png))})
    monkeypatch.setattr(m.requests, "get", lambda url, timeout, stream: resp)
    block = m.fetch_image("https://pbs.twimg.com/media/a.jpg")
    assert block is not None and block.mimeType == "image/jpeg"


def test_unparseable_content_length_falls_through_to_streaming_cap(monkeypatch):
    resp = StreamResponse([b"z" * 9_000_000], headers={"Content-Length": "garbage"})
    monkeypatch.setattr(m.requests, "get", lambda url, timeout, stream: resp)
    assert m.fetch_image("https://pbs.twimg.com/media/huge.jpg") is None


# --- fix 6: pixel-bomb gate before decode -------------------------------------


def test_oversized_dimensions_rejected_from_headers_without_decode(monkeypatch):
    """8000x8000 = 64M pixels: under the 8MB wire cap, over the pixel cap.
    Only the header is present, so any decode attempt would fail — a None
    here proves the gate fired on dimensions alone."""
    resp = StreamResponse([header_only_png(8000, 8000)])
    monkeypatch.setattr(m.requests, "get", lambda url, timeout, stream: resp)
    assert m.fetch_image("https://pbs.twimg.com/media/bomb.jpg") is None


def test_pixel_gate_precedes_decode(monkeypatch):
    from PIL import Image

    class HeaderOnlyImage:
        size = (9000, 9000)

        def convert(self, *args, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("decode must not happen past the pixel gate")

    monkeypatch.setattr(Image, "open", lambda *a, **k: HeaderOnlyImage())
    resp = StreamResponse([b"anything"])
    monkeypatch.setattr(m.requests, "get", lambda url, timeout, stream: resp)
    assert m.fetch_image("https://pbs.twimg.com/media/bomb.jpg") is None


def test_pillow_decompression_bomb_error_is_caught(monkeypatch):
    """30000x30000 = 900M pixels: Pillow itself raises DecompressionBombError
    at open — that must degrade to None, not escape."""
    resp = StreamResponse([header_only_png(30000, 30000)])
    monkeypatch.setattr(m.requests, "get", lambda url, timeout, stream: resp)
    assert m.fetch_image("https://pbs.twimg.com/media/bomb.jpg") is None


def test_normal_dimensions_under_the_cap_pass(monkeypatch):
    resp = StreamResponse([make_png_bytes(3000, 2000)])  # 6M pixels
    monkeypatch.setattr(m.requests, "get", lambda url, timeout, stream: resp)
    assert m.fetch_image("https://pbs.twimg.com/media/a.jpg") is not None
