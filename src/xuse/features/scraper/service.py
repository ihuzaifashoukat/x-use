import os
import logging
import sys
import time
import random
from typing import List, Optional
from urllib.parse import quote, quote_plus

from selenium.webdriver.common.by import By
from selenium.common.exceptions import TimeoutException, StaleElementReferenceException
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.common.action_chains import ActionChains

# Adjust import paths
try:
    from ...core.browser_manager import BrowserManager
    from ...core.config_loader import ConfigLoader
    from ...utils.logger import setup_logger
    from ...data_models import ScrapedTweet
    from ...utils.scroller import Scroller
    from ...utils.progress import Progress
except ImportError:
    sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    from xuse.core.browser_manager import BrowserManager
    from xuse.core.config_loader import ConfigLoader
    from xuse.utils.logger import setup_logger
    from xuse.models import ScrapedTweet
    from xuse.utils.scroller import Scroller
    from xuse.utils.progress import Progress

from .parsing import parse_tweet_card
from .selectors import X_TWEET_ARTICLE_XPATH

config = ConfigLoader()
setup_logger(config)
logger = logging.getLogger(__name__)


class TweetScraper:
    def __init__(self, browser_manager: BrowserManager, account_id: Optional[str] = None):
        self.browser_manager = browser_manager
        self.driver = self.browser_manager.get_driver()  # Ensure driver is initialized
        self.actions = ActionChains(self.driver)
        self.config_loader = browser_manager.config_loader  # Reuse config loader
        self.scroller = Scroller(self.driver)  # Initialize scroller
        self.account_id = account_id  # For logging or account-specific actions

        self.scrape_settings = self.config_loader.get_twitter_automation_setting("scraper_config", {})
        self.default_max_tweets = (
            self.config_loader.get_twitter_automation_setting("max_tweets_per_scrape", None)
            or self.config_loader.get_twitter_automation_setting("max_tweets_per_keyword_scrape", 50)
        )
        self.scroll_delay_min = self.scrape_settings.get("scroll_delay_min_seconds", 1.5)
        self.scroll_delay_max = self.scrape_settings.get("scroll_delay_max_seconds", 3.5)
        self.no_new_tweets_scroll_limit = self.scrape_settings.get(
            "no_new_tweets_scroll_limit", 5
        )

    def _get_tweet_cards_from_page(self) -> List[WebElement]:
        try:
            return self.driver.find_elements(By.XPATH, X_TWEET_ARTICLE_XPATH)
        except Exception as e:
            logger.error(f"Error finding tweet cards: {e}")
            return []

    def scrape_tweets_from_url(
        self,
        url: str,
        search_type: str,  # e.g., "keyword", "profile", "hashtag"
        max_tweets: Optional[int] = None,
        stop_if_no_new_tweets_count: Optional[int] = None,
    ) -> List[ScrapedTweet]:
        if max_tweets is None:
            max_tweets = self.default_max_tweets
        if stop_if_no_new_tweets_count is None:
            stop_if_no_new_tweets_count = self.no_new_tweets_scroll_limit

        logger.info(
            "Navigating to %s for scraping (%s). Max tweets: %s",
            url,
            search_type,
            max_tweets,
        )
        if not self.browser_manager.navigate_to(url):
            # Never scrape whatever page the driver happens to be on: those
            # tweets would be attributed to a URL they did not come from.
            logger.error(
                "Navigation to %s failed; aborting scrape to avoid returning stale-page tweets.",
                url,
            )
            return []
        time.sleep(5)

        scraped_tweets: List[ScrapedTweet] = []
        seen_tweet_ids = set()
        scroll_attempts_with_no_new_tweets = 0

        progress = Progress(max_tweets, description=f"Scraping {search_type}", unit="tweets")
        progress.set_progress(0, status_message="Starting")

        while len(scraped_tweets) < max_tweets:
            try:
                tweet_card_elements = self._get_tweet_cards_from_page()
                if not tweet_card_elements:
                    logger.info("No tweet card elements found on the page.")
                    scroll_attempts_with_no_new_tweets += 1
                    if scroll_attempts_with_no_new_tweets >= stop_if_no_new_tweets_count:
                        logger.info(
                            "No new tweets found after %s scrolls. Stopping.",
                            stop_if_no_new_tweets_count,
                        )
                        break
                    if not self.scroller.scroll_page():
                        break
                    time.sleep(random.uniform(self.scroll_delay_min, self.scroll_delay_max))
                    continue

                new_tweets_found_this_scroll = 0
                for card_el in tweet_card_elements:
                    if len(scraped_tweets) >= max_tweets:
                        break

                    try:
                        self.driver.execute_script(
                            "arguments[0].scrollIntoView({block: 'center'});", card_el
                        )
                        time.sleep(0.2)
                    except Exception as scroll_err:
                        logger.debug(
                            "Could not scroll tweet card into view: %s", scroll_err
                        )

                    parsed_tweet = parse_tweet_card(card_el, logger)
                    if parsed_tweet and parsed_tweet.tweet_id not in seen_tweet_ids:
                        scraped_tweets.append(parsed_tweet)
                        seen_tweet_ids.add(parsed_tweet.tweet_id)
                        new_tweets_found_this_scroll += 1
                        progress.set_progress(
                            len(scraped_tweets),
                            status_message=f"Found {len(scraped_tweets)}",
                        )

                if new_tweets_found_this_scroll == 0:
                    scroll_attempts_with_no_new_tweets += 1
                    logger.info(
                        "No new tweets found in this scroll. Attempt %s/%s",
                        scroll_attempts_with_no_new_tweets,
                        stop_if_no_new_tweets_count,
                    )
                else:
                    scroll_attempts_with_no_new_tweets = 0

                if scroll_attempts_with_no_new_tweets >= stop_if_no_new_tweets_count:
                    logger.info(
                        "Stopping scrape for %s: No new tweets after %s consecutive empty scrolls.",
                        url,
                        stop_if_no_new_tweets_count,
                    )
                    break

                if len(scraped_tweets) >= max_tweets:
                    logger.info("Reached max_tweets (%s) for %s.", max_tweets, url)
                    break

                if not self.scroller.scroll_page():
                    logger.info("End of page or scroll error for %s.", url)
                    break

                time.sleep(random.uniform(self.scroll_delay_min, self.scroll_delay_max))

            except TimeoutException:
                logger.warning("Timeout during tweet scraping for %s. May proceed with fewer tweets.", url)
                break
            except StaleElementReferenceException:
                logger.warning("Encountered stale element reference, attempting to re-fetch cards.")
                time.sleep(1)
                continue
            except Exception as e:
                logger.error("Unhandled exception during scraping %s: %s", url, e, exc_info=True)
                break

        progress.finish(final_message=f"Found {len(scraped_tweets)} tweets.")
        logger.info("Finished scraping for %s. Found %s tweets.", url, len(scraped_tweets))
        return scraped_tweets

    def scrape_tweets_by_keyword(self, keyword: str, max_tweets: Optional[int] = None) -> List[ScrapedTweet]:
        # Fully encode the keyword: '&', '#', '+', quotes etc. would otherwise
        # corrupt the query string.
        search_url = f"https://x.com/search?q={quote_plus(keyword)}&f=live"
        return self.scrape_tweets_from_url(search_url, "keyword", max_tweets)

    def scrape_tweets_from_profile(self, profile_url: str, max_tweets: Optional[int] = None) -> List[ScrapedTweet]:
        return self.scrape_tweets_from_url(profile_url, "profile", max_tweets)

    def scrape_tweets_by_hashtag(self, hashtag: str, max_tweets: Optional[int] = None) -> List[ScrapedTweet]:
        clean_hashtag = hashtag.lstrip('#')
        hashtag_url = f"https://x.com/hashtag/{quote(clean_hashtag, safe='')}?f=live"
        return self.scrape_tweets_from_url(hashtag_url, "hashtag", max_tweets)


if __name__ == "__main__":
    # The interactive example runner previously in the monolith can be re-added here if desired.
    logger.info("Run the scraper via your application entrypoint or integrate into tests.")

