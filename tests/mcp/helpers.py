"""Shared fakes, helpers, and fixtures for the MCP tool test-suite.

Everything goes through the injection seams of ``create_server``
(config_loader=, session_pool=, draft_store=) and SessionPool's
``browser_factory`` — no real browser, no network, no real config/data files.

Note: fixtures live here (not in a tests/mcp/conftest.py) because a second
conftest.py would collide with tests/conftest.py under pytest's default
prepend import mode. Test modules import the fixtures they need from this
module — an imported fixture is registered for the importing module.
"""
import json
from typing import Any, Dict, List, Optional

import pytest

from xuse.mcp.drafts import DraftStore
from xuse.mcp.server import create_server
from xuse.mcp.sessions import SessionPool
from xuse.queue import QueueStore


class FakeBrowserManager:
    """Stands in for BrowserManager: records lifecycle, never opens a browser."""

    def __init__(self) -> None:
        self.driver_started = False
        self.closed = False

    def get_driver(self) -> object:
        self.driver_started = True
        return object()

    def close_driver(self) -> None:
        self.closed = True


class FakeBrowserFactory:
    """SessionPool ``browser_factory`` seam: counts cold starts per account."""

    def __init__(self) -> None:
        self.created: List[FakeBrowserManager] = []
        self.account_dicts: List[Dict[str, Any]] = []

    def __call__(self, account_dict: Dict[str, Any]) -> FakeBrowserManager:
        self.account_dicts.append(account_dict)
        manager = FakeBrowserManager()
        self.created.append(manager)
        return manager


class FakeMetrics:
    """MetricsRecorder stand-in: captures events/counters in memory."""

    def __init__(self, account_id: str) -> None:
        self.account_id = account_id
        self.events: List[tuple] = []
        self.counters: Dict[str, int] = {}

    def log_event(self, action: str, status: str, details: Optional[dict] = None) -> None:
        self.events.append((action, status, details))

    def increment(self, counter: str) -> None:
        self.counters[counter] = self.counters.get(counter, 0) + 1


def make_account(account_id: str = "acc1", **overrides: Any) -> Dict[str, Any]:
    """Minimal valid account dict with pacing delays zeroed for fast tests."""
    account: Dict[str, Any] = {
        "account_id": account_id,
        "is_active": True,
        "target_keywords": ["ai"],
        "action_config": {
            "min_delay_between_actions_seconds": 0,
            "max_delay_between_actions_seconds": 0,
        },
    }
    account.update(overrides)
    return account


def make_fake_publisher(instances: list) -> type:
    """Build a TweetPublisher fake whose instances record post_new_tweet calls."""

    class FakePublisher:
        def __init__(self, browser_manager, llm_service, account_config) -> None:
            self.browser_manager = browser_manager
            self.calls: List[Dict[str, Any]] = []
            instances.append(self)

        async def post_new_tweet(self, content, llm_settings=None) -> bool:
            self.calls.append({"text": content.text, "llm_settings": llm_settings})
            return True

    return FakePublisher


async def call_tool(server, name: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Invoke an MCP tool and return its result as a plain dict.

    Parses the JSON text content block (stable across FastMCP structured-
    output wrapping). If a tool ever raised through the guard instead of
    returning an error envelope, this would raise and fail the test.
    """
    content, _structured = await server.call_tool(name, arguments or {})
    assert content, f"tool '{name}' returned no content blocks"
    return json.loads(content[0].text)


def assert_error_envelope(result: Dict[str, Any], message_part: Optional[str] = None) -> Dict[str, Any]:
    """Assert the structured error envelope shape; return the error object."""
    assert result["ok"] is False, f"expected failure envelope, got: {result}"
    error = result["error"]
    assert isinstance(error["type"], str) and error["type"]
    assert isinstance(error["message"], str) and error["message"]
    if message_part is not None:
        assert message_part in error["message"]
    return error


# ---------------------------------------------------------------------------
# Fixtures — import these into test modules that need them (see module docstring).
# ---------------------------------------------------------------------------


@pytest.fixture
def accounts() -> List[Dict[str, Any]]:
    return [make_account("acc1"), make_account("acc2")]


@pytest.fixture
def mcp_settings() -> Dict[str, Any]:
    return {}


@pytest.fixture
def config_loader(make_config_loader, mcp_settings, accounts):
    return make_config_loader(settings=mcp_settings, accounts=accounts)


@pytest.fixture
def browser_factory() -> FakeBrowserFactory:
    return FakeBrowserFactory()


@pytest.fixture
def session_pool(config_loader, browser_factory) -> SessionPool:
    return SessionPool(config_loader, browser_factory=browser_factory)


@pytest.fixture
def drafts_path(tmp_path):
    return tmp_path / "drafts.jsonl"


@pytest.fixture
def draft_store(drafts_path) -> DraftStore:
    return DraftStore(drafts_path)


@pytest.fixture
def queue_store(tmp_path):
    return QueueStore(tmp_path / "queue.jsonl")


@pytest.fixture
def mcp_server(config_loader, session_pool, draft_store, queue_store):
    return create_server(config_loader=config_loader, session_pool=session_pool,
                         draft_store=draft_store, queue_store=queue_store)
