import logging
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from selenium import webdriver
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.firefox.service import Service as FirefoxService
from selenium.webdriver.remote.webdriver import WebDriver

from selenium.common.exceptions import WebDriverException
from webdriver_manager.chrome import ChromeDriverManager
from webdriver_manager.firefox import GeckoDriverManager

logger = logging.getLogger(__name__)

try:
    import undetected_chromedriver as uc
    UC_AVAILABLE = True
except Exception:
    UC_AVAILABLE = False
    uc = None  # type: ignore

try:
    from selenium_stealth import stealth as selenium_stealth_apply
    SELENIUM_STEALTH_AVAILABLE = True
except Exception:
    SELENIUM_STEALTH_AVAILABLE = False
    selenium_stealth_apply = None  # type: ignore


def _detect_chrome_major_version() -> Optional[int]:
    """Best-effort installed-Chrome major version. undetected-chromedriver
    otherwise fetches the LATEST driver build, which refuses to drive a
    browser one major behind ("This version of ChromeDriver only supports
    Chrome version N+1")."""
    try:
        if sys.platform.startswith("win"):
            import winreg
            for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                for path in (r"Software\Google\Chrome\BLBeacon", r"Software\Chromium\BLBeacon"):
                    try:
                        with winreg.OpenKey(root, path) as key:
                            value, _ = winreg.QueryValueEx(key, "version")
                        return int(str(value).split(".")[0])
                    except OSError:
                        continue
        else:
            for binary in ("google-chrome", "chromium", "chromium-browser"):
                path = shutil.which(binary)
                if not path:
                    continue
                out = subprocess.check_output([path, "--version"], timeout=10, text=True)
                match = re.search(r"(\d+)\.", out or "")
                if match:
                    return int(match.group(1))
    except Exception:
        pass
    return None


def init_chrome_driver(
    options: ChromeOptions,
    *,
    use_undetected: bool,
    configured_path: Optional[str],
    service_args: Optional[list],
    version_main: Optional[int] = None,
) -> WebDriver:
    if use_undetected and UC_AVAILABLE:
        logger.info("Using undetected_chromedriver for Chrome.")
        pinned = version_main or _detect_chrome_major_version()
        kwargs = {"options": options}
        if pinned:
            kwargs["version_main"] = pinned  # fetch the driver matching the installed browser
            logger.info("Pinning undetected-chromedriver to Chrome major version %s.", pinned)
        return uc.Chrome(**kwargs)  # type: ignore
    local_driver = configured_path or shutil.which('chromedriver')
    if local_driver:
        logger.info(f"Using local chromedriver at: {local_driver}")
        service = ChromeService(executable_path=local_driver)
    else:
        logger.info("Local chromedriver not found. Falling back to webdriver_manager (requires internet).")
        service = ChromeService(ChromeDriverManager().install(), service_args=service_args if service_args else None)
    return webdriver.Chrome(service=service, options=options)


def init_firefox_driver(
    options: FirefoxOptions,
    *,
    configured_path: Optional[str],
    service_args: Optional[list],
) -> WebDriver:
    local_driver = configured_path or shutil.which('geckodriver')
    if local_driver:
        logger.info(f"Using local geckodriver at: {local_driver}")
        service = FirefoxService(executable_path=local_driver)
    else:
        logger.info("Local geckodriver not found. Falling back to webdriver_manager (requires internet).")
        service = FirefoxService(GeckoDriverManager().install(), service_args=service_args if service_args else None)
    return webdriver.Firefox(service=service, options=options)


def apply_stealth_if_configured(driver: WebDriver, browser_type: str, enable_stealth: bool) -> None:
    if browser_type == 'chrome' and enable_stealth and SELENIUM_STEALTH_AVAILABLE:
        try:
            selenium_stealth_apply(  # type: ignore
                driver,
                languages=["en-US", "en"],
                vendor="Google Inc.",
                platform="Win32",
                webgl_vendor="Intel Inc.",
                renderer="Intel Iris OpenGL Engine",
                fix_hairline=True,
            )
            logger.info("Applied selenium-stealth anti-detection tweaks.")
        except Exception as e:
            logger.warning(f"Failed to apply selenium-stealth tweaks: {e}")

