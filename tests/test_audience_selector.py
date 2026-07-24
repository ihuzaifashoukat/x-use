"""audience_selector: community targeting must hit the configured community.

- community_id (set by the post_tweet tool's executor) drives ID-based
  selection via /i/communities/<id> links — exact id boundary, no prefix
  collisions — and takes precedence over name matching.
- Name matching is exact-match-first; substring is a fallback and the
  shortest candidate wins, so 'Crypto' can never land in 'Crypto News' when
  'Crypto' itself is listed.
- post_to_community with neither id nor name aborts (False) instead of
  silently posting to the public audience.
"""

from unittest.mock import MagicMock

import pytest
from selenium.common.exceptions import NoSuchElementException, StaleElementReferenceException, TimeoutException

import xuse.features.publisher.audience_selector as aud
import xuse.utils.selenium_waits as waits_mod
from xuse.features.publisher.audience_selector import (
    _find_community_by_id,
    _find_community_by_name,
    select_community_if_configured,
)
from xuse.models import AccountConfig


@pytest.fixture(autouse=True)
def fast_waits_and_no_sleeps(monkeypatch):
    class FastWait:
        def __init__(self, ctx, timeout):
            self.ctx = ctx

        def until(self, condition, message=""):
            for _ in range(3):
                try:
                    r = condition(self.ctx)
                    if r:
                        return r
                except Exception:
                    pass
            raise TimeoutException("fast-wait: condition never satisfied")

    monkeypatch.setattr(aud, "WebDriverWait", FastWait)
    monkeypatch.setattr(waits_mod, "WebDriverWait", FastWait)
    monkeypatch.setattr(aud.time, "sleep", lambda *_a: None)


class FakeSpan:
    def __init__(self, text):
        self._t = text

    @property
    def text(self):
        return self._t


class _Link:
    def __init__(self, href):
        self._href = href

    def get_attribute(self, name):
        return self._href if name == "href" else None


class FakeItem:
    """A menuitem with a community name span and (optionally) an id link."""

    def __init__(self, name=None, href=None):
        self.name = name
        self.href = href
        self.clicks = 0

    def find_elements(self, by, value):
        if value == ".//span[normalize-space(text())]":
            return [FakeSpan(self.name)] if self.name else []
        if value == ".//a[@href]":
            return [_Link(self.href)] if self.href else []
        return []

    def click(self):
        self.clicks += 1


class FakeContainer:
    def __init__(self, items):
        self._items = items

    def find_elements(self, by, value):
        if value == ".//div[@role='menuitem']":
            return list(self._items)
        return []


class TestFindCommunityByName:
    def test_exact_match_beats_earlier_substring(self):
        container = FakeContainer([FakeItem("Crypto News"), FakeItem("Crypto")])
        match = _find_community_by_name(container, "Crypto")
        assert match is not None and match.name == "Crypto"

    def test_short_name_does_not_match_inside_unrelated_name(self):
        container = FakeContainer([FakeItem("Daily AI Roundup"), FakeItem("AI")])
        match = _find_community_by_name(container, "AI")
        assert match is not None and match.name == "AI"

    def test_substring_fallback_picks_shortest_candidate(self):
        container = FakeContainer([FakeItem("AI Weekly News"), FakeItem("AI News")])
        match = _find_community_by_name(container, "AI")
        assert match is not None and match.name == "AI News"

    def test_no_match_returns_none(self):
        container = FakeContainer([FakeItem("Gardening"), FakeItem("Cooking")])
        assert _find_community_by_name(container, "Crypto") is None

    def test_blank_name_returns_none(self):
        container = FakeContainer([FakeItem("Crypto")])
        assert _find_community_by_name(container, "  ") is None


class TestFindCommunityById:
    def test_matches_exact_id(self):
        target = FakeItem("Crypto Hub", href="https://x.com/i/communities/123")
        container = FakeContainer([FakeItem("Other", href="https://x.com/i/communities/999"), target])
        assert _find_community_by_id(container, "123") is target

    def test_id_prefix_does_not_collide(self):
        wrong = FakeItem("Wrong", href="https://x.com/i/communities/1234")
        right = FakeItem("Right", href="https://x.com/i/communities/123")
        container = FakeContainer([wrong, right])
        assert _find_community_by_id(container, "123") is right
        assert _find_community_by_id(container, "1234") is wrong

    def test_missing_id_returns_none(self):
        container = FakeContainer([FakeItem("Crypto", href="https://x.com/i/communities/999")])
        assert _find_community_by_id(container, "123") is None


LAYERS_Q = "//div[@data-testid='layers']"
HOVERCARD_Q = "//div[@data-testid='HoverCard']"
AUDIENCE_BTN_Q = ".//button[@aria-label='Choose audience']"
AUDIENCE_VERIFY_Q = "//button[contains(@aria-label, 'audience')]"


class El:
    def __init__(self, routes=None, text=""):
        self._routes = dict(routes or {})
        self._text = text
        self.stale = False

    @property
    def text(self):
        return self._text

    def click(self):
        pass

    def is_enabled(self):
        if self.stale:
            raise StaleElementReferenceException("detached")
        return True

    def is_displayed(self):
        return True

    def find_element(self, by, value):
        entry = self._routes.get(value)
        if entry is None:
            raise NoSuchElementException(f"not found: {value}")
        return entry

    def find_elements(self, by, value):
        entry = self._routes.get(value)
        if entry is None:
            return []
        return list(entry) if isinstance(entry, list) else [entry]

    def get_attribute(self, name):
        return None

    def send_keys(self, *a):
        pass


class FakeDriver(El):
    def __init__(self, routes=None):
        super().__init__(routes=routes)
        self.queries = []

    def find_element(self, by, value):
        self.queries.append(value)
        return super().find_element(by, value)

    def execute_script(self, *a, **k):
        pass


def build_driver(menu_items):
    """Driver wired for the full audience-picker flow; the audience button
    click reveals a HoverCard containing `menu_items`. Selecting an item
    detaches the container."""
    layers = El()
    hovercard = El(routes={".//div[@role='menuitem']": menu_items})
    driver = FakeDriver(
        routes={
            LAYERS_Q: layers,
            AUDIENCE_VERIFY_Q: El(text="Everyone"),
        }
    )
    audience_btn = El()

    def reveal():
        driver._routes[HOVERCARD_Q] = hovercard

    audience_btn.click = reveal
    layers._routes[AUDIENCE_BTN_Q] = audience_btn
    # `_click_element_safely` re-checks clickability of the found element via
    # a self XPath on the driver.
    driver._routes["."] = audience_btn
    return driver, hovercard


class TestSelectCommunityIfConfigured:
    def test_not_configured_is_noop_true(self):
        driver = FakeDriver()
        cfg = AccountConfig(account_id="a", post_to_community=False)
        assert select_community_if_configured(driver, cfg) is True
        assert driver.queries == []

    def test_configured_without_id_or_name_aborts(self):
        driver = FakeDriver()
        cfg = AccountConfig(account_id="a", post_to_community=True)
        assert select_community_if_configured(driver, cfg) is False
        # The audience picker must not even open for a misconfigured account.
        assert AUDIENCE_BTN_Q not in driver.queries

    def test_id_based_selection_is_used_when_id_present(self):
        id_item = FakeItem("Totally Different Name", href="https://x.com/i/communities/123")
        name_item = FakeItem("Crypto", href="https://x.com/i/communities/999")
        driver, hovercard = build_driver([name_item, id_item])
        # Selecting the item detaches the audience container.
        id_item.click = lambda: (setattr(id_item, "clicks", id_item.clicks + 1), setattr(hovercard, "stale", True))

        cfg = AccountConfig(
            account_id="a", post_to_community=True, community_id="123", community_name="Crypto"
        )
        assert select_community_if_configured(driver, cfg) is True
        assert id_item.clicks == 1
        assert name_item.clicks == 0

    def test_exact_name_beats_partial_in_full_flow(self):
        partial = FakeItem("Crypto News", href="https://x.com/i/communities/555")
        exact = FakeItem("Crypto", href="https://x.com/i/communities/777")
        driver, hovercard = build_driver([partial, exact])
        exact.click = lambda: (setattr(exact, "clicks", exact.clicks + 1), setattr(hovercard, "stale", True))

        cfg = AccountConfig(account_id="a", post_to_community=True, community_name="Crypto")
        assert select_community_if_configured(driver, cfg) is True
        assert exact.clicks == 1
        assert partial.clicks == 0

    def test_unresolvable_community_returns_false(self):
        driver, _hovercard = build_driver([FakeItem("Gardening", href="https://x.com/i/communities/1")])
        cfg = AccountConfig(
            account_id="a", post_to_community=True, community_id="123", community_name="Crypto"
        )
        assert select_community_if_configured(driver, cfg) is False
