"""wait_for_any_present / wait_for_any_clickable must bound the TOTAL wait by
the caller's timeout — not multiply it by the locator count.

A shared deadline is computed once (time.monotonic() + timeout); each
per-locator wait receives only the time remaining on that deadline. The
scripted fake clock simulates elapsed time between locator attempts.
"""

from selenium.common.exceptions import NoSuchElementException, TimeoutException

import xuse.utils.selenium_waits as waits


class FakeDriver:
    """find_element succeeds only for the configured 'good' locator value."""

    def __init__(self, good=None):
        self.good = good

    def find_element(self, by, value):
        if value == self.good:
            return object()
        raise NoSuchElementException(value)


class ImmediateWait:
    """Records the timeout it was constructed with; evaluates the condition once."""

    calls = []

    def __init__(self, ctx, timeout):
        self.ctx = ctx
        ImmediateWait.calls.append(timeout)

    def until(self, condition, message=""):
        # Mirror WebDriverWait's default ignored-exceptions behavior.
        try:
            r = condition(self.ctx)
        except NoSuchElementException:
            r = None
        if r:
            return r
        raise TimeoutException("not found")


def run_wait(monkeypatch, func, monotonic_readings, locators, timeout, good=None):
    """Drive `func` with a scripted clock. readings[0] backs the deadline
    computation; each later reading is the clock when the next locator
    attempt starts (i.e. earlier waits consumed the intervening time)."""
    ImmediateWait.calls = []
    readings = iter(monotonic_readings)
    monkeypatch.setattr(waits, "WebDriverWait", ImmediateWait)
    monkeypatch.setattr(waits.time, "monotonic", lambda: next(readings))
    result = func(FakeDriver(good=good), locators, timeout=timeout)
    return result, ImmediateWait.calls


LOCS = [("xpath", f"//b{i}") for i in range(5)]


def test_five_failing_locators_cost_one_timeout_not_five(monkeypatch):
    # The first locator's wait consumes the whole 10s budget; the remaining
    # four locators must be skipped (old behavior: five 10s waits = 50s).
    result, calls = run_wait(monkeypatch, waits.wait_for_any_present, [0, 0, 10, 10, 10], LOCS, timeout=10)
    assert result is None
    assert calls == [10]


def test_each_locator_gets_only_the_remaining_time(monkeypatch):
    # 4s elapse during the first wait, then 5s, then the deadline is blown.
    result, calls = run_wait(monkeypatch, waits.wait_for_any_present, [0, 0, 4, 9, 10], LOCS, timeout=10)
    assert result is None
    assert calls == [10, 6, 1]


def test_later_locator_still_found_within_deadline(monkeypatch):
    result, calls = run_wait(monkeypatch, waits.wait_for_any_present, [0, 0, 2, 5], LOCS, timeout=10, good="//b2")
    assert result is not None
    assert calls == [10, 8, 5]


def test_clickable_variant_shares_the_deadline(monkeypatch):
    result, calls = run_wait(monkeypatch, waits.wait_for_any_clickable, [0, 0, 6, 12], LOCS, timeout=10)
    assert result is None
    assert calls == [10, 4]
