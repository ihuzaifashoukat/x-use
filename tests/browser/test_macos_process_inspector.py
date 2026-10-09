"""macOS ownership capture with synthetic processes and no account state."""
import asyncio
import os
import sys
import time
from types import SimpleNamespace

import pytest

from xuse.browser.sessions import (
    _OwnedBrowserProcesses,
    _ProcessInspector,
)


@pytest.mark.parametrize("padding", ["", "    ", "\t  "])
def test_macos_ps_start_time_accepts_column_padding(monkeypatch, padding):
    import xuse.browser.sessions as sessions

    stamp = "Fri Oct  9 17:37:34 2026"
    calls = []

    def ps_output(command, **kwargs):
        calls.append(command)
        assert kwargs["check"] is True and kwargs["env"]["LC_ALL"] == "C"
        return SimpleNamespace(stdout=f"  200  100 {stamp}{padding}\n")

    monkeypatch.setattr(sessions, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(sessions.subprocess, "run", ps_output)
    # Exercise the POSIX reader portably, without initializing Win32 handles.
    inspector = _ProcessInspector.__new__(_ProcessInspector)
    inspector.kernel = None
    assert inspector.snapshot() == {
        200: (100, time.mktime(time.strptime(stamp, "%a %b %d %H:%M:%S %Y")))
    }
    assert calls == [["ps", "-axo", "pid=,ppid=,lstart="]]


def test_macos_ps_start_time_still_rejects_non_whitespace_suffix(monkeypatch):
    import xuse.browser.sessions as sessions

    monkeypatch.setattr(sessions, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(sessions.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        stdout="200 100 Fri Oct  9 17:37:34 2026 forged\n",
    ))
    inspector = _ProcessInspector.__new__(_ProcessInspector)
    inspector.kernel = None
    with pytest.raises(ValueError, match="unconverted data remains"):
        inspector.snapshot()


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "darwin", reason="macOS ps identity evidence")
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
