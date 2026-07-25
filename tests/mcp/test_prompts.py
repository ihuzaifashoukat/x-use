"""MCP prompts: the portable, client-agnostic form of the bundled skills.

A prompt that names a tool x-use does not have, or that tells a client to
publish without approval, is worse than no prompt at all. These tests pin both.
"""
import pytest

from xuse.mcp import prompts as prompts_mod

from helpers import make_account  # noqa: F401
from helpers import accounts, browser_factory, config_loader, draft_store  # noqa: F401
from helpers import drafts_path, mcp_server, mcp_settings, queue_store, session_pool  # noqa: F401

EXPECTED = {
    "research_niche": {"account", "keywords", "profiles"},
    "draft_replies": {"account", "tweet_urls", "lane"},
    "review_and_publish": {"account"},
    "daily_check": {"account"},
    "setup_account": set(),
}

# Every tool name a prompt is allowed to mention. Anything else is either a
# typo or an invented capability, and both mislead the client.
REAL_TOOLS = {
    "list_accounts", "get_account", "get_metrics", "search_tweets", "search_profile",
    "get_tweet", "prepare_reply", "list_queue", "list_drafts", "get_draft",
    "reject_draft", "get_run_status", "get_account_health", "list_proxies",
    "post_tweet", "generate_and_post", "reply_to_tweet", "engage", "run_cycle",
    "approve_draft", "queue_post", "queue_engagement", "cancel_queued_action",
    "process_queue", "research_and_stage", "draft_post_variations", "add_account",
    "update_account", "set_account_active", "remove_account", "add_proxy",
    "remove_proxy", "test_proxy",
}


async def render(server, name, arguments=None) -> str:
    result = await server.get_prompt(name, arguments or {})
    return "\n".join(m.content.text for m in result.messages)


def flat(text: str) -> str:
    """Collapse whitespace so content assertions survive line wrapping."""
    return " ".join(text.split())


@pytest.mark.asyncio
async def test_registers_the_documented_prompts_with_their_arguments(mcp_server):
    listed = {p.name: {a.name for a in (p.arguments or [])} for p in await mcp_server.list_prompts()}
    assert listed == EXPECTED


@pytest.mark.asyncio
async def test_every_prompt_has_a_description(mcp_server):
    for prompt in await mcp_server.list_prompts():
        assert prompt.description and len(prompt.description) > 40, prompt.name


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(EXPECTED))
async def test_prompts_render_with_no_arguments(mcp_server, name):
    """Clients surface prompts with every optional argument blank. None may fail."""
    body = await render(mcp_server, name)
    assert len(body) > 200


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(EXPECTED))
async def test_prompts_only_name_tools_that_exist(mcp_server, name):
    import re

    body = await render(mcp_server, name)
    # Any identifier written as a call, e.g. `get_account("x")` or process_queue(
    called = set(re.findall(r"\b([a-z_][a-z0-9_]{4,})\(", body))
    invented = {c for c in called if c not in REAL_TOOLS}
    assert not invented, f"{name} references non-existent tools: {sorted(invented)}"


@pytest.mark.asyncio
async def test_publishing_prompt_states_the_approval_rule(mcp_server):
    body = flat(await render(mcp_server, "review_and_publish", {"account": "acc1"}))
    assert "PUBLISHES TO X" in body
    assert "approve_draft" in body
    # Must warn that omitting account drains every configured account.
    assert "drains every configured account" in body
    # Must not tell a client to loop approvals.
    assert "Never loop over the whole list" in body
    # Must say the post URL is not returned, or agents invent one.
    assert "approve_draft returns no post URL" in body


@pytest.mark.asyncio
async def test_drafting_prompt_forbids_auto_text_and_states_the_clamp(mcp_server):
    body = await render(mcp_server, "draft_replies", {"account": "acc1"})
    assert 'Never pass text="auto"' in body
    assert str(prompts_mod.MAX_REPLY_CHARS) in body


@pytest.mark.asyncio
async def test_drafting_prompt_is_lane_aware(mcp_server):
    """Lane is chosen at staging time because drafts and the queue are separate
    stores with no bridge; the prompt must not offer both at once."""
    draft = await render(mcp_server, "draft_replies", {"account": "acc1", "lane": "draft"})
    queued = await render(mcp_server, "draft_replies", {"account": "acc1", "lane": "queue"})

    assert "reply_to_tweet(account, tweet_url" in draft and "reject_draft" in draft
    assert "queue_engagement(account" in queued and "cancel_queued_action" in queued
    assert "queue_engagement(account" not in draft
    assert "reply_to_tweet(account, tweet_url" not in queued


@pytest.mark.asyncio
async def test_prompts_default_to_the_first_active_account(make_config_loader, drafts_path,
                                                            queue_store):
    from xuse.mcp.drafts import DraftStore
    from xuse.mcp.server import create_server

    loader = make_config_loader(accounts=[make_account("paused", is_active=False),
                                          make_account("live")])
    server = create_server(config_loader=loader, draft_store=DraftStore(drafts_path),
                           queue_store=queue_store)
    assert "`live`" in await render(server, "daily_check")


@pytest.mark.asyncio
async def test_prompts_render_when_nothing_is_configured(make_config_loader, drafts_path,
                                                          queue_store):
    """A fresh install has no accounts. Prompts still have to render, since
    setup_account is exactly what that user needs."""
    from xuse.mcp.drafts import DraftStore
    from xuse.mcp.server import create_server

    server = create_server(config_loader=make_config_loader(accounts=[]),
                           draft_store=DraftStore(drafts_path), queue_store=queue_store)
    for name in sorted(EXPECTED):
        assert len(await render(server, name)) > 200


@pytest.mark.asyncio
async def test_setup_prompt_never_asks_for_cookie_contents(mcp_server):
    """Cookie import is by server-side path precisely so values never cross the
    wire; the onboarding prompt must not undo that."""
    body = flat(await render(mcp_server, "setup_account"))
    assert "cookie_file=<path>" in body
    assert "never pass through the conversation" in body
    assert "do not ask the user to paste" in body
