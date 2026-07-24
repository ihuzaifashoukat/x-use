"""The bundled skills pack: 5 skills, valid frontmatter, size-bounded."""
from importlib.resources import files

EXPECTED = {"x-use", "x-use-setup", "x-use-engage", "x-use-content", "x-use-review"}


def _skill_dirs():
    return {d.name: d for d in files("xuse.skills_pack").iterdir()
            if d.is_dir() and (d / "SKILL.md").is_file()}


def test_pack_contains_exactly_the_five_skills():
    assert set(_skill_dirs()) == EXPECTED


def test_every_skill_has_valid_frontmatter_and_is_short():
    for name, d in _skill_dirs().items():
        text = (d / "SKILL.md").read_text(encoding="utf-8")
        lines = text.splitlines()
        assert lines[0].strip() == "---", f"{name}: frontmatter must start at line 1"
        end = lines[1:].index("---") + 1
        frontmatter = lines[1:end]
        assert any(l.startswith(f"name: {name}") for l in frontmatter), \
            f"{name}: frontmatter name must equal the directory name"
        assert any(l.startswith("description: ") for l in frontmatter), \
            f"{name}: description is required for skill discovery"
        assert len(lines) < 150, f"{name}: keep skills under 150 lines"
