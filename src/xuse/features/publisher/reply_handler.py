import logging
import time
import random
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, ElementClickInterceptedException

from xuse.core.browser_manager import BrowserManager
from xuse.features.scraper.parsing import find_article_with_status_id
from xuse.models import ScrapedTweet

logger = logging.getLogger(__name__)

# Error detection is scoped to toast elements: an unscoped
# //*[contains(., 'went wrong')] matches the page root whenever the text
# appears anywhere, and a page-level substring is not a reply failure.
REPLY_ERROR_TOAST_XPATH = (
    "//div[@data-testid='toast'][contains(., 'Try again') or contains(., 'rate limit') "
    "or contains(., 'over the limit') or contains(., 'went wrong')]"
)
REPLY_ANY_TOAST_XPATH = "//div[@data-testid='toast']"


def _find_error_toast(driver, timeout: float = 3.0):
    """Return the platform error toast element if one appears, else None."""
    try:
        return WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.XPATH, REPLY_ERROR_TOAST_XPATH))
        )
    except Exception:
        return None


def _confirm_reply_submission(driver, dialog, textarea, timeout: float = 10.0) -> bool:
    """Wait for a post-submit confirmation signal.

    Modal path: the dialog detaches. Inline path (no dialog): a toast appears
    or the composer textarea detaches (re-render after a successful post).
    A reply that produced no signal is treated as not submitted.
    """
    if dialog is not None:
        try:
            WebDriverWait(driver, timeout).until(EC.staleness_of(dialog))
            return True
        except Exception:
            return False
    conditions = [EC.presence_of_element_located((By.XPATH, REPLY_ANY_TOAST_XPATH))]
    if textarea is not None:
        conditions.append(EC.staleness_of(textarea))
    try:
        WebDriverWait(driver, timeout).until(EC.any_of(*conditions))
        return True
    except Exception:
        return False


def reply_to_tweet(browser_manager: BrowserManager, original_tweet: ScrapedTweet, reply_text: str) -> bool:
    driver = browser_manager.get_driver()
    # Small human-like jitter to reduce rate/automation detection
    time.sleep(random.uniform(0.8, 2.2))
    if not original_tweet.tweet_url:
        logger.error(f"Cannot reply to tweet {original_tweet.tweet_id}: Missing tweet URL.")
        return False
    if not reply_text:
        logger.error(f"Cannot reply to tweet {original_tweet.tweet_id}: Reply text is empty.")
        return False

    logger.info(
        f"Attempting to reply to tweet {original_tweet.tweet_id} with text: '{reply_text[:50]}...'"
    )

    try:
        browser_manager.navigate_to(str(original_tweet.tweet_url))
        time.sleep(3)

        # Match the article by exact status id: contains(@href, ...) would
        # prefix-match a different tweet whose id starts with this one.
        main_tweet_element = WebDriverWait(driver, 15).until(
            lambda d: find_article_with_status_id(d, str(original_tweet.tweet_id))
        )

        reply_icon_button = WebDriverWait(main_tweet_element, 10).until(
            EC.element_to_be_clickable((By.XPATH, ".//button[@data-testid='reply']"))
        )
        reply_icon_button.click()
        logger.info(f"Clicked reply icon for tweet {original_tweet.tweet_id}.")
        time.sleep(2)

        # Target the active reply modal/dialog (robust: allow inline composer fallback)
        dialog = None
        try:
            dialog = WebDriverWait(driver, 12).until(
                EC.presence_of_element_located((By.XPATH, "//div[@role='dialog' and @aria-modal='true']"))
            )
        except TimeoutException:
            dialog = None

        # Locate and type into the reply textarea; try within dialog first, then global fallback
        reply_text_area = None
        candidates = []
        if dialog is not None:
            candidates.append((dialog, ".//div[@data-testid='tweetTextarea_0' and @role='textbox']"))
        # Global fallback in case modal wasn't detected or different structure
        candidates.append((driver, "//div[@data-testid='tweetTextarea_0' and @role='textbox']"))
        # Some variants omit role attr; include relaxed fallback
        candidates.append((driver, "//div[@data-testid='tweetTextarea_0']"))

        last_error = None
        for scope, xpath in candidates:
            try:
                reply_text_area = WebDriverWait(scope, 18).until(
                    EC.presence_of_element_located((By.XPATH, xpath))
                )
                break
            except Exception as e:
                last_error = e
                continue
        if not reply_text_area:
            raise TimeoutException(f"Reply textarea not found. Last error: {last_error}")
        try:
            reply_text_area.click()
            reply_text_area.send_keys(Keys.CONTROL, "a")
            reply_text_area.send_keys(Keys.BACKSPACE)
        except Exception:
            pass
        # Enforce platform cap for replies
        safe_reply = (reply_text or "")[:270]
        reply_text_area.send_keys(safe_reply)
        logger.info("Typed reply text into textarea.")

        # Wait for overlay/mask to disappear
        try:
            WebDriverWait(driver, 5).until(
                EC.invisibility_of_element_located((By.CSS_SELECTOR, "[data-testid='twc-cc-mask']"))
            )
        except Exception:
            pass

        # Wait for the Reply button to become enabled within the dialog (or global if dialog missing)
        def find_enabled_reply_button():
            try:
                scope = dialog if dialog is not None else driver
                btn = scope.find_element(
                    By.XPATH,
                    ".//button[@data-testid='tweetButton' and not(@disabled) and not(@aria-disabled='true')]",
                )
                return btn
            except Exception:
                return None

        reply_post_button = None
        for attempt in range(3):
            reply_post_button = find_enabled_reply_button()
            if reply_post_button:
                break
            # Nudge the editor to trigger enablement
            try:
                reply_text_area.send_keys(" ")
                reply_text_area.send_keys(Keys.BACKSPACE)
            except Exception:
                pass
            time.sleep(0.5)

        if not reply_post_button:
            # As a fallback, attempt Ctrl+Enter to submit
            logger.warning("Reply button still disabled; attempting Ctrl+Enter fallback.")
            try:
                reply_text_area.send_keys(Keys.CONTROL, Keys.ENTER)
            except Exception as e:
                logger.error(f"Failed to submit reply via Ctrl+Enter fallback: {e}")
                return False

            error_toast = _find_error_toast(driver)
            if error_toast is not None:
                logger.error(
                    "Reply failed — platform error or limit detected after keyboard fallback: %s",
                    (error_toast.text or "").strip(),
                )
                return False

            # Never claim success without a confirmation signal, whether or
            # not a modal dialog was detected (inline composer path).
            if not _confirm_reply_submission(driver, dialog, reply_text_area):
                logger.error(
                    f"Ctrl+Enter fallback produced no submission confirmation for tweet "
                    f"{original_tweet.tweet_id}; treating the reply as failed."
                )
                return False
            return True

        # Click the enabled Reply button with fallbacks
        try:
            try:
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", reply_post_button)
            except Exception:
                pass
            reply_post_button.click()
        except ElementClickInterceptedException:
            logger.warning("Reply button click intercepted, trying JS click.")
            try:
                driver.execute_script("arguments[0].click();", reply_post_button)
            except Exception:
                logger.warning("JS click failed; sending Ctrl+Enter as fallback for reply.")
                try:
                    reply_text_area.send_keys(Keys.CONTROL, Keys.ENTER)
                except Exception as e:
                    logger.error(f"Failed to submit reply via keyboard fallback: {e}")
                    return False

        logger.info("Clicked 'Reply' button in composer.")

        # A platform error toast (rate limit, spam filter, generic failure)
        # means the reply was rejected — report failure, never success.
        error_toast = _find_error_toast(driver)
        if error_toast is not None:
            logger.error(
                "Reply failed — platform error or limit detected: %s",
                (error_toast.text or "").strip(),
            )
            return False

        # Require a confirmation signal: dialog detach, toast, or composer
        # re-render. No signal means we cannot claim the reply was posted.
        if not _confirm_reply_submission(driver, dialog, reply_text_area):
            logger.error(
                f"No submission confirmation for reply to tweet {original_tweet.tweet_id}; "
                "treating the reply as failed."
            )
            return False

        time.sleep(random.uniform(1.2, 2.6))
        return True
    except TimeoutException as e:
        logger.error(
            f"Timeout while trying to reply to tweet {original_tweet.tweet_id}: {e}"
        )
        return False
    except Exception as e:
        logger.error(
            f"Failed to reply to tweet {original_tweet.tweet_id}: {e}", exc_info=True
        )
        return False
