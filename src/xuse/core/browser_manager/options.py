import logging
from typing import Union, Optional
from urllib.parse import urlparse

from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.firefox.options import Options as FirefoxOptions

from .ua import get_user_agent

logger = logging.getLogger(__name__)


def configure_driver_options(
    options: Union[ChromeOptions, FirefoxOptions],
    browser_type: str,
    *,
    headless: bool,
    window_size: Optional[str],
    proxy: Optional[str],
    additional_options: Optional[list],
    custom_user_agent: Optional[str] = None,
    for_undetected: bool = False,
) -> Union[ChromeOptions, FirefoxOptions]:
    user_agent = get_user_agent(custom_user_agent)
    options.add_argument(f"user-agent={user_agent}")

    if headless:
        if browser_type == 'chrome':
            options.add_argument('--headless=new')
        else:
            options.add_argument('--headless')
        options.add_argument('--disable-gpu')

    if window_size:
        options.add_argument(f"--window-size={window_size}")

    if proxy:
        scheme = urlparse(proxy).scheme or 'http'
        if browser_type == 'chrome':
            options.add_argument(f"--proxy-server={proxy}")
        elif browser_type == 'firefox':
            host = urlparse(proxy).hostname
            port = urlparse(proxy).port or 0
            if host and port:
                options.set_preference('network.proxy.type', 1)
                if scheme.startswith('socks'):
                    options.set_preference('network.proxy.socks', host)
                    options.set_preference('network.proxy.socks_port', int(port))
                    options.set_preference('network.proxy.socks_version', 4 if scheme == 'socks4' else 5)
                else:
                    options.set_preference('network.proxy.http', host)
                    options.set_preference('network.proxy.http_port', int(port))
                    options.set_preference('network.proxy.ssl', host)
                    options.set_preference('network.proxy.ssl_port', int(port))
            else:
                logger.warning(f"Proxy URL appears invalid for Firefox prefs: {proxy}")

    # Reduce automation fingerprints for Chrome — plain chromedriver only.
    # undetected-chromedriver applies its own equivalents internally, and
    # passing excludeSwitches through its capability flow makes modern
    # chromedriver reject the session ("unrecognized chrome option").
    if browser_type == 'chrome' and not for_undetected:
        try:
            options.add_experimental_option("excludeSwitches", ["enable-automation"])
            options.add_experimental_option('useAutomationExtension', False)
            options.add_argument("--disable-blink-features=AutomationControlled")
        except Exception:
            pass

    if isinstance(additional_options, list):
        for opt in additional_options:
            if isinstance(opt, str):
                options.add_argument(opt)
            else:
                logger.warning(f"Ignoring non-string driver option: {opt}")
    elif additional_options is not None:
        logger.warning(f"'driver_options' in config is not a list: {additional_options}")

    return options

