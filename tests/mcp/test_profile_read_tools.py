"""Registered profile reads use the shared browser policy without staging work."""
import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from xuse.browser.errors import SessionError
from xuse.mcp.drafts import DraftStore
from xuse.mcp.safety import SafetyStore
from xuse.mcp.server import create_server
from xuse.models import ScrapedTweet
from xuse.outreach import OutreachStore
from xuse.queue import QueueStore

from helpers import assert_error_envelope, call_tool, make_account


PROFILE_READS = ("get_profile_context", "get_profile_posts", "get_profile_connections")


@pytest.fixture
def profile_server(make_config_loader, tmp_path, monkeypatch):
    accounts = [make_account("a"), make_account("inactive", is_active=False), make_account("b")]
    loader = make_config_loader(accounts=accounts)
    now = [1_800_000_000.0]
    llm_calls = []

    def forbidden_llm(*args, **kwargs):
        llm_calls.append((args, kwargs))
        raise AssertionError("Profile reads must not call the LLM.")

    monkeypatch.setattr("xuse.mcp.executor.get_llm", forbidden_llm)

    class Browser:
        def __init__(self):
            self.calls = []
            self.failure = None
            self.raw_element_data = {"private": "PRIVATE_RAW_DOM_SENTINEL"}

        def read(self, operation, *args):
            self.calls.append((operation, *args))
            if self.failure is not None:
                raise self.failure

        def post(self, handle):
            return ScrapedTweet(tweet_id="123", tweet_url=f"https://x.com/{handle}/status/123",
                                user_handle="@" + handle, text_content="A post about building tools.",
                                raw_element_data=self.raw_element_data)

        async def get_profile_context(self, handle, post_limit):
            self.read("get_profile_context", handle, post_limit)
            return {"profile": {"handle": handle.lower(), "name": "Ada", "bio": "Builds tools",
                                "url": "https://x.com/" + handle.lower(), "can_message": True},
                    "posts": [self.post(handle)], "partial": True,
                    "pagination": "bounded_scroll", "source": "browser_dom"}

        async def get_profile_posts(self, handle, feed, limit):
            self.read("get_profile_posts", handle, feed, limit)
            return {"handle": handle.lower(), "feed": feed, "posts": [self.post(handle)],
                    "count": 1, "partial": True, "pagination": "bounded_scroll", "source": "browser_dom"}

        async def get_profile_connections(self, handle, relationship, limit):
            self.read("get_profile_connections", handle, relationship, limit)
            return {"handle": handle.lower(), "relationship": relationship,
                    "connections": [{"handle": "grace", "url": "https://x.com/grace",
                                     "visible_text": "Grace @grace"}],
                    "count": 1, "partial": True, "pagination": "visible_only", "source": "browser_dom"}

        async def verify_session(self):
            self.read("verify_session")
            return {"success": True}

        async def unlock_messages(self, pin):
            # The secret is validated at the fake boundary without recording it.
            assert pin.isdigit() and 4 <= len(pin) <= 12
            self.read("unlock_messages")
            return {"success": True}

    class Pool:
        # A warm pool deliberately does not recheck activation itself. The
        # shared MCP boundary must stop reads before reserving or acquiring it.
        backend = "patchright"
        idle_timeout_seconds = 600

        def __init__(self):
            self.browser = Browser()
            self.started = 0
            self.after_acquire = None

        def find_account_dict(self, account):
            for raw in accounts:
                if raw["account_id"] == account:
                    return raw
            raise SessionError("unknown_account")

        @asynccontextmanager
        async def session(self, account):
            self.started += 1
            await asyncio.sleep(0)
            if self.after_acquire is not None:
                self.after_acquire(account)
            yield self.browser

    pool = Pool()
    server = create_server(config_loader=loader, session_pool=pool, draft_mode=False,
                           draft_store=DraftStore(tmp_path / "drafts.jsonl"),
                           queue_store=QueueStore(tmp_path / "queue.jsonl"),
                           outreach_store=OutreachStore(tmp_path / "outreach.sqlite3"),
                           safety_store=SafetyStore(tmp_path / "safety.sqlite3", clock=lambda: now[0]))
    return SimpleNamespace(server=server, ctx=server.xuse_ctx, pool=pool, now=now, llm_calls=llm_calls)


def assert_no_staged_work(env):
    assert len(env.ctx.draft_store) == 0
    assert len(env.ctx.queue_store) == 0
    assert env.llm_calls == []
    assert all(call[0] in PROFILE_READS for call in env.pool.browser.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", PROFILE_READS)
@pytest.mark.parametrize("profile", ["Ada_1", " @Ada_1 ", "https://x.com/Ada_1",
                                    "x.com/Ada_1", "https://twitter.com/Ada_1/status/123?ref=profile"])
async def test_profile_forms_pass_only_the_exact_handle_to_browser(profile_server, tool, profile):
    env = profile_server
    result = await call_tool(env.server, tool, {"account": "a", "profile": profile})
    assert result["ok"] and result["account"] == "a"
    assert env.pool.browser.calls[0][:2] == (tool, "Ada_1")
    assert len(env.pool.browser.calls) == env.pool.started == 1
    assert env.ctx.safety_store.status("a")["daily_used"] == {"read": 1}
    assert result["partial"] is True and result["source"] == "browser_dom"
    assert result["action_id"]
    assert_no_staged_work(env)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,expected", [
    ("get_profile_context", ("get_profile_context", "ada", 5)),
    ("get_profile_posts", ("get_profile_posts", "ada", "posts", 10)),
    ("get_profile_connections", ("get_profile_connections", "ada", "following", 20)),
])
async def test_registered_defaults_match_bounded_browser_arguments(profile_server, tool, expected):
    env = profile_server
    result = await call_tool(env.server, tool, {"account": "a", "profile": "ada"})
    assert result["ok"] and env.pool.browser.calls == [expected]
    if tool == "get_profile_context":
        assert result["profile"]["handle"] == "ada" and result["profile"]["can_message"]
    elif tool == "get_profile_posts":
        assert result["feed"] == "posts" and result["count"] == 1
    else:
        assert result["relationship"] == "following" and result["count"] == 1
        assert result["connections"] == [{"handle": "grace", "url": "https://x.com/grace",
                                          "visible_text": "Grace @grace"}]
        assert result["pagination"] == "visible_only"
    assert_no_staged_work(env)


@pytest.mark.asyncio
@pytest.mark.parametrize("feed", ["posts", "replies", "media"])
async def test_posts_accept_all_documented_feeds_and_preserve_partial_pagination(profile_server, feed):
    env = profile_server
    result = await call_tool(env.server, "get_profile_posts", {
        "account": "a", "profile": "ada", "feed": feed, "limit": 50})
    assert result["ok"] and result["feed"] == feed
    assert result["pagination"] == "bounded_scroll" and result["partial"] is True
    assert env.pool.browser.calls == [("get_profile_posts", "ada", feed, 50)]
    assert_no_staged_work(env)


@pytest.mark.asyncio
@pytest.mark.parametrize("relationship", ["followers", "following"])
async def test_connections_accept_only_public_relationships(profile_server, relationship):
    env = profile_server
    result = await call_tool(env.server, "get_profile_connections", {
        "account": "a", "profile": "ada", "relationship": relationship, "limit": 50})
    assert result["ok"] and result["relationship"] == relationship
    assert result["pagination"] == "visible_only" and result["partial"] is True
    assert env.pool.browser.calls == [("get_profile_connections", "ada", relationship, 50)]
    assert_no_staged_work(env)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,arguments", [
    ("get_profile_context", {"post_limit": 0}), ("get_profile_context", {"post_limit": 11}),
    ("get_profile_posts", {"limit": 0}), ("get_profile_posts", {"limit": 51}),
    ("get_profile_posts", {"feed": "likes"}), ("get_profile_posts", {"feed": "Posts"}),
    ("get_profile_connections", {"limit": 0}), ("get_profile_connections", {"limit": 51}),
    ("get_profile_connections", {"relationship": "contacts"}),
    ("get_profile_connections", {"relationship": "Followers"}),
])
async def test_unsupported_options_fail_before_browser_or_budget_reservation(profile_server, tool, arguments):
    env = profile_server
    result = await call_tool(env.server, tool, {"account": "a", "profile": "ada", **arguments})
    assert_error_envelope(result)
    assert env.pool.started == 0 and env.pool.browser.calls == []
    assert env.ctx.safety_store.status("a")["daily_used"] == {}
    assert_no_staged_work(env)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", PROFILE_READS)
@pytest.mark.parametrize("profile", ["", "https://evil.test/ada", "https://x.com/home",
                                    "ada/someone", "a" * 16, "ada@example.test"])
async def test_invalid_profile_fails_before_browser_or_budget_reservation(profile_server, tool, profile):
    env = profile_server
    result = await call_tool(env.server, tool, {"account": "a", "profile": profile})
    assert_error_envelope(result)
    assert env.pool.started == 0 and env.pool.browser.calls == []
    assert env.ctx.safety_store.status("a")["daily_used"] == {}
    assert_no_staged_work(env)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", PROFILE_READS)
async def test_inactive_warm_account_never_reserves_or_acquires_browser(profile_server, tool):
    env = profile_server
    result = await call_tool(env.server, tool, {"account": "inactive", "profile": "ada"})
    assert_error_envelope(result, "paused")
    assert env.pool.started == 0 and env.pool.browser.calls == []
    assert env.ctx.safety_store.status("inactive")["daily_used"] == {}
    assert_no_staged_work(env)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", PROFILE_READS)
async def test_deactivation_while_acquiring_warm_session_prevents_the_read(profile_server, tool):
    env = profile_server

    def deactivate(account):
        env.pool.find_account_dict(account)["is_active"] = False

    env.pool.after_acquire = deactivate
    result = await call_tool(env.server, tool, {"account": "a", "profile": "ada"})
    error = assert_error_envelope(result, "paused")
    assert env.pool.started == 1 and env.pool.browser.calls == []
    assert env.ctx.safety_store.reference("a", error["action_id"])["status"] == "failed"
    assert env.ctx.safety_store.status("a")["daily_used"] == {"read": 1}
    assert_no_staged_work(env)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["get_profile_context", "get_profile_posts"])
async def test_raw_dom_is_excluded_before_json_serialization(profile_server, tool, caplog):
    env = profile_server
    env.pool.browser.raw_element_data = {"private": "PRIVATE_RAW_DOM_SENTINEL", "dom": object()}
    result = await call_tool(env.server, tool, {"account": "a", "profile": "ada"})
    assert result["ok"]
    assert result["posts"][0]["tweet_id"] == "123"
    assert "raw_element_data" not in result["posts"][0]
    assert "PRIVATE_RAW_DOM_SENTINEL" not in json.dumps(result) + caplog.text
    assert result["pagination"] == "bounded_scroll"
    assert_no_staged_work(env)


@pytest.mark.asyncio
async def test_profile_tools_share_normal_read_cooldown_and_account_budgets(profile_server):
    env = profile_server
    first = await call_tool(env.server, "get_profile_context", {"account": "a", "profile": "ada"})
    assert first["ok"]
    blocked = await call_tool(env.server, "get_profile_posts", {"account": "a", "profile": "ada"})
    error = assert_error_envelope(blocked)
    assert error["reason"] == "cooldown" and error["retry_after_seconds"] == 2
    assert env.pool.started == 1
    other = await call_tool(env.server, "get_profile_posts", {"account": "b", "profile": "ada"})
    assert other["ok"]
    env.now[0] += 3
    last = await call_tool(env.server, "get_profile_connections", {"account": "a", "profile": "ada"})
    assert last["ok"]
    assert env.pool.started == 3
    assert env.ctx.safety_store.status("a")["daily_used"] == {"read": 2}
    assert env.ctx.safety_store.status("b")["daily_used"] == {"read": 1}
    assert_no_staged_work(env)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", PROFILE_READS)
async def test_shared_manual_pause_blocks_every_profile_read(profile_server, tool):
    env = profile_server
    env.ctx.safety_store.pause("a", "manual_pause")
    result = await call_tool(env.server, tool, {"account": "a", "profile": "ada"})
    error = assert_error_envelope(result)
    assert error["reason"] == "manual_pause"
    assert env.pool.started == 0 and env.pool.browser.calls == []
    assert env.ctx.safety_store.status("a")["daily_used"] == {}
    assert_no_staged_work(env)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", PROFILE_READS)
async def test_manual_pause_during_warm_session_acquisition_prevents_the_read(profile_server, tool):
    env = profile_server
    env.pool.after_acquire = lambda account: env.ctx.safety_store.pause(account, "manual_pause")
    result = await call_tool(env.server, tool, {"account": "a", "profile": "ada"})
    error = assert_error_envelope(result)
    assert error["reason"] == "manual_pause"
    assert env.pool.started == 1 and env.pool.browser.calls == []
    assert env.ctx.safety_store.reference("a", error["action_id"])["status"] == "failed"
    assert env.ctx.safety_store.status("a")["daily_used"] == {"read": 1}
    assert_no_staged_work(env)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,pause,operation,budget", [
    ("resume_account_actions", "manual_pause", "verify_session", "read"),
    ("unlock_inbox", "pin_required", "unlock_messages", "unlock"),
])
async def test_explicit_recovery_keeps_its_narrow_pause_exemption(profile_server, monkeypatch, tool, pause, operation, budget):
    env = profile_server
    monkeypatch.setenv("XUSE_INBOX_PIN", "1357")
    env.ctx.safety_store.pause("a", pause)
    result = await call_tool(env.server, tool, {"account": "a"})
    assert result["ok"]
    assert env.pool.started == 1 and env.pool.browser.calls == [(operation,)]
    status = env.ctx.safety_store.status("a")
    assert status["daily_used"] == {budget: 1} and status["pause"] is None
    assert len(env.ctx.draft_store) == len(env.ctx.queue_store) == 0
    assert env.llm_calls == [] and "1357" not in json.dumps(result)


@pytest.mark.asyncio
async def test_expired_browser_session_returns_safe_recovery_and_pauses_other_profile_reads(profile_server, caplog):
    env = profile_server
    failure = SessionError("session_expired")
    failure.operator_hint = "PRIVATE_SESSION_SENTINEL"
    env.pool.browser.failure = failure
    result = await call_tool(env.server, "get_profile_context", {"account": "a", "profile": "ada"})
    error = assert_error_envelope(result)
    assert error["type"] == "SessionError" and error["reason"] == "session_expired"
    assert error["action_id"]
    assert "Close the session" in error["recovery"] and "Restart the MCP server" in error["recovery"]
    assert "PRIVATE_SESSION_SENTINEL" not in json.dumps(result) + caplog.text
    assert env.ctx.safety_store.reference("a", error["action_id"])["status"] == "failed"
    env.now[0] += 3
    blocked = await call_tool(env.server, "get_profile_connections", {"account": "a", "profile": "ada"})
    assert assert_error_envelope(blocked)["reason"] == "session_expired"
    assert env.pool.started == 1 and len(env.pool.browser.calls) == 1
    assert env.ctx.safety_store.status("a")["daily_used"] == {"read": 1}
    assert_no_staged_work(env)
