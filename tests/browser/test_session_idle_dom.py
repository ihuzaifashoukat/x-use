"""Native idle retirement and fresh reopen with local DOM and no credentials."""
import asyncio
import importlib
import os
import time
from types import SimpleNamespace

import pytest

from xuse.browser.sessions import AccountOwnerLock, PatchrightSessionPool, PlaywrightSessionPool, SessionEntry, _driver_process


@pytest.mark.asyncio
@pytest.mark.parametrize("driver", os.environ.get("XUSE_TEST_BROWSER_DRIVERS", os.environ.get("XUSE_TEST_BROWSER_DRIVER", "patchright,playwright")).split(","))
async def test_native_idle_reclamation_drains_owned_processes_and_reopens(driver, make_config_loader, tmp_path):
    assert driver in {"patchright", "playwright"}
    try:
        importlib.import_module(f"{driver}.async_api")
    except ModuleNotFoundError:
        if os.environ.get("XUSE_REQUIRE_BROWSER_TESTS") == "1":
            pytest.fail("Required native browser driver unavailable")
        pytest.skip("Optional native browser driver unavailable")
    settings = {"mcp": {"browser_headless": True}}
    channel = os.environ.get("XUSE_TEST_BROWSER_CHANNEL", "chrome")
    if channel != "chromium":
        settings["mcp"]["browser_channel"] = channel
    pool_type = PatchrightSessionPool if driver == "patchright" else PlaywrightSessionPool
    pool = pool_type(make_config_loader(settings=settings), lock_directory=tmp_path / "locks",
                     idle_timeout_seconds=100, reap_interval_seconds=0.01)
    pins = []
    owner = AccountOwnerLock("native-idle-" + driver, tmp_path / "locks")
    try:
        try:
            browser = await pool._get_browser()
        except Exception:
            if os.environ.get("XUSE_REQUIRE_BROWSER_TESTS") == "1":
                pytest.fail("Required native browser unavailable")
            pytest.skip("Native browser unavailable")
        context = await browser.new_context(service_workers="block")
        await context.route("**/*", lambda route: route.abort())
        page = await context.new_page()
        await page.set_content("<p>Local idle lifecycle fixture</p>")
        owner.acquire()
        entry = SessionEntry(SimpleNamespace(page=page), context, owner)
        pool._entries["fixture"] = entry
        await pool._refresh_owned_processes()
        guard = pool._process_guard
        assert guard is not None
        inspector = guard.inspector
        driver_process = _driver_process(pool._runtime)
        for process in guard.processes.values():
            try:
                pins.append(inspector.pin(process.pid, process.birth, inspector.cutoff()))
            except ProcessLookupError:
                pass  # A short-lived renderer may already have exited.
        assert any(pin.pid == driver_process.pid for pin in pins)
        assert len(pins) > 1
        entry.last_used = pool._runtime_last_used = time.monotonic() - 10
        pool.idle_timeout_seconds = 0.02

        async def drained():
            while pool._runtime is not None:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(drained(), 15)
        assert not browser.is_connected() and page.is_closed()
        assert driver_process.returncode is not None
        assert all(not pin.alive() for pin in pins)
        assert owner._file is None and pool._process_guard is None and not pool._closed
        assert pool.session_status("fixture")["state"] == "cold"
        pool.idle_timeout_seconds = 100
        fresh = await pool._get_browser()
        assert fresh is not browser and fresh.is_connected()
        assert _driver_process(pool._runtime) is not driver_process
        fresh_context = await fresh.new_context(service_workers="block")
        await fresh_context.route("**/*", lambda route: route.abort())
        fresh_page = await fresh_context.new_page()
        await fresh_page.set_content("<p>Fresh local browser works</p>")
        assert await fresh_page.locator("p").inner_text() == "Fresh local browser works"
        await fresh_context.close()
    finally:
        await pool.close_all()
        owner.release()
        for pin in pins:
            pin.close()
