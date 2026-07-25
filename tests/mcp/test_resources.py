"""MCP resources: the read-only context surface.

Resources must never leak a secret, never start a browser, and never take the
server down on a bad URI. No browser and no network here.
"""
import json

import pytest

from helpers import make_account  # noqa: F401
from helpers import accounts, browser_factory, config_loader, draft_store  # noqa: F401
from helpers import drafts_path, mcp_server, mcp_settings, queue_store, session_pool  # noqa: F401

STATIC_URIS = {"xuse://accounts", "xuse://drafts/pending"}
TEMPLATE_URIS = {"xuse://accounts/{account_id}", "xuse://accounts/{account_id}/persona"}


async def read(server, uri: str) -> str:
    contents = await server.read_resource(uri)
    return list(contents)[0].content


@pytest.mark.asyncio
async def test_registers_the_documented_resources(mcp_server):
    listed = {str(r.uri) for r in await mcp_server.list_resources()}
    templated = {t.uriTemplate for t in await mcp_server.list_resource_templates()}
    assert listed == STATIC_URIS
    assert templated == TEMPLATE_URIS


@pytest.mark.asyncio
async def test_every_resource_declares_a_mime_type(mcp_server):
    for resource in await mcp_server.list_resources():
        assert resource.mimeType, f"{resource.uri} has no mimeType"


@pytest.mark.asyncio
async def test_accounts_roster_lists_configured_accounts(mcp_server):
    payload = json.loads(await read(mcp_server, "xuse://accounts"))
    assert payload["count"] == 2
    assert [a["account_id"] for a in payload["accounts"]] == ["acc1", "acc2"]


@pytest.mark.asyncio
async def test_account_resource_masks_secrets(make_config_loader, drafts_path, queue_store,
                                               session_pool):
    """Same masking as the tools: a resource is not a back door around it."""
    from xuse.mcp.drafts import DraftStore
    from xuse.mcp.server import create_server

    loader = make_config_loader(accounts=[make_account(
        "acc1",
        password="hunter2",
        cookies=[{"name": "auth_token", "value": "SECRET_COOKIE_VALUE"}],
        proxy="http://user:supersecret@proxy.example:8080",
    )])
    server = create_server(config_loader=loader, draft_store=DraftStore(drafts_path),
                           queue_store=queue_store)
    body = await read(server, "xuse://accounts/acc1")

    assert "SECRET_COOKIE_VALUE" not in body
    assert "hunter2" not in body
    assert "supersecret" not in body
    assert "proxy.example" in body  # host survives; only userinfo is masked

    roster = await read(server, "xuse://accounts")
    for secret in ("SECRET_COOKIE_VALUE", "hunter2", "supersecret"):
        assert secret not in roster


@pytest.mark.asyncio
async def test_persona_resource_returns_markdown_and_guides_when_unset(
        make_config_loader, drafts_path, queue_store):
    from xuse.mcp.drafts import DraftStore
    from xuse.mcp.server import create_server

    loader = make_config_loader(accounts=[
        make_account("withp", persona="Terse engineer. No hashtags."),
        make_account("nop"),
    ])
    server = create_server(config_loader=loader, draft_store=DraftStore(drafts_path),
                           queue_store=queue_store)

    assert "Terse engineer" in await read(server, "xuse://accounts/withp/persona")
    # An unset persona is a normal state for a new account, so it explains the
    # remedy instead of erroring.
    unset = await read(server, "xuse://accounts/nop/persona")
    assert "No persona is set" in unset
    assert "update_account" in unset


@pytest.mark.asyncio
async def test_pending_drafts_resource_shows_only_pending(mcp_server, draft_store):
    keep = draft_store.create(account="acc1", action="post_tweet",
                              payload={"text": "still pending"}, preview="p")
    gone = draft_store.create(account="acc1", action="post_tweet",
                              payload={"text": "already rejected"}, preview="p")
    draft_store.set_status(gone.draft_id, "rejected")

    payload = json.loads(await read(mcp_server, "xuse://drafts/pending"))
    ids = [d["draft_id"] for d in payload["drafts"]]
    assert keep.draft_id in ids
    assert gone.draft_id not in ids
    assert payload["count"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("uri", [
    "xuse://accounts/ghost",
    "xuse://accounts/ghost/persona",
    "xuse://accounts/..%2F..%2Fetc/persona",
    "xuse://accounts/has.dot",
])
async def test_bad_account_uris_error_without_killing_the_server(mcp_server, uri):
    with pytest.raises(Exception):
        await read(mcp_server, uri)
    # The contract that matters: the server keeps serving afterwards.
    assert len(await mcp_server.list_tools()) == 33
    assert json.loads(await read(mcp_server, "xuse://accounts"))["count"] == 2


@pytest.mark.asyncio
async def test_no_resource_starts_a_browser(mcp_server, browser_factory):
    for uri in ("xuse://accounts", "xuse://drafts/pending",
                "xuse://accounts/acc1", "xuse://accounts/acc1/persona"):
        await read(mcp_server, uri)
    assert browser_factory.created == []
