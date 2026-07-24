import logging
import time
import random
from typing import Optional

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

from xuse.core.browser_manager import BrowserManager
from xuse.features.scraper.parsing import find_article_with_status_id
from xuse.models import ScrapedTweet

from .reply_handler import REPLY_ANY_TOAST_XPATH, _find_error_toast

logger = logging.getLogger(__name__)


def _confirm_quote_submission(driver, quote_text_area, timeout: float = 10.0) -> bool:
    """Wait for a post-submit confirmation signal for a quote tweet: a toast
    appears or the quote composer detaches (textarea goes stale)."""
    conditions = [EC.presence_of_element_located((By.XPATH, REPLY_ANY_TOAST_XPATH))]
    if quote_text_area is not None:
        conditions.append(EC.staleness_of(quote_text_area))
    try:
        WebDriverWait(driver, timeout).until(EC.any_of(*conditions))
        return True
    except Exception:
        return False


def retweet_or_quote(
    browser_manager: BrowserManager,
    original_tweet: ScrapedTweet,
    final_quote_text: Optional[str],
) -> bool:
    driver = browser_manager.get_driver()
    if not original_tweet.tweet_url:
        logger.error(f"Cannot retweet tweet {original_tweet.tweet_id}: Missing tweet URL.")
        return False

    is_quote_tweet = bool(final_quote_text)
    action_type_log = "Quote Tweet" if is_quote_tweet else "Retweet"
    logger.info(f"Attempting {action_type_log} for tweet ID: {original_tweet.tweet_id}")
    if final_quote_text:
        logger.info(f"Quote text: '{final_quote_text[:50]}...'")

    try:
        browser_manager.navigate_to(str(original_tweet.tweet_url))
        time.sleep(random.uniform(2.0, 3.8))

        # Match the article by exact status id: contains(@href, ...) would
        # prefix-match a different tweet whose id starts with this one.
        main_tweet_element = WebDriverWait(driver, 15).until(
            lambda d: find_article_with_status_id(d, str(original_tweet.tweet_id))
        )

        # If already reposted, the action button is 'unretweet'.
        try:
            already_reposted_btn = WebDriverWait(main_tweet_element, 3).until(
                EC.presence_of_element_located((By.XPATH, ".//button[@data-testid='unretweet']"))
            )
            if already_reposted_btn:
                if is_quote_tweet:
                    # A quote was requested but the quote composer is not
                    # reachable from this state — do NOT claim success and
                    # silently drop the quote text.
                    logger.error(
                        f"Tweet {original_tweet.tweet_id} is already reposted; the requested "
                        "quote was not posted. Reporting failure instead of a fabricated success."
                    )
                    return False
                # Plain retweet is idempotent: already reposted == desired state.
                logger.info(f"Tweet {original_tweet.tweet_id} already reposted. Skipping confirm.")
                return True
        except TimeoutException:
            pass

        # Otherwise click the retweet (repost) icon
        try:
            retweet_icon_button = WebDriverWait(main_tweet_element, 8).until(
                EC.element_to_be_clickable((By.XPATH, ".//button[@data-testid='retweet']"))
            )
        except TimeoutException:
            # As a fallback, the button might be present but not immediately clickable; try presence then JS click
            retweet_icon_button = WebDriverWait(main_tweet_element, 8).until(
                EC.presence_of_element_located((By.XPATH, ".//button[@data-testid='retweet']"))
            )
        try:
            try:
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", retweet_icon_button)
            except Exception:
                pass
            retweet_icon_button.click()
        except Exception:
            try:
                driver.execute_script("arguments[0].click();", retweet_icon_button)
            except Exception:
                pass
        logger.info(f"Clicked retweet icon for tweet {original_tweet.tweet_id}.")
        time.sleep(0.6)

        if is_quote_tweet:
            # Generic, resilient selection for quote option
            try:
                quote_option = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((By.XPATH, "//div[@role='menuitem' and contains(., 'Quote')]"))
                )
            except TimeoutException:
                quote_option = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((By.XPATH, "//div[@data-testid='Dropdown']//a[contains(@href,'/compose/tweet')]"))
                )
            quote_option.click()
            logger.info("Clicked 'Quote' option.")
            time.sleep(2)

            quote_text_area_xpath = "//div[@data-testid='tweetTextarea_0' and @role='textbox']"
            quote_text_area = WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.XPATH, quote_text_area_xpath))
            )
            try:
                quote_text_area.click()
                from selenium.webdriver.common.keys import Keys
                quote_text_area.send_keys(Keys.CONTROL, "a")
                quote_text_area.send_keys(Keys.BACKSPACE)
            except Exception:
                pass
            # Enforce platform cap for quote text as safety
            safe_quote = (final_quote_text or "")[:270]
            quote_text_area.send_keys(safe_quote)
            logger.info("Typed quote text.")

            post_button = WebDriverWait(driver, 12).until(
                EC.element_to_be_clickable((By.XPATH, "//button[@data-testid='tweetButton']"))
            )
            post_button.click()
            logger.info("Clicked 'Post' for quote tweet.")

            error_toast = _find_error_toast(driver)
            if error_toast is not None:
                logger.error(
                    "Quote tweet failed — platform error or limit detected: %s",
                    (error_toast.text or "").strip(),
                )
                return False
            if not _confirm_quote_submission(driver, quote_text_area):
                logger.error(
                    f"No submission confirmation for quote of tweet {original_tweet.tweet_id}; "
                    "treating the quote as failed."
                )
                return False
        else:
            # Wait for either the confirm button inside Dropdown or the dropdown text fallbacks
            confirm_retweet_button = None
            last_error = None
            for attempt in range(3):
                dropdown = None
                try:
                    dropdown = WebDriverWait(driver, 6).until(
                        EC.presence_of_element_located((By.XPATH, "//div[@data-testid='Dropdown' or @role='menu']"))
                    )
                except TimeoutException as e:
                    last_error = e
                # Preferred: explicit retweetConfirm within dropdown
                if dropdown is not None and confirm_retweet_button is None:
                    try:
                        confirm_retweet_button = WebDriverWait(dropdown, 4).until(
                            EC.element_to_be_clickable((By.XPATH, ".//*[@data-testid='retweetConfirm']"))
                        )
                    except TimeoutException as e:
                        last_error = e
                # Fallback: text contains Repost/Retweet
                if confirm_retweet_button is None:
                    try:
                        confirm_retweet_button = WebDriverWait(driver, 4).until(
                            EC.element_to_be_clickable((By.XPATH, "//div[@role='menu']//div[contains(., 'Repost')] | //div[@data-testid='Dropdown']//div[contains(., 'Repost')] | //div[@role='menu']//div[contains(., 'Retweet')]"))
                        )
                    except TimeoutException as e:
                        last_error = e
                if confirm_retweet_button:
                    break
                # If menu not found, retry the icon click
                try:
                    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", retweet_icon_button)
                except Exception:
                    pass
                try:
                    retweet_icon_button.click()
                except Exception:
                    try:
                        driver.execute_script("arguments[0].click();", retweet_icon_button)
                    except Exception:
                        pass
                time.sleep(0.8)

            if not confirm_retweet_button:
                # Before failing, if the tweet now shows unretweet, consider it successful
                try:
                    WebDriverWait(main_tweet_element, 3).until(
                        EC.presence_of_element_located((By.XPATH, ".//button[@data-testid='unretweet']"))
                    )
                    logger.info(f"Repost appears active for tweet {original_tweet.tweet_id} (unretweet visible).")
                    return True
                except TimeoutException:
                    raise TimeoutException(f"Retweet confirm not found. Last error: {last_error}")

            try:
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", confirm_retweet_button)
            except Exception:
                pass
            try:
                confirm_retweet_button.click()
            except Exception:
                driver.execute_script("arguments[0].click();", confirm_retweet_button)
            logger.info("Clicked 'Repost' (confirm retweet) option.")

            # Confirmation: a successful repost flips the action button to
            # 'unretweet' (same state-flip idiom as the like flow). Without
            # the flip there is no evidence the repost happened.
            try:
                WebDriverWait(main_tweet_element, 8).until(
                    EC.presence_of_element_located((By.XPATH, ".//button[@data-testid='unretweet']"))
                )
            except TimeoutException:
                logger.error(
                    f"Repost of tweet {original_tweet.tweet_id} was not confirmed "
                    "(button never flipped to 'unretweet')."
                )
                return False

        # Light backoff after action to avoid rapid-fire sequences
        time.sleep(random.uniform(2.0, 4.5))
        logger.info(f"{action_type_log} for tweet {original_tweet.tweet_id} successful.")
        return True
    except TimeoutException as e:
        logger.error(f"Timeout during {action_type_log.lower()} for tweet {original_tweet.tweet_id}: {e}")
        return False
    except Exception as e:
        logger.error(f"Failed to {action_type_log.lower()} tweet {original_tweet.tweet_id}: {e}", exc_info=True)
        return False
