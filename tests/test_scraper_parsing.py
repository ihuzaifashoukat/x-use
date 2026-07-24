"""Tests for xuse.features.scraper.parsing (pure logic — fake WebElements, no browser).

The parser locates sub-elements by XPath and only relies on three WebElement
behaviors: find_element/find_elements (raising NoSuchElementException when
absent) and get_attribute. FakeElement reproduces exactly that contract, keyed
on the same query strings the production code builds from selectors.py
constants, so the tests track selector changes automatically.
"""

import logging
from datetime import datetime, timezone

import pytest
from selenium.common.exceptions import NoSuchElementException, StaleElementReferenceException

from xuse.features.scraper import selectors as S
from xuse.features.scraper.parsing import _parse_int_from_text, parse_tweet_card

logger = logging.getLogger("test_scraper_parsing")


def q(xpath: str) -> str:
    """Card-relative query prefix, exactly as parse_tweet_card builds it."""
    return f".{xpath}"


def engagement(testid: str) -> str:
    return q(S.X_ENGAGEMENT_BUTTON_XPATH.format(testid=testid))


class FakeElement:
    def __init__(self, text="", attrs=None, mapping=None):
        self.text = text
        self._attrs = dict(attrs or {})
        self._mapping = dict(mapping or {})

    def find_element(self, by, value):
        entry = self._mapping.get(value)
        if entry is None:
            raise NoSuchElementException(f"not found: {value}")
        if isinstance(entry, Exception):
            raise entry
        if isinstance(entry, list):
            if not entry:
                raise NoSuchElementException(f"not found: {value}")
            return entry[0]
        return entry

    def find_elements(self, by, value):
        entry = self._mapping.get(value)
        if entry is None:
            return []
        if isinstance(entry, Exception):
            raise entry
        return list(entry) if isinstance(entry, list) else [entry]

    def get_attribute(self, name):
        return self._attrs.get(name)


def full_card_mapping():
    return {
        q(S.X_USER_NAME_XPATH): FakeElement(text="Jane Doe"),
        q(S.X_USER_HANDLE_XPATH): FakeElement(text="@janedoe"),
        q(S.X_TWEET_TEXT_XPATH): [FakeElement(text="Hello "), FakeElement(text="world")],
        q(S.X_STATUS_LINK_XPATH): FakeElement(
            attrs={"href": "https://x.com/janedoe/status/1234567890?ref_src=twsrc"}
        ),
        S.X_TIME_TAG: FakeElement(attrs={"datetime": "2024-01-15T12:30:00.000Z"}),
        engagement("reply"): FakeElement(text="1.5K"),
        engagement("retweet"): FakeElement(text="2M"),
        engagement("like"): FakeElement(text="42"),
        q(S.X_ANALYTICS_VIEW_XPATH): FakeElement(text="10K"),
        q(S.X_HASHTAG_LINKS_XPATH): [FakeElement(text="#ai"), FakeElement(text="#ml")],
        q(S.X_MENTION_LINKS_XPATH): [FakeElement(text="@someone")],
        q(S.X_PROFILE_IMG_XPATH): FakeElement(
            attrs={"src": "https://pbs.twimg.com/profile_images/x.jpg"}
        ),
        q(S.X_MEDIA_IMAGE_XPATH): [
            FakeElement(attrs={"src": "https://pbs.twimg.com/media/a.jpg"}),
            FakeElement(attrs={"src": "https://pbs.twimg.com/media/a.jpg"}),  # duplicate -> deduped
        ],
        q(S.X_MEDIA_VIDEO_XPATH): [
            FakeElement(attrs={"poster": "https://pbs.twimg.com/media/b.jpg"}),  # video: poster fallback
        ],
        q(S.X_VERIFIED_ICON_SVG): FakeElement(),
    }


def minimal_card_mapping(text="Just some text here."):
    """Only the fields parse_tweet_card requires: text parts + status link."""
    return {
        q(S.X_TWEET_TEXT_XPATH): [FakeElement(text=text)],
        q(S.X_STATUS_LINK_XPATH): FakeElement(
            attrs={"href": "https://x.com/janedoe/status/999"}
        ),
    }


class TestParseIntFromText:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("42", 42),
            (" 17 ", 17),
            ("1.5K", 1500),
            ("2M", 2_000_000),
            ("0", 0),
            ("", 0),
            (None, 0),
            ("abc", 0),
            # Lowercase suffixes and thousands separators parse fine.
            ("3k", 3000),
            ("2.5m", 2_500_000),
            ("1,234", 1234),
            ("1,234.5K", 1_234_500),
        ],
    )
    def test_count_parsing(self, raw, expected):
        assert _parse_int_from_text(raw) == expected


class TestParseTweetCard:
    def test_full_card_all_fields(self):
        tweet = parse_tweet_card(FakeElement(mapping=full_card_mapping()), logger)

        assert tweet is not None
        assert tweet.tweet_id == "1234567890"  # query string stripped
        assert "/status/1234567890" in str(tweet.tweet_url)
        assert tweet.user_name == "Jane Doe"
        assert tweet.user_handle == "@janedoe"
        assert tweet.user_is_verified is True
        assert tweet.text_content == "Hello world"
        assert tweet.reply_count == 1500
        assert tweet.retweet_count == 2_000_000
        assert tweet.like_count == 42
        assert tweet.view_count == 10_000
        assert tweet.tags == ["#ai", "#ml"]
        assert tweet.mentions == ["@someone"]
        assert str(tweet.profile_image_url).rstrip("/") == (
            "https://pbs.twimg.com/profile_images/x.jpg"
        )
        assert {str(u).rstrip("/") for u in tweet.embedded_media_urls} == {
            "https://pbs.twimg.com/media/a.jpg",
            "https://pbs.twimg.com/media/b.jpg",
        }
        assert tweet.created_at == datetime(2024, 1, 15, 12, 30, tzinfo=timezone.utc)
        assert tweet.is_thread_candidate is False

    def test_minimal_card_uses_defaults(self):
        tweet = parse_tweet_card(FakeElement(mapping=minimal_card_mapping()), logger)

        assert tweet is not None
        assert tweet.tweet_id == "999"
        assert tweet.text_content == "Just some text here."
        assert tweet.user_name is None
        assert tweet.user_handle is None
        assert tweet.user_is_verified is False
        assert tweet.created_at is None
        assert tweet.reply_count == 0
        assert tweet.retweet_count == 0
        assert tweet.like_count == 0
        assert tweet.view_count == 0
        assert tweet.tags == []
        assert tweet.mentions == []
        assert tweet.profile_image_url is None
        assert tweet.embedded_media_urls == []

    @pytest.mark.parametrize(
        "text",
        [
            "(1/4) Big news coming",
            "3/5 of a longer thought",
            "A thread about testing",
            "🧵 on software",
        ],
    )
    def test_thread_indicators_flag_candidate(self, text):
        tweet = parse_tweet_card(FakeElement(mapping=minimal_card_mapping(text)), logger)
        assert tweet is not None
        assert tweet.is_thread_candidate is True

    def test_empty_text_returns_none(self):
        mapping = minimal_card_mapping()
        mapping[q(S.X_TWEET_TEXT_XPATH)] = [FakeElement(text="  ")]
        assert parse_tweet_card(FakeElement(mapping=mapping), logger) is None

    def test_no_text_elements_returns_none(self):
        mapping = minimal_card_mapping()
        del mapping[q(S.X_TWEET_TEXT_XPATH)]
        assert parse_tweet_card(FakeElement(mapping=mapping), logger) is None

    def test_missing_status_link_returns_none(self):
        mapping = minimal_card_mapping()
        del mapping[q(S.X_STATUS_LINK_XPATH)]
        assert parse_tweet_card(FakeElement(mapping=mapping), logger) is None

    def test_status_link_without_status_path_returns_none(self):
        # tweet_id stays None -> ScrapedTweet validation fails -> parser skips the card.
        mapping = minimal_card_mapping()
        mapping[q(S.X_STATUS_LINK_XPATH)] = FakeElement(attrs={"href": "https://x.com/janedoe"})
        assert parse_tweet_card(FakeElement(mapping=mapping), logger) is None

    def test_stale_card_during_text_extraction_returns_none(self):
        mapping = minimal_card_mapping()
        mapping[q(S.X_TWEET_TEXT_XPATH)] = StaleElementReferenceException("stale")
        assert parse_tweet_card(FakeElement(mapping=mapping), logger) is None

    def test_stale_status_link_returns_none(self):
        mapping = minimal_card_mapping()
        mapping[q(S.X_STATUS_LINK_XPATH)] = StaleElementReferenceException("stale")
        assert parse_tweet_card(FakeElement(mapping=mapping), logger) is None

    def test_missing_engagement_buttons_count_as_zero(self):
        mapping = full_card_mapping()
        del mapping[engagement("like")]
        tweet = parse_tweet_card(FakeElement(mapping=mapping), logger)
        assert tweet is not None
        assert tweet.like_count == 0
        assert tweet.reply_count == 1500  # other counts unaffected

    def test_missing_time_element_leaves_created_at_none(self):
        mapping = full_card_mapping()
        del mapping[S.X_TIME_TAG]
        tweet = parse_tweet_card(FakeElement(mapping=mapping), logger)
        assert tweet is not None
        assert tweet.created_at is None

    def test_unverified_when_icon_absent(self):
        mapping = full_card_mapping()
        del mapping[q(S.X_VERIFIED_ICON_SVG)]
        tweet = parse_tweet_card(FakeElement(mapping=mapping), logger)
        assert tweet is not None
        assert tweet.user_is_verified is False


class TestMediaExtraction:
    def test_typed_media_with_alt_text(self):
        mapping = minimal_card_mapping()
        mapping[q(S.X_MEDIA_IMAGE_XPATH)] = [
            FakeElement(attrs={"src": "https://pbs.twimg.com/media/a.jpg", "alt": "a chart"}),
            FakeElement(attrs={"src": "https://pbs.twimg.com/media/b.jpg", "alt": ""}),
        ]
        mapping[q(S.X_MEDIA_VIDEO_XPATH)] = [
            FakeElement(attrs={"poster": "https://pbs.twimg.com/media/vid.jpg"}),
        ]
        tweet = parse_tweet_card(FakeElement(mapping=mapping), logger)

        assert tweet is not None
        assert [(m.type, str(m.url)) for m in tweet.media] == [
            ("image", "https://pbs.twimg.com/media/a.jpg"),
            ("image", "https://pbs.twimg.com/media/b.jpg"),
            ("video", "https://pbs.twimg.com/media/vid.jpg"),
        ]
        assert tweet.media[0].alt_text == "a chart"
        assert tweet.media[1].alt_text is None  # empty alt -> None
        assert tweet.media[2].alt_text is None

    def test_media_urls_deduped_by_url(self):
        mapping = minimal_card_mapping()
        mapping[q(S.X_MEDIA_IMAGE_XPATH)] = [
            FakeElement(attrs={"src": "https://pbs.twimg.com/media/a.jpg"}),
            FakeElement(attrs={"src": "https://pbs.twimg.com/media/a.jpg"}),
        ]
        tweet = parse_tweet_card(FakeElement(mapping=mapping), logger)
        assert tweet is not None
        assert len(tweet.media) == 1

    def test_embedded_media_urls_unchanged_and_no_media_by_default(self):
        tweet = parse_tweet_card(FakeElement(mapping=minimal_card_mapping()), logger)
        assert tweet is not None
        assert tweet.media == []
        assert tweet.embedded_media_urls == []
