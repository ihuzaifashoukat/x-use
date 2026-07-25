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


def test_root_skill_matches_what_the_sync_script_composes():
    """The repo-root SKILL.md is generated from the `x-use` router skill. Agents
    that clone or index the repository look for it, so it must not drift."""
    from sync_skills import compose_root_skill

    root_skill = ROOT / "SKILL.md"
    assert root_skill.is_file(), "root SKILL.md is missing — run: python scripts/sync_skills.py"
    assert root_skill.read_text(encoding="utf-8") == compose_root_skill(), \
        "root SKILL.md is stale — run: python scripts/sync_skills.py"


def test_root_skill_has_frontmatter_agents_can_parse():
    lines = (ROOT / "SKILL.md").read_text(encoding="utf-8").splitlines()
    assert lines[0].strip() == "---", "frontmatter must start at line 1"
    end = lines[1:].index("---") + 1
    frontmatter = lines[1:end]
    assert any(l.startswith("name: x-use") for l in frontmatter)
    assert any(l.startswith("description: ") for l in frontmatter)


def test_plugin_manifest_and_marketplace_exist():
    import json
    plugin = json.loads(
        (ROOT / "plugins" / "x-use" / ".claude-plugin" / "plugin.json")
        .read_text(encoding="utf-8"))
    assert plugin["name"] == "x-use"
    marketplace = json.loads((ROOT / "marketplace.json").read_text(encoding="utf-8"))
    assert any(p["name"] == "x-use" for p in marketplace["plugins"])
