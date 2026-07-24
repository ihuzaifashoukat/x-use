"""Skills installer: copies the pack into Claude + Codex skill dirs under a
given HOME; never overwrites without force; list reflects reality."""
from xuse import skills_installer as si


def test_install_copies_all_skills_to_both_targets(tmp_path):
    result = si.install_skills(home=tmp_path)
    names = {name for name, _ in si.packaged_skills()}
    assert names  # sanity: pack exists
    for name in names:
        for target in si.SKILL_TARGETS:
            skill_file = tmp_path / target / name / "SKILL.md"
            assert skill_file.is_file()
            assert f"name: {name}" in skill_file.read_text(encoding="utf-8")
    assert len(result["installed"]) == len(names) * len(si.SKILL_TARGETS)
    assert result["skipped"] == []


def test_second_install_skips_without_force(tmp_path):
    si.install_skills(home=tmp_path)
    second = si.install_skills(home=tmp_path)
    assert second["installed"] == []
    assert len(second["skipped"]) == len(si.packaged_skills()) * len(si.SKILL_TARGETS)


def test_force_overwrites_local_edits(tmp_path):
    si.install_skills(home=tmp_path)
    victim = tmp_path / si.SKILL_TARGETS[0] / "x-use" / "SKILL.md"
    victim.write_text("user edit", encoding="utf-8")
    si.install_skills(home=tmp_path, force=True)
    assert victim.read_text(encoding="utf-8") != "user edit"


def test_list_skills_reports_presence(tmp_path):
    si.install_skills(home=tmp_path)
    state = si.list_skills(home=tmp_path)
    assert all(all(targets.values()) for targets in state.values())
    empty = si.list_skills(home=tmp_path / "elsewhere")
    assert all(not any(targets.values()) for targets in empty.values())
