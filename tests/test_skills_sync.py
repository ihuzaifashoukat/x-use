"""Drift guard: plugins/x-use/skills/ must mirror src/xuse/skills_pack/
byte-for-byte, and the root SKILL.md must match what sync_skills.py composes.
Run scripts/sync_skills.py when this fails."""
import filecmp
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "src" / "xuse" / "skills_pack"
PLUGIN = ROOT / "plugins" / "x-use" / "skills"

sys.path.insert(0, str(ROOT / "scripts"))


def _tree(base: Path):
    return sorted(p.relative_to(base) for p in base.rglob("SKILL.md"))


def test_plugin_tree_mirrors_skills_pack():
    pack_files = _tree(PACK)
    plugin_files = _tree(PLUGIN)
    assert pack_files, "skills pack is missing"
    assert pack_files == plugin_files, (
        f"skill trees differ (pack={pack_files}, plugin={plugin_files}) — "
        "run: python scripts/sync_skills.py")
    for rel in pack_files:
        assert filecmp.cmp(PACK / rel, PLUGIN / rel, shallow=False), \
            f"{rel} differs between pack and plugin — run: python scripts/sync_skills.py"


def test_root_skill_is_the_setup_skill():
    """The repo-root SKILL.md is what an agent reads on first contact with the
    repository, before any x-use tool exists. It has to carry the whole path from
    nothing to a configured account, so pin the load-bearing steps: without any
    one of these the agent stalls or, worse, invents a step."""
    text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    flat = " ".join(text.split())

    for required in (
        "pip install x-use-mcp",       # install
        "x-use doctor",                # verify
        "x-use skills install",        # workflow skills
        "claude mcp add x-use",        # Claude Code registration
        '"command": "x-use"',          # JSON-client registration
        "[mcp_servers.x-use]",         # Codex registration
        "add_account(",                # first account
        "update_account(",            # keywords, handles, persona
        "get_account_health(",         # verification
        "approve_draft",               # the gate
    ):
        assert required in flat, f"root SKILL.md no longer covers: {required}"

    # The ordering constraint that actually trips agents up: a stdio server is
    # only loaded at client startup, so tools cannot appear mid-session.
    assert "restart the client" in flat, "root SKILL.md must tell the user to restart"
    # Cookie values must never be pasted into a conversation.
    assert "Never ask the user to paste" in flat


def test_registration_snippets_agree_between_root_and_packaged_setup():
    """Two setup skills exist on purpose: the root one runs before x-use is
    installed (shell-first, no tools), the packaged one runs after. They overlap
    on the client registration commands, and a stale copy in either sends users
    to a config that does not work."""
    root = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    packaged = (PACK / "x-use-setup" / "SKILL.md").read_text(encoding="utf-8")
    for snippet in ("claude mcp add x-use -- x-use mcp", "[mcp_servers.x-use]"):
        assert snippet in root and snippet in packaged, \
            f"registration snippet drifted between root and x-use-setup: {snippet}"


def test_root_skill_is_not_a_copy_of_a_packaged_skill():
    """It was briefly generated from the router skill. That was the wrong content
    for first contact, and a copy here would silently drift back."""
    root = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    for packaged in _tree(PACK):
        assert root != (PACK / packaged).read_text(encoding="utf-8"), \
            f"root SKILL.md duplicates {packaged}"


def test_plugin_manifest_and_marketplace_exist():
    import json
    plugin = json.loads(
        (ROOT / "plugins" / "x-use" / ".claude-plugin" / "plugin.json")
        .read_text(encoding="utf-8"))
    assert plugin["name"] == "x-use"
    marketplace = json.loads((ROOT / "marketplace.json").read_text(encoding="utf-8"))
    assert any(p["name"] == "x-use" for p in marketplace["plugins"])
