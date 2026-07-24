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
from xuse.models import AccountConfig

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
        errors = []
        for acc in accounts:
            try:
                AccountConfig.model_validate(normalize_account_dict(acc))
            except Exception as e:
                errors.append(f"{acc.get('account_id', '<unknown>')}: {e}")
        if errors:
            raise ConfigWriteError("Validation failed: " + "; ".join(errors))

    def _backup(self) -> None:
        if not self.accounts_file.is_file() or self.max_backups <= 0:
            return
        self.backups_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        shutil.copy2(self.accounts_file, self.backups_dir / f"accounts-{stamp}.json")
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
