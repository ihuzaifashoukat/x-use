import time
import typing
from typing import Iterable, Tuple, Optional

from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException


Locator = Tuple[str, str]


def wait_for_any_present(context: typing.Union[WebDriver, WebElement],
                         locators: Iterable[Locator],
                         timeout: int = 10) -> Optional[WebElement]:
    """
    Waits for the first present element among the provided locators within the given context.
    Returns the found WebElement or None if none are found within timeout.

    The timeout bounds the TOTAL wait across all locators (shared deadline),
    not each locator individually.
    """
    deadline = time.monotonic() + timeout
    for by, value in locators:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            return WebDriverWait(context, remaining).until(
                EC.presence_of_element_located((by, value))
            )
        except TimeoutException:
            continue
    return None


def wait_for_any_clickable(context: typing.Union[WebDriver, WebElement],
                           locators: Iterable[Locator],
                           timeout: int = 10) -> Optional[WebElement]:
    """
    Waits for the first clickable element among the provided locators within the given context.
    Returns the found WebElement or None if none are found within timeout.

    The timeout bounds the TOTAL wait across all locators (shared deadline),
    not each locator individually.
    """
    deadline = time.monotonic() + timeout
    for by, value in locators:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            return WebDriverWait(context, remaining).until(
                EC.element_to_be_clickable((by, value))
            )
        except TimeoutException:
            continue
    return None
