import logging
import time
import random
from typing import List

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException, ElementClickInterceptedException

from xuse.core.browser_manager import BrowserManager
from xuse.models import AccountConfig
from xuse.utils.selenium_waits import wait_for_any_present
from .audience_selector import select_community_if_configured
from .reply_handler import REPLY_ANY_TOAST_XPATH

logger = logging.getLogger(__name__)

# Platform rejections surface as toast messages; scoped to toast elements so
# unrelated page text cannot produce false positives.
COMPOSER_ERROR_TOAST_XPATH = (
    "//div[@data-testid='toast'][contains(., 'Try again') or contains(., 'rate limit') "
    "or contains(., 'over the limit') or contains(., 'went wrong') "
    "or contains(., 'already said that')]"
)


def post_new_tweet(
    browser_manager: BrowserManager,
    account_config: AccountConfig,
    tweet_text: str,
    final_media_paths: List[str],
) -> bool:
    # X constraints: up to 4 images OR 1 video; don't mix types.
    def _filter_media_paths_for_x(paths: List[str]) -> List[str]:
        if not paths:
            return []
        imgs_ext = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
        vids_ext = {".mp4", ".mov", ".m4v", ".webm"}
        images, videos, others = [], [], []
        for p in paths:
            try:
                ext = (p.rsplit(".", 1)[-1] if "." in p else "").lower()
                ext = f".{ext}" if ext else ""
            except Exception:
                ext = ""
            if ext in imgs_ext:
                images.append(p)
            elif ext in vids_ext:
                videos.append(p)
            else:
                others.append(p)
        # Prefer video alone if present; otherwise up to 4 images
        if videos:
            return [videos[0]]
        if images:
            return images[:4]
        # Fallback: allow first file if type unknown
        return others[:1]

    final_media_paths = _filter_media_paths_for_x(list(final_media_paths or []))
    driver = browser_manager.get_driver()
    logger.info(
        f"Attempting to post tweet: '{(tweet_text or '')[:50]}...' with {len(final_media_paths)} media file(s)."
    )

    try:
        driver.get("https://x.com/home")
        time.sleep(random.uniform(2.2, 3.7))
        try:
            main_tweet_button = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, "//a[@data-testid='SideNav_NewTweet_Button']"))
            )
            main_tweet_button.click()
            logger.info("Clicked main tweet button to open composer.")
            time.sleep(random.uniform(1.2, 2.2))
        except TimeoutException:
            logger.info("Main tweet button not found; going directly to composer URL.")
            try:
                driver.get("https://x.com/compose/post")
                time.sleep(random.uniform(2.0, 3.0))
            except Exception:
                driver.get("https://x.com/compose/tweet")
                time.sleep(random.uniform(2.0, 3.0))

        try:
            layers = WebDriverWait(driver, 5).until(
                EC.presence_of_element_located((By.XPATH, "//div[@data-testid='layers']"))
            )
        except TimeoutException:
            layers = driver

        text_area = wait_for_any_present(
            layers,
            [
                (By.XPATH, ".//div[@data-testid='tweetTextarea_0']"),
                (By.XPATH, "//div[@data-testid='tweetTextarea_0']"),
            ],
            timeout=20,
        )
        if not text_area:
            logger.error("Composer textarea not found.")
            return False
        # Focus textarea first to ensure controls render, but don't type yet
        try:
            text_area.click()
        except Exception:
            pass

        # Select audience/community before typing or uploading media
        if not select_community_if_configured(driver, account_config):
            logger.error("Failed to select community audience; aborting post.")
            return False

        # Re-acquire layers and textarea after potential re-render from audience switch
        try:
            layers = WebDriverWait(driver, 5).until(
                EC.presence_of_element_located((By.XPATH, "//div[@data-testid='layers']"))
            )
        except TimeoutException:
            layers = driver

        text_area = wait_for_any_present(
            layers,
            [
                (By.XPATH, ".//div[@data-testid='tweetTextarea_0']"),
                (By.XPATH, "//div[@data-testid='tweetTextarea_0']"),
            ],
            timeout=20,
        )
        if not text_area:
            logger.error("Composer textarea not found after audience selection.")
            return False
        # Ensure tweet text respects platform cap
        safe_tweet_text = (tweet_text or "")[:270]
        try:
            text_area.click()
            text_area.send_keys(Keys.CONTROL, "a")
            text_area.send_keys(Keys.BACKSPACE)
        except Exception:
            pass
        text_area.send_keys(safe_tweet_text)
        logger.info("Typed tweet text into textarea.")

        if final_media_paths:
            file_input_xpath = "//input[@data-testid='fileInput' and @type='file']"
            try:
                add_media_button = driver.find_element(By.XPATH, "//button[@data-testid='mediaButton']")
                add_media_button.click()
                time.sleep(1)
            except NoSuchElementException:
                logger.debug("Did not find a separate 'Add Media' button, proceeding to file input.")

            file_input = wait_for_any_present(
                layers,
                [
                    (By.XPATH, file_input_xpath),
                    (By.XPATH, ".//input[@type='file' and contains(@data-testid, 'fileInput')]")
                ],
                timeout=10,
            )
            if file_input:
                files_to_upload_str = "\n".join(final_media_paths)
                file_input.send_keys(files_to_upload_str)
                logger.info(
                    f"Sent {len(final_media_paths)} media file(s) to input: {files_to_upload_str}"
                )
                # Wait until Tweet button reflects readiness (enabled). Helps ensure upload processed.
                try:
                    def _tweet_button_ready(drv):
                        try:
                            btns = drv.find_elements(By.XPATH, "//button[@data-testid='tweetButton']")
                            if not btns:
                                return False
                            btn = btns[0]
                            disabled = btn.get_attribute("disabled")
                            aria_dis = btn.get_attribute("aria-disabled")
                            return (disabled is None) and (aria_dis != "true")
                        except Exception:
                            return False

                    WebDriverWait(driver, 30).until(lambda d: _tweet_button_ready(d))
                    logger.info("Media upload appears complete; Tweet button enabled.")
                except TimeoutException:
                    logger.warning("Tweet button did not enable after media upload wait; proceeding anyway.")
                time.sleep(random.uniform(1.0, 2.0))
            else:
                logger.warning("File input not found; skipping media upload.")

        # Now click Post button
        post_button_xpath = "//button[@data-testid='tweetButton']"
        try:
            WebDriverWait(driver, 3).until(
                EC.invisibility_of_element_located((By.CSS_SELECTOR, "[data-testid='twc-cc-mask']"))
            )
        except Exception:
            pass

        # Try from layers first then fallback
        try:
            post_button = WebDriverWait(layers, 10).until(
                EC.element_to_be_clickable((By.XPATH, ".//button[@data-testid='tweetButton']"))
            )
        except TimeoutException:
            post_button = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, post_button_xpath))
            )

        try:
            try:
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", post_button)
            except Exception:
                pass
            post_button.click()
        except ElementClickInterceptedException:
            logger.warning("Post button click intercepted, trying JS click.")
            try:
                driver.execute_script("arguments[0].click();", post_button)
            except Exception:
                logger.warning("JS click failed; sending Ctrl+Enter as fallback.")
                try:
                    text_area.send_keys(Keys.CONTROL, Keys.ENTER)
                except Exception as e:
                    logger.error(f"Failed to submit post via keyboard fallback: {e}")
                    return False
        logger.info("Clicked 'Post' button in composer.")

        # Confirmation phase: a clicked Post button is not proof of a posted
        # tweet — duplicate text, rate limits and spam interstitials reject
        # silently at the UI level. Fail loudly on a platform error toast...
        error_toast = None
        try:
            error_toast = WebDriverWait(driver, 3).until(
                EC.presence_of_element_located((By.XPATH, COMPOSER_ERROR_TOAST_XPATH))
            )
        except Exception:
            error_toast = None
        if error_toast is not None:
            logger.error(
                "Post failed — platform error or limit detected: %s",
                (error_toast.text or "").strip(),
            )
            return False

        # ...otherwise watch for a state flip (same idiom as the like flow):
        # a toast appears or the composer textarea detaches as the composer
        # closes. No signal means no evidence the tweet was posted.
        try:
            WebDriverWait(driver, 10).until(
                EC.any_of(
                    EC.presence_of_element_located((By.XPATH, REPLY_ANY_TOAST_XPATH)),
                    EC.staleness_of(text_area),
                )
            )
        except Exception:
            logger.error(
                "No post confirmation observed (no toast and the composer stayed open); "
                "treating the post as failed."
            )
            return False

        time.sleep(random.uniform(1.0, 2.0))
        return True
    except TimeoutException as e:
        logger.error(f"Timeout while trying to post tweet: {e}")
        return False
    except Exception as e:
        logger.error(f"Failed to post tweet: {e}", exc_info=True)
        return False
