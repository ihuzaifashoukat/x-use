"""Proxy tools: validation, masking, pool CRUD on settings.json (tmp), and
test_proxy resolution with a faked BrowserManager. No browser, no network."""
import pytest

import xuse.mcp.proxy_tools as proxy_mod

from helpers import assert_error_envelope, call_tool  # noqa: F401
from helpers import accounts, browser_factory, config_loader, draft_store  # noqa: F401
from helpers import drafts_path, make_account, mcp_settings, queue_store  # noqa: F401
from helpers import session_pool  # noqa: F401

POOL_SETTINGS = {
    "browser_settings": {
        "proxy_pools": {"resi": ["http://user:pass@1.2.3.4:8080"]},
        "proxy_pool_strategy": "hash",
    }
}


@pytest.fixture
def mcp_server(make_config_loader, drafts_path, queue_store):
    from xuse.mcp.drafts import DraftStore
    from xuse.mcp.server import create_server
    from xuse.mcp.sessions import SessionPool
    loader = make_config_loader(
        settings=POOL_SETTINGS,
        accounts=[make_account("acc1", proxy="pool:resi")],
    )
    pool = SessionPool(loader, browser_factory=lambda d: None)
    return create_server(config_loader=loader, session_pool=pool,
                         draft_store=DraftStore(drafts_path), queue_store=queue_store)


@pytest.mark.asyncio
async def test_list_proxies_masks_credentials(mcp_server):
    result = await call_tool(mcp_server, "list_proxies", {})
    assert result["ok"] is True
    assert result["pools"]["resi"]["size"] == 1
    assert result["pools"]["resi"]["proxies"] == ["http://***@1.2.3.4:8080"]
    assert result["strategy"] == "hash"
    assert result["accounts"] == [{"account_id": "acc1", "proxy": "pool:resi"}]


@pytest.mark.asyncio
async def test_add_proxy_appends_and_dedupes(mcp_server):
    added = await call_tool(mcp_server, "add_proxy",
                            {"pool": "resi", "proxy_url": "socks5://9.9.9.9:1080"})
    assert added["ok"] is True and added["pools"]["resi"]["size"] == 2
    dup = await call_tool(mcp_server, "add_proxy",
                          {"pool": "resi", "proxy_url": "socks5://9.9.9.9:1080"})
    assert_error_envelope(dup, "already present")


@pytest.mark.asyncio
async def test_add_proxy_creates_pool_and_validates_scheme(mcp_server):
    created = await call_tool(mcp_server, "add_proxy",
                              {"pool": "dc", "proxy_url": "http://5.6.7.8:3128"})
    assert created["ok"] is True and created["pools"]["dc"]["size"] == 1
    bad = await call_tool(mcp_server, "add_proxy",
                          {"pool": "dc", "proxy_url": "ftp://nope"})
    assert_error_envelope(bad, "scheme")


@pytest.mark.asyncio
async def test_add_proxy_accepts_env_interpolated(mcp_server):
    result = await call_tool(mcp_server, "add_proxy",
                             {"pool": "dc", "proxy_url": "http://u:${RESI_PASS}@h:8080"})
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_remove_proxy_requires_confirm_and_reports_affected(mcp_server):
    refused = await call_tool(mcp_server, "remove_proxy",
                              {"pool": "resi", "proxy_url": "http://user:pass@1.2.3.4:8080"})
    assert_error_envelope(refused, "confirm=true")
    removed = await call_tool(mcp_server, "remove_proxy",
                              {"pool": "resi", "proxy_url": "http://user:pass@1.2.3.4:8080",
                               "confirm": True})
    assert removed["ok"] is True
    assert removed["pools"]["resi"]["size"] == 0
    assert removed["affected_accounts"] == ["acc1"]  # assigned pool:resi


@pytest.mark.asyncio
async def test_test_proxy_uses_explicit_url_and_masks(mcp_server, monkeypatch):
    calls = {}

    class FakeBM:
        def __init__(self, account_config=None):
            calls["proxy"] = account_config.get("proxy")

        def get_driver(self):
            class D:
                def get(self, url): calls["url"] = url
                @property
                def page_source(self): return '{"ip":"203.0.113.7"}'
            return D()

        def close_driver(self): calls["closed"] = True

    monkeypatch.setattr(proxy_mod, "BrowserManager", FakeBM)
    result = await call_tool(mcp_server, "test_proxy",
                             {"proxy_url": "http://user:secret@8.8.8.8:8000"})
    assert result["ok"] is True
    assert result["egress_ip"] == "203.0.113.7"
    assert result["proxy"] == "http://***@8.8.8.8:8000"
    assert calls["closed"] is True


@pytest.mark.asyncio
async def test_test_proxy_resolves_account_pool(mcp_server, monkeypatch):
    seen = {}

    class FakeBM:
        def __init__(self, account_config=None):
            seen["proxy"] = account_config.get("proxy")

        def get_driver(self):
            class D:
                def get(self, url): pass
                @property
                def page_source(self): return '{"ip":"198.51.100.3"}'
            return D()

        def close_driver(self): pass

    monkeypatch.setattr(proxy_mod, "BrowserManager", FakeBM)
    result = await call_tool(mcp_server, "test_proxy", {"account": "acc1"})
    assert result["ok"] is True
    assert seen["proxy"] == "http://user:pass@1.2.3.4:8080"  # resolved from pool:resi
    assert result["proxy"] == "http://***@1.2.3.4:8080"


@pytest.mark.asyncio
async def test_test_proxy_without_target_errors(mcp_server):
    result = await call_tool(mcp_server, "test_proxy", {})
    assert_error_envelope(result, "proxy_url")
