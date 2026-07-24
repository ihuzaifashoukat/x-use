"""TweetScraper must never attribute a stale page to the requested URL, and
search URLs must be query-encoded.

- scrape_tweets_from_url: when navigate_to() reports failure, abort with an
  empty result instead of scraping whatever page the driver happens to be on.
- scrape_tweets_by_keyword / scrape_tweets_by_hashtag: encode user input
  before interpolating it into the URL ('a&b' must not split the query).
"""

from unittest.mock import MagicMock

import pytest

import xuse.features.scraper.service as scraper_mod
from xuse.features.scraper.service import TweetScraper


@pytest.fixture
def scraper_env(monkeypatch):
    """TweetScraper wired to fakes: no browser, no sleeps, no progress UI."""
    monkeypatch.setattr(scraper_mod.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(scraper_mod.random, "uniform", lambda a, b: 0.0)
    monkeypatch.setattr(scraper_mod, "Scroller", lambda _d: MagicMock(scroll_page=MagicMock(return_value=False)))
    monkeypatch.setattr(scraper_mod, "Progress", lambda *a, **k: MagicMock())

    env = {}

    def make_scraper(navigate_result):
        driver = MagicMock(name="driver")
        driver.find_elements.return_value = [MagicMock(name="card")]
        bm = MagicMock(name="browser_manager")
        bm.navigate_to.return_value = navigate_result
        bm.get_driver.return_value = driver
        bm.config_loader.get_twitter_automation_setting.side_effect = lambda k, d=None: d
        scraper = TweetScraper(bm, account_id="acct1")
        env["scraper"] = scraper
        env["driver"] = driver
        env["bm"] = bm
        return scraper

    env["make_scraper"] = make_scraper
    return env


class TestNavigateFailure:
    def test_failed_navigation_returns_empty_and_never_scrapes(self, scraper_env, monkeypatch, caplog):
        scraper = scraper_env["make_scraper"](navigate_result=False)
        fake_tweet = MagicMock(name="ScrapedTweet")
        fake_tweet.tweet_id = "STALE_PAGE_TWEET"
        # Even if the stale page would yield tweets, they must not be returned.
        monkeypatch.setattr(scraper_mod, "parse_tweet_card", lambda _card, _logger: fake_tweet)

        with caplog.at_level("ERROR", logger=scraper_mod.logger.name):
            result = scraper.scrape_tweets_from_url(
                "https://x.com/search?q=test&f=live", "keyword", max_tweets=5
            )

        assert result == []
        scraper_env["driver"].find_elements.assert_not_called()
        assert any(
            "nav" in rec.getMessage().lower() and "fail" in rec.getMessage().lower()
            for rec in caplog.records
        )

    def test_successful_navigation_scrapes_normally(self, scraper_env, monkeypatch):
        scraper = scraper_env["make_scraper"](navigate_result=True)
        fake_tweet = MagicMock(name="ScrapedTweet")
        fake_tweet.tweet_id = "REAL_TWEET"
        monkeypatch.setattr(scraper_mod, "parse_tweet_card", lambda _card, _logger: fake_tweet)

        result = scraper.scrape_tweets_from_url(
            "https://x.com/someone", "profile", max_tweets=5
        )

        assert [t.tweet_id for t in result] == ["REAL_TWEET"]


class TestSearchUrlEncoding:
    def _captured_url(self, scraper, monkeypatch):
        captured = {}

        def fake_from_url(url, search_type, max_tweets=None, **kwargs):
            captured["url"] = url
            captured["search_type"] = search_type
            return []

        monkeypatch.setattr(scraper, "scrape_tweets_from_url", fake_from_url)
        return captured

    def test_keyword_is_query_encoded(self, scraper_env, monkeypatch):
        scraper = scraper_env["make_scraper"](navigate_result=True)
        captured = self._captured_url(scraper, monkeypatch)
        scraper.scrape_tweets_by_keyword("a&b")
        url = captured["url"]
        assert "a%26b" in url
        assert "q=a&b" not in url
        assert url.endswith("&f=live")

    def test_keyword_with_spaces_and_plus(self, scraper_env, monkeypatch):
        scraper = scraper_env["make_scraper"](navigate_result=True)
        captured = self._captured_url(scraper, monkeypatch)
        scraper.scrape_tweets_by_keyword("cats & dogs+")
        url = captured["url"]
        assert "&" not in url.split("q=", 1)[1].split("&f=", 1)[0]
        assert url.endswith("&f=live")

    def test_hashtag_is_path_encoded(self, scraper_env, monkeypatch):
        scraper = scraper_env["make_scraper"](navigate_result=True)
        captured = self._captured_url(scraper, monkeypatch)
        scraper.scrape_tweets_by_hashtag("#a&b")
        url = captured["url"]
        assert "a&b" not in url
        assert "a%26b" in url
