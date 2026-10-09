"""Bind a reviewed attachment to the exact bytes uploaded by the browser."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
from pathlib import Path
import tempfile

from xuse.core.local_state import private_state_file
from .errors import BrowserActionError


def _copy_reviewed(paths, manifest, directory):
    if not isinstance(paths, (list, tuple)) or not isinstance(manifest, list) or len(paths) != len(manifest) or not 1 <= len(paths) <= 4:
        raise BrowserActionError("media_invalid")
    copies = []
    for index, (value, expected) in enumerate(zip(paths, manifest)):
        source = Path(value).resolve()
        if not isinstance(expected, dict) or expected.get("path") != str(source):
            raise BrowserActionError("media_invalid")
        maximum = 512 * 1024 * 1024 if source.suffix.lower() in {".mp4", ".mov"} else 20 * 1024 * 1024
        target = Path(directory) / (str(index) + source.suffix)
        private_state_file(target)
        digest, total = hashlib.sha256(), 0
        with source.open("rb") as incoming, target.open("wb") as outgoing:
            while chunk := incoming.read(1024 * 1024):
                total += len(chunk)
                if total > maximum:
                    raise BrowserActionError("media_invalid")
                digest.update(chunk)
                outgoing.write(chunk)
        if total == 0 or digest.hexdigest() != expected.get("sha256"):
            raise BrowserActionError("media_invalid")
        copies.append(str(target))
    return copies


@asynccontextmanager
async def reviewed_uploads(paths, manifest=None):
    """Keep private snapshots alive until submission/confirmation completes.

    Original file edits during page navigation cannot replace reviewed bytes.
    No assertion is made against malicious code running as the same OS user.
    """
    if manifest is None:
        yield paths
        return
    with tempfile.TemporaryDirectory(prefix="xuse-reviewed-upload-") as directory:
        # Cleanup operates only on this helper's newly created temporary root.
        directory = Path(directory).resolve()
        if not directory.is_relative_to(Path(tempfile.gettempdir()).resolve()):
            raise BrowserActionError("media_invalid")
        task = asyncio.create_task(asyncio.to_thread(_copy_reviewed, paths, manifest, directory))
        try:
            snapshots = await asyncio.shield(task)
        except asyncio.CancelledError:
            # A worker thread cannot be cancelled. Let it close files before
            # the temporary directory is cleaned up; never start an upload.
            try:
                await task
            except Exception:
                pass
            raise
        yield snapshots
