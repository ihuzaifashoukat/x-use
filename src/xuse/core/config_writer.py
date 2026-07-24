"""Safe writer for config/accounts.json.

Every mutation: load fresh from disk (never trust an in-memory copy) ->
apply -> validate every account with the pydantic models -> timestamped
backup -> atomic temp-file replace. A failed write leaves the original
file untouched.
"""
import json
import logging
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from xuse.core.config_loader import normalize_account_dict
from xuse.models import AccountConfig, ActionConfig
from xuse.utils.proxy_manager import validate_proxy_url

logger = logging.getLogger(__name__)


class ConfigWriteError(Exception):
    """Validation or IO failure while mutating accounts.json. The original
    file is guaranteed untouched when this is raised."""


class AccountsConfigWriter:
    def __init__(self, accounts_file: Path, backups_dir: Optional[Path] = None,
                 max_backups: int = 10):
        self.accounts_file = Path(accounts_file)
        self.backups_dir = (Path(backups_dir) if backups_dir
                            else self.accounts_file.parent / "backups")
        self.max_backups = int(max_backups)

    def load(self) -> List[Dict[str, Any]]:
        if not self.accounts_file.is_file():
            return []
        try:
            data = json.loads(self.accounts_file.read_text(encoding="utf-8"))
        except Exception as e:
            raise ConfigWriteError(f"Could not read {self.accounts_file}: {e}") from e
        if not isinstance(data, list):
            raise ConfigWriteError(f"{self.accounts_file} does not contain a JSON array.")
        return data

    def mutate(self, mutate_fn: Callable[[List[Dict[str, Any]]], List[Dict[str, Any]]]
               ) -> List[Dict[str, Any]]:
        accounts = self.load()
        updated = mutate_fn(accounts)
        self._validate(updated)
        self._backup()
        self._atomic_write(updated)
        return updated

    def _validate(self, accounts: List[Dict[str, Any]]) -> None:
        errors: List[str] = []
        for acc in accounts:
            label = acc.get("account_id", "<unknown>") if isinstance(acc, dict) else "<unknown>"
            normalized = normalize_account_dict(acc) if isinstance(acc, dict) else acc
            try:
                AccountConfig.model_validate(normalized)
            except Exception as e:
                errors.append(f"{label}: {e}")
            if not isinstance(acc, dict):
                continue
            # Pydantic ignores unknown keys by default, but the RAW dict (not
            # the cleaned model) is what gets written — so a typo'd knob would
            # persist and be silently ignored forever. Reject them at the gate.
            action_config = normalized.get("action_config")
            if isinstance(action_config, dict):
                unknown = sorted(set(action_config) - set(ActionConfig.model_fields))
                if unknown:
                    errors.append(
                        f"{label}: unknown action_config key(s): {', '.join(unknown)}")
            proxy = normalized.get("proxy")
            if proxy and str(proxy).strip():
                self._validate_proxy(label, str(proxy).strip(), errors)
        if errors:
            raise ConfigWriteError("Validation failed: " + "; ".join(errors))

    @staticmethod
    def _validate_proxy(label: str, proxy: str, errors: List[str]) -> None:
        """Accounts may carry a direct proxy URL (same rules as the MCP proxy
        tools) or a pool:<name> reference, which only accounts may use."""
        if proxy.startswith("pool:"):
            if not proxy[len("pool:"):].strip():
                errors.append(f"{label}: proxy pool reference 'pool:' has no pool name.")
            return
        try:
            validate_proxy_url(proxy)
        except ValueError as e:
            errors.append(f"{label}: {e}")

    def _backup(self) -> None:
        if not self.accounts_file.is_file() or self.max_backups <= 0:
            return
        try:
            self.backups_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            shutil.copy2(self.accounts_file, self.backups_dir / f"accounts-{stamp}.json")
        except OSError as e:
            # The guarantee is "backup before replace": a failed backup must
            # block the atomic write, and it must surface as the documented
            # error type, not a raw OSError.
            raise ConfigWriteError(
                f"Backup of {self.accounts_file} failed: {e}") from e
        backups = sorted(self.backups_dir.glob("accounts-*.json"))
        for old in backups[:-self.max_backups]:
            try:
                old.unlink()
            except OSError:
                logger.warning("Could not prune old backup %s", old)

    def _atomic_write(self, accounts: List[Dict[str, Any]]) -> None:
        target = self.accounts_file
        fd, tmp_name = tempfile.mkstemp(dir=str(target.parent),
                                        prefix=".accounts-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(accounts, f, indent=2, ensure_ascii=False)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, target)
        except Exception as e:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise ConfigWriteError(f"Failed to write {target}: {e}") from e
