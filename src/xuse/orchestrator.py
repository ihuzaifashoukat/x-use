import asyncio
import logging
import sys
import os
import time
import random
from datetime import datetime, timezone

# Ensure src directory is in Python path for imports
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from xuse.core.config_loader import ConfigLoader
from xuse.core.browser_manager import BrowserManager
from xuse.core.llm_service import LLMService
from xuse.utils.logger import setup_logger
from xuse.utils.file_handler import FileHandler
from xuse.models import AccountConfig, TweetContent, LLMSettings, ScrapedTweet, ActionConfig
from xuse.features.scraper import TweetScraper
from xuse.features.publisher import TweetPublisher
from xuse.features.engagement import TweetEngagement
from xuse.features.analyzer import TweetAnalyzer
from xuse.utils.metrics import MetricsRecorder

# Initialize main config loader and logger
main_config_loader = ConfigLoader()
# Configure logging and get module logger
setup_logger(main_config_loader)
logger = logging.getLogger(__name__)

class TwitterOrchestrator:
    def __init__(self):
        self.config_loader = main_config_loader
        self.file_handler = FileHandler(self.config_loader)
        self.global_settings = self.config_loader.get_settings()
        self.accounts_data = self.config_loader.get_accounts_config()
        
        self.processed_action_keys = self.file_handler.load_processed_action_keys() # Load processed action keys
        # Analysis and decision config snapshots
        ta = self.global_settings.get('twitter_automation', {})
        self.analysis_config = ta.get('analysis_config', {})
        self.engagement_decision_cfg = ta.get('engagement_decision', {"enabled": False})

    @staticmethod
    def _is_own_tweet(user_handle: str, account: AccountConfig, browser_manager: BrowserManager) -> bool:
        """Return True if the given user_handle belongs to the current account.

        Compares against:
        - configured account.self_handles (if provided)
        - browser_manager.logged_in_handle (if detected)
        - account.account_id (as last-resort heuristic)
        """
        try:
            handle = (user_handle or "").strip().lstrip('@').lower()
            candidates = set()
            # Explicit self handles from config
            for h in (account.self_handles or []):
                if isinstance(h, str) and h.strip():
                    candidates.add(h.strip().lstrip('@').lower())
            # Runtime-detected handle from UI
            if getattr(browser_manager, 'logged_in_handle', None):
                candidates.add(str(browser_manager.logged_in_handle).strip().lstrip('@').lower())
            # Heuristic fallback: account_id (in case user set it to the handle)
            if isinstance(account.account_id, str) and account.account_id.strip():
                candidates.add(account.account_id.strip().lstrip('@').lower())
            return handle in candidates and handle != ""
        except Exception:
            return False

    async def _decide_competitor_action(self, analyzer: TweetAnalyzer, tweet: ScrapedTweet, account: AccountConfig) -> str:
        """Return one of: 'repost', 'retweet', 'quote_tweet', 'like' based on relevance and sentiment, honoring per-account overrides and thresholds."""
        acc_ac = account.action_config
        decision_enabled = (acc_ac.enable_engagement_decision if (acc_ac and acc_ac.enable_engagement_decision is not None)
                            else self.engagement_decision_cfg.get('enabled', False))
        use_sentiment = (acc_ac.use_sentiment_in_decision if (acc_ac and acc_ac.use_sentiment_in_decision is not None)
                         else self.engagement_decision_cfg.get('use_sentiment', True))
        # Thresholds: per-account overrides > global cfg > defaults
        ed_thr = (self.engagement_decision_cfg.get('thresholds') or {}) if isinstance(self.engagement_decision_cfg, dict) else {}
        quote_min = (acc_ac.decision_quote_min if (acc_ac and acc_ac.decision_quote_min is not None) else float(ed_thr.get('quote_min', 0.75)))
        retweet_min = (acc_ac.decision_retweet_min if (acc_ac and acc_ac.decision_retweet_min is not None) else float(ed_thr.get('retweet_min', 0.5)))
        repost_min = (acc_ac.decision_repost_min if (acc_ac and acc_ac.decision_repost_min is not None) else float(ed_thr.get('repost_min', 0.35)))
        if not decision_enabled:
            return (acc_ac.competitor_post_interaction_type if acc_ac else 'repost')
        # Try structured analysis first
        structured = await analyzer.analyze_tweet_structured(tweet, keywords=account.target_keywords)
        if structured and isinstance(structured, dict) and 'recommended_action' in structured:
            rel = float(structured.get('relevance', 0.0) or 0.0)
            sentiment = str(structured.get('sentiment', 'neutral') or 'neutral').lower()
            rec = str(structured.get('recommended_action')).lower()
            # Apply minimal guardrails based on thresholds
            if rel < repost_min:
                return 'like'
            if rec in ('quote_tweet', 'retweet', 'repost', 'like'):
                return rec
        # Fallback: Compute relevance and optionally sentiment
        rel = await analyzer.score_relevance(tweet, keywords=account.target_keywords)
        sentiment = 'neutral'
        if use_sentiment:
            try:
                sentiment = await analyzer.classify_sentiment(tweet)
            except Exception:
                sentiment = 'neutral'
        # Simple heuristic mapping
        if rel >= quote_min and sentiment in ('positive', 'neutral'):
            return 'quote_tweet'
        if rel >= retweet_min and sentiment in ('positive', 'neutral'):
            return 'retweet'
        if rel >= repost_min:
            return 'repost'
        return 'like'

    async def _process_account(self, account_dict: dict):
        """Processes tasks for a single Twitter account."""
        
        # Normalize legacy override keys to current AccountConfig fields
        def _normalize_account_config(d: dict) -> dict:
            normalized = dict(d)  # shallow copy
            # Map legacy override fields if the new ones are missing
            if 'target_keywords' not in normalized and 'target_keywords_override' in normalized:
                normalized['target_keywords'] = normalized.get('target_keywords_override')
            if 'competitor_profiles' not in normalized and 'competitor_profiles_override' in normalized:
                normalized['competitor_profiles'] = normalized.get('competitor_profiles_override')
            if 'news_sites' not in normalized and 'news_sites_override' in normalized:
                normalized['news_sites'] = normalized.get('news_sites_override')
            if 'research_paper_sites' not in normalized and 'research_paper_sites_override' in normalized:
                normalized['research_paper_sites'] = normalized.get('research_paper_sites_override')
            if 'action_config' not in normalized and 'action_config_override' in normalized:
                normalized['action_config'] = normalized.get('action_config_override')
            # Keep llm_settings_override as-is (same field name in model)
            return normalized

        account_dict = _normalize_account_config(account_dict)

        # Create AccountConfig Pydantic model from the dictionary
        try:
            # A simple way to map, assuming keys in dict match model fields or are handled by default values
            # account_config_data = {k: account_dict.get(k) for k in AccountConfig.model_fields.keys() if account_dict.get(k) is not None}
            # if 'cookies' in account_dict and isinstance(account_dict['cookies'], str): # If 'cookies' is a file path string
            #     account_config_data['cookie_file_path'] = account_dict['cookies']
            #     if 'cookies' in account_config_data: del account_config_data['cookies'] # Avoid conflict if model expects List[AccountCookie]
            
            # Use Pydantic's parse_obj method for robust parsing from dict
            account = AccountConfig.model_validate(account_dict) # Replaced AccountConfig(**account_dict) for better validation
            
        except Exception as e: # Catch Pydantic ValidationError specifically if needed
            logger.error(f"Failed to parse account configuration for {account_dict.get('account_id', 'UnknownAccount')}: {e}. Skipping account.")
            return

        if not account.is_active:
            logger.info(f"Account {account.account_id} is inactive. Skipping.")
            return

        logger.info(f"--- Starting processing for account: {account.account_id} ---")
        
        browser_manager = None
        try:
            browser_manager = BrowserManager(account_config=account_dict) # Pass original dict for cookie path handling
            llm_service = LLMService(config_loader=self.config_loader)
            
            # Initialize feature modules with the current account's context
            scraper = TweetScraper(browser_manager, account_id=account.account_id)
            publisher = TweetPublisher(browser_manager, llm_service, account) # Publisher needs AccountConfig model
            engagement = TweetEngagement(browser_manager, account) # Engagement needs AccountConfig model
            metrics = MetricsRecorder(account_id=account.account_id, config_loader=self.config_loader)
            metrics.mark_run_start()

            # --- Define actions based on global and account-specific settings ---
            automation_settings = self.global_settings.get('twitter_automation', {}) # Global settings for twitter_automation
            
            # Determine current ActionConfig: account's action_config > global default action_config
            global_action_config_dict = automation_settings.get('action_config', {}) # Global default action_config
            current_action_config = account.action_config or ActionConfig(**global_action_config_dict) # account.action_config is now the primary source if it exists

            # Initialize TweetAnalyzer
            analyzer = TweetAnalyzer(llm_service, account_config=account)

            # Determine LLM settings for different actions:
            # Priority: Account's general LLM override -> Action-specific LLM settings from current_action_config
            llm_for_post = account.llm_settings_override or current_action_config.llm_settings_for_post
            llm_for_reply = account.llm_settings_override or current_action_config.llm_settings_for_reply
            llm_for_thread_analysis = account.llm_settings_override or current_action_config.llm_settings_for_thread_analysis
            
            # Action 1: Scrape competitor profiles and generate/post new tweets
            # Content sources are now directly from the account config, defaulting to empty lists if not provided.
            competitor_profiles_for_account = account.competitor_profiles
            
            if current_action_config.enable_competitor_reposts and competitor_profiles_for_account:
                logger.info(f"[{account.account_id}] Starting competitor profile scraping and posting using {len(competitor_profiles_for_account)} profiles.")
                for profile_url in competitor_profiles_for_account:
                    logger.info(f"[{account.account_id}] Scraping profile: {str(profile_url)}")
                    
                    tweets_from_profile = await asyncio.to_thread(
                        scraper.scrape_tweets_from_profile, 
                        str(profile_url), 
                        max_tweets=current_action_config.max_posts_per_competitor_run * 3
                    )
                    
                    posts_made_this_profile = 0
                    for scraped_tweet in tweets_from_profile:
                        if posts_made_this_profile >= current_action_config.max_posts_per_competitor_run:
                            break
                        # Optional relevance filter (settings-driven)
                        try:
                            acc_ac = account.action_config
                            enable_rel = (acc_ac.enable_relevance_filter_competitor_reposts if (acc_ac and acc_ac.enable_relevance_filter_competitor_reposts is not None)
                                          else self.analysis_config.get('enable_relevance_filter', {}).get('competitor_reposts', True))
                            thr = (acc_ac.relevance_threshold_competitor_reposts if (acc_ac and acc_ac.relevance_threshold_competitor_reposts is not None)
                                   else float(self.analysis_config.get('thresholds', {}).get('competitor_reposts_min', 0.35)))
                            if enable_rel:
                                rel_score = await analyzer.score_relevance(scraped_tweet, keywords=account.target_keywords)
                                if rel_score < thr:
                                    logger.debug(f"[{account.account_id}] Skipping tweet {scraped_tweet.tweet_id} (rel {rel_score:.2f} < {thr}).")
                                    continue
                        except Exception:
                            pass
                        
                        if current_action_config.repost_only_tweets_with_media and not scraped_tweet.embedded_media_urls:
                            logger.debug(f"[{account.account_id}] Skipping tweet {scraped_tweet.tweet_id} (no media).")
                            continue
                        if scraped_tweet.like_count < current_action_config.min_likes_for_repost_candidate:
                            logger.debug(f"[{account.account_id}] Skipping tweet {scraped_tweet.tweet_id} (likes {scraped_tweet.like_count} < min).")
                            continue
                        if scraped_tweet.retweet_count < current_action_config.min_retweets_for_repost_candidate:
                            logger.debug(f"[{account.account_id}] Skipping tweet {scraped_tweet.tweet_id} (retweets {scraped_tweet.retweet_count} < min).")
                            continue

                        interaction_type = await self._decide_competitor_action(analyzer, scraped_tweet, account)
                        action_key = f"{interaction_type}_{account.account_id}_{scraped_tweet.tweet_id}"
                            
                        if action_key in self.processed_action_keys:
                            logger.info(f"[{account.account_id}] Action '{action_key}' already processed. Skipping.")
                            continue

                        if scraped_tweet.is_thread_candidate and current_action_config.enable_thread_analysis:
                            logger.info(f"[{account.account_id}] Analyzing thread candidacy for tweet {scraped_tweet.tweet_id}...")
                            is_confirmed = await analyzer.check_if_thread_with_llm(scraped_tweet, custom_llm_settings=llm_for_thread_analysis)
                            scraped_tweet.is_confirmed_thread = is_confirmed
                            logger.info(f"[{account.account_id}] Thread analysis result for {scraped_tweet.tweet_id}: {is_confirmed}")

                        interaction_success = False

                        if interaction_type == "like":
                            logger.info(f"[{account.account_id}] Decided to like tweet {scraped_tweet.tweet_id}.")
                            interaction_success = await engagement.like_tweet(tweet_id=scraped_tweet.tweet_id, tweet_url=str(scraped_tweet.tweet_url) if scraped_tweet.tweet_url else None)
                            if interaction_success:
                                metrics.increment('likes')
                                metrics.log_event('like', 'success', {'source': 'competitor', 'tweet_id': scraped_tweet.tweet_id})
                            else:
                                metrics.increment('errors')
                                metrics.log_event('like', 'failure', {'source': 'competitor', 'tweet_id': scraped_tweet.tweet_id})
                        elif interaction_type == "repost":
                            prompt = f"Rewrite this tweet in an engaging way: '{scraped_tweet.text_content}' by {scraped_tweet.user_handle or 'a user'}."
                            if scraped_tweet.is_confirmed_thread:
                                prompt = f"This tweet is part of a thread. Rewrite its essence engagingly: '{scraped_tweet.text_content}' by {scraped_tweet.user_handle or 'a user'}."
                            new_tweet_content = TweetContent(text=prompt)
                            logger.info(f"[{account.account_id}] Generating and posting new tweet based on {scraped_tweet.tweet_id}")
                            interaction_success = await publisher.post_new_tweet(new_tweet_content, llm_settings=llm_for_post)
                            metrics.log_event('post', 'success' if interaction_success else 'failure', {'source': 'competitor', 'tweet_id': scraped_tweet.tweet_id})
                            if interaction_success:
                                metrics.increment('posts')
                        
                        elif interaction_type == "retweet":
                            logger.info(f"[{account.account_id}] Attempting to retweet {scraped_tweet.tweet_id}")
                            interaction_success = await publisher.retweet_tweet(scraped_tweet)
                            metrics.log_event('retweet', 'success' if interaction_success else 'failure', {'tweet_id': scraped_tweet.tweet_id})
                            if interaction_success:
                                metrics.increment('retweets')
                        
                        elif interaction_type == "quote_tweet":
                            quote_prompt_template = current_action_config.prompt_for_quote_tweet_from_competitor
                            quote_prompt = quote_prompt_template.format(
                                user_handle=(scraped_tweet.user_handle or "a user"), 
                                tweet_text=scraped_tweet.text_content
                            )
                            logger.info(f"[{account.account_id}] Attempting to quote tweet {scraped_tweet.tweet_id} with generated text.")
                            # LLM settings for quote tweets could be distinct if added to ActionConfig, for now using llm_for_post
                            interaction_success = await publisher.retweet_tweet(scraped_tweet, 
                                                                                quote_text_prompt_or_direct=quote_prompt, 
                                                                                llm_settings_for_quote=llm_for_post)
                            metrics.log_event('quote_tweet', 'success' if interaction_success else 'failure', {'tweet_id': scraped_tweet.tweet_id})
                            if interaction_success:
                                metrics.increment('quote_tweets')
                        else:
                            logger.warning(f"[{account.account_id}] Unknown competitor_post_interaction_type: {interaction_type}")
                            continue

                        if interaction_success:
                            self.file_handler.save_processed_action_key(action_key, timestamp=datetime.now().isoformat())
                            self.processed_action_keys.add(action_key) # Add to in-memory set for current run
                            if interaction_type != 'like':
                                posts_made_this_profile += 1
                            await asyncio.sleep(random.uniform(current_action_config.min_delay_between_actions_seconds, current_action_config.max_delay_between_actions_seconds))
                        else:
                            logger.error(f"[{account.account_id}] Failed to {interaction_type} based on tweet {scraped_tweet.tweet_id}")
                            metrics.increment('errors')
            
            elif current_action_config.enable_competitor_reposts:
                 logger.info(f"[{account.account_id}] Competitor reposts enabled, but no competitor profiles configured for this account.")

            # Action 1.5: Engage with posts inside a configured Community
            if (
                current_action_config.enable_community_engagement
                and account.community_id
            ):
                try:
                    community_url = f"https://x.com/i/communities/{account.community_id}"
                    logger.info(f"[{account.account_id}] Scraping community timeline: {community_url}")
                    # Scrape more than we plan to act on so we can filter
                    to_scrape = max(10, current_action_config.max_community_engagements_per_run * 4)
                    community_tweets = await asyncio.to_thread(
                        scraper.scrape_tweets_from_url,
                        community_url,
                        "community",
                        to_scrape,
                    )

                    total_community_actions = 0
                    replies_made_in_community = 0

                    for ct in community_tweets:
                        if total_community_actions >= current_action_config.max_community_engagements_per_run:
                            break

                        # Avoid re-processing the same tweet for the same action
                        # We'll create an action key per decided action below.

                        # Decide how to engage (reuse competitor decision logic + thresholds)
                        # Skip own posts entirely in the community
                        if ct.user_handle and self._is_own_tweet(ct.user_handle, account, browser_manager):
                            logger.info(f"[{account.account_id}] Skipping own community post {ct.tweet_id} ({ct.user_handle}).")
                            try:
                                skip_key = f"skip_own_{account.account_id}_{ct.tweet_id}"
                                self.file_handler.save_processed_action_key(skip_key, timestamp=datetime.now().isoformat())
                                self.processed_action_keys.add(skip_key)
                            except Exception:
                                pass
                            continue

                        decided_action = await self._decide_competitor_action(analyzer, ct, account)
                        # Map 'repost' to 'retweet' for community (we do not synthesize new standalone posts here)
                        if decided_action == 'repost' or decided_action == 'quote_tweet':
                            # For community, collapse to a simple retweet action
                            decided_action = 'retweet'

                        interaction_success = False

                        if decided_action == 'like' and current_action_config.enable_community_likes:
                            action_key = f"community_like_{account.account_id}_{ct.tweet_id}"
                            if action_key in self.processed_action_keys:
                                continue
                            logger.info(f"[{account.account_id}] Liking community post {ct.tweet_id}")
                            interaction_success = await engagement.like_tweet(ct.tweet_id, ct.tweet_url)
                            metrics.log_event('community_like', 'success' if interaction_success else 'failure', {'tweet_id': ct.tweet_id})
                            if interaction_success:
                                metrics.increment('likes')
                                self.file_handler.save_processed_action_key(action_key, timestamp=datetime.now().isoformat())
                                self.processed_action_keys.add(action_key)
                                total_community_actions += 1

                        elif decided_action == "retweet" and current_action_config.enable_community_retweets:
                            action_key = f"community_{decided_action}_{account.account_id}_{ct.tweet_id}"
                            if action_key in self.processed_action_keys:
                                continue
                            logger.info(f"[{account.account_id}] Retweeting community post {ct.tweet_id}")
                            interaction_success = await publisher.retweet_tweet(ct, quote_text_prompt_or_direct=None)
                            metrics.log_event('community_retweet', 'success' if interaction_success else 'failure', {'tweet_id': ct.tweet_id})
                            if interaction_success:
                                metrics.increment('retweets')

                            if interaction_success:
                                self.file_handler.save_processed_action_key(action_key, timestamp=datetime.now().isoformat())
                                self.processed_action_keys.add(action_key)
                                total_community_actions += 1

                        # Optional: Replies to community posts (independent gate and cap)
                        if (
                            current_action_config.enable_community_replies
                            and replies_made_in_community < current_action_config.max_community_replies_per_run
                        ):
                            reply_key = f"community_reply_{account.account_id}_{ct.tweet_id}"
                            if reply_key in self.processed_action_keys:
                                # Already replied to this post in past runs
                                pass
                            else:
                                # Respect recency window if configured
                                try:
                                    hours_limit = current_action_config.community_reply_only_recent_tweets_hours
                                    if hours_limit and ct.created_at:
                                        now_utc = datetime.now(timezone.utc)
                                        age_hours = (now_utc - ct.created_at).total_seconds() / 3600
                                        if age_hours > hours_limit:
                                            logger.debug(f"[{account.account_id}] Skipping community reply for {ct.tweet_id}, age {age_hours:.1f}h > {hours_limit}h")
                                            raise Exception("skip_reply_due_to_age")
                                except Exception:
                                    # Skip on explicit age failure; do not treat as error
                                    pass
                                else:
                                    # Do not reply to own posts
                                    if ct.user_handle and self._is_own_tweet(ct.user_handle, account, browser_manager):
                                        logger.info(f"[{account.account_id}] Skipping own community post {ct.tweet_id} for reply.")
                                        try:
                                            skip_key = f"skip_own_{account.account_id}_{ct.tweet_id}"
                                            self.file_handler.save_processed_action_key(skip_key, timestamp=datetime.now().isoformat())
                                            self.processed_action_keys.add(skip_key)
                                        except Exception:
                                            pass
                                        continue
                                    # Generate a concise reply
                                    reply_prompt = (
                                        f"Write a concise, natural reply under 270 characters. Avoid hashtags, links, emojis.\n\n"
                                        f"Original post by @{ct.user_handle or 'user'}:\n\"{ct.text_content}\"\n\nYour reply:"
                                    )
                                    logger.info(f"[{account.account_id}] Replying to community post {ct.tweet_id}")
                                    generated_reply_text = await llm_service.generate_text(
                                        prompt=reply_prompt,
                                        service_preference=llm_for_reply.service_preference,
                                        model_name=llm_for_reply.model_name_override,
                                        max_tokens=llm_for_reply.max_tokens,
                                        temperature=llm_for_reply.temperature,
                                    )
                                    generated_reply_text = (generated_reply_text or "")[:270].rstrip()
                                    if generated_reply_text:
                                        reply_success = await publisher.reply_to_tweet(ct, generated_reply_text)
                                        metrics.log_event('community_reply', 'success' if reply_success else 'failure', {'tweet_id': ct.tweet_id})
                                        if reply_success:
                                            metrics.increment('replies')
                                            self.file_handler.save_processed_action_key(reply_key, timestamp=datetime.now().isoformat())
                                            self.processed_action_keys.add(reply_key)
                                            replies_made_in_community += 1
                                            total_community_actions += 1

                        # Backoff between actions to be human-like
                        if total_community_actions >= current_action_config.max_community_engagements_per_run:
                            break
                        await asyncio.sleep(random.uniform(current_action_config.min_delay_between_actions_seconds, current_action_config.max_delay_between_actions_seconds))

                except Exception as e:
                    logger.error(f"[{account.account_id}] Failed during community engagement: {e}", exc_info=True)
                    metrics.increment('errors')

            # Action 2: Scrape keywords and reply
            target_keywords_for_account = account.target_keywords
            if current_action_config.enable_keyword_replies and target_keywords_for_account:
                logger.info(f"[{account.account_id}] Starting keyword scraping and replying for {len(target_keywords_for_account)} keywords.")
                for keyword in target_keywords_for_account:
                    logger.info(f"[{account.account_id}] Processing keyword for replies: '{keyword}'")
                    # Scrape tweets for the keyword
                    tweets_for_keyword = await asyncio.to_thread(
                        scraper.scrape_tweets_by_keyword,
                        keyword,
                        max_tweets=current_action_config.max_replies_per_keyword_run * 2 # Get more to filter
                    )
                    
                    replies_made_this_keyword = 0
                    for scraped_tweet_to_reply in tweets_for_keyword:
                        if replies_made_this_keyword >= current_action_config.max_replies_per_keyword_run:
                            break

                        action_key = f"reply_{account.account_id}_{scraped_tweet_to_reply.tweet_id}"
                        if action_key in self.processed_action_keys:
                            logger.info(f"[{account.account_id}] Already replied or processed tweet {scraped_tweet_to_reply.tweet_id}. Skipping.")
                            continue
                        
                        if current_action_config.avoid_replying_to_own_tweets and scraped_tweet_to_reply.user_handle and self._is_own_tweet(scraped_tweet_to_reply.user_handle, account, browser_manager):
                            logger.info(f"[{account.account_id}] Skipping own tweet {scraped_tweet_to_reply.tweet_id} for reply.")
                            continue

                        if current_action_config.reply_only_to_recent_tweets_hours and scraped_tweet_to_reply.created_at:
                            now_utc = datetime.now(timezone.utc)
                            tweet_age_hours = (now_utc - scraped_tweet_to_reply.created_at).total_seconds() / 3600
                            if tweet_age_hours > current_action_config.reply_only_to_recent_tweets_hours:
                                logger.info(f"[{account.account_id}] Skipping old tweet {scraped_tweet_to_reply.tweet_id} (age: {tweet_age_hours:.1f}h > limit: {current_action_config.reply_only_to_recent_tweets_hours}h).")
                                continue
                        
                        # Thread Analysis for context before replying (optional, could make reply more relevant)
                        if scraped_tweet_to_reply.is_thread_candidate and current_action_config.enable_thread_analysis:
                            logger.info(f"[{account.account_id}] Analyzing thread candidacy for reply target tweet {scraped_tweet_to_reply.tweet_id}...")
                            is_confirmed = await analyzer.check_if_thread_with_llm(scraped_tweet_to_reply, custom_llm_settings=llm_for_thread_analysis)
                            scraped_tweet_to_reply.is_confirmed_thread = is_confirmed
                            logger.info(f"[{account.account_id}] Thread analysis for reply target {scraped_tweet_to_reply.tweet_id}: {is_confirmed}")

                        # Generate reply text (explicitly constrain length and style)
                        reply_prompt_context = (
                            "This tweet is part of a thread." if scraped_tweet_to_reply.is_confirmed_thread else "This is a standalone tweet."
                        )
                        reply_prompt = (
                            f"Write a concise, natural reply under 270 characters. {reply_prompt_context} "
                            f"Avoid hashtags, links, and emojis unless essential. One short paragraph.\n\n"
                            f"Original tweet by @{scraped_tweet_to_reply.user_handle or 'user'}:\n"
                            f"\"{scraped_tweet_to_reply.text_content}\"\n\nYour reply:"
                        )
                        
                        logger.info(f"[{account.account_id}] Generating reply for tweet {scraped_tweet_to_reply.tweet_id}...")
                        generated_reply_text = await llm_service.generate_text(
                            prompt=reply_prompt,
                            service_preference=llm_for_reply.service_preference,
                            model_name=llm_for_reply.model_name_override,
                            max_tokens=llm_for_reply.max_tokens,
                            temperature=llm_for_reply.temperature
                        )

                        if not generated_reply_text:
                            logger.error(f"[{account.account_id}] Failed to generate reply text for tweet {scraped_tweet_to_reply.tweet_id}. Skipping.")
                            continue
                        # Hard-cap reply length to 270 characters
                        generated_reply_text = (generated_reply_text or "")[:270].rstrip()
                        
                        # Optional relevance filter for keyword replies
                        try:
                            acc_ac = account.action_config
                            enable_rel_reply = (acc_ac.enable_relevance_filter_keyword_replies if (acc_ac and acc_ac.enable_relevance_filter_keyword_replies is not None)
                                                else self.analysis_config.get('enable_relevance_filter', {}).get('keyword_replies', False))
                            thr_reply = (acc_ac.relevance_threshold_keyword_replies if (acc_ac and acc_ac.relevance_threshold_keyword_replies is not None)
                                         else float(self.analysis_config.get('thresholds', {}).get('keyword_replies_min', 0.35)))
                            if enable_rel_reply:
                                rel_reply = await analyzer.score_relevance(scraped_tweet_to_reply, keywords=account.target_keywords)
                                if rel_reply < thr_reply:
                                    logger.debug(f"[{account.account_id}] Skipping reply to {scraped_tweet_to_reply.tweet_id} (rel {rel_reply:.2f} < {thr_reply}).")
                                    continue
                        except Exception:
                            pass

                        logger.info(f"[{account.account_id}] Attempting to post reply to tweet {scraped_tweet_to_reply.tweet_id}...")
                        reply_success = await publisher.reply_to_tweet(scraped_tweet_to_reply, generated_reply_text)
                        metrics.log_event('reply', 'success' if reply_success else 'failure', {'tweet_id': scraped_tweet_to_reply.tweet_id})
                        if reply_success:
                            metrics.increment('replies')
                        else:
                            metrics.increment('errors')

                        if reply_success:
                            self.file_handler.save_processed_action_key(action_key, timestamp=datetime.now().isoformat())
                            self.processed_action_keys.add(action_key)
                            replies_made_this_keyword += 1
                            await asyncio.sleep(random.uniform(current_action_config.min_delay_between_actions_seconds, current_action_config.max_delay_between_actions_seconds))
                        else:
                            logger.error(f"[{account.account_id}] Failed to post reply to tweet {scraped_tweet_to_reply.tweet_id}.")
                            # Optionally, add to a temporary blocklist for this session to avoid retrying immediately
                    logger.info(f"[{account.account_id}] Finished processing keyword '{keyword}' for replies.")
            elif current_action_config.enable_keyword_replies:
                logger.info(f"[{account.account_id}] Keyword replies enabled, but no target keywords configured for this account.")

            # Action 2b: Retweet tweets from keywords
            if getattr(current_action_config, 'enable_keyword_retweets', False) and target_keywords_for_account:
                logger.info(f"[{account.account_id}] Starting keyword-based retweets for {len(target_keywords_for_account)} keywords.")
                for keyword in target_keywords_for_account:
                    logger.info(f"[{account.account_id}] Processing keyword for retweets: '{keyword}'")
                    tweets_for_keyword = await asyncio.to_thread(
                        scraper.scrape_tweets_by_keyword,
                        keyword,
                        max_tweets=max(5, current_action_config.max_retweets_per_keyword_run * 3)
                    )
                    retweets_made = 0
                    for tweet_candidate in tweets_for_keyword:
                        if retweets_made >= current_action_config.max_retweets_per_keyword_run:
                            break
                        # Optional relevance filter: reuse likes filter settings if available
                        try:
                            acc_ac = account.action_config
                            enable_rel_like = (acc_ac.enable_relevance_filter_likes if (acc_ac and acc_ac.enable_relevance_filter_likes is not None)
                                               else self.analysis_config.get('enable_relevance_filter', {}).get('likes', True))
                            thr_like = (acc_ac.relevance_threshold_likes if (acc_ac and acc_ac.relevance_threshold_likes is not None)
                                        else float(self.analysis_config.get('thresholds', {}).get('likes_min', 0.3)))
                            if enable_rel_like:
                                rel_like = await analyzer.score_relevance(tweet_candidate, keywords=account.target_keywords)
                                if rel_like < thr_like:
                                    continue
                        except Exception:
                            pass
                        # Avoid retweeting own tweets
                        if tweet_candidate.user_handle and self._is_own_tweet(tweet_candidate.user_handle, account, browser_manager):
                            continue
                        interaction_success = await publisher.retweet_tweet(tweet_candidate)
                        if interaction_success:
                            retweets_made += 1
                            metrics.increment('retweets')
                            metrics.log_event('retweet', 'success', {'source': 'keyword', 'keyword': keyword, 'tweet_id': tweet_candidate.tweet_id})
                            await asyncio.sleep(random.uniform(current_action_config.min_delay_between_actions_seconds, current_action_config.max_delay_between_actions_seconds))
                        else:
                            metrics.increment('errors')
                            metrics.log_event('retweet', 'failure', {'source': 'keyword', 'keyword': keyword, 'tweet_id': tweet_candidate.tweet_id})
                logger.info(f"[{account.account_id}] Finished keyword-based retweets.")


            # Action 3: Scrape news/research sites and post summaries/links
            news_sites_for_account = account.news_sites
            research_sites_for_account = account.research_paper_sites
            if current_action_config.enable_content_curation_posts and (news_sites_for_account or research_sites_for_account):
                 logger.info(f"[{account.account_id}] Content curation from news/research sites is planned.")
            elif current_action_config.enable_content_curation_posts:
                logger.info(f"[{account.account_id}] Content curation enabled, but no news/research sites configured for this account.")


            # Action 4: Like tweets
            if current_action_config.enable_liking_tweets:
                # Default to account target keywords if like list not configured
                keywords_to_like = current_action_config.like_tweets_from_keywords or (account.target_keywords or [])
                if keywords_to_like:
                    logger.info(f"[{account.account_id}] Starting to like tweets based on {len(keywords_to_like)} keywords.")
                    likes_done_this_run = 0
                    for keyword in keywords_to_like:
                        if likes_done_this_run >= current_action_config.max_likes_per_run:
                            break
                        logger.info(f"[{account.account_id}] Searching for tweets with keyword '{keyword}' to like.")
                        tweets_to_potentially_like = await asyncio.to_thread(
                            scraper.scrape_tweets_by_keyword,
                            keyword,
                            max_tweets=current_action_config.max_likes_per_run * 2 # Fetch more to have options
                        )
                        for tweet_to_like in tweets_to_potentially_like:
                            if likes_done_this_run >= current_action_config.max_likes_per_run:
                                break
                            
                            action_key = f"like_{account.account_id}_{tweet_to_like.tweet_id}"
                            if action_key in self.processed_action_keys:
                                logger.info(f"[{account.account_id}] Already liked or processed tweet {tweet_to_like.tweet_id}. Skipping.")
                                continue
                            
                            if current_action_config.avoid_replying_to_own_tweets and tweet_to_like.user_handle and self._is_own_tweet(tweet_to_like.user_handle, account, browser_manager):
                                logger.info(f"[{account.account_id}] Skipping own tweet {tweet_to_like.tweet_id} for liking.")
                                continue

                            # Optional relevance filter for likes pipeline (settings-driven)
                            try:
                                acc_ac = account.action_config
                                enable_rel_like = (acc_ac.enable_relevance_filter_likes if (acc_ac and acc_ac.enable_relevance_filter_likes is not None)
                                                   else self.analysis_config.get('enable_relevance_filter', {}).get('likes', True))
                                thr_like = (acc_ac.relevance_threshold_likes if (acc_ac and acc_ac.relevance_threshold_likes is not None)
                                            else float(self.analysis_config.get('thresholds', {}).get('likes_min', 0.3)))
                                if enable_rel_like:
                                    rel_like = await analyzer.score_relevance(tweet_to_like, keywords=account.target_keywords)
                                    if rel_like < thr_like:
                                        logger.debug(f"[{account.account_id}] Skipping like {tweet_to_like.tweet_id} (rel {rel_like:.2f} < {thr_like}).")
                                        continue
                            except Exception:
                                pass

                            logger.info(f"[{account.account_id}] Attempting to like tweet {tweet_to_like.tweet_id} from URL: {tweet_to_like.tweet_url}")
                            like_success = await engagement.like_tweet(tweet_id=tweet_to_like.tweet_id, tweet_url=str(tweet_to_like.tweet_url) if tweet_to_like.tweet_url else None)
                            metrics.log_event('like', 'success' if like_success else 'failure', {'tweet_id': tweet_to_like.tweet_id})
                            
                            if like_success:
                                self.file_handler.save_processed_action_key(action_key, timestamp=datetime.now().isoformat())
                                self.processed_action_keys.add(action_key)
                                likes_done_this_run += 1
                                await asyncio.sleep(random.uniform(current_action_config.min_delay_between_actions_seconds / 2, current_action_config.max_delay_between_actions_seconds / 2)) # Shorter delay for likes
                                metrics.increment('likes')
                            else:
                                logger.warning(f"[{account.account_id}] Failed to like tweet {tweet_to_like.tweet_id}.")
                                metrics.increment('errors')
                
                elif current_action_config.like_tweets_from_feed:
                    logger.warning(f"[{account.account_id}] Liking tweets from feed is enabled but not yet implemented.")
                else:
                    logger.info(f"[{account.account_id}] Liking tweets enabled, but no keywords specified and feed liking is off.")
            
            logger.info(f"[{account.account_id}] Finished processing tasks for this account.")

        except Exception as e:
            logger.error(f"[{account.account_id or 'UnknownAccount'}] Unhandled error during account processing: {e}", exc_info=True)
        finally:
            if browser_manager:
                # driver.quit() is a blocking, unbounded Selenium call — offload
                # it so teardown never stalls the event loop (run_cycle drives
                # this coroutine on the MCP server loop; SessionPool.close
                # already does the same).
                await asyncio.to_thread(browser_manager.close_driver)
            try:
                metrics.mark_run_finish()
            except Exception:
                pass
            # Safely log account ID
            account_id_for_log = account_dict.get('account_id', 'UnknownAccount')
            if 'account' in locals() and hasattr(account, 'account_id'):
                account_id_for_log = account.account_id
            logger.info(f"--- Finished processing for account: {account_id_for_log} ---")
            # The delay_between_accounts_seconds will now apply after each account finishes,
            # but accounts will start concurrently.
            # If a delay *between starts* is needed, a different mechanism (e.g., semaphore with delays) is required.
            await asyncio.sleep(self.global_settings.get('delay_between_accounts_seconds', 10)) # Reduced default for concurrent example

    async def run(self):
        logger.info("Twitter Orchestrator starting...")
        if not self.accounts_data:
            logger.warning("No accounts found in configuration. Orchestrator will exit.")
            return

        tasks = []
        for account_dict in self.accounts_data:
            tasks.append(self._process_account(account_dict))
        
        logger.info(f"Starting concurrent processing for {len(tasks)} accounts.")
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for i, result in enumerate(results):
            account_id = self.accounts_data[i].get('account_id', f"AccountIndex_{i}")
            if isinstance(result, Exception):
                logger.error(f"Error processing account {account_id}: {result}", exc_info=result)
            else:
                logger.info(f"Successfully completed processing for account {account_id}.")

        logger.info("Twitter Orchestrator finished processing all accounts.")


if __name__ == "__main__":
    orchestrator = TwitterOrchestrator()
    try:
        asyncio.run(orchestrator.run())
    except KeyboardInterrupt:
        logger.info("Orchestrator run interrupted by user.")
    except Exception as e:
        logger.critical(f"Orchestrator failed with critical error: {e}", exc_info=True)
    finally:
        logger.info("Orchestrator shutdown complete.")
