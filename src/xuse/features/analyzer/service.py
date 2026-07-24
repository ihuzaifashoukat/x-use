import os
import logging
import sys
from typing import Optional, List

# Adjust import paths to support running as a script
try:
    from ...core.llm_service import LLMService
    from ...core.config_loader import ConfigLoader
    from ...utils.logger import setup_logger
    from ...data_models import ScrapedTweet, LLMSettings, AccountConfig
except ImportError:
    sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    from xuse.core.llm_service import LLMService
    from xuse.core.config_loader import ConfigLoader
    from xuse.utils.logger import setup_logger
    from xuse.models import ScrapedTweet, LLMSettings, AccountConfig

from .prompts import build_thread_prompt, build_relevance_prompt, build_sentiment_prompt
from .schema import structured_analysis_schema
from .heuristics import keyword_relevance_score, heuristic_sentiment


logger = logging.getLogger(__name__)


def _ensure_logging(config_loader: Optional[ConfigLoader] = None) -> None:
    """Configure x-use logging lazily, on first use — never at import time
    (importing this module must not reconfigure the host application's root
    logger or read config), and never over existing root handlers."""
    if logging.getLogger().handlers:
        return
    setup_logger(config_loader)


class TweetAnalyzer:
    def __init__(self, llm_service: LLMService, account_config: Optional[AccountConfig] = None):
        _ensure_logging(llm_service.config_loader)
        self.llm_service = llm_service
        self.account_config = account_config  # For account-specific LLM settings if needed
        self.config_loader = llm_service.config_loader  # Reuse config loader

    def _resolve_llm_settings(self, custom: Optional[LLMSettings], default_for: str = "thread") -> LLMSettings:
        if custom:
            return custom
        if self.account_config and self.account_config.action_config:
            if default_for == "thread":
                s = self.account_config.action_config.llm_settings_for_thread_analysis
                if s:
                    return s
        # Fallbacks from global
        global_action_config = self.config_loader.get_twitter_automation_setting("action_config", {})
        if global_action_config:
            if default_for == "thread":
                d = global_action_config.get("llm_settings_for_thread_analysis", {})
                return LLMSettings(**d) if d else LLMSettings()
        # Absolute fallback
        return LLMSettings(max_tokens=70, temperature=0.2, service_preference="gemini")

    def _account_keywords(self) -> List[str]:
        return list(self.account_config.target_keywords or []) if self.account_config and self.account_config.target_keywords else []

    async def check_if_thread_with_llm(self, tweet: ScrapedTweet, custom_llm_settings: Optional[LLMSettings] = None) -> bool:
        if not tweet or not tweet.text_content:
            return False

        llm_settings_to_use = self._resolve_llm_settings(custom_llm_settings, default_for="thread")
        prompt = build_thread_prompt(tweet.text_content)

        logger.debug(f"Thread analysis prompt for tweet {tweet.tweet_id}:\n{prompt}")
        logger.info(
            "Using LLM settings for thread analysis: Service=%s, Model=%s",
            llm_settings_to_use.service_preference,
            llm_settings_to_use.model_name_override or "default",
        )

        response_text = await self.llm_service.generate_text(
            prompt=prompt,
            service_preference=llm_settings_to_use.service_preference,
            model_name=llm_settings_to_use.model_name_override,
            max_tokens=llm_settings_to_use.max_tokens,
            temperature=llm_settings_to_use.temperature,
        )

        if response_text:
            t = response_text.strip().lower()
            logger.debug("LLM response for thread analysis of tweet %s: '%s'", tweet.tweet_id, t)
            if t == "true":
                return True
            if t == "false":
                return False
            logger.warning(
                "LLM for thread analysis (tweet %s) returned non-boolean: '%s'. Assuming not a thread.",
                tweet.tweet_id,
                t,
            )
            return False

        logger.warning(
            "LLM did not return a response for thread analysis of tweet %s. Assuming not a thread.",
            tweet.tweet_id,
        )
        return False

    async def analyze_tweet_structured(
        self,
        tweet: ScrapedTweet,
        keywords: Optional[list] = None,
        custom_llm_settings: Optional[LLMSettings] = None,
    ) -> Optional[dict]:
        if not tweet or not tweet.text_content:
            return None
        llm = self.llm_service
        if not llm:
            return None

        schema = structured_analysis_schema()
        kws = ", ".join((keywords or self._account_keywords()) or [])
        task = (
            "Analyze the tweet for relevance to the given keywords, sentiment, and recommend an action for engagement.\n"
            f"Tweet: {tweet.text_content}\nKeywords: {kws}"
        )
        llm_settings_to_use = custom_llm_settings or (
            self.account_config.llm_settings_override if self.account_config else None
        )
        try:
            data, err = await llm.generate_structured(
                task_instruction=task,
                schema=schema,
                service_preference=(llm_settings_to_use.service_preference if llm_settings_to_use else None),
                model_name=(llm_settings_to_use.model_name_override if llm_settings_to_use else None),
                max_tokens=(llm_settings_to_use.max_tokens if llm_settings_to_use else 120),
                temperature=0.2,
            )
            if data:
                return data
            logger.debug("Structured analysis failed: %s", err)
            return None
        except Exception as e:
            logger.warning("Structured analysis error: %s", e)
            return None

    async def score_relevance(
        self,
        tweet: ScrapedTweet,
        keywords: Optional[list] = None,
        custom_llm_settings: Optional[LLMSettings] = None,
    ) -> float:
        if not tweet or not tweet.text_content:
            return 0.0
        kws = list((keywords or self._account_keywords()) or [])
        base = keyword_relevance_score(tweet.text_content, kws)

        llm_settings_to_use = custom_llm_settings or (
            self.account_config.llm_settings_override if self.account_config else None
        )
        if self.llm_service and llm_settings_to_use and kws:
            prompt = build_relevance_prompt(tweet.text_content, [k.lower() for k in kws])
            ans = await self.llm_service.generate_text(
                prompt=prompt,
                service_preference=llm_settings_to_use.service_preference,
                model_name=llm_settings_to_use.model_name_override,
                max_tokens=8,
                temperature=0.0,
            )
            try:
                if ans:
                    v = float(ans.strip())
                    if 0.0 <= v <= 1.0:
                        return v
            except Exception:
                pass
        return base

    async def classify_sentiment(
        self,
        tweet: ScrapedTweet,
        custom_llm_settings: Optional[LLMSettings] = None,
    ) -> str:
        if not tweet or not tweet.text_content:
            return "neutral"
        base = heuristic_sentiment(tweet.text_content)

        llm_settings_to_use = custom_llm_settings or (
            self.account_config.llm_settings_override if self.account_config else None
        )
        if self.llm_service and llm_settings_to_use:
            prompt = build_sentiment_prompt(tweet.text_content)
            ans = await self.llm_service.generate_text(
                prompt=prompt,
                service_preference=llm_settings_to_use.service_preference,
                model_name=llm_settings_to_use.model_name_override,
                max_tokens=3,
                temperature=0.0,
            )
            if ans:
                a = ans.strip().lower()
                if a in ("positive", "neutral", "negative"):
                    return a
        return base


if __name__ == "__main__":
    import asyncio

    async def test_analyzer():
        cfg_loader = ConfigLoader()
        llm = LLMService(config_loader=cfg_loader)
        analyzer = TweetAnalyzer(llm_service=llm)

        from xuse.models import ScrapedTweet, LLMSettings  # type: ignore

        tweet1_standalone = ScrapedTweet(
            tweet_id="s1", text_content="Just enjoyed a great cup of coffee! #morning"
        )
        tweet2_thread_indicator_text = ScrapedTweet(
            tweet_id="t1",
            text_content="My thoughts on the new AI model (1/5): It's truly groundbreaking...",
        )
        tweet3_thread_emoji = ScrapedTweet(
            tweet_id="t2", text_content="Let's dive deep into this topic. 🧵 First point..."
        )
        tweet4_ambiguous = ScrapedTweet(
            tweet_id="a1",
            text_content="This is an interesting development that deserves more discussion.",
        )

        tweets_to_test = [
            tweet1_standalone,
            tweet2_thread_indicator_text,
            tweet3_thread_emoji,
            tweet4_ambiguous,
        ]

        test_llm_settings = LLMSettings(
            service_preference="gemini", max_tokens=10, temperature=0.1
        )

        for i, tweet_obj in enumerate(tweets_to_test):
            logger.info("\n--- Analyzing tweet %s (ID: %s) ---", i + 1, tweet_obj.tweet_id)
            logger.info("Text: %s", tweet_obj.text_content)
            is_thread = await analyzer.check_if_thread_with_llm(
                tweet_obj, custom_llm_settings=test_llm_settings
            )
            logger.info("LLM determined as thread: %s", is_thread)
            tweet_obj.is_confirmed_thread = is_thread
            logger.info(
                "Updated tweet object: %s",
                tweet_obj.model_dump_json(indent=2, exclude_none=True),
            )

    logger.info(
        "To run the analyzer test, execute: python -m src.features.analyzer.service"
    )

