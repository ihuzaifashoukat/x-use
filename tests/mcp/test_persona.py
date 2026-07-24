"""Persona: add/update/clear via account tools, visible on get_account,
validated through the atomic writer. Fakes only — no browser."""
import pytest

from helpers import assert_error_envelope, call_tool  # noqa: F401 (fixtures)
from helpers import accounts, browser_factory, config_loader, draft_store  # noqa: F401
from helpers import drafts_path, mcp_server, mcp_settings, queue_store, session_pool  # noqa: F401

PERSONA = "# Builder persona\nShip in public. Casual, concrete, no buzzwords."


@pytest.mark.asyncio
async def test_add_account_with_persona(mcp_server):
    result = await call_tool(mcp_server, "add_account",
                             {"account_id": "p1", "persona": PERSONA})
    assert result["ok"] is True
    assert result["account"]["persona"] == PERSONA


@pytest.mark.asyncio
async def test_get_account_returns_persona(mcp_server):
    await call_tool(mcp_server, "add_account", {"account_id": "p2", "persona": PERSONA})
    result = await call_tool(mcp_server, "get_account", {"account": "p2"})
    assert result["account"]["persona"] == PERSONA


@pytest.mark.asyncio
async def test_update_and_clear_persona(mcp_server):
    await call_tool(mcp_server, "add_account", {"account_id": "p3", "persona": PERSONA})
    updated = await call_tool(mcp_server, "update_account",
                              {"account": "p3", "persona": "new voice"})
    assert updated["account"]["persona"] == "new voice"
    cleared = await call_tool(mcp_server, "update_account",
                              {"account": "p3", "persona": ""})
    assert "persona" not in cleared["account"] or cleared["account"]["persona"] is None


@pytest.mark.asyncio
async def test_persona_over_4000_chars_rejected(mcp_server):
    result = await call_tool(mcp_server, "add_account",
                             {"account_id": "p4", "persona": "x" * 4001})
    assert_error_envelope(result, "4000")


@pytest.mark.asyncio
async def test_accounts_without_persona_still_valid(mcp_server):
    result = await call_tool(mcp_server, "get_account", {"account": "acc1"})
    assert result["ok"] is True  # existing fixture account has no persona
