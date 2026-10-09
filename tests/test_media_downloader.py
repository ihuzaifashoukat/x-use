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
        self.close_count = 0

    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def iter_content(self, chunk_size=1024):
        yield self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False

    def close(self):
        self.close_count += 1


class FakeSession:
    trust_env = False

    def __init__(self, resp):
        self.resp = resp
        self.head_resp = FakeResp(body=b"")
        # Deliberately different: HEAD metadata must not validate the GET body.
        self.head_resp.headers["content-length"] = "999"
        self.get_calls = 0
        self.closed = False

    def head(self, url, **kw):
        return self.head_resp

    def get(self, url, **kw):
        self.get_calls += 1
        return self.resp

    def close(self):
        self.closed = True


def run_download(monkeypatch, tmp_path, resp, max_retries=2):
    """Wire requests.head/get to always return `resp`; return (result, gets, saved_files)."""
    session = FakeSession(resp)
    resp.session = session
    monkeypatch.setattr(dl, "_new_requests_session", lambda: session)

    out_dir = str(tmp_path / "media")
    result = dl.download_with_retries("https://x.com/img.jpg", out_dir, timeout=1, max_retries=max_retries)
    saved = []
    if os.path.isdir(out_dir):
        saved = [f for f in os.listdir(out_dir) if not f.endswith(".part")]
    return result, session.get_calls, saved


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
    resp = FakeResp(status=503)
    result, gets, _saved = run_download(monkeypatch, tmp_path, resp, max_retries=2)
    assert result is None
    assert gets == 3  # initial attempt + 2 retries
    assert resp.close_count == 3
    assert resp.session.head_resp.close_count == 3
    assert resp.session.closed


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
    assert resp.close_count == 1
    assert resp.session.head_resp.close_count == 1
    assert resp.session.closed
    with open(result, "rb") as f:
        assert f.read() == body


def test_generic_image_content_type_allowed(monkeypatch, tmp_path):
    resp = FakeResp(status=200, ctype="image/webp", body=b"RIFFxxxxWEBP")
    result, _gets, _saved = run_download(monkeypatch, tmp_path, resp)
    assert result is not None


@pytest.mark.parametrize(
    "header_name",
    ["../../escape.jpg", r"..\..\escape.jpg", r"C:\outside\escape.jpg"],
)
def test_content_disposition_path_is_reduced_to_safe_basename(
    monkeypatch, tmp_path, header_name
):
    resp = FakeResp()
    resp.headers["content-disposition"] = f'attachment; filename="{header_name}"'
    result, _gets, saved = run_download(monkeypatch, tmp_path, resp, max_retries=0)

    media_dir = tmp_path / "media"
    assert result is not None
    assert os.path.commonpath((str(media_dir.resolve()), os.path.realpath(result))) == str(media_dir.resolve())
    assert os.path.basename(result) == "escape.jpg"
    assert saved == ["escape.jpg"]
    assert not (tmp_path / "escape.jpg").exists()


def test_existing_filename_symlink_cannot_escape_output_directory(monkeypatch, tmp_path):
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"keep")
    try:
        (media_dir / "img.jpg").symlink_to(outside)
    except OSError:
        pytest.skip("Creating symlinks is unavailable on this system.")

    resp = FakeResp()
    result, _gets, _saved = run_download(monkeypatch, tmp_path, resp, max_retries=0)

    assert result is None
    assert outside.read_bytes() == b"keep"
    assert (media_dir / "img.jpg").is_symlink()


class RedirectProbeAdapter(requests.adapters.BaseAdapter):
    """Synthetic redirect chain that records only prepared request headers."""

    def __init__(self):
        self.seen = []

    def send(self, request, **kwargs):
        self.seen.append((request.url, dict(request.headers), kwargs.get("proxies")))
        response = requests.Response()
        response.request = request
        response.url = request.url
        response._content_consumed = True
        if "x.com" in request.url:
            response.status_code = 302
            response.headers["Location"] = "https://cdn.example.test/image.jpg"
            response.headers["Set-Cookie"] = "x-session=redirect-cookie; Domain=x.com; Path=/"
            response._content = b""
        elif request.method == "HEAD":
            response.status_code = 200
            response.headers["Content-Length"] = "12"
            response._content = b""
        else:
            response.status_code = 200
            response.headers["Content-Type"] = "image/jpeg"
            response.headers["Content-Length"] = "12"
            response._content = b"\xff\xd8\xff" + b"x" * 9
        return response

    def close(self):
        pass


def test_public_media_redirect_never_receives_account_credentials(monkeypatch, tmp_path):
    adapter = RedirectProbeAdapter()

    def new_session():
        session = requests.Session()
        session.trust_env = False
        session.mount("https://", adapter)
        return session

    monkeypatch.setattr(dl, "_new_requests_session", new_session)

    class Driver:
        def get_cookies(self):
            return [{"name": "auth_token", "value": "synthetic-secret-cookie", "domain": ".x.com"}]

    class Manager:
        driver = Driver()
        effective_proxy = "http://proxy-user:proxy-secret@proxy.example.test:8080"

    result = dl.download_with_retries(
        "https://x.com/media/image.jpg?token=synthetic-url-secret",
        str(tmp_path / "media"), max_retries=0, browser_manager=Manager(),
    )

    assert result is not None
    external = [entry for entry in adapter.seen if "cdn.example.test" in entry[0]]
    assert external
    for _url, headers, proxies in external:
        assert "Cookie" not in headers
        assert "Authorization" not in headers
        assert proxies == {"http": Manager.effective_proxy, "https": Manager.effective_proxy}


def test_download_errors_do_not_log_proxy_secrets(monkeypatch, tmp_path, caplog):
    class ExplodingSession(FakeSession):
        def head(self, url, **kw):
            raise requests.ConnectionError("failed via http://proxy-user:proxy-secret@proxy.example.test")

        def get(self, url, **kw):
            raise requests.ConnectionError("failed via http://proxy-user:proxy-secret@proxy.example.test")

    monkeypatch.setattr(dl, "_new_requests_session", lambda: ExplodingSession(None))
    dl.download_with_retries("https://x.com/image.jpg", str(tmp_path / "media"),
                             max_retries=0, browser_manager=type("M", (), {
                                 "effective_proxy": "http://proxy-user:proxy-secret@proxy.example.test"
                             })())
    assert "proxy-secret" not in caplog.text


def test_interruption_closes_session(monkeypatch, tmp_path):
    class InterruptedSession(FakeSession):
        def get(self, url, **kw):
            raise KeyboardInterrupt()

    session = InterruptedSession(None)
    monkeypatch.setattr(dl, "_new_requests_session", lambda: session)
    with pytest.raises(KeyboardInterrupt):
        dl.download_with_retries("https://x.com/image.jpg", str(tmp_path / "media"), max_retries=0)
    assert session.head_resp.close_count == 1
    assert session.closed


def test_compressed_get_ignores_wire_content_length(monkeypatch, tmp_path):
    resp = FakeResp(body=b"decoded-image-body")
    resp.headers["content-encoding"] = "gzip"
    resp.headers["content-length"] = "8"
    result, _gets, _saved = run_download(monkeypatch, tmp_path, resp, max_retries=0)
    assert result is not None


def test_head_length_does_not_override_get_response_length(monkeypatch, tmp_path):
    resp = FakeResp(body=b"image-body")
    result, _gets, _saved = run_download(monkeypatch, tmp_path, resp, max_retries=0)
    assert result is not None
    assert resp.session.head_resp.headers["content-length"] == "999"
    assert resp.close_count == 1
