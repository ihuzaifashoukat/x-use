"""MCP approval, queue, policy and campaign integration using async fakes."""
import asyncio
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from xuse.browser.errors import BrowserActionError, BrowserBlocked
from xuse.mcp.drafts import DraftStore
from xuse.mcp.server import create_server, shutdown
from xuse.mcp.safety import SafetyStore
from xuse.models import ScrapedTweet
from xuse.outreach import OutreachStore
from xuse.queue import QueueStore
from helpers import call_tool, make_account, FakeMetrics


# These phases include private SQLite I/O with a 10 s connection timeout.
# A fixture phase is an ordering assertion, not a 2 s storage latency contract.
STORAGE_WAIT_SECONDS = 30


async def wait_for_tool_phase(task, event):
    waiter = asyncio.create_task(event.wait())
    try:
        done, _ = await asyncio.wait(
            {task, waiter}, timeout=STORAGE_WAIT_SECONDS,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if task in done:
            result = task.result()
            pytest.fail(f"Tool finished before the expected fixture phase: {result!r}")
        assert waiter in done, "Tool did not reach the expected fixture phase"
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


async def settled_tool_result(task):
    # Observe owned work without wait_for injecting another cancellation.
    done, _ = await asyncio.wait({task}, timeout=STORAGE_WAIT_SECONDS)
    assert task in done, "Tool did not settle after the fixture released it"
    return task.result()


class Browser:
    def __init__(self):
        self.calls = []
        self.fail_send = False
        self.before_send = None
        self.blocked = False

    async def send_message(self, recipient, text):
        if self.before_send:
            self.before_send()
        self.recipient_validator(recipient)
        self.calls.append(("send_message", recipient, text))
        if self.fail_send:
            raise BrowserActionError("send_unconfirmed")
        return {"success": True, "message_id": "message-1"}

    async def post(self, text, media=None, community=None):
        self.calls.append(("post", text))
        return {"success": True, "tweet_id": "123"}

    async def follow(self, handle):
        if self.before_send:
            self.before_send()
        self.action_validator()
        self.calls.append(("follow", handle))
        return {"success": True}

    async def search_tweets(self, query, limit):
        self.calls.append(("search", query))
        return [ScrapedTweet(tweet_id="123", tweet_url="https://x.com/test/status/123", text_content="hello")]

    async def unlock_messages(self, pin):
        assert pin == "1234"
        return {"success": True}

    async def navigate(self, url):
        if self.blocked:
            raise BrowserBlocked("challenge")

    async def ensure_ready(self):
        if self.blocked:
            raise BrowserBlocked("login_required")

    async def verify_session(self):
        await self.navigate("https://x.com/home")
        await self.ensure_ready()
        return {"success": True}


class Pool:
    backend = "playwright"
    idle_timeout_seconds = 600

    def __init__(self, loader):
        self.loader = loader
        self.browser = Browser()
        self.started = 0
        self.closed = False

    def find_account_dict(self, account):
        return next(raw for raw in self.loader.get_accounts_config() if raw["account_id"] == account)

    @property
    def active_accounts(self):
        return iter(["acc1"] if self.started else [])

    def entry_for(self, account):
        return SimpleNamespace(browser_manager=self.browser) if self.started else None

    @asynccontextmanager
    async def session(self, account):
        self.started += 1
        yield self.browser

    async def close(self, account):
        self.closed = True

    async def close_all(self):
        self.closed = True


@pytest.fixture
def async_server(make_config_loader, tmp_path):
    loader = make_config_loader(settings={"queue": {"min_delay_seconds": 0, "max_delay_seconds": 0}},
                                accounts=[make_account()])
    pool = Pool(loader)
    policy = SafetyStore(tmp_path / "policy.sqlite3", {"read_interval_seconds": 0,
                       "write_interval_seconds": 0, "max_actions_per_minute": 100})
    server = create_server(loader, session_pool=pool, draft_store=DraftStore(tmp_path / "drafts.jsonl"),
                           queue_store=QueueStore(tmp_path / "queue.jsonl"), safety_store=policy,
                           outreach_store=OutreachStore(tmp_path / "outreach.sqlite3"))
    server.xuse_ctx.processed_keys = set()
    server.xuse_ctx.metrics_factory = FakeMetrics
    return server


async def campaign(server):
    lead = (await call_tool(server, "upsert_lead", {"account": "acc1", "handle": "alex"}))["lead"]
    campaign = (await call_tool(server, "create_campaign", {"account": "acc1", "name": "Followup", "message_template": "Hello {name}."}))["campaign"]
    await call_tool(server, "add_campaign_leads", {"account": "acc1", "campaign_id": campaign["campaign_id"], "lead_ids": [lead["lead_id"]]})
    result = await call_tool(server, "prepare_campaign_messages", {"account": "acc1", "campaign_id": campaign["campaign_id"]})
    return lead, campaign, result["drafts"][0]["draft_id"]


@pytest.mark.asyncio
async def test_dm_always_drafts_then_approves_exact_payload(async_server):
    async_server.xuse_ctx.draft_mode = False
    draft = await call_tool(async_server, "send_message", {"account": "acc1", "recipient": "@Alex", "text": "hello"})
    assert draft["ok"] and async_server.xuse_ctx.session_pool.started == 0
    approved = await call_tool(async_server, "approve_draft", {"draft_id": draft["draft_id"]})
    assert approved["ok"]
    assert async_server.xuse_ctx.session_pool.browser.calls == [("send_message", "@alex", "hello")]
    again = await call_tool(async_server, "approve_draft", {"draft_id": draft["draft_id"]})
    assert not again["ok"]


@pytest.mark.asyncio
async def test_paused_campaign_preserves_pending_draft_and_recovers(async_server):
    _, campaign_record, draft_id = await campaign(async_server)
    params = {"account": "acc1", "campaign_id": campaign_record["campaign_id"]}
    await call_tool(async_server, "set_campaign_status", {**params, "status": "paused"})
    result = await call_tool(async_server, "approve_draft", {"draft_id": draft_id})
    assert not result["ok"] and async_server.xuse_ctx.draft_store.get(draft_id).status == "pending"
    assert not async_server.xuse_ctx.session_pool.browser.calls
    await call_tool(async_server, "set_campaign_status", {**params, "status": "ready"})
    assert (await call_tool(async_server, "approve_draft", {"draft_id": draft_id}))["ok"]


@pytest.mark.asyncio
async def test_pause_during_browser_navigation_is_rechecked_at_send(async_server):
    _, campaign_record, draft_id = await campaign(async_server)
    store = async_server.xuse_ctx.outreach_store
    browser = async_server.xuse_ctx.session_pool.browser
    browser.before_send = lambda: store.set_campaign_status("acc1", campaign_record["campaign_id"], "paused")
    assert not (await call_tool(async_server, "approve_draft", {"draft_id": draft_id}))["ok"]
    assert not browser.calls


@pytest.mark.asyncio
async def test_opt_out_between_preparation_and_approval_suppresses_send(async_server):
    lead, _, draft_id = await campaign(async_server)
    await call_tool(async_server, "opt_out_lead", {"account": "acc1", "lead_id": lead["lead_id"]})
    assert not (await call_tool(async_server, "approve_draft", {"draft_id": draft_id}))["ok"]
    assert not async_server.xuse_ctx.session_pool.browser.calls
    direct = await call_tool(async_server, "send_message", {"account": "acc1", "recipient": "@alex", "text": "bypass"})
    assert not direct["ok"]


@pytest.mark.asyncio
async def test_opt_out_during_follow_navigation_prevents_click(async_server):
    lead = (await call_tool(async_server, "upsert_lead", {"account": "acc1", "handle": "alex"}))["lead"]
    draft = await call_tool(async_server, "follow_profile", {"account": "acc1", "profile": "alex"})
    ctx = async_server.xuse_ctx
    ctx.session_pool.browser.before_send = lambda: ctx.outreach_store.opt_out_lead("acc1", lead["lead_id"])
    result = await call_tool(async_server, "approve_draft", {"draft_id": draft["draft_id"]})
    assert not result["ok"] and not ctx.session_pool.browser.calls


@pytest.mark.asyncio
async def test_resume_probe_remains_bounded_by_read_budget(async_server):
    ctx = async_server.xuse_ctx
    ctx.safety_store.pause("acc1", "manual_pause")
    ctx.safety_store.caps["read"] = 0
    result = await call_tool(async_server, "resume_account_actions", {"account": "acc1"})
    assert not result["ok"] and result["error"]["reason"] == "daily_budget"
    assert ctx.safety_store.status("acc1")["paused"]
    assert ctx.session_pool.started == 0


@pytest.mark.asyncio
async def test_uncertain_campaign_requires_reconciliation_and_never_auto_retries(async_server):
    lead, campaign_record, draft_id = await campaign(async_server)
    ctx = async_server.xuse_ctx
    ctx.session_pool.browser.fail_send = True
    result = await call_tool(async_server, "approve_draft", {"draft_id": draft_id})
    action_id = result["error"]["action_id"]
    assert ctx.draft_store.get(draft_id).status == "uncertain"
    assert not (await call_tool(async_server, "approve_draft", {"draft_id": draft_id}))["ok"]
    params = {"account": "acc1", "campaign_id": campaign_record["campaign_id"]}
    assert (await call_tool(async_server, "prepare_campaign_messages", params))["count"] == 0
    reconciled = await call_tool(async_server, "resolve_action_outcome", {"account": "acc1", "action_id": action_id, "observed_outcome": "not_sent"})
    assert reconciled["ok"]
    assert (await call_tool(async_server, "prepare_campaign_messages", params))["count"] == 1
    assert len(ctx.session_pool.browser.calls) == 1


@pytest.mark.asyncio
async def test_repeating_old_not_sent_reconciliation_does_not_release_new_draft(async_server):
    _, campaign_record, old_draft = await campaign(async_server)
    ctx = async_server.xuse_ctx
    ctx.session_pool.browser.fail_send = True
    failed = await call_tool(async_server, "approve_draft", {"draft_id": old_draft})
    recovery = {"account": "acc1", "action_id": failed["error"]["action_id"], "observed_outcome": "not_sent"}
    assert (await call_tool(async_server, "resolve_action_outcome", recovery))["ok"]
    params = {"account": "acc1", "campaign_id": campaign_record["campaign_id"]}
    prepared = await call_tool(async_server, "prepare_campaign_messages", params)
    new_draft = prepared["drafts"][0]["draft_id"]
    assert new_draft != old_draft
    again = await call_tool(async_server, "resolve_action_outcome", recovery)
    assert not again["ok"]
    member = ctx.outreach_store.get_campaign("acc1", campaign_record["campaign_id"]).members[0]
    assert member.draft_id == new_draft and member.status == "pending"
    assert ctx.draft_store.get(new_draft).status == "pending"
    assert (await call_tool(async_server, "prepare_campaign_messages", params))["count"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("new_status", ["succeeded", "uncertain"])
async def test_old_plain_draft_reconciliation_cannot_replace_newer_outcome(async_server, new_status):
    ctx = async_server.xuse_ctx
    prepared = await call_tool(async_server, "send_message", {"account": "acc1", "recipient": "@alex", "text": "exact"})
    draft_id = prepared["draft_id"]
    ctx.session_pool.browser.fail_send = True
    failed = await call_tool(async_server, "approve_draft", {"draft_id": draft_id})
    old_id = failed["error"]["action_id"]
    recovery = {"account": "acc1", "action_id": old_id, "observed_outcome": "not_sent"}
    assert (await call_tool(async_server, "resolve_action_outcome", recovery))["ok"]
    ctx.session_pool.browser.fail_send = new_status == "uncertain"
    await call_tool(async_server, "approve_draft", {"draft_id": draft_id})
    expected = "executed" if new_status == "succeeded" else "uncertain"
    assert ctx.draft_store.get(draft_id).status == expected
    again = await call_tool(async_server, "resolve_action_outcome", recovery)
    assert again["ok"] and again["draft_update_skipped"]
    assert ctx.draft_store.get(draft_id).status == expected
    assert len(ctx.session_pool.browser.calls) == 2


@pytest.mark.asyncio
async def test_repeat_not_sent_cannot_reopen_rejected_plain_draft(async_server):
    ctx = async_server.xuse_ctx
    prepared = await call_tool(async_server, "send_message", {"account": "acc1", "recipient": "@alex", "text": "exact"})
    ctx.session_pool.browser.fail_send = True
    failed = await call_tool(async_server, "approve_draft", {"draft_id": prepared["draft_id"]})
    recovery = {"account": "acc1", "action_id": failed["error"]["action_id"], "observed_outcome": "not_sent"}
    assert (await call_tool(async_server, "resolve_action_outcome", recovery))["ok"]
    assert (await call_tool(async_server, "reject_draft", {"draft_id": prepared["draft_id"]}))["ok"]
    assert (await call_tool(async_server, "resolve_action_outcome", recovery))["draft_update_skipped"]
    assert ctx.draft_store.get(prepared["draft_id"]).status == "rejected"


@pytest.mark.asyncio
async def test_confirmed_send_bookkeeping_crash_is_repaired_without_resend(async_server, monkeypatch):
    _, campaign_record, draft_id = await campaign(async_server)
    ctx = async_server.xuse_ctx
    original = ctx.outreach_store.record_delivery
    monkeypatch.setattr(ctx.outreach_store, "record_delivery", lambda *a: (_ for _ in ()).throw(OSError("storage unavailable")))
    result = await call_tool(async_server, "approve_draft", {"draft_id": draft_id})
    assert not result["ok"]
    action = ctx.safety_store.status("acc1")["recent_actions"][0]
    assert action["status"] == "succeeded"
    monkeypatch.setattr(ctx.outreach_store, "record_delivery", original)
    repair = await call_tool(async_server, "resolve_action_outcome", {"account": "acc1", "action_id": action["action_id"], "observed_outcome": "succeeded"})
    assert repair["ok"] and ctx.draft_store.get(draft_id).status == "executed"
    assert ctx.outreach_store.get_campaign("acc1", campaign_record["campaign_id"]).members[0].status == "delivered"
    assert len(ctx.session_pool.browser.calls) == 1


@pytest.mark.asyncio
async def test_post_search_and_queue_use_async_backend_and_same_budget(async_server):
    ctx = async_server.xuse_ctx
    ctx.draft_mode = False
    ctx.safety_store.caps["post"] = 1
    assert (await call_tool(async_server, "search_tweets", {"account": "acc1", "keywords": "test"}))["tweets"][0]["tweet_id"] == "123"
    assert (await call_tool(async_server, "post_tweet", {"account": "acc1", "text": "first"}))["ok"]
    await call_tool(async_server, "queue_post", {"account": "acc1", "text": "second"})
    await call_tool(async_server, "process_queue", {"account": "acc1", "max_actions": 1})
    assert [call for call in ctx.session_pool.browser.calls if call[0] == "post"] == [("post", "first")]


@pytest.mark.asyncio
async def test_pin_never_enters_policy_database_or_tool_result(async_server, monkeypatch):
    monkeypatch.setenv("XUSE_INBOX_PIN", "1234")
    ctx = async_server.xuse_ctx
    ctx.safety_store.pause("acc1", "pin_required")
    result = await call_tool(async_server, "unlock_inbox", {"account": "acc1"})
    assert result["ok"] and "1234" not in str(result)
    assert b"1234" not in ctx.safety_store.path.read_bytes()
    assert not ctx.safety_store.status("acc1")["paused"]


@pytest.mark.asyncio
async def test_resume_requires_authenticated_readiness_and_shutdown_closes_pool(async_server):
    ctx = async_server.xuse_ctx
    ctx.safety_store.pause("acc1", "challenge")
    ctx.session_pool.browser.blocked = True
    assert not (await call_tool(async_server, "resume_account_actions", {"account": "acc1"}))["ok"]
    assert ctx.safety_store.status("acc1")["paused"]
    await shutdown(async_server)
    assert ctx.session_pool.closed


@pytest.mark.asyncio
async def test_running_send_cannot_be_reconciled_until_it_finishes(async_server):
    ctx = async_server.xuse_ctx
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_send(recipient, text):
        entered.set()
        await release.wait()
        return {"success": True, "message_id": "confirmed"}

    ctx.session_pool.browser.send_message = delayed_send
    draft = await call_tool(async_server, "send_message", {"account": "acc1", "recipient": "@alex", "text": "reviewed"})
    task = asyncio.create_task(call_tool(async_server, "approve_draft", {"draft_id": draft["draft_id"]}))
    try:
        await wait_for_tool_phase(task, entered)
        action = ctx.safety_store.status("acc1")["recent_actions"][0]
        assert action["status"] == "started"
        result = await call_tool(async_server, "resolve_action_outcome", {
            "account": "acc1", "action_id": action["action_id"], "observed_outcome": "not_sent"})
        assert not result["ok"] and result["error"]["reason"] == "account_busy"
        assert ctx.safety_store.status("acc1")["recent_actions"][0]["status"] == "started"
    finally:
        release.set()
        result = await settled_tool_result(task)
    assert result["ok"]


@pytest.mark.asyncio
async def test_send_timeout_preserves_action_id_and_prevents_retry(async_server):
    ctx = async_server.xuse_ctx
    ctx.config_loader.settings.setdefault("mcp", {})["tool_timeout_seconds"] = 1
    cancelled = asyncio.Event()

    async def hanging_send(recipient, text):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    ctx.session_pool.browser.send_message = hanging_send
    draft = await call_tool(async_server, "send_message", {"account": "acc1", "recipient": "@alex", "text": "reviewed"})
    result = await call_tool(async_server, "approve_draft", {"draft_id": draft["draft_id"]})
    assert not result["ok"] and result["error"]["reason"] == "tool_timeout"
    assert cancelled.is_set()
    action = ctx.safety_store.status("acc1")["recent_actions"][0]
    assert result["error"]["action_id"] == action["action_id"]
    assert action["status"] == "uncertain"
    assert ctx.draft_store.get(draft["draft_id"]).status == "uncertain"
    assert not (await call_tool(async_server, "approve_draft", {"draft_id": draft["draft_id"]}))["ok"]


@pytest.mark.asyncio
async def test_client_cancellation_preserves_uncertain_send_for_reconciliation(async_server):
    ctx = async_server.xuse_ctx
    entered = asyncio.Event()

    async def hanging_send(recipient, text):
        entered.set()
        await asyncio.Event().wait()

    ctx.session_pool.browser.send_message = hanging_send
    draft = await call_tool(async_server, "send_message", {
        "account": "acc1", "recipient": "@alex", "text": "reviewed"})
    task = asyncio.create_task(call_tool(async_server, "approve_draft", {"draft_id": draft["draft_id"]}))
    try:
        await wait_for_tool_phase(task, entered)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await settled_tool_result(task)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    record = ctx.safety_store.status("acc1")["uncertain_actions"][0]
    assert record["status"] == "uncertain"
    assert ctx.safety_store.reference("acc1", record["action_id"])["draft_id"] == draft["draft_id"]
    assert ctx.draft_store.get(draft["draft_id"]).status == "uncertain"
    assert not (await call_tool(async_server, "approve_draft", {"draft_id": draft["draft_id"]}))["ok"]
    repaired = await call_tool(async_server, "resolve_action_outcome", {
        "account": "acc1", "action_id": record["action_id"], "observed_outcome": "not_sent"})
    assert repaired["ok"] and ctx.draft_store.get(draft["draft_id"]).status == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["reserve", "uncertain_finish", "succeeded_finish"])
@pytest.mark.parametrize("trigger", ["client", "timeout"])
async def test_repeated_cancellation_settles_ledger_before_releasing_account(
    async_server, monkeypatch, boundary, trigger
):
    from xuse.mcp.safety import PolicyError

    ctx = async_server.xuse_ctx
    ctx.config_loader.settings.setdefault("mcp", {})["tool_timeout_seconds"] = 1 if trigger == "timeout" else 10
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    sending = asyncio.Event()
    method = "reserve" if boundary == "reserve" else "finish"
    original = getattr(ctx.safety_store, method)

    def held_ledger_call(*args, **kwargs):
        loop.call_soon_threadsafe(entered.set)
        if not release.wait(STORAGE_WAIT_SECONDS):
            raise AssertionError("ledger fixture did not release")
        return original(*args, **kwargs)

    monkeypatch.setattr(ctx.safety_store, method, held_ledger_call)
    if boundary == "uncertain_finish":
        async def hanging_send(recipient, text):
            sending.set()
            await asyncio.Event().wait()
        ctx.session_pool.browser.send_message = hanging_send

    draft = await call_tool(async_server, "send_message", {
        "account": "acc1", "recipient": "@alex", "text": "reviewed"})
    cancellation_ids = []

    async def approve():
        try:
            return await call_tool(async_server, "approve_draft", {"draft_id": draft["draft_id"]})
        except asyncio.CancelledError as exc:
            # Python 3.10 may replace the error when awaiting a cancelled
            # Task. Check the ID at the tool boundary, where draft approval
            # consumes it, instead of depending on Task exception identity.
            cancellation_ids.append(getattr(exc, "action_id", None))
            raise

    task = asyncio.create_task(approve())
    try:
        if boundary == "uncertain_finish" and trigger == "client":
            await wait_for_tool_phase(task, sending)
            task.cancel()
        await wait_for_tool_phase(task, entered)
        if trigger == "timeout":
            # Allow the tool deadline to cancel the operation while this
            # ledger thread is still blocked; then cancel its caller too.
            await asyncio.sleep(1.1)
        else:
            task.cancel()
        for _ in range(2):
            await asyncio.sleep(0)
            task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "Cancellation returned before ledger cleanup"
        with pytest.raises(PolicyError) as busy:
            with ctx.safety_store.operation_lock("acc1"):
                pytest.fail("Account lock released while ledger work is in flight")
        assert busy.value.reason == "account_busy"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await settled_tool_result(task)
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    record = ctx.safety_store.status("acc1")["recent_actions"][0]
    assert cancellation_ids == [record["action_id"]]
    confirmed = boundary == "succeeded_finish"
    assert record["status"] == ("succeeded" if confirmed else "uncertain")
    assert ctx.draft_store.get(draft["draft_id"]).status == ("executed" if confirmed else "uncertain")
    assert ctx.safety_store.reference("acc1", record["action_id"])["draft_id"] == draft["draft_id"]
    assert ctx.session_pool.started == (0 if boundary == "reserve" else 1)
    assert len(ctx.session_pool.browser.calls) == (1 if confirmed else 0)
    with ctx.safety_store.operation_lock("acc1"):
        pass  # Cleanup completed before ownership returned to another caller.


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["challenge_pause", "recovery_resume"])
async def test_repeated_cancellation_settles_pause_bookkeeping(async_server, monkeypatch, boundary):
    from xuse.mcp.safety import PolicyError

    ctx = async_server.xuse_ctx
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    method = "pause" if boundary == "challenge_pause" else "resume_if_unchanged"
    if boundary == "recovery_resume":
        ctx.safety_store.pause("acc1", "challenge")
    else:
        async def blocked_send(recipient, text):
            raise BrowserBlocked("challenge")
        ctx.session_pool.browser.send_message = blocked_send
    original = getattr(ctx.safety_store, method)

    def held_pause_call(*args, **kwargs):
        loop.call_soon_threadsafe(entered.set)
        if not release.wait(STORAGE_WAIT_SECONDS):
            raise AssertionError("pause fixture did not release")
        return original(*args, **kwargs)

    monkeypatch.setattr(ctx.safety_store, method, held_pause_call)
    draft = None
    if boundary == "challenge_pause":
        draft = await call_tool(async_server, "send_message", {
            "account": "acc1", "recipient": "@alex", "text": "reviewed"})
        operation, arguments = "approve_draft", {"draft_id": draft["draft_id"]}
    else:
        operation, arguments = "resume_account_actions", {"account": "acc1"}
    cancellation_ids = []

    async def perform():
        try:
            return await call_tool(async_server, operation, arguments)
        except asyncio.CancelledError as exc:
            cancellation_ids.append(getattr(exc, "action_id", None))
            raise

    task = asyncio.create_task(perform())
    try:
        await wait_for_tool_phase(task, entered)
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
        assert not task.done()
        with pytest.raises(PolicyError, match="Another operation"):
            with ctx.safety_store.operation_lock("acc1"):
                pytest.fail("Pause bookkeeping lost its account lock")
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await settled_tool_result(task)
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    status = ctx.safety_store.status("acc1")
    record = status["recent_actions"][0]
    assert cancellation_ids == [record["action_id"]]
    assert status["paused"] == (boundary == "challenge_pause")
    assert record["status"] == ("uncertain" if draft else "succeeded")
    if draft:
        assert ctx.draft_store.get(draft["draft_id"]).status == "uncertain"
    with ctx.safety_store.operation_lock("acc1"):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_count", [1, 3])
async def test_client_cancellation_before_denied_reservation_keeps_draft_pending(async_server, monkeypatch, cancel_count):
    ctx = async_server.xuse_ctx
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    cancelling = asyncio.Event()
    original_reserve, original_shield = ctx.safety_store.reserve, asyncio.shield
    ctx.safety_store.caps["message"] = 0

    def held_reservation(*args, **kwargs):
        loop.call_soon_threadsafe(entered.set)
        if not release.wait(STORAGE_WAIT_SECONDS):
            raise AssertionError("reservation fixture did not release")
        return original_reserve(*args, **kwargs)

    async def observed_shield(future):
        try:
            return await original_shield(future)
        except asyncio.CancelledError:
            cancelling.set()
            raise

    monkeypatch.setattr(ctx.safety_store, "reserve", held_reservation)
    monkeypatch.setattr(asyncio, "shield", observed_shield)
    draft = await call_tool(async_server, "send_message", {
        "account": "acc1", "recipient": "@alex", "text": "reviewed"})
    task = asyncio.create_task(call_tool(async_server, "approve_draft", {"draft_id": draft["draft_id"]}))
    try:
        await wait_for_tool_phase(task, entered)
        task.cancel()
        await wait_for_tool_phase(task, cancelling)
        for _ in range(cancel_count - 1):
            task.cancel()
            await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await settled_tool_result(task)
    finally:
        release.set()
    assert ctx.draft_store.get(draft["draft_id"]).status == "pending"
    assert ctx.safety_store.status("acc1")["recent_actions"] == []
    assert ctx.session_pool.started == 0
    assert (await call_tool(async_server, "reject_draft", {"draft_id": draft["draft_id"]}))["ok"]


@pytest.mark.asyncio
async def test_cancellation_racing_completed_failure_consumes_child_exception(async_server, monkeypatch):
    from xuse.mcp import browser_bridge
    from xuse.mcp.safety import PolicyError

    children, cancellation_ids = [], []

    async def fail_at_completion(*args, **kwargs):
        children.append(asyncio.current_task())
        asyncio.get_running_loop().call_soon(caller.cancel)
        raise PolicyError("synthetic completed failure", reason="challenge", action_id="synthetic-action")

    monkeypatch.setattr(browser_bridge, "_browser_call_locked", fail_at_completion)

    async def perform():
        try:
            await browser_bridge.browser_call(async_server.xuse_ctx, "acc1", "send_message", kind="message")
        except asyncio.CancelledError as exc:
            cancellation_ids.append(getattr(exc, "action_id", None))
            raise

    caller = asyncio.create_task(perform())
    with pytest.raises(asyncio.CancelledError):
        await settled_tool_result(caller)
    assert cancellation_ids == ["synthetic-action"]
    assert children[0].done() and not children[0].cancelled()
    # asyncio uses this marker to emit "Task exception was never retrieved"
    # at destruction. Do not retrieve the exception in the test itself.
    assert not children[0]._log_traceback


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["resume_account_actions", "unlock_inbox"])
async def test_new_manual_pause_survives_successful_inflight_recovery(async_server, monkeypatch, operation):
    ctx = async_server.xuse_ctx
    entered, release = asyncio.Event(), asyncio.Event()
    ctx.safety_store.pause("acc1", "pin_required" if operation == "unlock_inbox" else "challenge")

    async def successful_probe(*args):
        entered.set()
        await release.wait()
        return {"success": True}

    if operation == "unlock_inbox":
        monkeypatch.setenv("XUSE_INBOX_PIN", "1234")
        ctx.session_pool.browser.unlock_messages = successful_probe
    else:
        ctx.session_pool.browser.verify_session = successful_probe
    task = asyncio.create_task(call_tool(async_server, operation, {"account": "acc1"}))
    try:
        await wait_for_tool_phase(task, entered)
        assert (await call_tool(async_server, "pause_account_actions", {"account": "acc1"}))["ok"]
    finally:
        release.set()
        result = await settled_tool_result(task)
    assert not result["ok"] and result["error"]["reason"] == "pause_changed"
    status = ctx.safety_store.status("acc1")
    assert status["paused"] and status["pause"]["reason"] == "manual_pause"
