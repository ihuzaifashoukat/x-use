"""Orchestrator teardown must not block the event loop.

``_process_account``'s finally closes the browser via a blocking Selenium
``driver.quit()``; ``run_cycle`` drives this coroutine on the MCP server's
event loop, so the close must be offloaded to a worker thread.
"""
import threading
from unittest.mock import MagicMock

import pytest

import xuse.orchestrator as orch_mod


class RecordingBrowserManager:
    """BrowserManager stand-in recording the thread close_driver ran on."""

    instances = []

    def __init__(self, account_config=None):
        self.close_thread = None
        RecordingBrowserManager.instances.append(self)

    def close_driver(self):
        self.close_thread = threading.get_ident()


def _noop_action_config():
    return {
        "enable_competitor_reposts": False,
        "enable_keyword_replies": False,
        "enable_content_curation_posts": False,
        "enable_liking_tweets": False,
        "enable_keyword_retweets": False,
        "enable_thread_analysis": False,
        "enable_community_engagement": False,
        "min_delay_between_actions_seconds": 0,
        "max_delay_between_actions_seconds": 0,
    }


@pytest.mark.asyncio
async def test_teardown_closes_driver_off_the_event_loop(monkeypatch, make_config_loader):
    loader = make_config_loader(settings={"delay_between_accounts_seconds": 0}, accounts=[])
    RecordingBrowserManager.instances = []
    monkeypatch.setattr(orch_mod, "BrowserManager", RecordingBrowserManager)
    monkeypatch.setattr(orch_mod, "LLMService", MagicMock())
    monkeypatch.setattr(orch_mod, "TweetScraper", MagicMock())
    monkeypatch.setattr(orch_mod, "TweetPublisher", MagicMock())
    monkeypatch.setattr(orch_mod, "TweetEngagement", MagicMock())
    monkeypatch.setattr(orch_mod, "TweetAnalyzer", MagicMock())
    monkeypatch.setattr(orch_mod, "MetricsRecorder", MagicMock())

    orch = object.__new__(orch_mod.TwitterOrchestrator)
    orch.config_loader = loader
    orch.file_handler = MagicMock()
    orch.global_settings = {"delay_between_accounts_seconds": 0}
    orch.accounts_data = []
    orch.processed_action_keys = set()
    orch.analysis_config = {}
    orch.engagement_decision_cfg = {"enabled": False}

    account_dict = {
        "account_id": "acc1",
        "is_active": True,
        "action_config": _noop_action_config(),
    }
    loop_thread = threading.get_ident()
    await orch._process_account(account_dict)

    assert RecordingBrowserManager.instances, "BrowserManager was never constructed"
    browser_manager = RecordingBrowserManager.instances[0]
    assert browser_manager.close_thread is not None, "close_driver was never called"
    assert browser_manager.close_thread != loop_thread, (
        "close_driver ran inline on the event loop — blocking Selenium quit "
        "must be offloaded via asyncio.to_thread"
    )
