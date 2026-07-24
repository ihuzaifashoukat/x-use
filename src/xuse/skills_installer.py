"""Install the bundled agent skills into AI-client skill directories.

One pack (`xuse.skills_pack`, shipped in the wheel) serves every client that
speaks SKILL.md: Claude Code reads ~/.claude/skills/<name>/SKILL.md, Codex
reads ~/.agents/skills/<name>/SKILL.md. Installs never overwrite an existing
skill unless force=True.
"""
from importlib.resources import files
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SKILL_TARGETS = (".claude/skills", ".agents/skills")


def packaged_skills() -> List[Tuple[str, Any]]:
    """(name, resource-dir) for each skill in the bundled pack."""
    root = files("xuse.skills_pack")
    return sorted(
        (d.name, d)
        for d in root.iterdir()
        if d.is_dir() and (d / "SKILL.md").is_file()
    )


def install_skills(home: Optional[Path] = None, force: bool = False) -> Dict[str, List[str]]:
    """Copy every bundled skill into each client dir under `home`."""
    home = Path(home) if home else Path.home()
    installed: List[str] = []
    skipped: List[str] = []
    for name, src in packaged_skills():
        for target in SKILL_TARGETS:
            dest_dir = home / target / name
            dest_file = dest_dir / "SKILL.md"
            if dest_file.exists() and not force:
                skipped.append(str(dest_file))
                continue
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest_file.write_text((src / "SKILL.md").read_text(encoding="utf-8"),
                                 encoding="utf-8")
            installed.append(str(dest_file))
    return {"installed": installed, "skipped": skipped}


def list_skills(home: Optional[Path] = None) -> Dict[str, Dict[str, bool]]:
    """Per skill, per client dir: is it installed under `home`?"""
    home = Path(home) if home else Path.home()
    return {
        name: {target: (home / target / name / "SKILL.md").is_file()
               for target in SKILL_TARGETS}
        for name, _ in packaged_skills()
    }
