"""The bundled skills pack: 5 skills, parseable frontmatter, size-bounded.

The frontmatter tests parse real YAML rather than matching line prefixes. An
earlier version only checked `line.startswith("description: ")`, which happily
accepted a description containing ": ". That is a mapping to a YAML parser, and
it shipped eleven files no skill consumer could load.
"""
import glob
from importlib.resources import files
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml", reason="pyyaml is a dev dependency")

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {"x-use", "x-use-setup", "x-use-engage", "x-use-content", "x-use-review"}

# agentskills.io spec plus Anthropic's platform constraints.
MAX_NAME = 64
MAX_DESCRIPTION = 1024
RESERVED_IN_NAME = ("anthropic", "claude")


def _skill_dirs():
    return {d.name: d for d in files("xuse.skills_pack").iterdir()
            if d.is_dir() and (d / "SKILL.md").is_file()}


def split_frontmatter(text: str, label: str) -> str:
    lines = text.splitlines()
    assert lines[0].strip() == "---", f"{label}: frontmatter must start at line 1"
    assert "---" in lines[1:], f"{label}: frontmatter is never closed"
    return "\n".join(lines[1:lines[1:].index("---") + 1])


def every_skill_file():
    """Every SKILL.md that ships anywhere: the pack, the plugin mirror, and root."""
    paths = [ROOT / "SKILL.md"]
    paths += [Path(p) for p in sorted(glob.glob(str(ROOT / "src/xuse/skills_pack/*/SKILL.md")))]
    paths += [Path(p) for p in sorted(glob.glob(str(ROOT / "plugins/x-use/skills/*/SKILL.md")))]
    return paths


def _ids(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def test_pack_contains_exactly_the_five_skills():
    assert set(_skill_dirs()) == EXPECTED


@pytest.mark.parametrize("path", every_skill_file(), ids=_ids)
def test_frontmatter_is_valid_yaml(path):
    """The regression that broke every shipped skill: a ": " inside an unquoted
    description makes YAML read it as a nested mapping and reject the document."""
    label = _ids(path)
    raw = split_frontmatter(path.read_text(encoding="utf-8"), label)
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f" at line {mark.line + 1} column {mark.column + 1}" if mark else ""
        pytest.fail(f"{label}: frontmatter is not valid YAML{where}: {getattr(e, 'problem', e)}")
    assert isinstance(data, dict), f"{label}: frontmatter must parse to a mapping"
    assert isinstance(data.get("name"), str), f"{label}: name must be a string"
    assert isinstance(data.get("description"), str), f"{label}: description must be a string"


@pytest.mark.parametrize("path", every_skill_file(), ids=_ids)
def test_frontmatter_respects_the_agent_skills_spec(path):
    label = _ids(path)
    data = yaml.safe_load(split_frontmatter(path.read_text(encoding="utf-8"), label))
    name, description = data["name"], data["description"]

    assert 1 <= len(name) <= MAX_NAME, f"{label}: name must be 1-{MAX_NAME} chars"
    assert name == name.lower(), f"{label}: name must be lowercase"
    assert all(c.isalnum() or c == "-" for c in name), \
        f"{label}: name may only contain letters, digits, and hyphens"
    for reserved in RESERVED_IN_NAME:
        assert reserved not in name, f"{label}: name may not contain '{reserved}'"
    assert len(description) <= MAX_DESCRIPTION, \
        f"{label}: description exceeds {MAX_DESCRIPTION} chars"
    assert "<" not in name and "<" not in description, f"{label}: no XML tags in frontmatter"


def test_skill_name_matches_its_directory():
    """Directory-scoped skills are addressed by folder name; a mismatch makes the
    skill unaddressable. The root SKILL.md is exempt, its parent is the clone dir."""
    for name, d in _skill_dirs().items():
        data = yaml.safe_load(
            split_frontmatter((d / "SKILL.md").read_text(encoding="utf-8"), name))
        assert data["name"] == name, f"{name}: frontmatter name must equal the directory name"


def test_skills_stay_short_enough_to_stay_in_context():
    for name, d in _skill_dirs().items():
        lines = (d / "SKILL.md").read_text(encoding="utf-8").splitlines()
        assert len(lines) < 150, f"{name}: keep skills under 150 lines"


def test_no_em_dashes_in_the_shipped_skills():
    """House style. Removing em dashes is also what introduced the colons that
    broke the YAML, so both rules are pinned here to stop one fix breaking the other."""
    for path in every_skill_file():
        assert "—" not in path.read_text(encoding="utf-8"), \
            f"{_ids(path)}: contains an em dash"
