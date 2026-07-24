"""Media downloader: retry policy and content-type enforcement.

- RETRYABLE_STATUS must gate retries: a 404 fails on the first attempt
  instead of burning the full backoff loop; a 503 is retried.
- A disallowed content-type (e.g. a text/html error page served with 200) is
  a failed download: nothing is written to disk and None is returned, so an
  HTML page can never be attached to a tweet as an image.
"""

import os

import pytest
import requests

import xuse.features.publisher.media_manager.downloader as dl


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr(dl.time, "sleep", lambda *_a: None)
    monkeypatch.setattr(dl.random, "uniform", lambda a, b: 0.0)


class FakeResp:
    def __init__(self, status=200, ctype="image/jpeg", body=b"\xff\xd8\xff" + b"x" * 10):
        self.status_code = status
        self.ok = 200 <= status < 400
        self.headers = {"content-type": ctype, "content-length": str(len(body))}
        self._body = body

    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def iter_content(self, chunk_size=1024):
        yield self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def run_download(monkeypatch, tmp_path, resp, max_retries=2):
    """Wire requests.head/get to always return `resp`; return (result, gets, saved_files)."""
    calls = {"get": 0}

    def fake_get(url, **kw):
        calls["get"] += 1
        return resp

    monkeypatch.setattr(dl.requests, "head", lambda *a, **k: resp)
    monkeypatch.setattr(dl.requests, "get", fake_get)

    out_dir = str(tmp_path / "media")
    result = dl.download_with_retries("https://x.com/img.jpg", out_dir, timeout=1, max_retries=max_retries)
    saved = []
    if os.path.isdir(out_dir):
        saved = [f for f in os.listdir(out_dir) if not f.endswith(".part")]
    return result, calls["get"], saved


def test_404_fails_immediately_without_retries(monkeypatch, tmp_path):
    result, gets, saved = run_download(monkeypatch, tmp_path, FakeResp(status=404, ctype="text/html", body=b"nope"))
    assert result is None
    assert gets == 1
    assert saved == []


def test_403_fails_immediately_without_retries(monkeypatch, tmp_path):
    result, gets, _saved = run_download(monkeypatch, tmp_path, FakeResp(status=403, body=b"forbidden"))
    assert result is None
    assert gets == 1


def test_503_is_retried_up_to_the_cap(monkeypatch, tmp_path):
    result, gets, _saved = run_download(monkeypatch, tmp_path, FakeResp(status=503), max_retries=2)
    assert result is None
    assert gets == 3  # initial attempt + 2 retries


def test_html_error_page_is_rejected_not_saved(monkeypatch, tmp_path):
    body = b"<html>rate limited</html>"
    resp = FakeResp(status=200, ctype="text/html", body=body)
    result, _gets, saved = run_download(monkeypatch, tmp_path, resp, max_retries=0)
    assert result is None
    assert saved == []


def test_json_body_is_rejected_not_saved(monkeypatch, tmp_path):
    resp = FakeResp(status=200, ctype="application/json", body=b'{"error": "gone"}')
    result, _gets, saved = run_download(monkeypatch, tmp_path, resp, max_retries=0)
    assert result is None
    assert saved == []


def test_valid_image_is_saved_and_returned(monkeypatch, tmp_path):
    body = b"\xff\xd8\xff\xe0" + b"\x00" * 32
    resp = FakeResp(status=200, ctype="image/jpeg", body=body)
    result, gets, saved = run_download(monkeypatch, tmp_path, resp)
    assert result is not None and os.path.exists(result)
    assert gets == 1
    assert len(saved) == 1
    with open(result, "rb") as f:
        assert f.read() == body


def test_generic_image_content_type_allowed(monkeypatch, tmp_path):
    resp = FakeResp(status=200, ctype="image/webp", body=b"RIFFxxxxWEBP")
    result, _gets, _saved = run_download(monkeypatch, tmp_path, resp)
    assert result is not None
