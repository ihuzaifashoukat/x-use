"""One version, everywhere.

src/xuse/__init__.py sat at 2.0.0 through three releases because nothing inside
the package read it. External MCP directory scanners did, and published 2.0.0
next to a 2.3.1 release. pyproject.toml now derives its version from that
attribute, and this pins the rest of the chain to it.
"""
import json
import re
from pathlib import Path

import pytest

import xuse

ROOT = Path(__file__).resolve().parents[1]
SEMVER = re.compile(r"\d+\.\d+\.\d+")


def read_json(name: str):
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def test_version_is_a_plain_semver_string():
    assert SEMVER.fullmatch(xuse.__version__), xuse.__version__


def test_pyproject_derives_its_version_from_the_package():
    """A second literal in pyproject.toml is exactly how the drift happened."""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'dynamic = ["version"]' in text
    assert 'version = {attr = "xuse.__version__"}' in text
    assert not re.search(r'^version = "', text, re.MULTILINE), \
        "pyproject.toml has a hardcoded version again; it must stay dynamic"


def test_server_json_matches_the_package_version():
    """server.json is what the official MCP Registry publishes. If it drifts,
    every directory that ingests the registry shows the wrong version."""
    manifest = read_json("server.json")
    assert manifest["version"] == xuse.__version__
    for package in manifest["packages"]:
        assert package["version"] == xuse.__version__, package["identifier"]


def test_changelog_documents_the_current_version():
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## [{xuse.__version__}]" in changelog, \
        f"CHANGELOG.md has no entry for {xuse.__version__}"


@pytest.mark.asyncio
async def test_mcp_handshake_advertises_the_package_version(make_config_loader, tmp_path):
    """FastMCP takes no `version`, so without the fix in server.py the handshake
    reports the MCP SDK's version as x-use's."""
    from xuse.mcp.drafts import DraftStore
    from xuse.mcp.server import create_server

    server = create_server(config_loader=make_config_loader(accounts=[]),
                           draft_store=DraftStore(tmp_path / "d.jsonl"))
    assert server._mcp_server.version == xuse.__version__
