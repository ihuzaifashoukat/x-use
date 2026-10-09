"""Tests for orchestrator config handling (pure logic — no browser, no network).

Covers two seams of xuse.orchestrator.TwitterOrchestrator._process_account:

1. Legacy ``*_override`` key normalization onto AccountConfig fields. The
   normalizer is a closure inside ``_process_account``; it is exercised through
   the inactive-account early-return path (which runs before any browser/LLM
   construction), spying on ``AccountConfig.model_validate`` to capture the
   normalized dict and resulting model.

2. ActionConfig precedence (per-account > global > model default), exercised
   through the active-account path with all heavy collaborators
   (BrowserManager, LLMService, scraper/publisher/engagement/metrics/analyzer)
   replaced by MagicMock fakes and every pipeline enable-flag switched off.

Note: ``_process_account`` sleeps ``global_settings['delay_between_accounts_seconds']``
in its ``finally`` block; tests set it to 0 to stay fast.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import xuse.orchestrator as orchestrator_module
from xuse.models import AccountConfig, ActionConfig, ScrapedTweet
from xuse.orchestrator import TwitterOrchestrator

# Every pipeline gate switched off so an "active" account flows straight
# through _process_account without scraping/posting/sleeping per-action.
ALL_PIPELINES_OFF = {
    "enable_competitor_reposts": False,
    "enable_keyword_replies": False,
    "enable_content_curation_posts": False,
    "enable_liking_tweets": False,
    "enable_keyword_retweets": False,
    "enable_thread_analysis": False,
    "enable_community_engagement": False,
}


def _bare_orchestrator() -> TwitterOrchestrator:
    """Orchestrator shell without __init__ (no config files or dirs touched).

    Sufficient for the inactive-account early-return path, which never reads
    any instance attribute.
    """
    return TwitterOrchestrator.__new__(TwitterOrchestrator)


def _wired_orchestrator(global_settings: dict) -> TwitterOrchestrator:
    """Orchestrator shell with the instance attributes _process_account reads."""
    orch = TwitterOrchestrator.__new__(TwitterOrchestrator)
    orch.config_loader = MagicMock(name="config_loader")
    orch.file_handler = MagicMock(name="file_handler")
    orch.global_settings = global_settings
    orch.accounts_data = []
    orch.processed_action_keys = set()
    orch.analysis_config = {}
    orch.engagement_decision_cfg = {"enabled": False}
    return orch


@pytest.fixture
def model_validate_spy(monkeypatch):
    """Capture (input dict, result model) of every AccountConfig.model_validate call."""
    captured_inputs = []
    captured_results = []
    real_model_validate = AccountConfig.model_validate

    def spy(cls, obj, *args, **kwargs):
        captured_inputs.append(dict(obj))
        result = real_model_validate(obj, *args, **kwargs)
        captured_results.append(result)
        return result

    monkeypatch.setattr(AccountConfig, "model_validate", classmethod(spy))
    return captured_inputs, captured_results


@pytest.fixture
def fake_collaborators(monkeypatch):
    """Replace every heavy collaborator in the orchestrator namespace with a MagicMock."""
    fakes = {}
    for name in (
        "BrowserManager",
        "LLMService",
        "TweetScraper",
        "TweetPublisher",
        "TweetEngagement",
        "MetricsRecorder",
        "TweetAnalyzer",
    ):
        fake = MagicMock(name=name)
        monkeypatch.setattr(orchestrator_module, name, fake)
        fakes[name] = fake
    return fakes


@pytest.fixture
def action_config_spy(monkeypatch):
    """Capture calls to ActionConfig(...) made by the orchestrator namespace.

    The orchestrator builds the effective config as
    ``account.action_config or ActionConfig(**global_action_config_dict)``;
    recording constructor calls shows exactly when the global fallback fires.
    """
    calls = []
    results = []
    real_action_config = ActionConfig

    def spy(**kwargs):
        calls.append(kwargs)
        config = real_action_config(**kwargs)
        results.append(config)
        return config

    monkeypatch.setattr(orchestrator_module, "ActionConfig", spy)
    return calls, results


class TestLegacyOverrideNormalization:
    def test_legacy_override_keys_map_to_current_fields(self, model_validate_spy):
        captured_inputs, captured_results = model_validate_spy
        orch = _bare_orchestrator()
        account_dict = {
            "account_id": "legacy_acc",
            "is_active": False,  # early return before any browser/LLM work
            "target_keywords_override": ["ai", "ml"],
            "competitor_profiles_override": ["https://x.com/rival"],
            "news_sites_override": ["https://news.example.com"],
            "research_paper_sites_override": ["https://papers.example.com"],
            "action_config_override": {"max_likes_per_run": 9},
        }

        asyncio.run(orch._process_account(account_dict))

        assert len(captured_inputs) == 1
        normalized = captured_inputs[0]
        assert normalized["target_keywords"] == ["ai", "ml"]
        assert normalized["competitor_profiles"] == ["https://x.com/rival"]
        assert normalized["news_sites"] == ["https://news.example.com"]
        assert normalized["research_paper_sites"] == ["https://papers.example.com"]
        assert normalized["action_config"] == {"max_likes_per_run": 9}

        # ...and the normalized dict validates into the model with those values.
        account = captured_results[0]
        assert account.target_keywords == ["ai", "ml"]
        assert len(account.competitor_profiles) == 1
        assert account.action_config.max_likes_per_run == 9

    def test_new_keys_win_over_legacy_keys(self, model_validate_spy):
        captured_inputs, captured_results = model_validate_spy
        orch = _bare_orchestrator()
        account_dict = {
            "account_id": "mixed_acc",
            "is_active": False,
            "target_keywords": ["new-style"],
            "target_keywords_override": ["legacy"],
            "action_config": {"max_likes_per_run": 3},
            "action_config_override": {"max_likes_per_run": 99},
        }

        asyncio.run(orch._process_account(account_dict))

        normalized = captured_inputs[0]
        assert normalized["target_keywords"] == ["new-style"]
        assert normalized["action_config"] == {"max_likes_per_run": 3}
        account = captured_results[0]
        assert account.target_keywords == ["new-style"]
        assert account.action_config.max_likes_per_run == 3

    def test_llm_settings_override_passes_through_untouched(self, model_validate_spy):
        captured_inputs, captured_results = model_validate_spy
        orch = _bare_orchestrator()
        override = {"service_preference": "openai", "max_tokens": 99}
        account_dict = {
            "account_id": "llm_acc",
            "is_active": False,
            "llm_settings_override": override,
        }

        asyncio.run(orch._process_account(account_dict))

        # Same field name in the model: the normalizer must not rename or drop it.
        assert captured_inputs[0]["llm_settings_override"] == override
        account = captured_results[0]
        assert account.llm_settings_override.service_preference == "openai"
        assert account.llm_settings_override.max_tokens == 99

    def test_normalizer_does_not_mutate_caller_dict(self, model_validate_spy):
        orch = _bare_orchestrator()
        account_dict = {
            "account_id": "immut_acc",
            "is_active": False,
            "target_keywords_override": ["ai"],
        }

        asyncio.run(orch._process_account(account_dict))

        assert "target_keywords" not in account_dict
        assert account_dict["target_keywords_override"] == ["ai"]

    def test_invalid_account_config_returns_without_raising(self, model_validate_spy):
        captured_inputs, captured_results = model_validate_spy
        orch = _bare_orchestrator()
        # Missing required account_id -> validation fails -> account skipped.
        asyncio.run(orch._process_account({"is_active": False, "target_keywords_override": ["x"]}))

        # Normalization still ran before validation was attempted...
        assert captured_inputs[0]["target_keywords"] == ["x"]
        # ...but no model was produced and no exception escaped.
        assert captured_results == []


class TestActionConfigPrecedence:
    def test_per_account_action_config_wins_over_global(
        self, fake_collaborators, action_config_spy
    ):
        calls, _ = action_config_spy
        orch = _wired_orchestrator(
            {
                "delay_between_accounts_seconds": 0,
                "twitter_automation": {
                    "action_config": dict(ALL_PIPELINES_OFF, max_likes_per_run=7)
                },
            }
        )
        account_dict = {
            "account_id": "acct_a",
            "is_active": True,
            "action_config": dict(ALL_PIPELINES_OFF, max_likes_per_run=42),
        }

        asyncio.run(orch._process_account(account_dict))

        # The global ActionConfig was never constructed: per-account config
        # short-circuited the `or`, so the global max_likes_per_run=7 lost.
        assert calls == []
        # The pipeline ran to completion and cleaned up its (fake) browser.
        fake_collaborators["BrowserManager"].return_value.close_driver.assert_called_once()

    def test_global_action_config_used_when_account_has_none(
        self, fake_collaborators, action_config_spy
    ):
        calls, results = action_config_spy
        global_action_config = dict(ALL_PIPELINES_OFF, max_likes_per_run=7)
        orch = _wired_orchestrator(
            {
                "delay_between_accounts_seconds": 0,
                "twitter_automation": {"action_config": global_action_config},
            }
        )

        asyncio.run(orch._process_account({"account_id": "acct_b", "is_active": True}))

        assert calls == [global_action_config]
        effective = results[0]
        assert effective.max_likes_per_run == 7  # from global settings
        # Model defaults fill whatever the global config does not specify.
        assert effective.min_delay_between_actions_seconds == 60
        assert effective.max_delay_between_actions_seconds == 180

    def test_model_defaults_when_neither_account_nor_global_configured(
        self, fake_collaborators, action_config_spy
    ):
        calls, results = action_config_spy
        orch = _wired_orchestrator(
            {"delay_between_accounts_seconds": 0, "twitter_automation": {}}
        )

        asyncio.run(orch._process_account({"account_id": "acct_c", "is_active": True}))

        assert calls == [{}]
        effective = results[0]
        assert effective.max_likes_per_run == 5
        assert effective.min_delay_between_actions_seconds == 60
        assert effective.max_delay_between_actions_seconds == 180


class TestIsOwnTweet:
    """Pure staticmethod: handle matching against self_handles / logged-in handle / account_id."""

    def test_matches_self_handles_case_and_at_prefix_insensitively(self):
        account = AccountConfig(account_id="acct", self_handles=["myhandle"])
        browser = SimpleNamespace(logged_in_handle=None)
        assert TwitterOrchestrator._is_own_tweet("@MyHandle", account, browser) is True
        assert TwitterOrchestrator._is_own_tweet("myhandle", account, browser) is True

    def test_matches_runtime_detected_logged_in_handle(self):
        account = AccountConfig(account_id="acct")
        browser = SimpleNamespace(logged_in_handle="DetectedHandle")
        assert TwitterOrchestrator._is_own_tweet("@detectedhandle", account, browser) is True

    def test_falls_back_to_account_id(self):
        account = AccountConfig(account_id="solo_handle")
        browser = SimpleNamespace(logged_in_handle=None)
        assert TwitterOrchestrator._is_own_tweet("@Solo_Handle", account, browser) is True

    def test_other_handles_do_not_match(self):
        account = AccountConfig(account_id="acct", self_handles=["myhandle"])
        browser = SimpleNamespace(logged_in_handle="myhandle")
        assert TwitterOrchestrator._is_own_tweet("someone_else", account, browser) is False

    def test_empty_handle_never_matches(self):
        account = AccountConfig(account_id="acct")
        browser = SimpleNamespace()  # no logged_in_handle attribute at all
        assert TwitterOrchestrator._is_own_tweet("", account, browser) is False
        assert TwitterOrchestrator._is_own_tweet(None, account, browser) is False


def test_keyword_retweets_are_deduplicated_across_cycles(fake_collaborators):
    orch = _wired_orchestrator({"delay_between_accounts_seconds": 0})
    tweet = ScrapedTweet(
        tweet_id="123", user_handle="other", text_content="x topic",
        tweet_url="https://x.com/other/status/123",
    )
    fake_collaborators["TweetScraper"].return_value.scrape_tweets_by_keyword.return_value = [tweet]
    fake_collaborators["TweetPublisher"].return_value.retweet_tweet = AsyncMock(return_value=True)
    fake_collaborators["TweetAnalyzer"].return_value.score_relevance = AsyncMock(return_value=1.0)

    action_config = ActionConfig(
        **dict(ALL_PIPELINES_OFF, enable_keyword_retweets=True),
        max_retweets_per_keyword_run=1,
        enable_relevance_filter_likes=False,
        min_delay_between_actions_seconds=0,
        max_delay_between_actions_seconds=0,
    )
    account = {
        "account_id": "acct",
        "is_active": True,
        "target_keywords": ["topic"],
        "action_config": action_config.model_dump(),
    }

    asyncio.run(orch._process_account(account))
    asyncio.run(orch._process_account(account))

    key = "retweet_acct_123"
    assert fake_collaborators["TweetPublisher"].return_value.retweet_tweet.await_count == 1
    assert key in orch.processed_action_keys
    orch.file_handler.save_processed_action_key.assert_called_once()
