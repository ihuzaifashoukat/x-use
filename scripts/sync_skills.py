"""Sync the canonical skills pack into every place a copy has to exist.

Canonical source: src/xuse/skills_pack/<skill>/SKILL.md (ships in the wheel).
Marketplace copy:  plugins/x-use/skills/<skill>/SKILL.md (what Claude's
/plugin install fetches).
Repo root:         SKILL.md, composed from the `x-use` router skill plus an
                   install footer. Agents that clone or index the repository
                   look for a root SKILL.md, and directories that scan for
                   Agent Skills key off it.

tests/test_skills_sync.py fails CI on drift in any of the three.

Run: python scripts/sync_skills.py
"""
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "src" / "xuse" / "skills_pack"
PLUGIN = ROOT / "plugins" / "x-use" / "skills"
ROOT_SKILL = ROOT / "SKILL.md"

# The root copy stands alone: a reader who cloned the repo has none of the
# workflow skills installed yet, so the router needs to say how to get them.
ROOT_FOOTER = """
## Getting the workflow skills

This file is the router. The four workflow skills it points at ship inside the
package:

```bash
pip install x-use-mcp
x-use skills install
```

That writes them to `~/.claude/skills/` and `~/.agents/skills/`, so Claude Code
and Codex-style agents both pick them up. Claude Code users can install the
plugin from this repository's marketplace instead.

The server also exposes the same workflows as MCP **prompts** (`research_niche`,
`draft_replies`, `review_and_publish`, `daily_check`, `setup_account`), which
need no installation and work in any MCP client, and read-only **resources**
(`xuse://accounts`, `xuse://accounts/{account_id}/persona`,
`xuse://drafts/pending`) for context you want attached rather than fetched.

Working from a clone? The canonical skill sources live in
`src/xuse/skills_pack/<name>/SKILL.md`. Edit those, then run
`python scripts/sync_skills.py`; this file is generated from them.
"""


def compose_root_skill() -> str:
    """Root SKILL.md = the `x-use` router skill + the install footer."""
    body = (PACK / "x-use" / "SKILL.md").read_text(encoding="utf-8")
    return body.rstrip("\n") + "\n" + ROOT_FOOTER


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

    ROOT_SKILL.write_text(compose_root_skill(), encoding="utf-8")
    print(f"wrote {ROOT_SKILL.relative_to(ROOT)} (composed from skills_pack/x-use)")


if __name__ == "__main__":
    main()
