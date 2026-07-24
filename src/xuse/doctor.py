"""x-use doctor — environment and config health checks.

Prints one PASS/FAIL/SKIP line per check (browser/driver, per-account cookies,
LLM keys, proxies) with remediation hints. Exit code: 0 if nothing failed, 1 otherwise.
"""
import json
import logging
import os
import shutil
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import typer

from xuse.core.config_loader import CONFIG_DIR, PROJECT_ROOT, ConfigLoader

logger = logging.getLogger(__name__)

try:  # Reuse the engine's key resolution/placeholder rejection when importable.
    from xuse.core.llm_service.clients import _is_api_key_valid, _resolve_api_key
except Exception:  # pragma: no cover - fallback for partial installs
    def _is_api_key_valid(key_name: str, key_value: Optional[str]) -> bool:
        if not key_value:
            return False
        if "YOUR_" in key_value.upper() and "_KEY" in key_value.upper():
            return False
        return True

    def _resolve_api_key(key_name: str, config_value: Optional[str]) -> Tuple[Optional[str], str]:
        env_value = os.environ.get("OPENAI_API_KEY")
        if env_value and env_value.strip():
            return env_value, "env var OPENAI_API_KEY"
        if config_value and str(config_value).strip():
            return str(config_value), "settings.json"
        return None, "none"


@dataclass
class Check:
    name: str
    status: str  # "PASS" | "FAIL" | "SKIP"
    detail: str = ""
    hint: str = ""


# --- Cookie helpers (shared with init_wizard) ---

def resolve_cookie_path(candidate: str) -> Optional[Path]:
    """Resolve a cookie path the same way browser_manager/cookies.py does:
    config dir first, then project root, then absolute."""
    for base in (CONFIG_DIR, PROJECT_ROOT):
        p = base / candidate
        if p.is_file():
            return p
    abs_path = Path(candidate)
    if abs_path.is_absolute() and abs_path.is_file():
        return abs_path
    return None


def check_cookie_data(data: Any) -> Tuple[bool, List[str]]:
    """Validate parsed cookie JSON: non-empty list, auth_token + ct0 present
    with values, and not expired. Returns (ok, problems)."""
    if not isinstance(data, list) or not data:
        return False, ["cookie file does not contain a non-empty JSON array"]
    by_name = {c.get("name"): c for c in data if isinstance(c, dict)}
    problems: List[str] = []
    for required in ("auth_token", "ct0"):
        cookie = by_name.get(required)
        if not cookie:
            problems.append(f"missing '{required}' cookie")
            continue
        if not cookie.get("value"):
            problems.append(f"'{required}' cookie has an empty value")
        exp = cookie.get("expires") or cookie.get("expirationDate") or cookie.get("expiry")
        if exp:
            try:
                exp_value = float(exp)
            except (TypeError, ValueError):
                exp_value = None  # unparseable expiry — treated as a session cookie
            # exp_value <= 0 (puppeteer exports use -1, others 0) marks a
            # session cookie: valid by definition, never an expiry problem.
            if exp_value is not None and 0 < exp_value < time.time():
                try:
                    when = datetime.fromtimestamp(exp_value).strftime("%Y-%m-%d")
                except (OSError, OverflowError, ValueError):
                    when = "an unknown date"  # never let timestamp conversion crash the check
                problems.append(f"'{required}' cookie expired on {when}")
    return (not problems), problems


# --- Individual checks ---

def _check_config_files(loader: ConfigLoader) -> List[Check]:
    settings_ok = loader.settings_file.is_file() and bool(loader.settings)
    accounts_ok = loader.accounts_file.is_file() and bool(loader.accounts)
    return [
        Check("config/settings.json", "PASS" if settings_ok else "FAIL",
              "loaded" if settings_ok else "missing, empty, or invalid JSON",
              "" if settings_ok else "run `x-use init` or copy a preset from presets/settings/"),
        Check("config/accounts.json", "PASS" if accounts_ok else "FAIL",
              f"{len(loader.accounts)} account(s)" if accounts_ok else "missing, empty, or invalid JSON",
              "" if accounts_ok else "run `x-use init` or copy a preset from presets/accounts/"),
    ]


def _windows_browser_paths(binary: str) -> List[Path]:
    roots = [os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"),
             os.environ.get("LOCALAPPDATA")]
    candidates = {
        "chrome": ["Google\\Chrome\\Application\\chrome.exe", "Chromium\\Application\\chrome.exe"],
        "firefox": ["Mozilla Firefox\\firefox.exe"],
    }
    return [Path(root) / rel for root in filter(None, roots)
            for rel in candidates.get(binary, []) if (Path(root) / rel).is_file()]


def _check_browser(settings: Dict[str, Any]) -> List[Check]:
    bs = settings.get("browser_settings", {}) or {}
    browser_type = str(bs.get("type") or "chrome").lower()

    if browser_type == "firefox":
        binary_names = ("firefox", "firefox.exe")
        driver_name, driver_key = "geckodriver", "gecko_driver_path"
    else:
        browser_type = "chrome"
        binary_names = ("chrome", "chrome.exe", "google-chrome", "chromium")
        driver_name, driver_key = "chromedriver", "chrome_driver_path"

    binary = next((shutil.which(n) for n in binary_names if shutil.which(n)), None)
    if not binary:
        win_paths = _windows_browser_paths(browser_type)
        binary = str(win_paths[0]) if win_paths else None
    checks = [Check(
        f"browser:{browser_type}",
        "PASS" if binary else "FAIL",
        binary or f"no {browser_type} binary found in PATH or standard install locations",
        "" if binary else f"install {browser_type.capitalize()} or fix browser_settings.type in config/settings.json",
    )]

    configured = bs.get(driver_key)
    local_driver = configured or shutil.which(driver_name)
    if local_driver:
        checks.append(Check(f"driver:{driver_name}", "PASS", str(local_driver)))
    elif browser_type == "chrome" and bs.get("use_undetected_chromedriver"):
        try:
            import undetected_chromedriver  # noqa: F401
            checks.append(Check(f"driver:{driver_name}", "PASS",
                                "managed by undetected-chromedriver (auto-downloads on first run)"))
        except Exception:
            checks.append(Check(f"driver:{driver_name}", "FAIL",
                                "use_undetected_chromedriver is on but the package is not importable",
                                "pip install undetected-chromedriver"))
    else:
        checks.append(Check(f"driver:{driver_name}", "PASS",
                            "no local driver; webdriver-manager will download one (requires internet)"))
    return checks


def _check_cookies(accounts: List[Dict[str, Any]]) -> List[Check]:
    checks: List[Check] = []
    if not accounts:
        return [Check("cookies", "SKIP", "no accounts configured")]
    for acc in accounts:
        account_id = acc.get("account_id") or "<unknown>"
        suffix = " (inactive)" if not acc.get("is_active", True) else ""
        if acc.get("cookies"):
            ok, problems = check_cookie_data(acc["cookies"])
            checks.append(Check(
                f"cookies:{account_id}{suffix}",
                "PASS" if ok else "FAIL",
                "inline cookies valid" if ok else "; ".join(problems),
                "" if ok else "re-export cookies from your browser for x.com",
            ))
            continue
        candidate = acc.get("cookie_file_path")
        if not candidate:
            checks.append(Check(f"cookies:{account_id}{suffix}", "FAIL",
                                "no cookie_file_path or inline cookies configured",
                                "run `x-use init` or set cookie_file_path in config/accounts.json"))
            continue
        resolved = resolve_cookie_path(str(candidate))
        if not resolved:
            checks.append(Check(f"cookies:{account_id}{suffix}", "FAIL",
                                f"cookie file not found: {candidate}",
                                "export x.com cookies to that path (see data/cookies/dummy_cookies_example.json for the shape)"))
            continue
        try:
            data = json.loads(resolved.read_text(encoding="utf-8"))
        except Exception as e:
            checks.append(Check(f"cookies:{account_id}{suffix}", "FAIL",
                                f"{resolved} is not valid JSON: {e}"))
            continue
        ok, problems = check_cookie_data(data)
        checks.append(Check(
            f"cookies:{account_id}{suffix}",
            "PASS" if ok else "FAIL",
            str(resolved) if ok else "; ".join(problems),
            "" if ok else "re-export fresh x.com cookies (need auth_token + ct0, unexpired)",
        ))
    return checks


def _check_llm_keys(settings: Dict[str, Any]) -> List[Check]:
    """Single OpenAI-compatible client: a key is optional (interactive MCP
    use is keyless), so a missing key is SKIP, never FAIL.

    Uses the same env-first resolution as build_client so doctor certifies
    the key the runtime will actually use: a placeholder (e.g. the wizard's
    YOUR_OPENAI_API_KEY default) that shadows a valid config key and disables
    the client is a FAIL naming the effective source, not a false PASS."""
    llm_block = settings.get("llm", {}) or {}
    api_keys = settings.get("api_keys", {}) or {}
    config_key = llm_block.get("api_key") or api_keys.get("openai_api_key")
    base_url = os.environ.get("OPENAI_BASE_URL") or llm_block.get("base_url") or "api.openai.com"
    model = os.environ.get("OPENAI_MODEL") or llm_block.get("model") or "gpt-4o-mini"

    key, source = _resolve_api_key("openai_api_key", config_key)
    if key is None:
        return [Check("llm", "SKIP",
                      "no LLM key — only needed for \"auto\" text and background automation",
                      "set OPENAI_API_KEY (+ OPENAI_BASE_URL/OPENAI_MODEL) in .env or the llm block in config/settings.json")]
    if not _is_api_key_valid("openai_api_key", key):
        return [Check("llm", "FAIL",
                      f"key from {source} is a placeholder — the runtime LLM client is disabled",
                      "replace the placeholder with a real key, or remove it from .env so a valid settings.json key can apply")]
    return [Check("llm", "PASS", f"model={model}, base_url={base_url}, key from {source}")]


def _redact_proxy(url: str) -> str:
    try:
        parts = urlparse(url)
        host = parts.hostname or "?"
        port = f":{parts.port}" if parts.port else ""
        return f"{parts.scheme}://{host}{port}"
    except Exception:
        return "<unparseable proxy URL>"


def _check_proxies(loader: ConfigLoader, accounts: List[Dict[str, Any]]) -> List[Check]:
    proxied = [a for a in accounts if a.get("proxy")]
    if not proxied:
        return [Check("proxy", "SKIP", "no per-account proxies configured")]
    from xuse.utils.proxy_manager import ProxyManager
    manager = ProxyManager(loader)
    checks: List[Check] = []
    for acc in proxied:
        account_id = acc.get("account_id") or "<unknown>"
        try:
            resolved = manager.resolve(acc.get("proxy"), account_id=account_id)
        except Exception as e:
            checks.append(Check(f"proxy:{account_id}", "FAIL", f"resolution error: {e}"))
            continue
        if not resolved:
            checks.append(Check(f"proxy:{account_id}", "FAIL",
                                f"could not resolve '{acc.get('proxy')}'",
                                "check proxy_pools in browser_settings and ${VAR} env interpolation"))
            continue
        display = _redact_proxy(resolved)
        try:
            parts = urlparse(resolved)
            if not parts.hostname or not parts.port:
                raise ValueError("missing host/port")
            start = time.monotonic()
            with socket.create_connection((parts.hostname, parts.port), timeout=5):
                latency_ms = int((time.monotonic() - start) * 1000)
            checks.append(Check(f"proxy:{account_id}", "PASS", f"{display} reachable ({latency_ms} ms)"))
        except Exception as e:
            checks.append(Check(f"proxy:{account_id}", "FAIL",
                                f"{display} unreachable: {e}",
                                "verify the proxy is up and credentials/env vars are set"))
    return checks


# --- Entry point ---

def run_checks() -> int:
    """Run all checks, print the table, return process exit code."""
    from xuse.utils.env import load_env
    load_env()  # make .env-provided LLM keys visible to the checks below
    loader = ConfigLoader()
    settings = loader.get_settings() or {}
    accounts = loader.get_accounts_config() or []

    checks: List[Check] = []
    checks += _check_config_files(loader)
    checks += _check_browser(settings)
    checks += _check_cookies(accounts)
    checks += _check_llm_keys(settings)
    checks += _check_proxies(loader, accounts)

    colors = {"PASS": typer.colors.GREEN, "FAIL": typer.colors.RED, "SKIP": typer.colors.YELLOW}
    name_width = max((len(c.name) for c in checks), default=10)
    typer.echo("")
    for c in checks:
        typer.secho(f"{c.status:<5}", fg=colors[c.status], bold=True, nl=False)
        typer.echo(f"  {c.name:<{name_width}}  {c.detail}")
        if c.status == "FAIL" and c.hint:
            typer.secho(f"       {'':<{name_width}}  -> {c.hint}", fg=typer.colors.BRIGHT_BLACK)
    typer.echo("")

    failed = [c for c in checks if c.status == "FAIL"]
    if failed:
        typer.secho(f"{len(failed)} check(s) failed: {', '.join(c.name for c in failed)}",
                    fg=typer.colors.RED)
        return 1
    typer.secho("All checks passed.", fg=typer.colors.GREEN)
    return 0
