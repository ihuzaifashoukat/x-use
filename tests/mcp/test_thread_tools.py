"""Thread orchestration through real browser action and safety boundaries.

The synthetic async browser emits explicit new-post evidence; no live account,
PIN, cookies, network, or repository state is used.
"""
import asyncio
from contextlib import asynccontextmanager
import importlib
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from PIL import Image
import pytest

from xuse.browser.errors import BrowserActionError
from xuse.mcp.drafts import DraftStore
from xuse.mcp.executor import Ctx, ToolError
from xuse.mcp.safety import SafetyStore
from xuse.mcp.thread_store import ThreadStore
from xuse.mcp.thread_tools import execute_thread_draft, register_thread_tools
from helpers import FakeMetrics, call_tool, make_account


_DRIVERS = os.environ.get(
    "XUSE_TEST_BROWSER_DRIVERS", os.environ.get("XUSE_TEST_BROWSER_DRIVER", "patchright,playwright")
).split(",")


class SyntheticBrowser:
    def __init__(self):
        self.calls = []
        self.response = None
        self.fail = False
        self.wait = None
        self.started = asyncio.Event()

    async def submit(self, operation, text, parent, media, media_manifest=None):
        self.action_validator()
        self.calls.append({"operation": operation, "text": text, "parent": parent, "media": media,
                           "media_manifest": media_manifest})
        self.started.set()
        if self.wait:
            await self.wait.wait()
        if self.fail:
            raise BrowserActionError("outcome_unknown")
        if self.response is not None:
            return self.response
        identifier = str(100 + len(self.calls))
        return {"success": True, "status": "confirmed",
                "evidence": {"tweet_id": identifier, "tweet_url": f"https://x.com/test/status/{identifier}"}}

    async def post(self, text, media=None, community=None, media_manifest=None):
        return await self.submit("post", text, None, media, media_manifest)

    async def reply(self, url, text, media=None, media_manifest=None):
        return await self.submit("reply", text, url, media, media_manifest)


class Pool:
    backend = "playwright"
    idle_timeout_seconds = 600

    def __init__(self, loader):
        self.loader = loader
        self.browser = SyntheticBrowser()

    def find_account_dict(self, account):
        return next(a for a in self.loader.get_accounts_config() if a["account_id"] == account)

    @asynccontextmanager
    async def session(self, account):
        yield self.browser


@pytest.fixture
def thread_server(tmp_path, make_config_loader):
    loader = make_config_loader(accounts=[make_account()])
    now = [100000.0]
    ctx = Ctx(loader, Pool(loader), DraftStore(tmp_path / "drafts.jsonl"),
              processed_keys=set(), metrics_factory=FakeMetrics)
    ctx.safety_store = SafetyStore(tmp_path / "safety.sqlite3", {
        "read_interval_seconds": 0, "write_interval_seconds": 0,
        "max_actions_per_minute": 100}, clock=lambda: now[0])
    ctx.thread_store = ThreadStore(tmp_path / "threads.sqlite3")
    server = FastMCP("Synthetic thread test")
    register_thread_tools(server, ctx)
    return server, ctx, now


async def prepare(thread_server, posts=None, **kwargs):
    server, ctx, _ = thread_server
    result = await call_tool(server, "prepare_thread", {"account": "acc1", "posts": posts or [{"text": "first"}, {"text": "second"}], **kwargs})
    assert result["ok"], result
    assert result["state"] == "blocked" and result["next_index"] == 0
    assert result["published"] == [] and result["error"]["reason"] == "approval_required"
    return ctx.draft_store.get(result["draft_id"])


async def approve(ctx, draft):
    ctx.draft_store.set_status(draft.draft_id, "approved")
    return await execute_thread_draft(ctx, draft)


@pytest.mark.asyncio
async def test_always_drafts_exact_review_and_no_llm(thread_server):
    _, ctx, _ = thread_server
    ctx.draft_mode = False
    draft = await prepare(thread_server, [{"text": " exact text "}, {"text": "auto"}])
    assert draft.action == "publish_thread" and draft.status == "pending"
    assert draft.payload["posts"][0]["text"] == " exact text "
    assert " exact text " in draft.preview and "auto" in draft.preview
    assert not ctx.session_pool.browser.calls and ctx.llm_service is None
    with pytest.raises(ToolError, match="approved"):
        await execute_thread_draft(ctx, draft)
    result = await call_tool(thread_server[0], "continue_thread", {"run_id": draft.payload["run_id"]})
    assert not result["ok"] and not ctx.session_pool.browser.calls


@pytest.mark.asyncio
async def test_all_parts_chain_only_to_new_confirmed_urls(thread_server):
    _, ctx, _ = thread_server
    draft = await prepare(thread_server, [{"text": "one"}, {"text": "two"}, {"text": "three"}], reply_to="https://x.com/other/status/9")
    result = await approve(ctx, draft)
    assert result["state"] == "complete" and result["next_index"] == 3
    calls = ctx.session_pool.browser.calls
    assert [c["operation"] for c in calls] == ["reply"] * 3
    assert [c["parent"] for c in calls] == ["https://x.com/other/status/9", *result["published"][:2]]
    assert result["published"] == [f"https://x.com/test/status/{i}" for i in (101, 102, 103)]
    assert (await execute_thread_draft(ctx, draft))["state"] == "complete"
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_spacing_partial_restart_and_resume_exact_next_part(thread_server):
    server, ctx, now = thread_server
    ctx.safety_store.write_interval = 90
    draft = await prepare(thread_server)
    first = await approve(ctx, draft)
    assert first["state"] == "blocked" and first["next_index"] == 1
    assert first["error"]["reason"] == "cooldown" and first["error"]["retry_after_seconds"] == 90
    assert len(ctx.session_pool.browser.calls) == 1
    ctx.thread_store = ThreadStore(ctx.thread_store.path)
    ctx.draft_store = DraftStore(ctx.draft_store._path)
    before = await call_tool(server, "continue_thread", {"run_id": first["run_id"]})
    assert before["state"] == "blocked" and before["status"] == "partial"
    now[0] += 90
    final = await call_tool(server, "continue_thread", {"run_id": first["run_id"]})
    assert final["state"] == "complete" and final["status"] == "executed"
    assert [c["text"] for c in ctx.session_pool.browser.calls] == ["first", "second"]
    read = await call_tool(server, "get_thread_run", {"run_id": first["run_id"]})
    assert read["published"] == final["published"]


@pytest.mark.asyncio
async def test_daily_cap_recovers_without_repeated_publish(thread_server):
    server, ctx, now = thread_server
    ctx.safety_store.caps["reply"] = 0
    draft = await prepare(thread_server)
    blocked = await approve(ctx, draft)
    assert blocked["error"]["reason"] == "daily_budget" and blocked["next_index"] == 1
    assert len(ctx.session_pool.browser.calls) == 1
    ctx.safety_store.caps["reply"] = 1
    final = await call_tool(server, "continue_thread", {"run_id": blocked["run_id"]})
    assert final["state"] == "complete" and len(ctx.session_pool.browser.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    {"success": True, "status": "confirmed"},
    {"success": True, "status": "already_done", "evidence": {"tweet_id": "1", "tweet_url": "https://x.com/test/status/1"}},
    {"success": True, "status": "confirmed", "evidence": {"tweet_id": "9", "tweet_url": "https://x.com/other/status/9"}},
    {"success": True, "status": "confirmed", "evidence": {"tweet_id": "1", "tweet_url": "https://external.invalid/test/status/1"}},
    {"success": True, "status": "confirmed", "evidence": {"tweet_id": "2", "tweet_url": "https://x.com/test/status/1"}},
])
async def test_ambiguous_confirmation_never_retries(thread_server, response):
    server, ctx, _ = thread_server
    ctx.session_pool.browser.response = response
    draft = await prepare(thread_server, reply_to="https://x.com/other/status/9")
    result = await approve(ctx, draft)
    assert result["state"] == "uncertain" and not result["published"]
    again = await call_tool(server, "continue_thread", {"run_id": result["run_id"]})
    assert again["state"] == "uncertain" and len(ctx.session_pool.browser.calls) == 1


@pytest.mark.asyncio
async def test_crash_in_flight_restart_does_not_resend(thread_server):
    server, ctx, _ = thread_server
    draft = await prepare(thread_server)
    store = ctx.thread_store
    store.authorize(draft.payload["run_id"])
    store.start(draft.payload["run_id"], 0, None)
    ctx.thread_store = ThreadStore(store.path)
    resumed = await call_tool(server, "continue_thread", {"run_id": draft.payload["run_id"]})
    assert resumed["state"] == "uncertain" and resumed["error"]["reason"] == "interrupted_write"
    assert not ctx.session_pool.browser.calls


@pytest.mark.asyncio
async def test_concurrent_approval_and_refresh_cannot_repeat_submit(thread_server):
    _, ctx, _ = thread_server
    browser = ctx.session_pool.browser
    browser.wait = asyncio.Event()
    draft = await prepare(thread_server, [{"text": "one"}])
    task = asyncio.create_task(approve(ctx, draft))
    await browser.started.wait()
    ctx.thread_store = ThreadStore(ctx.thread_store.path)
    with pytest.raises(ToolError, match="Another caller"):
        await execute_thread_draft(ctx, draft)
    browser.wait.set()
    assert (await task)["state"] == "complete"
    assert len(browser.calls) == 1


@pytest.mark.asyncio
async def test_unknown_browser_outcome_stops_and_never_repeats(thread_server):
    server, ctx, _ = thread_server
    ctx.session_pool.browser.fail = True
    draft = await prepare(thread_server)
    result = await approve(ctx, draft)
    assert result["state"] == "uncertain" and result["error"]["action_id"]
    ctx.session_pool.browser.fail = False
    assert (await call_tool(server, "continue_thread", {"run_id": result["run_id"]}))["state"] == "uncertain"
    assert len(ctx.session_pool.browser.calls) == 1


@pytest.mark.asyncio
async def test_media_review_reply_forwarding_and_changed_file_preflight(thread_server, tmp_path):
    _, ctx, _ = thread_server
    path = tmp_path / "photo.png"
    Image.new("RGB", (10, 10), "red").save(path)
    draft = await prepare(thread_server, [{"text": "photo", "media": [str(path)]}], reply_to="https://x.com/other/status/9")
    assert str(path) in draft.preview
    assert draft.payload["posts"][0]["media_manifest"][0]["sha256"]
    assert (await approve(ctx, draft))["state"] == "complete"
    assert ctx.session_pool.browser.calls[0]["media"] == [str(path)]
    assert ctx.session_pool.browser.calls[0]["media_manifest"] == draft.payload["posts"][0]["media_manifest"]
    second = await prepare(thread_server, [{"text": "one"}, {"text": "later photo", "media": [str(path)]}])
    Image.new("RGB", (10, 10), "blue").save(path)
    result = await approve(ctx, second)
    assert result["state"] == "blocked" and result["next_index"] == 0
    assert len(ctx.session_pool.browser.calls) == 1


@pytest.mark.asyncio
async def test_draft_tampering_prevents_all_writes(thread_server):
    _, ctx, _ = thread_server
    draft = await prepare(thread_server)
    draft.payload["posts"][0]["text"] = "unreviewed"
    with pytest.raises(ToolError, match="changed"):
        await approve(ctx, draft)
    assert not ctx.session_pool.browser.calls


@pytest.mark.asyncio
async def test_input_bounds_and_bad_media_cannot_create_draft(thread_server):
    server, ctx, _ = thread_server
    for payload in [
        {"posts": []}, {"posts": [{"text": "x"}] * 21},
        {"posts": [{"text": "x" * 281}]}, {"posts": [{"text": "x", "media": "bad"}]},
        {"posts": [{"text": "x", "extra": "unreviewed"}]},
        {"posts": [{"text": "x"}], "reply_to": "https://x.com/a/status/not-numeric"},
    ]:
        result = await call_tool(server, "prepare_thread", {"account": "acc1", **payload})
        assert not result["ok"]
    assert len(ctx.draft_store) == 0 and not ctx.session_pool.browser.calls


@pytest.mark.asyncio
async def test_cancel_partial_persists_revocation_without_external_calls(thread_server):
    server, ctx, _ = thread_server
    ctx.safety_store.write_interval = 90
    draft = await prepare(thread_server)
    blocked = await approve(ctx, draft)
    assert blocked["published"] and len(ctx.session_pool.browser.calls) == 1
    cancelled = await call_tool(server, "cancel_thread", {"run_id": blocked["run_id"]})
    assert cancelled["state"] == "cancelled" and cancelled["published"] == blocked["published"]
    assert cancelled["status"] == "rejected"
    assert not ctx.thread_store.get(blocked["run_id"])["authorized"]
    assert ctx.draft_store.get(draft.draft_id).status == "rejected"
    ctx.thread_store = ThreadStore(ctx.thread_store.path)
    ctx.draft_store = DraftStore(ctx.draft_store._path)
    resumed = await call_tool(server, "continue_thread", {"run_id": blocked["run_id"]})
    assert resumed["state"] == "cancelled"
    again = await call_tool(server, "cancel_thread", {"run_id": blocked["run_id"]})
    assert again["state"] == "cancelled" and len(ctx.session_pool.browser.calls) == 1
    draft.status = "approved"
    with pytest.raises(ToolError, match="cancelled"):
        await execute_thread_draft(ctx, draft)


@pytest.mark.asyncio
async def test_cancel_uncertain_retains_safety_and_segment_evidence(thread_server):
    server, ctx, _ = thread_server
    ctx.session_pool.browser.fail = True
    result = await approve(ctx, await prepare(thread_server))
    prior = ctx.safety_store.status("acc1")["uncertain_actions"]
    cancelled = await call_tool(server, "cancel_thread", {"run_id": result["run_id"]})
    assert cancelled["state"] == "cancelled"
    assert cancelled["segments"][0]["state"] == "uncertain"
    assert cancelled["segments"][0]["action_id"] == result["error"]["action_id"]
    assert cancelled["error"]["previous_error"] == result["error"]
    assert ctx.safety_store.status("acc1")["uncertain_actions"] == prior
    assert len(ctx.session_pool.browser.calls) == 1


@pytest.mark.asyncio
async def test_cancel_complete_noop_and_unknown_error(thread_server):
    server, ctx, _ = thread_server
    result = await approve(ctx, await prepare(thread_server, [{"text": "one"}]))
    cancelled = await call_tool(server, "cancel_thread", {"run_id": result["run_id"]})
    assert cancelled["state"] == "complete" and cancelled["published"] == result["published"]
    assert not (await call_tool(server, "cancel_thread", {"run_id": "unknown"}))["ok"]
    assert len(ctx.session_pool.browser.calls) == 1


@pytest.mark.asyncio
async def test_cancel_busy_cannot_interrupt_claimed_send(thread_server):
    server, ctx, _ = thread_server
    browser = ctx.session_pool.browser
    browser.wait = asyncio.Event()
    draft = await prepare(thread_server, [{"text": "one"}])
    task = asyncio.create_task(approve(ctx, draft))
    await browser.started.wait()
    cancelled = await call_tool(server, "cancel_thread", {"run_id": draft.payload["run_id"]})
    assert not cancelled["ok"] and "Another caller" in cancelled["error"]["message"]
    browser.wait.set()
    assert (await task)["state"] == "complete"
    assert len(browser.calls) == 1


@pytest.mark.asyncio
async def test_media_rechecked_before_later_segment(thread_server, tmp_path):
    _, ctx, _ = thread_server
    path = tmp_path / "later.png"
    Image.new("RGB", (10, 10), "red").save(path)
    draft = await prepare(thread_server, [{"text": "first"}, {"text": "later", "media": [str(path)]}])
    browser = ctx.session_pool.browser
    original = browser.post

    async def mutate_after_first(*args, **kwargs):
        result = await original(*args, **kwargs)
        Image.new("RGB", (10, 10), "blue").save(path)
        return result

    browser.post = mutate_after_first
    result = await approve(ctx, draft)
    assert result["state"] == "blocked" and result["next_index"] == 1
    assert len(browser.calls) == 1


@pytest.mark.asyncio
async def test_prepare_refuses_inactive_or_unsupported_backend(thread_server):
    server, ctx, _ = thread_server
    ctx.session_pool.backend = "selenium"
    unsupported = await call_tool(server, "prepare_thread", {"account": "acc1", "posts": [{"text": "one"}]})
    assert not unsupported["ok"] and len(ctx.draft_store) == 0
    ctx.session_pool.backend = "playwright"
    ctx.session_pool.find_account_dict("acc1")["is_active"] = False
    inactive = await call_tool(server, "prepare_thread", {"account": "acc1", "posts": [{"text": "one"}]})
    assert not inactive["ok"] and len(ctx.draft_store) == 0


def integrated_server(thread_server, tmp_path):
    from xuse.mcp.server import create_server
    from xuse.outreach import OutreachStore
    from xuse.queue import QueueStore
    _, ctx, _ = thread_server
    server = create_server(ctx.config_loader, session_pool=ctx.session_pool,
                           draft_store=ctx.draft_store, safety_store=ctx.safety_store,
                           queue_store=QueueStore(tmp_path / "queue.jsonl"),
                           outreach_store=OutreachStore(tmp_path / "outreach.sqlite3"))
    server.xuse_ctx.processed_keys = ctx.processed_keys
    server.xuse_ctx.metrics_factory = ctx.metrics_factory
    server.xuse_ctx.thread_store = ctx.thread_store
    return server


@pytest.mark.asyncio
async def test_integrated_approval_partial_never_marks_executed(thread_server, tmp_path):
    _, ctx, now = thread_server
    ctx.safety_store.write_interval = 90
    server = integrated_server(thread_server, tmp_path)
    prepared = await call_tool(server, "prepare_thread", {"account": "acc1", "posts": [{"text": "first"}, {"text": "second"}]})
    approved = await call_tool(server, "approve_draft", {"draft_id": prepared["draft_id"]})
    assert approved["ok"] and approved["status"] == "partial"
    assert approved["result"]["state"] == "blocked" and approved["result"]["next_index"] == 1
    assert server.xuse_ctx.draft_store.get(prepared["draft_id"]).status == "partial"
    assert not (await call_tool(server, "approve_draft", {"draft_id": prepared["draft_id"]}))["ok"]
    assert len(ctx.session_pool.browser.calls) == 1
    now[0] += 90
    continued = await call_tool(server, "continue_thread", {"run_id": prepared["run_id"]})
    assert continued["state"] == "complete" and continued["status"] == "executed"
    assert len(ctx.session_pool.browser.calls) == 2


@pytest.mark.asyncio
async def test_integrated_zero_write_block_and_uncertainty_status(thread_server, tmp_path):
    _, ctx, _ = thread_server
    server = integrated_server(thread_server, tmp_path)
    ctx.safety_store.caps["post"] = 0
    draft = await call_tool(server, "prepare_thread", {"account": "acc1", "posts": [{"text": "first"}]})
    blocked = await call_tool(server, "approve_draft", {"draft_id": draft["draft_id"]})
    assert blocked["ok"] and blocked["status"] == "pending" and not blocked["result"]["published"]
    assert not ctx.session_pool.browser.calls
    ctx.safety_store.caps["post"] = 1
    ctx.session_pool.browser.fail = True
    uncertain = await call_tool(server, "continue_thread", {"run_id": draft["run_id"]})
    assert uncertain["state"] == "uncertain" and uncertain["status"] == "uncertain"
    assert server.xuse_ctx.draft_store.get(draft["draft_id"]).status == "uncertain"
    assert not (await call_tool(server, "approve_draft", {"draft_id": draft["draft_id"]}))["ok"]
    assert len(ctx.session_pool.browser.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("driver", _DRIVERS)
async def test_native_intercepted_thread_with_reply_media(thread_server, tmp_path, monkeypatch, driver):
    """Run approval and continuation through a native Chromium DOM adapter.

    All requests are fulfilled from synthetic documents or aborted.
    """
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "browser"))
    from test_messaging_dom import FixtureApp
    from test_post_dom import nested_reply_document, SOURCE_TEXT

    if driver not in {"patchright", "playwright"}:
        pytest.fail("Unsupported offline browser-test driver.")
    try:
        api = importlib.import_module(f"{driver}.async_api")
    except ModuleNotFoundError:
        if os.environ.get("XUSE_REQUIRE_BROWSER_TESTS") == "1":
            pytest.fail("Required thread-test driver is unavailable.", pytrace=False)
        pytest.skip(f"Optional {driver} driver is unavailable for offline DOM scenarios.")
    channel = os.environ.get("XUSE_TEST_BROWSER_CHANNEL", "chrome")
    if channel not in {"chrome", "msedge", "chromium"}:
        pytest.fail("Unsupported offline browser-test channel.")
    async with api.async_playwright() as runtime:
        options = {"headless": True}
        if channel != "chromium":
            options["channel"] = channel
        try:
            browser = await runtime.chromium.launch(**options)
        except Exception:
            if os.environ.get("XUSE_REQUIRE_BROWSER_TESTS") == "1":
                pytest.fail("Required local browser runtime is unavailable.", pytrace=False)
            pytest.skip("Local browser runtime unavailable for offline DOM scenarios.")
        try:
            context = await browser.new_context(service_workers="block")
            page = await context.new_page()
            app = FixtureApp(page)
            sends = []
            await page.expose_function("recordThreadSend", lambda kind: sends.append(kind))
            await context.route("**/*", app.intercept)
            app.document("/compose/tweet", '''
                <div role="dialog"><div contenteditable="true" data-testid="tweetTextarea_0"></div>
                <button data-testid="tweetButton" onclick="
                    window.recordThreadSend('post'); this.closest('[role=dialog]').remove();
                    const toast=document.createElement('div'); toast.dataset.testid='toast';
                    toast.innerHTML='Your post was sent <a href=/person/status/123>View</a>';
                    document.body.appendChild(toast);">Post</button></div>
            ''')
            reply_document = nested_reply_document().replace(
                "document.documentElement.dataset.sendClicks='1';",
                "window.recordThreadSend('reply'); document.documentElement.dataset.sendClicks='1';")
            upload = '''<input type="file" multiple onchange="
                document.documentElement.dataset.uploadCount=String(this.files.length);
                document.querySelector('[data-testid=attachments]').hidden=false;">
                <div data-testid="attachments" hidden>Photo preview</div>'''
            app.document("/person/status/123", reply_document.replace('<div data-testid="toolBar">', upload + '<div data-testid="toolBar">'))
            _, ctx, now = thread_server
            ctx.session_pool.browser = app.adapter
            ctx.session_pool.backend = driver
            ctx.safety_store.write_interval = 90
            server = integrated_server(thread_server, tmp_path)
            path = tmp_path / "photo.png"
            Image.new("RGB", (10, 10), "red").save(path)
            prepared = await call_tool(server, "prepare_thread", {"account": "acc1", "posts": [
                {"text": SOURCE_TEXT}, {"text": "Reviewed thread reply", "media": [str(path)]}]})
            assert prepared["ok"], prepared
            approved = await call_tool(server, "approve_draft", {"draft_id": prepared["draft_id"]})
            assert approved["ok"] and approved["status"] == "partial", approved
            assert approved["result"]["published"] == ["https://x.com/person/status/123"]
            assert sends == ["post"]
            now[0] += 90
            result = await call_tool(server, "continue_thread", {"run_id": prepared["run_id"]})
            assert result["state"] == "complete", result
            assert result["published"] == ["https://x.com/person/status/123", "https://x.com/person/status/124"]
            assert sends == ["post", "reply"]
            assert await page.locator("html").get_attribute("data-upload-count") == "1"
            assert await page.locator("html").get_attribute("data-submitted-text") == "Reviewed thread reply"
            assert all(host == "x.com" for host, _, _ in app.requests)
            assert (await call_tool(server, "continue_thread", {"run_id": prepared["run_id"]}))["state"] == "complete"
            assert sends == ["post", "reply"]
        finally:
            await browser.close()
