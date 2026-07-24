"""Sync the canonical skills pack into the marketplace plugin tree.

Canonical source: src/xuse/skills_pack/<skill>/SKILL.md (ships in the wheel).
Marketplace copy:  plugins/x-use/skills/<skill>/SKILL.md (what Claude's
/plugin install fetches). tests/test_skills_sync.py fails CI on drift.

Run: python scripts/sync_skills.py
"""
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "src" / "xuse" / "skills_pack"
PLUGIN = ROOT / "plugins" / "x-use" / "skills"


def main() -> None:
    if PLUGIN.exists():
        shutil.rmtree(PLUGIN)
    PLUGIN.mkdir(parents=True)
    synced = []
    for skill_dir in sorted(PACK.iterdir()):
        skill_file = skill_dir / "SKILL.md"
        if not (skill_dir.is_dir() and skill_file.is_file()):
            continue
        dest = PLUGIN / skill_dir.name
        dest.mkdir(parents=True)
        shutil.copy2(skill_file, dest / "SKILL.md")
        synced.append(skill_dir.name)
    print(f"synced {len(synced)} skills -> {PLUGIN.relative_to(ROOT)}: {', '.join(synced)}")


if __name__ == "__main__":
    main()
