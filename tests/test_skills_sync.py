"""Drift guard: plugins/x-use/skills/ must mirror src/xuse/skills_pack/
byte-for-byte. Run scripts/sync_skills.py when this fails."""
import filecmp
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "src" / "xuse" / "skills_pack"
PLUGIN = ROOT / "plugins" / "x-use" / "skills"


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


def test_plugin_manifest_and_marketplace_exist():
    import json
    plugin = json.loads(
        (ROOT / "plugins" / "x-use" / ".claude-plugin" / "plugin.json")
        .read_text(encoding="utf-8"))
    assert plugin["name"] == "x-use"
    marketplace = json.loads((ROOT / "marketplace.json").read_text(encoding="utf-8"))
    assert any(p["name"] == "x-use" for p in marketplace["plugins"])
