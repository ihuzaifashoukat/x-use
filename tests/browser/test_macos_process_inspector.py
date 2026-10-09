"""macOS ownership capture with synthetic processes and no account state."""
import asyncio
import os
import sys

import pytest

from xuse.browser.sessions import (
    _OwnedBrowserProcesses,
    _ProcessInspector,
)


pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS ps identity evidence")


@pytest.mark.asyncio
async def test_macos_threaded_capture_verifies_synthetic_child_and_natural_exit():
    child = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import sys; print('ready', flush=True); sys.stdin.readline()",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    guard = None
    try:
        assert await asyncio.wait_for(child.stdout.readline(), 5) == b"ready\n"
        inspector = _ProcessInspector()

        def capture():
            cutoff = inspector.cutoff()
            snapshot = inspector.snapshot()
            assert snapshot[child.pid][0] == os.getpid()
            return _OwnedBrowserProcesses.capture(inspector, child.pid, cutoff)

        # Production starts ownership inspection on the same thread boundary.
        # Raw ps/date exceptions remain visible in this credential-free test.
        guard = await asyncio.to_thread(capture)
        assert guard.complete and guard.processes[child.pid].alive()
        assert os.getpid() not in guard.processes
        child.stdin.close()
        await asyncio.wait_for(child.wait(), 5)
        await asyncio.to_thread(guard.refresh)
        assert guard.exited()
    finally:
        if child.returncode is None:
            child.stdin.close()
            await asyncio.wait_for(child.wait(), 5)
        if guard is not None:
            guard.close()
