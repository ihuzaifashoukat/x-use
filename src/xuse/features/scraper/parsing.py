import logging
import re
from typing import Optional, List
from datetime import datetime

from selenium.webdriver.common.by import By
from selenium.common.exceptions import NoSuchElementException, StaleElementReferenceException
from selenium.webdriver.remote.webelement import WebElement

from ...models import MediaItem, ScrapedTweet

from .selectors import (
    THREAD_INDICATORS,
    X_USER_NAME_XPATH,
    X_USER_HANDLE_XPATH,
    X_TWEET_TEXT_XPATH,
    X_STATUS_LINK_XPATH,
    X_TIME_TAG,
    X_ENGAGEMENT_BUTTON_XPATH,
    X_ANALYTICS_VIEW_XPATH,
    X_HASHTAG_LINKS_XPATH,
    X_MENTION_LINKS_XPATH,
    X_PROFILE_IMG_XPATH,
    X_MEDIA_IMAGE_XPATH,
    X_MEDIA_VIDEO_XPATH,
    X_VERIFIED_ICON_SVG,
)


_COUNT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([kKmM])?")

_STATUS_ID_RE = re.compile(r"/status/(\d+)")


def extract_status_id(href: Optional[str]) -> Optional[str]:
    """Extract the numeric status id from a tweet URL.

    Stops at the first non-digit so '/status/123' never matches '/status/1234'
    (prefix collision), while '/status/123/photo/1' and '/status/123?s=20'
    still resolve to '123'. Returns None when no status id is present.
    """
    if not href:
        return None
    match = _STATUS_ID_RE.search(href)
    return match.group(1) if match else None


def find_article_with_status_id(context, tweet_id: str):
    """Return the first <article> containing a status link whose id matches
    `tweet_id` exactly, or None.

    The id comparison happens in Python on extracted href ids; a DOM-level
    contains(@href, '/status/<id>') prefix-matches other tweets whose id
    merely starts with the target id.
    """
    target = str(tweet_id)
    try:
        articles = context.find_elements(
            By.XPATH, "//article[.//a[contains(@href, '/status/')]]"
        )
    except Exception:
        return None
    for article in articles:
        try:
            links = article.find_elements(By.XPATH, ".//a[contains(@href, '/status/')]")
        except Exception:
            continue
        for link in links:
            try:
                href = link.get_attribute("href")
            except StaleElementReferenceException:
                continue
            if extract_status_id(href) == target:
                return article
    return None


def _parse_int_from_text(text: str) -> int:
    """Parse an engagement count like '1,234', '1.5K', or '3k' into an int.

    Tolerates thousands separators and case-insensitive K/M suffixes; returns
    0 when no number is present.
    """
    if not text:
        return 0
    match = _COUNT_RE.search(text.strip().replace(",", ""))
    if not match:
        return 0
    try:
        value = float(match.group(1))
        suffix = (match.group(2) or "").lower()
        if suffix == "k":
            value *= 1_000
        elif suffix == "m":
            value *= 1_000_000
        return int(value)
    except (ValueError, OverflowError):
        return 0


def _get_count(card_element: WebElement, testid: str) -> int:
    try:
        element = card_element.find_element(
            By.XPATH, f".{X_ENGAGEMENT_BUTTON_XPATH.format(testid=testid)}"
        )
        return _parse_int_from_text(element.text)
    except (NoSuchElementException, StaleElementReferenceException):
        return 0


def parse_tweet_card(card_element: WebElement, logger: logging.Logger) -> Optional[ScrapedTweet]:
    try:
        user_name = None
        try:
            user_name_element = card_element.find_element(By.XPATH, f".{X_USER_NAME_XPATH}")
            user_name = user_name_element.text if user_name_element else None
        except (NoSuchElementException, StaleElementReferenceException):
            pass

        user_handle = None
        try:
            user_handle_element = card_element.find_element(By.XPATH, f".{X_USER_HANDLE_XPATH}")
            user_handle = user_handle_element.text if user_handle_element else None
        except (NoSuchElementException, StaleElementReferenceException):
            pass

        tweet_text_parts: List[str] = []
        try:
            text_elements = card_element.find_elements(By.XPATH, f".{X_TWEET_TEXT_XPATH}")
            for el in text_elements:
                try:
                    tweet_text_parts.append(el.text)
                except StaleElementReferenceException:
                    logger.debug("Stale element reference when extracting tweet text part.")
                    continue
        except StaleElementReferenceException:
            # Entire card went stale; skip quietly
            return None
        text_content = "".join(tweet_text_parts).strip()
        if not text_content:
            return None

        tweet_id = None
        tweet_url = None
        try:
            link_element = card_element.find_element(By.XPATH, f".{X_STATUS_LINK_XPATH}")
            href = link_element.get_attribute("href")
            extracted_id = extract_status_id(href)
            if href and extracted_id:
                tweet_url = href
                tweet_id = extracted_id
        except (NoSuchElementException, StaleElementReferenceException):
            logger.debug("Could not find tweet link/ID element for a card (missing or stale).")
            return None

        created_at_dt = None
        try:
            time_element = card_element.find_element(By.XPATH, X_TIME_TAG)
            datetime_str = time_element.get_attribute("datetime")
            if datetime_str:
                created_at_dt = datetime.fromisoformat(datetime_str.replace("Z", "+00:00"))
        except (NoSuchElementException, StaleElementReferenceException):
            logger.debug(f"Timestamp not found for tweet ID {tweet_id}")

        reply_count = _get_count(card_element, "reply")
        retweet_count = _get_count(card_element, "retweet")
        like_count = _get_count(card_element, "like")

        view_count = 0
        try:
            view_element = card_element.find_element(By.XPATH, f".{X_ANALYTICS_VIEW_XPATH}")
            view_count = _parse_int_from_text(view_element.text)
        except (NoSuchElementException, StaleElementReferenceException):
            pass

        tags = [tag.text for tag in card_element.find_elements(By.XPATH, f".{X_HASHTAG_LINKS_XPATH}")]
        mentions = [
            mention.text for mention in card_element.find_elements(By.XPATH, f".{X_MENTION_LINKS_XPATH}")
        ]

        profile_image_url = None
        try:
            img_element = card_element.find_element(By.XPATH, f".{X_PROFILE_IMG_XPATH}")
            profile_image_url = img_element.get_attribute("src")
        except (NoSuchElementException, StaleElementReferenceException):
            pass

        media_items: List[MediaItem] = []
        seen_media_urls = set()
        for img_el in card_element.find_elements(By.XPATH, f".{X_MEDIA_IMAGE_XPATH}"):
            src = img_el.get_attribute("src")
            if src and src not in seen_media_urls:
                seen_media_urls.add(src)
                media_items.append(
                    MediaItem(type="image", url=src, alt_text=img_el.get_attribute("alt") or None)
                )
        for vid_el in card_element.find_elements(By.XPATH, f".{X_MEDIA_VIDEO_XPATH}"):
            poster = vid_el.get_attribute("poster") or vid_el.get_attribute("src")
            if poster and poster not in seen_media_urls:
                seen_media_urls.add(poster)
                media_items.append(MediaItem(type="video", url=poster, alt_text=None))

        embedded_media_urls: List[str] = [str(m.url) for m in media_items]

        is_verified = False
        try:
            card_element.find_element(By.XPATH, f".{X_VERIFIED_ICON_SVG}")
            is_verified = True
        except (NoSuchElementException, StaleElementReferenceException):
            pass

        is_thread_candidate = False
        for indicator in THREAD_INDICATORS:
            if re.search(indicator, text_content, re.IGNORECASE):
                is_thread_candidate = True
                break
        # Heuristic around self-replies omitted (uncertain without reliable selector)

        return ScrapedTweet(
            tweet_id=tweet_id,
            user_name=user_name,
            user_handle=user_handle,
            user_is_verified=is_verified,
            created_at=created_at_dt,
            text_content=text_content,
            reply_count=reply_count,
            retweet_count=retweet_count,
            like_count=like_count,
            view_count=view_count,
            tags=tags,
            mentions=mentions,
            tweet_url=tweet_url,
            profile_image_url=profile_image_url,
            embedded_media_urls=embedded_media_urls,
            media=media_items,
            is_thread_candidate=is_thread_candidate,
        )

    except StaleElementReferenceException:
        # Dynamic DOM updates can stale cards mid-parse; skip without noise
        logger.debug("Tweet card went stale during parsing; skipping.")
        return None
    except Exception as e:  # Catch-all to avoid breaking the loop
        logger.error(f"Error parsing tweet card: {e}", exc_info=True)
        return None
