"""`x-use skills install` degrades to a friendly message + exit 1 when the
bundled pack is missing entirely (broken/partial install), instead of dying
with a raw traceback.
"""
from typer.testing import CliRunner

import xuse.skills_installer as installer
from xuse.cli import app

runner = CliRunner()


def test_skills_install_missing_pack_exits_1_with_friendly_message(monkeypatch):
    def boom(force=False):
        raise ModuleNotFoundError("No module named 'xuse.skills_pack'")

    monkeypatch.setattr(installer, "install_skills", boom)

    result = runner.invoke(app, ["skills", "install"])
    assert result.exit_code == 1
    assert "broken install" in result.output.lower()
    assert "Traceback" not in result.output


def test_skills_install_unexpected_error_also_exits_1(monkeypatch):
    def boom(force=False):
        raise TypeError("xuse.skills_pack is not a package")

    monkeypatch.setattr(installer, "install_skills", boom)

    result = runner.invoke(app, ["skills", "install"])
    assert result.exit_code == 1
    assert "Traceback" not in result.output


def test_skills_install_success_path_unchanged(monkeypatch, tmp_path):
    monkeypatch.setattr(
        installer, "install_skills",
        lambda force=False: {"installed": [str(tmp_path / "x/SKILL.md")], "skipped": []})

    result = runner.invoke(app, ["skills", "install"])
    assert result.exit_code == 0
    assert "installed:" in result.output
