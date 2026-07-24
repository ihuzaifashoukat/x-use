"""Status-id boundary matching: '/status/123' must never match '/status/1234'.

The engine locates tweet articles by id. A naive XPath
contains(@href, '/status/<id>') prefix-matches any id that merely STARTS with
the target id, so likes/replies/retweets can land on the wrong article. The
fix extracts the numeric id from the href in Python and compares exactly.
"""

import logging

import pytest
from selenium.common.exceptions import NoSuchElementException

from xuse.features.scraper.parsing import (
    extract_status_id,
    find_article_with_status_id,
    parse_tweet_card,
)

logger = logging.getLogger("test_status_id_matching")


class TestExtractStatusId:
    @pytest.mark.parametrize(
        "href,expected",
        [
            ("https://x.com/user/status/123", "123"),
            ("https://x.com/user/status/1234", "1234"),
            ("https://x.com/user/status/123/photo/1", "123"),
            ("https://x.com/user/status/123?s=20&t=abc", "123"),
            ("https://x.com/user/status/123/analytics", "123"),
        ],
    )
    def test_extracts_exact_numeric_id(self, href, expected):
        assert extract_status_id(href) == expected

    @pytest.mark.parametrize(
        "href",
        [
            None,
            "",
            "https://x.com/user",
            "https://x.com/user/statuses/",  # no digits
            "https://x.com/i/communities/123",
        ],
    )
    def test_returns_none_without_status_digits(self, href):
        assert extract_status_id(href) is None

    def test_prefix_ids_do_not_collide(self):
        assert extract_status_id("https://x.com/u/status/123") != extract_status_id(
            "https://x.com/u/status/1234"
        )


class _Link:
    def __init__(self, href):
        self._href = href

    def get_attribute(self, name):
        return self._href if name == "href" else None


class _Article:
    def __init__(self, hrefs):
        self._hrefs = hrefs
        self.queries = []

    def find_elements(self, by, value):
        self.queries.append(value)
        if "/status/" in value:
            return [_Link(h) for h in self._hrefs]
        return []


class _Driver:
    def __init__(self, articles):
        self._articles = articles

    def find_elements(self, by, value):
        if value.startswith("//article"):
            return list(self._articles)
        return []


class TestFindArticleWithStatusId:
    def test_exact_id_wins_over_prefix_sibling(self):
        wrong = _Article(["https://x.com/u/status/1234"])
        right = _Article(["https://x.com/u/status/123"])
        driver = _Driver([wrong, right])
        assert find_article_with_status_id(driver, "123") is right

    def test_no_prefix_match_returns_none(self):
        driver = _Driver([_Article(["https://x.com/u/status/1234"])])
        assert find_article_with_status_id(driver, "123") is None

    def test_matches_photo_and_query_variants_of_same_tweet(self):
        art = _Article(["https://x.com/u/status/123/photo/1"])
        driver = _Driver([art])
        assert find_article_with_status_id(driver, "123") is art

    def test_driver_errors_yield_none_not_raise(self):
        class BoomDriver:
            def find_elements(self, by, value):
                raise RuntimeError("dom went away")

        assert find_article_with_status_id(BoomDriver(), "123") is None


class TestParseTweetCardIdExtraction:
    """parse_tweet_card must store the bare numeric id, never a path tail."""

    def test_photo_href_yields_bare_id(self):
        from xuse.features.scraper import selectors as S

        class FakeElement:
            def __init__(self, text="", attrs=None, mapping=None):
                self.text = text
                self._attrs = dict(attrs or {})
                self._mapping = dict(mapping or {})

            def find_element(self, by, value):
                entry = self._mapping.get(value)
                if entry is None:
                    raise NoSuchElementException(value)
                if isinstance(entry, list):
                    if not entry:
                        raise NoSuchElementException(value)
                    return entry[0]
                return entry

            def find_elements(self, by, value):
                entry = self._mapping.get(value)
                if entry is None:
                    return []
                return list(entry) if isinstance(entry, list) else [entry]

            def get_attribute(self, name):
                return self._attrs.get(name)

        card = FakeElement(
            mapping={
                f".{S.X_TWEET_TEXT_XPATH}": [FakeElement(text="hello world")],
                f".{S.X_STATUS_LINK_XPATH}": FakeElement(
                    attrs={"href": "https://x.com/janedoe/status/987654321/photo/1"}
                ),
            }
        )
        tweet = parse_tweet_card(card, logger)
        assert tweet is not None
        assert tweet.tweet_id == "987654321"
