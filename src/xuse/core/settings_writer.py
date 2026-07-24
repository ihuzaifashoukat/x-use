"""Safe writer for config/settings.json — same guarantees as
AccountsConfigWriter: load fresh from disk -> apply -> validate shape ->
timestamped backup -> atomic temp-file replace. A failed write leaves the
original file untouched. Settings are dict-shaped (loose schema by design)."""
import json
import logging
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


class SettingsWriteError(Exception):
    """Validation or IO failure while mutating settings.json. The original
    file is guaranteed untouched when this is raised."""


class SettingsConfigWriter:
    def __init__(self, settings_file: Path, backups_dir: Optional[Path] = None,
                 max_backups: int = 10):
        self.settings_file = Path(settings_file)
        self.backups_dir = (Path(backups_dir) if backups_dir
                            else self.settings_file.parent / "backups")
        self.max_backups = int(max_backups)

    def load(self) -> Dict[str, Any]:
        if not self.settings_file.is_file():
            return {}
        try:
            data = json.loads(self.settings_file.read_text(encoding="utf-8"))
        except Exception as e:
            raise SettingsWriteError(f"Could not read {self.settings_file}: {e}") from e
        if not isinstance(data, dict):
            raise SettingsWriteError(f"{self.settings_file} does not contain a JSON object.")
        return data

    def mutate(self, mutate_fn: Callable[[Dict[str, Any]], Dict[str, Any]]) -> Dict[str, Any]:
        settings = self.load()
        updated = mutate_fn(settings)
        if not isinstance(updated, dict):
            raise SettingsWriteError("settings mutation must return a JSON object (dict).")
        self._backup()
        self._atomic_write(updated)
        return updated

    def _backup(self) -> None:
        if not self.settings_file.is_file() or self.max_backups <= 0:
            return
        self.backups_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        shutil.copy2(self.settings_file, self.backups_dir / f"settings-{stamp}.json")
        backups = sorted(self.backups_dir.glob("settings-*.json"))
        for old in backups[:-self.max_backups]:
            try:
                old.unlink()
            except OSError:
                logger.warning("Could not prune old backup %s", old)

    def _atomic_write(self, settings: Dict[str, Any]) -> None:
        target = self.settings_file
        fd, tmp_name = tempfile.mkstemp(dir=str(target.parent),
                                        prefix=".settings-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(settings, f, indent=2, ensure_ascii=False)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, target)
        except Exception as e:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise SettingsWriteError(f"Failed to write {target}: {e}") from e
