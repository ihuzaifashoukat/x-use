import os
import logging
import sys
import time
from typing import Optional
from selenium.webdriver.remote.webelement import WebElement # Import WebElement

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, ElementClickInterceptedException

# Adjust import paths
try:
    from ..core.browser_manager import BrowserManager
    from ..core.config_loader import ConfigLoader
    from ..utils.logger import setup_logger
    from ..data_models import ScrapedTweet, AccountConfig
    from .scraper.parsing import find_article_with_status_id
except ImportError:
    sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..')) # Add root src to path
    from xuse.core.browser_manager import BrowserManager
    from xuse.core.config_loader import ConfigLoader
    from xuse.utils.logger import setup_logger
    from xuse.models import ScrapedTweet, AccountConfig
    from xuse.features.scraper.parsing import find_article_with_status_id

config_loader_instance = ConfigLoader()
setup_logger(config_loader_instance)
logger = logging.getLogger(__name__)

class TweetEngagement:
    def __init__(self, browser_manager: BrowserManager, account_config: AccountConfig):
        self.browser_manager = browser_manager
        self.driver = self.browser_manager.get_driver()
        self.account_config = account_config
        self.config_loader = browser_manager.config_loader

    def _find_tweet_on_page(self, tweet_id: str) -> Optional[WebElement]:
        """
        Attempts to find a tweet article element by its ID within its URL.
        The status id is matched exactly (Python-side href extraction) so an
        id that merely prefixes another tweet's id cannot select the wrong
        article.
        """
        tweet_element = find_article_with_status_id(self.driver, str(tweet_id))
        if tweet_element is not None:
            logger.info(f"Found tweet element for ID {tweet_id} on page.")
            return tweet_element
        logger.warning(f"Tweet element with ID {tweet_id} not found on the current page.")
        return None

    async def like_tweet(self, tweet_id: str, tweet_url: Optional[str] = None) -> bool:
        """
        Likes a tweet given its ID. Navigates to the tweet URL if provided and necessary.
        """
        logger.info(f"Attempting to like tweet ID: {tweet_id}")
        if tweet_url is not None:
            # Callers may pass a pydantic HttpUrl; Selenium must JSON-serialize
            # the URL into the webdriver command, so coerce to str up front.
            tweet_url = str(tweet_url)

        original_url = None
        tweet_card_element = None

        try:
            # If a tweet URL is provided, navigate to it first.
            if tweet_url:
                original_url = self.driver.current_url
                if not tweet_id in original_url: # Only navigate if not already on a page related to the tweet
                    logger.info(f"Navigating to tweet URL: {tweet_url}")
                    if not self.browser_manager.navigate_to(tweet_url):
                        # Liking from whatever page happens to be loaded could
                        # hit the wrong tweet — abort instead.
                        logger.error(
                            f"Navigation to {tweet_url} failed; aborting like for tweet {tweet_id}."
                        )
                        return False
                    time.sleep(3) # Wait for page to load
            
            # Attempt to find the specific tweet card on the page
            # This is crucial if liking from a feed or search results.
            # If already on the tweet's direct page, the like button might be easier to find.
            tweet_card_element = self._find_tweet_on_page(tweet_id)
            
            if not tweet_card_element:
                # If not found on current page (e.g. after navigation or if not on direct URL)
                # and no URL was given to navigate to, we can't proceed.
                if not tweet_url:
                    logger.error(f"Cannot like tweet {tweet_id}: Not found on page and no URL provided.")
                    return False
                # If URL was provided but still not found, it's an issue.
                logger.error(f"Tweet {tweet_id} not found even after navigating to {tweet_url}.")
                return False

            # Already liked? On current X the button flips to
            # data-testid="unlike" — check that FIRST, before waiting on a
            # "like" button that will never exist in that state.
            if tweet_card_element.find_elements(By.XPATH, './/button[@data-testid="unlike"]'):
                logger.info(f"Tweet {tweet_id} is already liked.")
                return True

            like_button = WebDriverWait(tweet_card_element, 10).until(
                EC.element_to_be_clickable((By.XPATH, './/button[@data-testid="like"]'))
            )
            like_button.click()
            logger.info(f"Clicked like button for tweet {tweet_id}.")

            # Confirm: a successful click flips the button to "unlike".
            # (The old check re-queried data-testid="like" and read its
            # aria-label — both broke on current X: the testid flips, and the
            # aria text is "N Likes. Liked", not "Unlike". Found in live testing.)
            try:
                WebDriverWait(tweet_card_element, 5).until(
                    EC.presence_of_element_located((By.XPATH, './/button[@data-testid="unlike"]'))
                )
                logger.info(f"Successfully liked tweet {tweet_id}.")
                return True
            except TimeoutException:
                logger.warning(f"Failed to confirm like for tweet {tweet_id} (button never flipped to 'unlike').")
                return False

        except TimeoutException:
            logger.error(f"Timeout while trying to like tweet {tweet_id}.")
            return False
        except ElementClickInterceptedException:
            logger.error(f"Like button click intercepted for tweet {tweet_id}. Possible overlay or popup.")
            # Try to close overlays or scroll, then retry - advanced handling
            return False
        except Exception as e:
            logger.error(f"Failed to like tweet {tweet_id}: {e}", exc_info=True)
            return False
        finally:
            # Optionally navigate back if we moved from a different page
            # if tweet_url and original_url and original_url != self.driver.current_url:
            #     logger.info(f"Navigating back to original URL: {original_url}")
            #     self.driver.get(original_url)
            #     time.sleep(2)
            pass # No specific navigation back logic by default, handled by orchestrator if needed.

# Example usage and test function remains largely the same but will now use the implemented like_tweet.
# The placeholder warning in the test function about replacing tweet_id/url is still relevant for actual testing.

if __name__ == '__main__':
    import asyncio
    
    async def test_engagement():
        cfg_loader = ConfigLoader()
        accounts_data = cfg_loader.get_accounts_config()
        
        if not accounts_data:
            logger.error("No accounts configured in config/accounts.json. Cannot run engagement test.")
            return

        active_account_dict = next((acc for acc in accounts_data if acc.get("is_active", True)), None)
        if not active_account_dict:
            logger.error("No active accounts found in config/accounts.json.")
            return
            
        try:
            # Use Pydantic's model_validate for robust parsing
            account = AccountConfig.model_validate(active_account_dict)
        except Exception as e:
            logger.error(f"Error creating AccountConfig model from dict for {active_account_dict.get('account_id')}: {e}")
            return

        bm = BrowserManager(account_config=active_account_dict) 
        engagement = TweetEngagement(browser_manager=bm, account_config=account)

        # --- IMPORTANT: Replace with a REAL, ACCESSIBLE tweet_id and tweet_url for testing ---
        # This tweet should ideally NOT be liked by the test account initially.
        test_tweet_id = "1795000000000000000"  # Replace with a real tweet ID from X.com
        test_tweet_user = "x" # Replace with the user handle of the tweet poster
        test_tweet_url = f"https://x.com/{test_tweet_user}/status/{test_tweet_id}" 
        # --- End of placeholder section ---

        if "1795000000000000000" == test_tweet_id or "someuser" == test_tweet_user or "x" == test_tweet_user :
            logger.warning("Placeholder tweet_id/URL/user detected in engagement test. Test will likely fail or target a non-existent tweet.")
            logger.warning("Please update test_tweet_id, test_tweet_user, and test_tweet_url in src/features/engagement.py with real, accessible values.")
            bm.close_driver()
            return

        try:
            logger.info(f"Testing engagement for account: {account.account_id}")
            logger.info(f"Attempting to like tweet: {test_tweet_url}")

            success = await engagement.like_tweet(tweet_id=test_tweet_id, tweet_url=test_tweet_url)
            logger.info(f"Like tweet operation result: {success}")

            if success:
                logger.info("Waiting a few seconds to observe the 'liked' state if checking manually...")
                time.sleep(5)
                # Optionally, you could try to unlike it here if an unlike method existed.
                # For now, just confirms the like action was attempted.
            
        except Exception as e:
            logger.error(f"Error during engagement test: {e}", exc_info=True)
        finally:
            logger.info("Closing browser manager after engagement test...")
            engagement.browser_manager.close_driver()
            logger.info("Engagement test finished.")

    # To run this test:
    # 1. Ensure config/accounts.json has at least one active account with valid cookies.
    # 2. Replace the placeholder test_tweet_id, test_tweet_user, and test_tweet_url above with actual values.
    # 3. Uncomment the line below:
    # asyncio.run(test_engagement())
    if __name__ == '__main__':
         logger.info("To run the engagement test, uncomment 'asyncio.run(test_engagement())' at the end of the script and ensure placeholder tweet IDs/URLs are replaced with actual, accessible ones.")
