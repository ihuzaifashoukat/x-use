import asyncio
import hashlib
import subprocess
import sys

import pytest

from xuse.browser.errors import BrowserBlocked, SessionError
from xuse.browser.sessions import AccountOwnerLock, PatchrightSessionPool, PlaywrightSessionPool, SessionPool, resolve_account_proxy


class FakeContext:
    def __init__(self):
        self.closed = False
        self.cookies = []
        self.page_gate = None
        self.page_requested = asyncio.Event()

    def set_default_timeout(self, value):
        self.timeout = value

    def set_default_navigation_timeout(self, value):
        self.navigation_timeout = value

    async def add_cookies(self, value):
        self.cookies = value

    async def new_page(self):
        self.page_requested.set()
        if self.page_gate:
            await self.page_gate.wait()
        return object()

    async def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self):
        self.contexts = []
        self.closed = False
        self.page_gate = None

    async def new_context(self, **kwargs):
        context = FakeContext()
        context.options = kwargs
        context.page_gate = self.page_gate
        self.contexts.append(context)
        return context

    async def close(self):
        self.closed = True

    def is_connected(self):
        return not self.closed


class FakeRuntime:
    def __init__(self):
        self.chromium = self
        self.browser = FakeBrowser()
        self.launches = 0
        self.stopped = False

    async def launch(self, **kwargs):
        self.launches += 1
        self.launch_options = kwargs
        assert set(kwargs) <= {"headless", "channel", "args"}
        assert kwargs["args"] == [
            "--webrtc-ip-handling-policy=disable_non_proxied_udp",
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        ]
        return self.browser

    async def stop(self):
        self.stopped = True


class FakeXBrowser:
    backend = "playwright"

    def __init__(self, page, account):
        self.page, self.account = page, account

    async def navigate(self, url):
        assert url == "https://x.com/home"

    async def ensure_ready(self):
        return {"success": True}


@pytest.fixture(params=(PatchrightSessionPool, PlaywrightSessionPool), ids=("patchright", "playwright"))
def make_pool(request, make_config_loader, tmp_path, monkeypatch):
    import xuse.browser.page
    monkeypatch.setattr(xuse.browser.page, "XBrowser", FakeXBrowser)
    created = []

    def build(**kwargs):
        accounts = [{"account_id": key, "cookies": [
            {"name": "auth_token", "value": "test-" + key, "secure": True, "domain": ".x.com"},
            {"name": "ct0", "value": "test-csrf", "secure": True, "domain": ".x.com"},
        ]} for key in ("a", "b", "c")]
        loader = make_config_loader(settings={"mcp": {}}, accounts=accounts)
        runtime = FakeRuntime()
        pool = request.param(loader, playwright_factory=lambda: runtime,
                             lock_directory=tmp_path / "owner-locks", **kwargs)
        created.append(pool)
        return pool, runtime

    yield build
    # Each test closes its pool so no task/lock is left on a closed event loop.
    assert all(pool._closed for pool in created)


@pytest.mark.asyncio
async def test_lazy_creation_and_context_isolation(make_pool):
    pool, runtime = make_pool()
    assert runtime.launches == 0 and list(pool.active_accounts) == []
    try:
        first = await pool.acquire("a")
        assert await pool.acquire("a") is first
        second = await pool.acquire("b")
        assert first.context is not second.context
        assert first.context.cookies[0]["value"] != second.context.cookies[0]["value"]
        assert runtime.launches == 1
        assert first.browser_manager.backend == pool.backend
        assert first.browser_manager.messaging_timeout_ms == 60_000
    finally:
        await pool.close_all()
    assert runtime.stopped and runtime.browser.closed
    assert all(context.closed for context in runtime.browser.contexts)


def test_default_pool_uses_patchright():
    assert SessionPool is PatchrightSessionPool
    assert PatchrightSessionPool.backend == "patchright"
    assert PlaywrightSessionPool.backend == "playwright"


@pytest.mark.asyncio
async def test_same_account_actions_are_serialized(make_pool):
    pool, _ = make_pool()
    entered = asyncio.Event()
    release = asyncio.Event()
    order = []

    async def first():
        async with pool.session("a"):
            order.append("first")
            entered.set()
            await release.wait()

    async def second():
        async with pool.session("a"):
            order.append("second")

    try:
        one = asyncio.create_task(first())
        await entered.wait()
        two = asyncio.create_task(second())
        await asyncio.sleep(0)
        assert order == ["first"]
        release.set()
        await asyncio.gather(one, two)
        assert order == ["first", "second"]
    finally:
        await pool.close_all()


@pytest.mark.asyncio
async def test_start_cancellation_closes_partial_context_and_releases_ownership(make_pool):
    pool, runtime = make_pool()
    runtime.browser.page_gate = asyncio.Event()
    try:
        task = asyncio.create_task(pool.acquire("a"))
        while not runtime.browser.contexts:
            await asyncio.sleep(0)
        await runtime.browser.contexts[0].page_requested.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert runtime.browser.contexts[0].closed
        assert pool.entry_for("a") is None
        runtime.browser.page_gate = None
        assert await pool.acquire("a")
    finally:
        await pool.close_all()


@pytest.mark.asyncio
async def test_start_timeout_closes_partial_context(make_pool):
    pool, runtime = make_pool(cold_start_timeout_seconds=0.03)
    runtime.browser.page_gate = asyncio.Event()
    try:
        with pytest.raises(SessionError, match="startup_timeout"):
            await pool.acquire("a")
        assert runtime.browser.contexts[0].closed
    finally:
        await pool.close_all()


@pytest.mark.asyncio
async def test_cancellation_at_completed_cold_start_boundary_closes_untracked_entry(make_pool):
    pool, runtime = make_pool()
    start = pool._cold_start
    caller = asyncio.current_task()
    owners = []

    async def finish_then_cancel(account_id):
        entry = await start(account_id)
        owners.append(entry.owner_lock)
        asyncio.get_running_loop().call_soon(caller.cancel)
        return entry

    pool._cold_start = finish_then_cancel
    try:
        with pytest.raises(asyncio.CancelledError):
            await pool.acquire("a")
        assert runtime.browser.contexts[0].closed
        assert owners[0]._file is None
        assert pool.entry_for("a") is None
        pool._cold_start = start
        assert await pool.acquire("a")
    finally:
        await pool.close_all()


@pytest.mark.asyncio
async def test_cancelled_close_preserves_tracking_and_does_not_close_active_context(make_pool):
    pool, _ = make_pool()
    try:
        entry = await pool.acquire("a")
        async with entry.lock:
            task = asyncio.create_task(pool.close("a"))
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert pool.entry_for("a") is entry and not entry.context.closed
        await pool.close("a")
        assert entry.context.closed and pool.entry_for("a") is None
    finally:
        await pool.close_all()


@pytest.mark.asyncio
async def test_bounded_contexts_evict_idle_but_refuse_when_all_are_busy(make_pool):
    pool, _ = make_pool(max_sessions=1)
    try:
        first = await pool.acquire("a")
        second = await pool.acquire("b")
        assert first.context.closed and len(list(pool.active_accounts)) == 1
        async with second.lock:
            with pytest.raises(SessionError, match="session_limit"):
                await pool.acquire("c")
    finally:
        await pool.close_all()


@pytest.mark.asyncio
async def test_idle_reaper_skips_locked_contexts(make_pool):
    pool, _ = make_pool(idle_timeout_seconds=0.02, reap_interval_seconds=0.01)
    try:
        entry = await pool.acquire("a")
        async with entry.lock:
            await asyncio.sleep(0.06)
            assert not entry.context.closed
        for _ in range(30):
            if entry.context.closed:
                break
            await asyncio.sleep(0.01)
        assert entry.context.closed
    finally:
        await pool.close_all()


@pytest.mark.asyncio
async def test_close_all_cancels_in_progress_start(make_pool):
    pool, runtime = make_pool()
    runtime.browser.page_gate = asyncio.Event()
    task = asyncio.create_task(pool.acquire("a"))
    while not runtime.browser.contexts:
        await asyncio.sleep(0)
    await runtime.browser.contexts[0].page_requested.wait()
    await pool.close_all()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runtime.browser.contexts[0].closed and runtime.stopped


@pytest.mark.asyncio
async def test_invalid_credentials_never_start_browser(make_pool):
    pool, runtime = make_pool()
    pool.config_loader.accounts[0]["cookies"] = [{"name": "auth_token", "value": "secret-test"}]
    try:
        with pytest.raises(SessionError):
            await pool.acquire("a")
        assert runtime.launches == 0
    finally:
        await pool.close_all()


def test_owner_lock_is_enforced_across_processes_and_released(tmp_path):
    key = "a" * 64
    lock = AccountOwnerLock(key, tmp_path)
    program = (
        "import sys\nfrom pathlib import Path\n"
        "from xuse.browser.sessions import AccountOwnerLock\n"
        "from xuse.browser.errors import SessionError\n"
        "lock=AccountOwnerLock(sys.argv[1], Path(sys.argv[2]))\n"
        "try:\n lock.acquire()\nexcept SessionError as e:\n sys.exit(23 if e.reason=='account_in_use' else 24)\n"
        "lock.release()\n"
    )
    lock.acquire()
    try:
        result = subprocess.run([sys.executable, "-c", program, key, str(tmp_path)], capture_output=True, timeout=10)
        assert result.returncode == 23
    finally:
        lock.release()
    result = subprocess.run([sys.executable, "-c", program, key, str(tmp_path)], capture_output=True, timeout=10)
    assert result.returncode == 0


@pytest.mark.asyncio
async def test_explicit_channel_is_allowlisted_and_headless_ignores_legacy_visibility(make_pool):
    pool, runtime = make_pool()
    pool.config_loader.settings.update(mcp={"browser_channel": "chrome"}, browser_settings={"headless": False})
    try:
        await pool.acquire("a")
        assert runtime.launch_options["headless"] is True
        assert runtime.launch_options["channel"] == "chrome"
    finally:
        await pool.close_all()


@pytest.mark.asyncio
async def test_failed_context_close_terminates_runtime_before_releasing_ownership(make_pool):
    pool, runtime = make_pool()
    try:
        entry = await pool.acquire("a")

        async def fail_close():
            raise RuntimeError("private-browser-error")

        entry.context.close = fail_close
        await pool.close("a")
        assert runtime.browser.closed and pool._closed
        assert entry.owner_lock._file is None
    finally:
        await pool.close_all()


@pytest.mark.parametrize("value", [
    "http://proxy.example:0", "http://proxy.example:",
    "http://bad host:8080", "http://bad\nhost:8080",
    "http://user:private-proxy-secret%GG@proxy.example:8080",
    "http://user:%FF@proxy.example:8080",
    "http://user:private-proxy-secret%0A@proxy.example:8080",
    "socks5://user:private-proxy-secret@proxy.example:1080",
    "http://proxy.example:8080/path",
    "https://proxy.example:8080?secret=private-proxy-secret",
    "http://-proxy.example:8080", "http://proxy.example:65536",
    "pool:missing", {"server": "http://proxy.example:8080"},
])
def test_invalid_proxy_fails_closed_without_credential_diagnostics(value, make_config_loader, caplog):
    loader = make_config_loader(settings={"browser_settings": {"proxy": "http://fallback.example:8080"}}, accounts=[])
    with pytest.raises(SessionError) as error:
        resolve_account_proxy(loader, {"account_id": "fixture", "proxy": value})
    assert error.value.reason == "invalid_proxy"
    assert str(error.value) == "Browser session unavailable (invalid_proxy)."
    assert error.value.__cause__ is None
    assert "private-proxy-secret" not in caplog.text


def test_proxy_env_and_fallback_are_explicit_and_private(make_config_loader, monkeypatch):
    monkeypatch.setenv("XUSE_FIXTURE_PROXY_PASSWORD", "private%40password")
    loader = make_config_loader(settings={"browser_settings": {
        "proxy": "https://fixture:${XUSE_FIXTURE_PROXY_PASSWORD}@proxy.example:8443",
    }}, accounts=[])
    account = {"account_id": "fixture"}
    expected = {"server": "https://proxy.example:8443", "username": "fixture", "password": "private@password"}
    assert resolve_account_proxy(loader, account) == expected
    assert account == {"account_id": "fixture"}
    assert resolve_account_proxy(loader, {**account, "proxy": "socks5://[2001:db8::1]:1080"}) == {
        "server": "socks5://[2001:db8::1]:1080",
    }
    assert resolve_account_proxy(loader, {**account, "proxy": "http://proxy.example"}) == {
        "server": "http://proxy.example",
    }
    monkeypatch.delenv("XUSE_FIXTURE_PROXY_PASSWORD")
    with pytest.raises(SessionError, match="invalid_proxy"):
        resolve_account_proxy(loader, account)
    monkeypatch.setenv("XUSE_FIXTURE_PROXY_PASSWORD", "")
    with pytest.raises(SessionError, match="invalid_proxy"):
        resolve_account_proxy(loader, account)


def test_pool_assignment_is_stable_and_does_not_write_rotation_state(make_config_loader, tmp_path):
    state_file = tmp_path / "rotation-state.json"
    members = ["http://first.example:8080", "http://second.example:8080"]
    loader = make_config_loader(settings={"browser_settings": {
        "proxy_pools": {"work": members}, "proxy_pool_strategy": "hash",
        "proxy_pool_state_file": str(state_file),
    }}, accounts=[])
    account = {"account_id": "fixture", "proxy": "pool:work"}
    index = int(hashlib.sha256(b"fixture").hexdigest(), 16) % len(members)
    first = resolve_account_proxy(loader, account)
    assert first == {"server": members[index]}
    assert all(resolve_account_proxy(loader, account) == first for _ in range(5))
    assert not state_file.exists()
    loader.settings["browser_settings"]["proxy_pool_strategy"] = "round_robin"
    with pytest.raises(SessionError, match="invalid_proxy"):
        resolve_account_proxy(loader, account)
    assert not state_file.exists()


@pytest.mark.asyncio
async def test_accounts_route_independently_and_explicit_refresh_reimports_credentials(make_pool, monkeypatch):
    pool, runtime = make_pool()
    monkeypatch.setenv("XUSE_FIXTURE_PROXY_PASSWORD", "private%40password")
    accounts = pool.config_loader.accounts
    accounts[0]["proxy"] = "http://alice:${XUSE_FIXTURE_PROXY_PASSWORD}@first.example:8080"
    accounts[1]["proxy"] = "socks5://second.example:1080"
    pool.config_loader.settings["browser_settings"] = {"proxy": "https://fallback.example:8443"}
    try:
        first = await pool.acquire("a")
        second = await pool.acquire("b")
        third = await pool.acquire("c")
        assert first.context.options["proxy"] == {"server": "http://first.example:8080", "username": "alice", "password": "private@password"}
        assert second.context.options["proxy"] == {"server": "socks5://second.example:1080"}
        assert third.context.options["proxy"] == {"server": "https://fallback.example:8443"}
        assert "proxy" not in runtime.launch_options
        accounts[0]["proxy"] = "http://replacement.example:8080"
        accounts[0]["cookies"][0]["value"] = "replacement-auth-fixture"
        # Account update/close_session deliberately invalidates a warm context;
        # cookie imports are never silently substituted in an active context.
        await pool.close("a")
        refreshed = await pool.acquire("a")
        assert first.context.closed and refreshed.context is not first.context
        assert refreshed.context.options["proxy"] == {"server": "http://replacement.example:8080"}
        assert refreshed.context.cookies[0]["value"] == "replacement-auth-fixture"
        assert pool.entry_for("b") is second and not second.context.closed
        assert second.context.cookies[0]["value"] == "test-b"
    finally:
        await pool.close_all()


@pytest.mark.asyncio
async def test_bad_account_proxy_never_starts_browser_or_uses_global_fallback(make_pool):
    pool, runtime = make_pool()
    pool.config_loader.accounts[0]["proxy"] = "http://proxy.example:0"
    pool.config_loader.settings["browser_settings"] = {"proxy": "http://fallback.example:8080"}
    try:
        with pytest.raises(SessionError, match="invalid_proxy"):
            await pool.acquire("a")
        assert runtime.launches == 0 and runtime.browser.contexts == []
        pool.config_loader.accounts[0]["proxy"] = "http://valid.example:8080"
        entry = await pool.acquire("a")
        assert entry.context.options["proxy"]["server"] == "http://valid.example:8080"
    finally:
        await pool.close_all()


@pytest.mark.asyncio
async def test_proxy_context_failure_is_redacted_and_releases_account_owner(make_pool, caplog):
    pool, runtime = make_pool()
    pool.config_loader.accounts[0]["proxy"] = "http://user:private-proxy-secret@proxy.example:8080"
    create_context = runtime.browser.new_context

    async def fail_context(**kwargs):
        raise RuntimeError("Browser failure: user private-proxy-secret test-a")

    runtime.browser.new_context = fail_context
    try:
        with pytest.raises(SessionError) as error:
            await pool.acquire("a")
        assert error.value.reason == "startup_failed"
        assert "private-proxy-secret" not in str(error.value) + caplog.text
        assert error.value.__cause__ is None and pool.entry_for("a") is None
        runtime.browser.new_context = create_context
        assert await pool.acquire("a")  # Failed startup released its OS lock.
    finally:
        await pool.close_all()


@pytest.mark.asyncio
async def test_account_alias_with_same_auth_cookie_cannot_create_second_context(make_pool):
    pool, runtime = make_pool()
    pool.config_loader.accounts[1]["cookies"][0]["value"] = "test-a"
    try:
        first = await pool.acquire("a")
        with pytest.raises(SessionError, match="account_in_use"):
            await pool.acquire("b")
        assert len(runtime.browser.contexts) == 1
        await pool.close("a")
        second = await pool.acquire("b")
        assert first.context.closed and second.context is not first.context
    finally:
        await pool.close_all()


@pytest.mark.asyncio
async def test_closed_page_blocks_action_until_explicit_context_refresh(make_pool):
    pool, _ = make_pool()
    writes = []
    try:
        entry = await pool.acquire("a")
        entry.browser_manager.page = type("ClosedPage", (), {"is_closed": lambda self: True})()
        with pytest.raises(SessionError) as error:
            async with pool.session("a"):
                writes.append("must not run")
        assert error.value.reason == "session_expired" and "close_session" in error.value.operator_hint
        assert writes == [] and not entry.lock.locked()
        assert not entry.context.closed  # The operator chooses when to refresh.
        await pool.close("a")
        async with pool.session("a") as browser:
            assert browser is not entry.browser_manager
        assert entry.context.closed
    finally:
        await pool.close_all()


@pytest.mark.asyncio
async def test_disconnected_browser_blocks_action_without_relaunch_or_retry(make_pool):
    pool, runtime = make_pool()
    writes = []
    try:
        entry = await pool.acquire("a")
        runtime.browser.closed = True
        with pytest.raises(SessionError) as error:
            async with pool.session("a"):
                writes.append("must not run")
        assert error.value.reason == "session_expired" and "Restart the MCP server" in error.value.operator_hint
        assert writes == [] and runtime.launches == 1 and not entry.lock.locked()
        await pool.close("a")
        with pytest.raises(SessionError, match="browser_unavailable") as cold_error:
            await pool.acquire("a")
        assert "Restart the MCP server" in cold_error.value.operator_hint
        assert runtime.launches == 1  # A server restart creates a new runtime.
    finally:
        await pool.close_all()


@pytest.mark.asyncio
async def test_rejected_login_closes_partial_context_and_explicit_fresh_auth_works(make_pool, monkeypatch):
    pool, runtime = make_pool()
    original = FakeXBrowser.ensure_ready

    async def reject_login(self):
        raise BrowserBlocked("login_required")

    monkeypatch.setattr(FakeXBrowser, "ensure_ready", reject_login)
    try:
        with pytest.raises(BrowserBlocked, match="login_required"):
            await pool.acquire("a")
        assert pool.entry_for("a") is None and runtime.browser.contexts[0].closed
        pool.config_loader.accounts[0]["cookies"][0]["value"] = "fresh-auth-fixture"
        monkeypatch.setattr(FakeXBrowser, "ensure_ready", original)
        async with pool.session("a"):
            entry = pool.entry_for("a")
            assert entry.context.cookies[0]["value"] == "fresh-auth-fixture"
        assert len(runtime.browser.contexts) == 2
    finally:
        await pool.close_all()


@pytest.mark.asyncio
async def test_cancelled_close_keeps_entry_busy_until_context_cleanup_finishes(make_pool):
    pool, _ = make_pool()
    closing = asyncio.Event()
    finish = asyncio.Event()
    waiter = None
    try:
        entry = await pool.acquire("a")
        original_close = entry.context.close

        async def slow_close():
            closing.set()
            await finish.wait()
            await original_close()

        entry.context.close = slow_close
        closer = asyncio.create_task(pool.close("a"))
        await closing.wait()
        closer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closer
        pool.config_loader.accounts[0]["cookies"][0]["value"] = "rotated-fixture-auth"
        waiter = asyncio.create_task(pool.acquire("a"))
        await asyncio.sleep(0)
        assert pool.entry_for("a") is entry
        assert entry.closing and entry.lock.locked() and not entry.close_done.is_set()
        assert not waiter.done() and not entry.context.closed
        finish.set()
        fresh = await asyncio.wait_for(waiter, 2)
        assert entry.context.closed and entry.owner_lock._file is None
        assert fresh is not entry
    finally:
        finish.set()
        if waiter is not None and not waiter.done():
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        await pool.close_all()


@pytest.mark.asyncio
async def test_failed_eviction_cannot_start_another_context_on_closed_pool(make_pool):
    pool, runtime = make_pool(max_sessions=1)
    try:
        entry = await pool.acquire("a")

        async def fail_close():
            raise RuntimeError("synthetic cleanup failure")

        entry.context.close = runtime.browser.close = runtime.stop = fail_close
        with pytest.raises(SessionError, match="pool_closed"):
            await pool.acquire("b")
        assert len(runtime.browser.contexts) == 1
        assert entry.owner_lock._file is not None
        assert pool.entry_for("b") is None
    finally:
        runtime.browser.close = FakeBrowser.close.__get__(runtime.browser)
        runtime.stop = FakeRuntime.stop.__get__(runtime)
        await pool.close_all()


@pytest.mark.asyncio
async def test_export_expiry_does_not_invalidate_a_healthy_warm_context(make_pool, monkeypatch):
    import xuse.browser.cookies
    pool, runtime = make_pool()
    for cookie in pool.config_loader.accounts[0]["cookies"]:
        cookie["expires"] = 1100
    monkeypatch.setattr(xuse.browser.cookies.time, "time", lambda: 1000)
    try:
        entry = await pool.acquire("a")
        monkeypatch.setattr(xuse.browser.cookies.time, "time", lambda: 1200)
        async with pool.session("a") as manager:
            assert manager is entry.browser_manager
        assert len(runtime.browser.contexts) == 1
        await pool.close("a")
        with pytest.raises(SessionError, match="expired_credentials"):
            await pool.acquire("a")
        assert len(runtime.browser.contexts) == 1
    finally:
        await pool.close_all()


@pytest.mark.asyncio
async def test_invalidated_export_cannot_replace_cookies_inside_warm_context(make_pool):
    pool, runtime = make_pool()
    try:
        entry = await pool.acquire("a")
        pool.config_loader.accounts[0]["cookies"] = []
        async with pool.session("a") as manager:
            assert manager is entry.browser_manager
        assert entry.context.cookies[0]["value"] == "test-a"
        await pool.close("a")
        with pytest.raises(SessionError, match="invalid_cookies"):
            await pool.acquire("a")
        assert len(runtime.browser.contexts) == 1
    finally:
        await pool.close_all()


@pytest.mark.asyncio
async def test_shutdown_forces_an_already_closing_session_without_action_lock(make_pool):
    pool, runtime = make_pool()
    closer = None
    try:
        entry = await pool.acquire("a")
        async with entry.lock:
            closer = asyncio.create_task(pool.close("a"))
            for _ in range(10):
                if entry.closing:
                    break
                await asyncio.sleep(0)
            assert entry.closing and not closer.done()
            await asyncio.wait_for(pool.close_all(), 0.5)
            await asyncio.wait_for(closer, 0.5)
            assert entry.context.closed and entry.owner_lock._file is None
            assert runtime.browser.closed and runtime.stopped
            assert entry.lock.locked()  # The action still owns this lock.
            assert pool.entry_for("a") is None and entry.close_done.is_set()
    finally:
        if closer is not None and not closer.done():
            closer.cancel()
        if closer is not None:
            await asyncio.gather(closer, return_exceptions=True)
        await pool.close_all()


@pytest.mark.asyncio
async def test_close_cancellation_after_lock_acquisition_releases_only_its_lock(make_pool):
    pool, _ = make_pool()
    caller = asyncio.current_task()

    class BoundaryLock(asyncio.Lock):
        async def acquire(self):
            result = await super().acquire()
            asyncio.get_running_loop().call_soon(caller.cancel)
            return result

    try:
        entry = await pool.acquire("a")
        entry.lock = BoundaryLock()
        with pytest.raises(asyncio.CancelledError):
            await pool.close("a")
        assert pool.entry_for("a") is entry and not entry.context.closed
        assert not entry.lock.locked() and not entry.closing
        assert entry.close_done.is_set()
        entry.lock = asyncio.Lock()
        await pool.close("a")
        assert entry.context.closed and entry.owner_lock._file is None
    finally:
        await pool.close_all()


@pytest.mark.asyncio
async def test_caller_cancellation_and_forced_shutdown_do_not_swallow_cancellation(make_pool, monkeypatch):
    import xuse.browser.sessions
    pool, runtime = make_pool()
    closer = None
    try:
        entry = await pool.acquire("a")
        async with entry.lock:
            closer = asyncio.create_task(pool.close("a"))
            for _ in range(10):
                if entry.closing:
                    break
                await asyncio.sleep(0)
            assert entry.closing
            # Python 3.10 lacks Task.cancelling(). This task-like object makes
            # accidental use of it fail, without changing the test runner.
            class CompatibleAsyncio:
                def __getattr__(self, name):
                    if name == "current_task":
                        return lambda: object()
                    return getattr(asyncio, name)

            monkeypatch.setattr(xuse.browser.sessions, "asyncio", CompatibleAsyncio())
            shutdown = asyncio.create_task(pool.close_all())
            entry.force_close.set()
            closer.cancel()
            with pytest.raises(asyncio.CancelledError):
                await closer
            await asyncio.wait_for(shutdown, 0.5)
            assert entry.context.closed and entry.owner_lock._file is None
            assert entry.lock.locked() and runtime.stopped
            assert pool.entry_for("a") is None
    finally:
        if closer is not None and not closer.done():
            closer.cancel()
            await asyncio.gather(closer, return_exceptions=True)
        await pool.close_all()


class GuardProcess:
    def __init__(self, inspector, pid, birth):
        self.inspector, self.pid, self.birth = inspector, pid, birth
        self.closed = False

    def alive(self):
        return self.inspector.rows.get(self.pid, (None, None))[1] == self.birth

    def terminate(self):
        if self.alive():
            self.inspector.signaled.append((self.pid, self.birth))
            self.inspector.rows.pop(self.pid)

    def close(self):
        self.closed = True

    def freeze(self):
        return True

    def resume(self):
        pass


class GuardInspector:
    kernel = None
    def __init__(self):
        self.rows = {10: (1, 100), 11: (10, 101), 90: (1, 50)}
        self.signaled = []

    def cutoff(self):
        return 200

    def snapshot(self):
        return dict(self.rows)

    def pin(self, pid, birth, cutoff):
        if birth != self.rows.get(pid, (None, None))[1] or birth > cutoff:
            raise ProcessLookupError()
        return GuardProcess(self, pid, birth)


def test_owned_process_guard_never_signals_reused_pid_or_foreign_process():
    from xuse.browser.sessions import _OwnedBrowserProcesses
    inspector = GuardInspector()
    guard = _OwnedBrowserProcesses.capture(inspector, 10, 200)
    inspector.rows[11] = (90, 150)  # Old child exited; PID now belongs elsewhere.
    inspector.rows[12] = (10, 102)  # A renderer created after launch capture.
    guard.terminate()
    assert inspector.signaled == [(10, 100), (12, 102)]
    assert inspector.rows == {11: (90, 150), 90: (1, 50)}
    assert guard.exited()
    guard.close()
    assert all(process.closed for process in guard.processes.values())


def test_owned_process_guard_rejects_stale_parent_pid_from_older_process():
    from xuse.browser.sessions import _OwnedBrowserProcesses
    inspector = GuardInspector()
    inspector.rows[91] = (10, 99)  # Born before this root; stale PPID ownership.
    guard = _OwnedBrowserProcesses.capture(inspector, 10, 200)
    guard.terminate()
    assert (91, 99) not in inspector.signaled and 91 in inspector.rows


def test_child_born_during_snapshot_prevents_exit_proof_until_pinned():
    from xuse.browser.sessions import _OwnedBrowserProcesses
    inspector = GuardInspector()
    inspector.rows[12] = (10, 201)
    guard = _OwnedBrowserProcesses.capture(inspector, 10, 200)
    assert not guard.complete and 12 in guard.pending and not guard.exited()
    inspector.cutoff = lambda: 202
    guard.refresh()
    assert guard.complete and 12 in guard.processes
    guard.terminate()
    assert (12, 201) in inspector.signaled and guard.exited()


def test_unsupported_atomic_signal_can_still_confirm_later_natural_exit():
    from xuse.browser.sessions import _OwnedBrowserProcesses
    inspector = GuardInspector()
    guard = _OwnedBrowserProcesses.capture(inspector, 10, 200)

    def unavailable():
        raise OSError("synthetic unsupported atomic signal")

    for process in guard.processes.values():
        process.terminate = unavailable
    guard.terminate()
    assert not guard.exited() and guard.complete
    inspector.rows.pop(10)
    inspector.rows.pop(11)
    guard.refresh()
    assert guard.exited()


def test_unconfirmed_linux_stop_retains_exit_uncertainty_and_resumes_survivors(monkeypatch):
    from types import SimpleNamespace
    import xuse.browser.sessions
    inspector = GuardInspector()
    guard = xuse.browser.sessions._OwnedBrowserProcesses.capture(inspector, 10, 200)
    resumed = []
    for process in guard.processes.values():
        process.freeze = lambda: False
        process.terminate = lambda: None
        process.resume = lambda pid=process.pid: resumed.append(pid)
    monkeypatch.setattr(xuse.browser.sessions, "sys", SimpleNamespace(platform="linux"))
    guard.terminate()
    assert not guard.complete and guard.scan_failed and not guard.exited()
    assert resumed == [10, 11]


@pytest.mark.asyncio
async def test_cancelled_refresh_holds_guard_lock_until_worker_finishes(make_pool):
    import threading
    pool, _ = make_pool()
    entered, finish = threading.Event(), threading.Event()
    try:
        class RefreshGuard:
            complete = True

            def refresh(self):
                entered.set()
                assert finish.wait(3)

        pool._process_guard = RefreshGuard()
        refresh = asyncio.create_task(pool._refresh_owned_processes())
        while not entered.is_set():
            await asyncio.sleep(0.01)
        refresh.cancel()
        with pytest.raises(asyncio.CancelledError):
            await refresh
        assert pool._process_guard_lock.locked()
        finish.set()
        await asyncio.gather(*tuple(pool._cleanup_tasks))
        assert not pool._process_guard_lock.locked()
    finally:
        finish.set()
        pool._process_guard = None
        await pool.close_all()


@pytest.mark.asyncio
async def test_uncertain_process_drain_retains_owner_even_when_close_apis_succeed(make_pool):
    pool, runtime = make_pool()
    try:
        entry = await pool.acquire("a")

        class Undrained:
            complete = False

            def refresh(self):
                pass

            def exited(self):
                return False

            def terminate(self):
                raise OSError("synthetic atomic signaling unavailable")

        pool._process_guard = Undrained()
        runtime.browser.closed = True
        await pool.close("a")
        assert pool._closed and entry.owner_lock._file is not None
        assert entry.owner_lock in pool._stranded_owners
        await pool.close_all()
        assert runtime.stopped and entry.owner_lock._file is not None
    finally:
        pool._process_guard = None
        await pool.close_all()


@pytest.mark.asyncio
async def test_cancelled_shutdown_keeps_context_and_owner_cleanup_running(make_pool):
    pool, runtime = make_pool()
    started, finish = asyncio.Event(), asyncio.Event()
    try:
        entry = await pool.acquire("a")
        original = entry.context.close

        async def slow_close():
            started.set()
            await finish.wait()
            await original()

        entry.context.close = slow_close
        shutdown = asyncio.create_task(pool.close_all())
        await started.wait()
        shutdown.cancel()
        with pytest.raises(asyncio.CancelledError):
            await shutdown
        assert entry.owner_lock._file is not None and not entry.context.closed
        finish.set()
        await asyncio.wait_for(pool.close_all(), 2)
        assert entry.owner_lock._file is None and runtime.stopped
    finally:
        finish.set()
        await pool.close_all()


@pytest.mark.asyncio
async def test_start_cancelled_during_driver_capture_waits_for_owned_handles(make_pool, monkeypatch):
    import os
    import threading
    from types import SimpleNamespace
    import xuse.browser.sessions
    pool, runtime = make_pool()
    entered, finish = threading.Event(), threading.Event()
    inspector = GuardInspector()
    inspector.rows[10] = (os.getpid(), 100)
    snapshot = inspector.snapshot

    def slow_snapshot():
        entered.set()
        assert finish.wait(3)
        return snapshot()

    inspector.snapshot = slow_snapshot
    monkeypatch.setattr(xuse.browser.sessions, "_ProcessInspector", lambda: inspector)
    runtime._impl_obj = SimpleNamespace(_connection=SimpleNamespace(_transport=SimpleNamespace(
        _proc=SimpleNamespace(pid=10, returncode=None))))
    task = asyncio.create_task(pool.acquire("a"))
    try:
        while not entered.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0)
        assert not runtime.stopped and not task.done()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert runtime.stopped and runtime.launches == 0
        assert inspector.signaled == [(10, 100), (11, 101)]
        assert pool._process_guard is None and not pool._cleanup_tasks
    finally:
        finish.set()
        await asyncio.gather(task, return_exceptions=True)
        await pool.close_all()


@pytest.mark.asyncio
async def test_original_browser_block_is_preserved_when_process_drain_cannot_finish(make_pool, monkeypatch):
    pool, runtime = make_pool()
    error = BrowserBlocked("challenge")

    class Undrained:
        complete = True
        job = None

        def refresh(self):
            pass

        def exited(self):
            return False

        def terminate(self):
            raise OSError("synthetic signal unavailable")

    async def blocked(self):
        raise error

    monkeypatch.setattr(FakeXBrowser, "ensure_ready", blocked)
    pool._process_guard = Undrained()
    runtime.browser.is_connected = lambda: False
    try:
        with pytest.raises(BrowserBlocked) as caught:
            await pool.acquire("a")
        assert caught.value is error and pool._closed
        assert len(pool._stranded_owners) == 1
        assert all(owner._file is not None for owner in pool._stranded_owners)
    finally:
        pool._process_guard = None
        await pool.close_all()
