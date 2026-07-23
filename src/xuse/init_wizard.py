"""x-use init — interactive setup wizard.

Writes (only after confirmation when a file already exists):
- config/settings.json  from a preset in presets/settings/
- config/accounts.json  from a preset in presets/accounts/ (or a blank account)
- config/<account_id>_cookies.json  imported from a user-supplied export
- .env                  LLM API keys (never written into settings.json)

Reads only; engine config is validated with the Pydantic models before writing.
"""
import json
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

import typer

from xuse.core.config_loader import (
    CONFIG_DIR,
    PROJECT_ROOT,
    normalize_account_dict as _normalize_account_dict,
)
from xuse.doctor import check_cookie_data
from xuse.models import AccountConfig

PRESETS_DIR = PROJECT_ROOT / "presets"

SETTINGS_PRESET_BLURBS = {
    "beginner-defaults.json": "simple defaults (Firefox, headless, no proxies)",
    "beginner-chrome-undetected.json": "Chrome + undetected-chromedriver + stealth (recommended)",
    "beginner-proxies-hash.json": "Chrome + proxy pools (stable hash selection)",
    "beginner-proxies-roundrobin.json": "Chrome + proxy pools (round-robin rotation)",
}
ACCOUNTS_PRESET_BLURBS = {
    "growth.json": "proactive growth: competitor reposts + engagement decisioning",
    "brand_safe.json": "conservative, on-topic engagement",
    "replies_first.json": "support/FAQ-style reply focus",
    "engagement_light.json": "minimal, safe engagement",
    "community_posting.json": "posting into a specific community",
}

ENV_KEYS = [
    ("OPENAI_API_KEY", "OpenAI API key"),
    ("GEMINI_API_KEY", "Gemini API key"),
    ("AZURE_OPENAI_API_KEY", "Azure OpenAI API key"),
    ("AZURE_OPENAI_ENDPOINT", "Azure OpenAI endpoint"),
    ("AZURE_OPENAI_DEPLOYMENT", "Azure OpenAI deployment name"),
]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _choose_preset(kind: str, blurbs: Dict[str, str]) -> Optional[Path]:
    """Show a numbered preset menu; return the chosen path or None to skip."""
    folder = PRESETS_DIR / kind
    presets = sorted(folder.glob("*.json")) if folder.is_dir() else []
    if not presets:
        typer.secho(f"No presets found in {folder} — skipping.", fg=typer.colors.YELLOW)
        return None
    typer.echo(f"\nAvailable {kind} presets:")
    typer.echo("  0) skip (keep existing / configure later)")
    for i, p in enumerate(presets, start=1):
        blurb = blurbs.get(p.name, "")
        typer.echo(f"  {i}) {p.name}" + (f" — {blurb}" if blurb else ""))
    choice = typer.prompt("Choose a number", type=int, default=0)
    if choice <= 0 or choice > len(presets):
        return None
    return presets[choice - 1]


def _validate_accounts(accounts: List[Dict[str, Any]]) -> List[str]:
    errors = []
    for acc in accounts:
        try:
            AccountConfig.model_validate(_normalize_account_dict(acc))
        except Exception as e:
            errors.append(f"{acc.get('account_id', '<unknown>')}: {e}")
    return errors


def _load_env(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                values[k.strip()] = v.strip()
    return values


def _write_env(path: Path, updates: Dict[str, str]) -> None:
    existing = _load_env(path)
    existing.update(updates)
    lines = [
        "# x-use secrets — loaded at runtime; overrides config/settings.json api_keys.",
        "# This file is gitignored. Never commit it.",
    ]
    lines += [f"{k}={v}" for k, v in existing.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Wizard steps
# ---------------------------------------------------------------------------

def _settings_step() -> None:
    target = CONFIG_DIR / "settings.json"
    preset = _choose_preset("settings", SETTINGS_PRESET_BLURBS)
    if preset is None:
        typer.echo("Keeping existing settings.json." if target.exists()
                   else "Skipped — copy presets/settings/<preset>.json to config/settings.json before running.")
        return
    if target.exists() and not typer.confirm(f"{target} already exists. Overwrite?", default=False):
        typer.echo("Keeping existing settings.json.")
        return
    shutil.copyfile(preset, target)
    typer.secho(f"Wrote {target} from preset {preset.name}.", fg=typer.colors.GREEN)


def _import_cookies(account_id: str) -> str:
    """Prompt for a cookie export, copy it under config/, return cookie_file_path."""
    cookie_rel = f"config/{account_id}_cookies.json"
    src = typer.prompt(
        "Path to exported x.com cookies JSON (leave empty to do it later)",
        default="",
        show_default=False,
    ).strip()
    if not src:
        typer.echo(f"OK — place your cookie export at {cookie_rel} before running (see "
                   "data/cookies/dummy_cookies_example.json for the expected shape).")
        return cookie_rel
    src_path = Path(src).expanduser()
    if not src_path.is_file():
        typer.secho(f"File not found: {src_path} — keeping cookie path {cookie_rel}; copy it there manually.",
                    fg=typer.colors.YELLOW)
        return cookie_rel
    try:
        data = _load_json(src_path)
    except Exception as e:
        typer.secho(f"Could not parse {src_path} as JSON: {e} — not imported.",
                    fg=typer.colors.YELLOW)
        return cookie_rel
    ok, problems = check_cookie_data(data)
    if not ok:
        typer.secho("Warning: cookie file looks invalid: " + "; ".join(problems),
                    fg=typer.colors.YELLOW)
        if not typer.confirm("Import it anyway?", default=False):
            return cookie_rel
    dest = CONFIG_DIR / f"{account_id}_cookies.json"
    if dest.exists() and not typer.confirm(f"{dest} already exists. Overwrite?", default=False):
        return cookie_rel
    shutil.copyfile(src_path, dest)
    typer.secho(f"Imported cookies to {dest}.", fg=typer.colors.GREEN)
    return cookie_rel


def _accounts_step() -> None:
    target = CONFIG_DIR / "accounts.json"
    preset = _choose_preset("accounts", ACCOUNTS_PRESET_BLURBS)

    accounts: Optional[List[Dict[str, Any]]] = None
    if preset is not None:
        try:
            data = _load_json(preset)
            accounts = data if isinstance(data, list) else None
        except Exception as e:
            typer.secho(f"Preset {preset.name} is not valid JSON: {e}", fg=typer.colors.RED)
    if accounts is None:
        if target.exists():
            typer.echo("Keeping existing accounts.json.")
            return
        accounts = []

    if typer.confirm("\nConfigure an account now (id + cookies)?", default=True):
        default_id = ""
        if accounts:
            existing_id = str(accounts[0].get("account_id") or "")
            if existing_id and "your_" not in existing_id.lower():
                default_id = existing_id
        account_id = typer.prompt("Account id (handle or unique name)",
                                  default=default_id or None).strip()
        cookie_rel = _import_cookies(account_id)
        if accounts:
            accounts[0]["account_id"] = account_id
            accounts[0]["cookie_file_path"] = cookie_rel
            accounts[0]["is_active"] = True
        else:
            accounts = [{
                "account_id": account_id,
                "is_active": True,
                "cookie_file_path": cookie_rel,
                "target_keywords": [],
                "competitor_profiles": [],
            }]

    errors = _validate_accounts(accounts)
    if errors:
        typer.secho("Validation failed:", fg=typer.colors.RED)
        for err in errors:
            typer.echo(f"  - {err}")
        if not typer.confirm("Write config/accounts.json anyway?", default=False):
            typer.echo("accounts.json not written.")
            return

    if target.exists() and not typer.confirm(f"{target} already exists. Overwrite?", default=False):
        typer.echo("Keeping existing accounts.json.")
        return
    _write_json(target, accounts)
    src_note = f" from preset {preset.name}" if preset else ""
    typer.secho(f"Wrote {target}{src_note}.", fg=typer.colors.GREEN)


def _llm_keys_step() -> None:
    env_path = PROJECT_ROOT / ".env"
    if not typer.confirm("\nSet LLM API keys now (stored in gitignored .env)?", default=True):
        return
    existing = _load_env(env_path)
    updates: Dict[str, str] = {}
    for env_key, label in ENV_KEYS:
        value = typer.prompt(
            f"{label} (empty = skip)",
            default="",
            show_default=False,
            hide_input=True,
        ).strip()
        if value:
            updates[env_key] = value
    if not updates:
        typer.echo("No keys entered — skipping .env.")
        return
    if env_path.exists():
        typer.echo(f".env exists; the following keys will be set: {', '.join(sorted(updates))}")
        if not typer.confirm("Proceed?", default=True):
            return
    _write_env(env_path, updates)
    typer.secho(f"Wrote {env_path} ({len(updates)} key(s)).", fg=typer.colors.GREEN)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_wizard() -> None:
    typer.echo("x-use setup wizard")
    typer.echo("==================")
    typer.echo("This writes config/settings.json, config/accounts.json, cookie files under")
    typer.echo("config/, and .env. Existing files are never overwritten without confirmation.")
    try:
        _settings_step()
        _accounts_step()
        _llm_keys_step()
    except typer.Abort:
        typer.echo("\nAborted — no further changes made.")
        raise typer.Exit(1)
    typer.echo("\nDone. Next steps:")
    typer.echo("  1. x-use doctor   # verify browser, cookies, keys, proxies")
    typer.echo("  2. x-use run      # start the automation")
