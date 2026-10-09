"""Shared fixtures for the x-use pure-logic test suite.

All fixtures keep test state inside pytest's tmp_path — nothing here touches
the real repo config/, data/, or logs/ directories.
"""

import json
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pytest

from xuse.core.config_loader import ConfigLoader


def write_json(path: Path, data: Any) -> Path:
    """Write data as JSON to path, creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def isolate_mcp_state(tmp_path, monkeypatch):
    """Give default MCP stores and account paths a private per-test home.

    Injecting a draft/queue store does not cover newly added stores, whose
    defaults would otherwise write into the checkout. Keep tested settings
    intact, and let explicit monkeypatches in individual tests take precedence.
    """
    from xuse.core import config_loader
    from xuse.core.local_state import private_state_file

    home = tmp_path.resolve() / "mcp-home"
    private_state_file(home / ".bootstrap")
    config = home / "config"
    write_json(config / "settings.json", {})
    write_json(config / "accounts.json", [])
    monkeypatch.setattr(config_loader, "PROJECT_ROOT", home)
    monkeypatch.setattr(config_loader, "CONFIG_DIR", config)
    monkeypatch.setattr(config_loader, "DEFAULT_SETTINGS_FILE", config / "settings.json")
    monkeypatch.setattr(config_loader, "DEFAULT_ACCOUNTS_FILE", config / "accounts.json")
    # Python binds default arguments when the class is defined, independently
    # of the constants above. Default ConfigLoader() must also stay isolated.
    monkeypatch.setattr(ConfigLoader.__init__, "__defaults__",
                        (config / "settings.json", config / "accounts.json"))
    for name, module in tuple(sys.modules.items()):
        if name.startswith("xuse.mcp.") and module is not None:
            if hasattr(module, "PROJECT_ROOT"):
                monkeypatch.setattr(module, "PROJECT_ROOT", home)
            if hasattr(module, "CONFIG_DIR"):
                monkeypatch.setattr(module, "CONFIG_DIR", config)
    return home


@pytest.fixture
def make_config_loader(tmp_path) -> Callable[..., ConfigLoader]:
    """Factory building a ConfigLoader backed by tmp JSON files.

    Never reads the real config/settings.json or config/accounts.json.
    """

    def _factory(
        settings: Optional[Dict[str, Any]] = None,
        accounts: Optional[List[Dict[str, Any]]] = None,
    ) -> ConfigLoader:
        settings_file = tmp_path / "settings.json"
        accounts_file = tmp_path / "accounts.json"
        write_json(settings_file, settings if settings is not None else {})
        write_json(accounts_file, accounts if accounts is not None else [])
        return ConfigLoader(settings_file=settings_file, accounts_file=accounts_file)

    return _factory
