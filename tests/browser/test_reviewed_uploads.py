"""Reviewed bytes survive original-path changes without leaving temp files."""
import asyncio
import hashlib
from pathlib import Path
import threading

import pytest

from xuse.browser.errors import BrowserActionError
from xuse.browser import uploads


def approved(path):
    return [{"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}]


@pytest.mark.asyncio
async def test_source_edit_after_snapshot_does_not_change_upload(tmp_path):
    source = tmp_path / "image.png"
    source.write_bytes(b"approved-image-bytes")
    async with uploads.reviewed_uploads([str(source)], approved(source)) as paths:
        copy = Path(paths[0])
        assert copy != source and copy.read_bytes() == b"approved-image-bytes"
        source.write_bytes(b"changed-source")
        assert copy.read_bytes() == b"approved-image-bytes"
    assert not copy.exists() and source.read_bytes() == b"changed-source"


@pytest.mark.asyncio
async def test_changed_source_is_rejected_before_any_upload(tmp_path):
    source = tmp_path / "image.png"
    source.write_bytes(b"approved")
    manifest = approved(source)
    source.write_bytes(b"changed")
    with pytest.raises(BrowserActionError, match="media_invalid"):
        async with uploads.reviewed_uploads([str(source)], manifest):
            pytest.fail("Changed bytes cannot reach the browser.")


@pytest.mark.asyncio
async def test_cancelled_copy_finishes_before_temp_cleanup(tmp_path, monkeypatch):
    source = tmp_path / "image.png"
    source.write_bytes(b"approved")
    manifest = approved(source)
    started, release = threading.Event(), threading.Event()
    folders = []
    original = uploads._copy_reviewed

    def delayed(paths, expected, directory):
        folders.append(Path(directory))
        started.set()
        assert release.wait(5)
        return original(paths, expected, directory)

    monkeypatch.setattr(uploads, "_copy_reviewed", delayed)

    async def use():
        async with uploads.reviewed_uploads([str(source)], manifest):
            pytest.fail("Cancelled copy cannot upload.")

    task = asyncio.create_task(use())
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    assert folders[0].exists()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not folders[0].exists()
