"""Validate client manifests and installer-generated command configuration."""
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("xuse_mcp_config", ROOT / "scripts/mcp_config.py")
mcp_config = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mcp_config)


def test_rendered_client_config_preserves_windows_and_posix_paths():
    for command in (
        r"C:\Users\Example Name\x-use\.venv\Scripts\x-use.exe",
        "/home/example name/x-use/.venv/bin/x-use",
        "/home/example/o'brien/x-use/bin/x-use",
    ):
        data_home = r"C:\Users\Example Name\AppData\Local\x-use"
        rendered = mcp_config.render_config(command, data_home)
        assert json.loads(rendered) == {
            "mcpServers": {"x-use": {
                "command": command,
                "args": ["mcp"],
                "env": {"X_USE_HOME": data_home},
            }}
        }


def test_render_config_rejects_relative_command_or_data_home():
    with pytest.raises(ValueError, match="command path must be absolute"):
        mcp_config.render_config(".venv/bin/x-use", "/home/example/x-use-data")
    with pytest.raises(ValueError, match="data home must be absolute"):
        mcp_config.render_config("/opt/x-use/bin/x-use", "x-use-data")


def test_claude_plugin_manifests_are_valid_and_reference_x_use():
    plugin = json.loads((ROOT / "plugins/x-use/.claude-plugin/plugin.json").read_text(encoding="utf-8"))
    servers = json.loads((ROOT / "plugins/x-use/.mcp.json").read_text(encoding="utf-8"))
    assert plugin["name"] == "x-use"
    assert set(servers["x-use"]) >= {"command", "args"}
    assert servers["x-use"]["args"] == ["mcp"]


def test_runtime_sdk_floor_and_requirements_file_stay_in_sync():
    metadata = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert '"mcp>=1.30.0,<2"' in metadata
    assert "-e ." in requirements
    assert '{ name = "mcp", specifier = ">=1.30.0,<2" }' in lock
    assert "mcp>=1.30,<2" in readme


def test_install_wrappers_quote_paths_and_fail_explicit_update_errors():
    bash = (ROOT / "install.sh").read_text(encoding="utf-8")
    powershell = (ROOT / "install.ps1").read_text(encoding="utf-8")
    assert "scripts/mcp_config.py" in bash
    assert 'printf \'%q\' "${INSTALL_DIR}"' in bash
    assert 'git -C "${INSTALL_DIR}" pull --ff-only || die' in bash
    assert "scripts\\mcp_config.py" in powershell
    assert "Set-Location -LiteralPath '$quotedDir'" in powershell
    assert "git -C $Dir pull --ff-only" in powershell and "git pull failed" in powershell
