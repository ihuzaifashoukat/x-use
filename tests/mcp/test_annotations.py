"""Every tool declares its blast radius, and the declaration matches reality.

x-use's read-only vs write boundary used to live only in prose inside tool
descriptions. Nothing could enforce it, and MCP directories that read
annotations as the authoritative capability declaration saw a server with 33
tools and no stated behaviour at all.

These tests pin the boundary. The lists below are the contract: moving a tool
between them is a deliberate act that has to be written down here, which is
exactly the point. A new tool with no annotation fails the first test.

All tests run against injected fakes, no browser and no network.
"""
import pytest

from helpers import (  # noqa: F401 - imported fixtures register for this module
    accounts,
    browser_factory,
    config_loader,
    draft_store,
    drafts_path,
    make_account,
    mcp_server,
    mcp_settings,
    queue_store,
    session_pool,
)

# Reads that never mutate anything, local or remote.
READ_ONLY = {
    "list_accounts", "get_account", "get_account_health", "get_metrics",
    "list_drafts", "get_draft", "list_queue", "list_proxies", "get_run_status",
    "search_tweets", "search_profile", "get_tweet", "prepare_reply", "test_proxy",
}

# Reads and writes that cross the network: x.com through the browser, or the
# configured LLM. A host cannot treat any of these as replayable.
OPEN_WORLD = {
    "search_tweets", "search_profile", "get_tweet", "prepare_reply", "test_proxy",
    "approve_draft", "post_tweet", "generate_and_post", "reply_to_tweet",
    "engage", "process_queue", "run_cycle",
    "research_and_stage", "draft_post_variations",
}

# Tools that remove existing configuration, as opposed to adding to it.
DESTRUCTIVE = {"remove_account", "remove_proxy"}

# The only two tools that can turn stored intent into a public action on X.
PUBLISH_GATES = {"approve_draft", "process_queue"}


async def _annotations(server):
    return {t.name: t.annotations for t in await server.list_tools()}


@pytest.mark.asyncio
async def test_every_tool_declares_annotations(mcp_server):
    """A tool with no annotations is invisible to hosts and to directory scanners."""
    missing = [name for name, a in (await _annotations(mcp_server)).items() if a is None]
    assert not missing, f"tools with no annotations: {sorted(missing)}"


@pytest.mark.asyncio
async def test_read_only_set_is_exactly_as_declared(mcp_server):
    ann = await _annotations(mcp_server)
    actual = {name for name, a in ann.items() if a.readOnlyHint}
    assert actual == READ_ONLY, (
        f"unexpected read-only: {sorted(actual - READ_ONLY)}; "
        f"no longer read-only: {sorted(READ_ONLY - actual)}"
    )


@pytest.mark.asyncio
async def test_open_world_set_is_exactly_as_declared(mcp_server):
    ann = await _annotations(mcp_server)
    actual = {name for name, a in ann.items() if a.openWorldHint}
    assert actual == OPEN_WORLD, (
        f"newly reaches the network: {sorted(actual - OPEN_WORLD)}; "
        f"no longer reaches it: {sorted(OPEN_WORLD - actual)}"
    )


@pytest.mark.asyncio
async def test_only_config_removal_is_marked_destructive(mcp_server):
    """Posting is additive. Reserving this flag for real deletion keeps it useful."""
    ann = await _annotations(mcp_server)
    actual = {name for name, a in ann.items() if a.destructiveHint}
    assert actual == DESTRUCTIVE


@pytest.mark.asyncio
async def test_read_only_tools_claim_no_write_behaviour(mcp_server):
    """destructiveHint and idempotentHint are meaningless when readOnlyHint is set."""
    for name, a in (await _annotations(mcp_server)).items():
        if a.readOnlyHint:
            assert not a.destructiveHint, f"{name} is read-only but flagged destructive"


@pytest.mark.asyncio
async def test_the_publish_gates_are_not_read_only_and_reach_x(mcp_server):
    """The whole draft/queue design rests on these two being the only way out."""
    ann = await _annotations(mcp_server)
    for name in PUBLISH_GATES:
        assert ann[name] is not None, f"{name} lost its annotations"
        assert not ann[name].readOnlyHint, f"{name} must not be advertised as read-only"
        assert ann[name].openWorldHint, f"{name} publishes to X and must say so"


@pytest.mark.asyncio
async def test_descriptions_stay_ascii(mcp_server):
    """Descriptions ship in tools/list and get rendered by MCP directories.

    Em dashes were reaching those pages and mojibaking in consumers that assume
    latin-1, so the house no-em-dash rule is pinned where it is actually shipped.
    """
    offenders = {}
    for tool in await mcp_server.list_tools():
        bad = sorted({c for c in (tool.description or "") if ord(c) > 127})
        if bad:
            offenders[tool.name] = bad
    assert not offenders, f"non-ASCII in tool descriptions: {offenders}"
