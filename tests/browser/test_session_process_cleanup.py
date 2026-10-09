"""Windows OS-handle evidence for cleanup of a synthetic native browser.

No account configuration or real X pages are used. Only process handles from
the private test driver and its descendants may be terminated during teardown.
"""
import asyncio
import ctypes
import importlib
import os
import sys
import time
from ctypes import wintypes
from types import SimpleNamespace

import pytest

from xuse.browser.sessions import AccountOwnerLock, PatchrightSessionPool, PlaywrightSessionPool, SessionEntry, _OwnedBrowserProcesses, _ProcessInspector


def owned_process_handles(driver_pid):
    """Snapshot descendants, then pin identities with OS handles (no PID reuse)."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)

    class ProcessEntry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]

    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    for method in (kernel.Process32FirstW, kernel.Process32NextW):
        method.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
        method.restype = wintypes.BOOL
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.GetSystemTimeAsFileTime.argtypes = [ctypes.POINTER(wintypes.FILETIME)]
    cutoff = wintypes.FILETIME()
    kernel.GetSystemTimeAsFileTime(ctypes.byref(cutoff))
    snapshot_time = (cutoff.dwHighDateTime << 32) | cutoff.dwLowDateTime
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
    assert snapshot != ctypes.c_void_p(-1).value
    parents, executables = {}, {}
    item = ProcessEntry()
    item.dwSize = ctypes.sizeof(item)
    try:
        found = kernel.Process32FirstW(snapshot, ctypes.byref(item))
        while found:
            parents[item.th32ProcessID] = item.th32ParentProcessID
            executables[item.th32ProcessID] = item.szExeFile
            found = kernel.Process32NextW(snapshot, ctypes.byref(item))
    finally:
        kernel.CloseHandle(snapshot)
    owned = {driver_pid}
    while True:
        children = {pid for pid, parent in parents.items() if parent in owned}
        if children <= owned:
            break
        owned.update(children)
    handles, identities = [], {}
    for pid in owned:
        handle = kernel.OpenProcess(0x00100000 | 0x0001 | 0x1000, False, pid)  # SYNCHRONIZE | TERMINATE | QUERY_LIMITED_INFORMATION
        if handle:
            times = [wintypes.FILETIME() for _ in range(4)]
            queried = kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times))
            created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
            if queried and created <= snapshot_time:
                handles.append(handle)
                identities[handle] = {"pid": pid, "parent_pid": parents.get(pid), "executable": executables.get(pid)}
            else:
                # A PID reused after the snapshot belongs to another process.
                kernel.CloseHandle(handle)
    if len(handles) <= 1:
        for handle in handles:
            kernel.CloseHandle(handle)
    assert len(handles) > 1, "Native browser descendants must be observed."
    return kernel, handles, identities


@pytest.mark.skipif(os.name != "nt", reason="Windows process-handle evidence")
@pytest.mark.asyncio
@pytest.mark.parametrize("driver", os.environ.get("XUSE_TEST_BROWSER_DRIVER", os.environ.get("XUSE_TEST_BROWSER_DRIVERS", "patchright,playwright")).split(","))
@pytest.mark.parametrize("failure", ["none", "context", "context_browser", "driver_exit"])
async def test_native_synthetic_cleanup_drains_owned_processes(driver, failure, make_config_loader, tmp_path, record_property):
    assert driver in {"patchright", "playwright"}
    try:
        importlib.import_module(f"{driver}.async_api")
    except ModuleNotFoundError:
        if os.environ.get("XUSE_REQUIRE_BROWSER_TESTS") == "1":
            pytest.fail("Required native browser driver unavailable")
        pytest.skip("Optional native browser driver unavailable")
    channel = os.environ.get("XUSE_TEST_BROWSER_CHANNEL", "chrome")
    settings = {"mcp": {"browser_headless": True}}
    if channel != "chromium":
        settings["mcp"]["browser_channel"] = channel
    pool_type = PatchrightSessionPool if driver == "patchright" else PlaywrightSessionPool
    pool = pool_type(make_config_loader(settings=settings), lock_directory=tmp_path / "locks")
    kernel, handles = None, []
    owner = None
    try:
        try:
            browser = await pool._get_browser()
        except Exception:
            if os.environ.get("XUSE_REQUIRE_BROWSER_TESTS") == "1":
                pytest.fail("Required native browser unavailable")
            pytest.skip("Native browser unavailable")
        context = await browser.new_context(service_workers="block")
        await context.route("**/*", lambda route: route.abort())
        await context.add_cookies([
            {"name": "auth_token", "value": "synthetic-only", "domain": ".x.com", "path": "/", "secure": True},
            {"name": "ct0", "value": "synthetic-only", "domain": ".x.com", "path": "/", "secure": True},
        ])
        page = await context.new_page()
        await page.set_content("<p>Synthetic local process lifecycle</p>")
        # Private transport inspection is confined to this test. Production
        # lifecycle code continues to use the driver's public close APIs.
        process = pool._runtime._impl_obj._connection._transport._proc
        kernel, handles, identities = owned_process_handles(process.pid)
        cdp = await browser.new_browser_cdp_session()
        info = await cdp.send("SystemInfo.getProcessInfo")
        roles = {item["id"]: item["type"] for item in info["processInfo"]}
        await cdp.detach()
        for identity in identities.values():
            identity["role"] = "driver" if identity["pid"] == process.pid else roles.get(identity["pid"], "unlisted_descendant")
        # Transient utility children may exit between the process snapshot
        # and CDP role query. The test driver and main browser must be live;
        # already-exited pinned children still count as successfully drained.
        assert any(identities[handle]["role"] == "driver" and kernel.WaitForSingleObject(handle, 0) == 258 for handle in handles)
        assert any(identities[handle]["role"] == "browser" and kernel.WaitForSingleObject(handle, 0) == 258 for handle in handles)
        record_property("owned_process_count", len(handles))
        owner = AccountOwnerLock("c" * 64, tmp_path / "locks")
        owner.acquire()
        entry = SessionEntry(SimpleNamespace(page=page), context, owner)
        pool._entries["synthetic"] = entry
        release = owner.release
        remaining_at_release = []

        def observed_release():
            if owner._file is not None:
                remaining_at_release.append(sum(kernel.WaitForSingleObject(handle, 0) == 258 for handle in handles))
            release()

        owner.release = observed_release

        async def fail_close():
            raise RuntimeError("synthetic injected close failure")

        if failure in {"context", "context_browser"}:
            context.close = fail_close
        if failure in {"context_browser", "driver_exit"}:
            browser.close = fail_close
        if failure == "driver_exit":
            process.terminate()  # Only the driver process this test launched.
            await asyncio.wait_for(process.wait(), 5)
        started = time.monotonic()
        await pool.close_all()
        deadline = time.monotonic() + 5
        while any(kernel.WaitForSingleObject(handle, 0) == 258 for handle in handles) and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        record_property("cleanup_seconds", round(time.monotonic() - started, 3))
        record_property("processes_remaining_at_owner_release", remaining_at_release)
        remaining = [identities[handle] for handle in handles if kernel.WaitForSingleObject(handle, 0) == 258]
        record_property("remaining_processes_after_cleanup", remaining)
        if failure == "context_browser":
            assert remaining_at_release == [0], "Successful runtime stop must drain the observed native tree."
        assert all(kernel.WaitForSingleObject(handle, 0) == 0 for handle in handles), "Owned native processes survived cleanup."
        assert owner._file is None
        assert not pool._cleanup_tasks and not pool._stranded_owners
    finally:
        if kernel:
            for handle in handles:
                if kernel.WaitForSingleObject(handle, 0) == 258:
                    kernel.TerminateProcess(handle, 1)  # Pinned test-owned identity only.
            deadline = time.monotonic() + 5
            while any(kernel.WaitForSingleObject(handle, 0) == 258 for handle in handles) and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
        await pool.close_all()
        if owner:
            owner.release()
        if kernel:
            for handle in handles:
                kernel.CloseHandle(handle)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux pidfd identity evidence")
@pytest.mark.asyncio
async def test_linux_pidfd_cleanup_excludes_foreign_same_executable(tmp_path):
    if not hasattr(os, "pidfd_open"):
        pytest.skip("Atomic Linux process handles unavailable")
    program = (
        "import subprocess,sys,time\n"
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
        "print(child.pid,flush=True)\n"
        "time.sleep(60)\n"
    )
    owned = await asyncio.create_subprocess_exec(sys.executable, "-c", program, stdout=asyncio.subprocess.PIPE)
    foreign = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time; time.sleep(60)")
    guard = None
    try:
        child = int(await asyncio.wait_for(owned.stdout.readline(), 3))
        inspector = _ProcessInspector()
        guard = await asyncio.to_thread(_OwnedBrowserProcesses.capture, inspector, owned.pid, inspector.cutoff())
        assert child in guard.processes and foreign.pid not in guard.processes
        await asyncio.to_thread(guard.terminate)
        await asyncio.wait_for(owned.wait(), 3)
        deadline = time.monotonic() + 3
        while not guard.exited() and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        assert guard.exited() and foreign.returncode is None
    finally:
        if guard:
            guard.terminate()
            guard.close()
        for process in (owned, foreign):
            if process.returncode is None:
                process.kill()  # Only asyncio subprocess identities created by this test.
            await process.wait()
