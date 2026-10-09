"""Cross-platform uv setup orchestration without downloads or installations."""
import importlib.util
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/setup_uv.py"
SPEC = importlib.util.spec_from_file_location("xuse_setup_uv_test_module", SCRIPT)
setup_uv = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup_uv)


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project with spaces"
    (root / "src/xuse").mkdir(parents=True)
    (root / "src/xuse/__init__.py").write_text('"""Test project marker."""', encoding="utf-8")
    (root / "pyproject.toml").write_text('[project]\nname = "x-use-mcp"\n', encoding="utf-8")
    (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (root / "config").mkdir()
    (root / ".env.example").write_text("OPENAI_API_KEY=\n", encoding="utf-8")
    (root / "config/accounts.example.json").write_text('[{"account_id":"sample","is_active":false}]', encoding="utf-8")
    return root


class FakeCommands:
    """Model environment creation and ordered subprocess outcomes."""

    def __init__(self, root, platform="linux", doctor_status=0, fail=None):
        self.root, self.platform, self.doctor_status, self.fail = root, platform, doctor_status, fail
        self.calls = []

    def python(self, directory):
        path = setup_uv._venv_python(directory, self.platform)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"test Python marker")
        return path

    def __call__(self, command, **kwargs):
        assert isinstance(command, list)
        assert kwargs["shell"] is False and kwargs["check"] is False
        assert kwargs["cwd"] == str(self.root)
        assert kwargs["env"]["UV_PROJECT_ENVIRONMENT"] == str(self.root / ".venv")
        self.calls.append((command, kwargs))
        if self.fail is not None and self.fail(command):
            return SimpleNamespace(returncode=7)
        if command[1:3] == ["-m", "venv"]:
            self.python(Path(command[3]))
        elif command[1:4] == ["-m", "pip", "install"]:
            binary = Path(command[0]).parent / ("uv.exe" if self.platform == "win32" else "uv")
            binary.write_bytes(b"test uv marker")
        elif len(command) > 1 and command[1] == "sync":
            self.python(self.root / ".venv")
            if not (self.root / "uv.lock").exists():
                (self.root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
        elif command[1:] == ["-m", "xuse.cli", "doctor"]:
            return SimpleNamespace(returncode=self.doctor_status)
        return SimpleNamespace(returncode=0)


def fake_commands(monkeypatch, root, *, platform="linux", uv="/trusted tools/uv", **kwargs):
    runner = FakeCommands(root, platform, **kwargs)
    monkeypatch.setattr(setup_uv.shutil, "which", lambda name: uv)
    monkeypatch.setattr(setup_uv.subprocess, "run", runner)
    return runner


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
def test_one_command_bootstraps_uv_and_runtime_with_safe_paths_and_no_admin(monkeypatch, project, platform):
    monkeypatch.setattr(setup_uv.sys, "version_info", (3, 12, 0))
    monkeypatch.setattr(setup_uv.sys, "executable", "/validated/python")
    runner = fake_commands(monkeypatch, project, platform=platform, uv=None)
    assert setup_uv.setup(project, platform_name=platform) == 0
    commands = [command for command, _ in runner.calls]
    assert commands[0][1:3] == ["-m", "venv"]
    assert commands[0][-1] == str(project / ".cache/uv-bootstrap")
    assert commands[1][1:4] == ["-m", "pip", "install"]
    assert commands[1][-1] == f"uv=={setup_uv.UV_VERSION}"
    assert "--only-binary=:all:" in commands[1]
    sync = commands[2]
    assert sync[1] == "sync" and "--locked" in sync
    assert sync[sync.index("--project") + 1] == str(project)
    assert sync[sync.index("--python") + 1] == "/validated/python"
    assert "--no-dev" in sync and "--no-default-groups" in sync
    assert "--extra" not in sync
    assert commands[3][1:] == ["-m", "patchright", "install", "chromium"]
    assert commands[4][1:] == ["-m", "xuse.cli", "doctor"]
    assert not any("sudo" in command or "--with-deps" in command for command in commands)
    assert not any(command[-1] == "mcp" for command in commands)
    assert (project / ".env").read_text(encoding="utf-8") == "OPENAI_API_KEY=\n"
    assert json.loads((project / "config/settings.json").read_text(encoding="utf-8")) == setup_uv.DEFAULT_SETTINGS


@pytest.mark.parametrize("version", [(3, 10), (3, 11), (3, 12), (3, 13), (3, 14)])
def test_fresh_setup_prefers_validated_caller_python(monkeypatch, version, tmp_path):
    monkeypatch.setattr(setup_uv.sys, "version_info", (*version, 0))
    monkeypatch.setattr(setup_uv.sys, "executable", "C:/Python/python.exe")
    assert setup_uv._python_target(None, tmp_path / ".venv/Scripts/python.exe") == "C:/Python/python.exe"


@pytest.mark.parametrize("version", [(3, 9), (3, 15), (4, 0)])
def test_fresh_setup_falls_back_to_supported_python_for_unvalidated_caller(monkeypatch, version, tmp_path):
    monkeypatch.setattr(setup_uv.sys, "version_info", (*version, 0))
    monkeypatch.setattr(setup_uv.sys, "executable", "C:/Python/python.exe")
    assert setup_uv._python_target(None, tmp_path / ".venv/Scripts/python.exe") == "3.12"


def test_explicit_or_existing_environment_python_keeps_precedence(monkeypatch, tmp_path):
    monkeypatch.setattr(setup_uv.sys, "version_info", (3, 12, 0))
    monkeypatch.setattr(setup_uv.sys, "executable", "/validated/python")
    env_python = tmp_path / ".venv/Scripts/python.exe"
    env_python.parent.mkdir(parents=True)
    env_python.write_bytes(b"python marker")
    assert setup_uv._python_target("3.13", env_python) == "3.13"
    assert setup_uv._python_target(None, env_python) == str(env_python)


def test_existing_uv_and_environment_are_reused_and_user_config_is_retained(monkeypatch, project):
    runner = fake_commands(monkeypatch, project)
    python = runner.python(project / ".venv")
    (project / ".env").write_text("PRIVATE_EXISTING_ENV", encoding="utf-8")
    (project / "config/accounts.json").write_text("PRIVATE_EXISTING_ACCOUNTS", encoding="utf-8")
    (project / "config/settings.json").write_text('{"mcp":{"browser_backend":"patchright"},"operator":"preserve"}', encoding="utf-8")
    for _ in range(2):
        assert setup_uv.setup(project, platform_name="linux") == 0
    commands = [command for command, _ in runner.calls]
    syncs = [command for command in commands if len(command) > 1 and command[1] == "sync"]
    assert len(syncs) == 2
    assert all(command[command.index("--python") + 1] == str(python) for command in syncs)
    assert not any(command[1:3] == ["-m", "venv"] for command in commands)
    assert (project / ".env").read_text(encoding="utf-8") == "PRIVATE_EXISTING_ENV"
    assert (project / "config/accounts.json").read_text(encoding="utf-8") == "PRIVATE_EXISTING_ACCOUNTS"
    assert (project / "config/settings.json").read_text(encoding="utf-8") == '{"mcp":{"browser_backend":"patchright"},"operator":"preserve"}'


def test_explicit_data_home_controls_backend_and_sample_config_destinations(monkeypatch, project, tmp_path):
    data_home = tmp_path / "external account data"
    (data_home / "config").mkdir(parents=True)
    (data_home / "config/settings.json").write_text(
        '{"mcp":{"browser_backend":"playwright"}}', encoding="utf-8")
    monkeypatch.setenv("X_USE_HOME", str(data_home))
    runner = fake_commands(monkeypatch, project)

    assert setup_uv.setup(project, platform_name="linux", skip_browser=True) == 0

    sync = next(command for command, _ in runner.calls if command[1] == "sync")
    assert sync[-2:] == ["--extra", "playwright"]
    assert (data_home / ".env").is_file()
    assert (data_home / "config/accounts.json").is_file()
    assert json.loads((data_home / "config/settings.json").read_text(encoding="utf-8")) == {
        "mcp": {"browser_backend": "playwright"}
    }
    assert "playwright" in (data_home / "config/settings.json").read_text(encoding="utf-8")
    assert not (project / ".env").exists()
    assert not (project / "config/accounts.json").exists()


@pytest.mark.parametrize("value", ["relative/data", "${X_USE_HOME}"])
def test_invalid_explicit_data_home_fails_before_install(monkeypatch, project, value):
    monkeypatch.setenv("X_USE_HOME", value)
    runner = fake_commands(monkeypatch, project)
    with pytest.raises(setup_uv.SetupError, match="X_USE_HOME must be an absolute"):
        setup_uv.setup(project, platform_name="linux")
    assert runner.calls == []


@pytest.mark.skipif(os.name == "nt", reason="Windows configuration privacy uses directory ACLs")
def test_new_configuration_is_private_at_creation(project):
    setup_uv._bootstrap_config(project)
    for path in (project / ".env", project / "config/accounts.json", project / "config/settings.json"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_external_data_home_gets_safe_settings_without_copying_checkout_config(project, tmp_path):
    (project / "config/settings.json").write_text(
        '{"llm":{"api_key":"DO_NOT_COPY_CHECKOUT_SECRET"}}', encoding="utf-8")
    data_home = tmp_path / "fresh private data"

    setup_uv._bootstrap_config(project, data_home)

    rendered = (data_home / "config/settings.json").read_text(encoding="utf-8")
    assert json.loads(rendered) == setup_uv.DEFAULT_SETTINGS
    assert "DO_NOT_COPY_CHECKOUT_SECRET" not in rendered


def test_development_extra_and_linux_system_packages_require_explicit_flags(monkeypatch, project):
    runner = fake_commands(monkeypatch, project)
    assert setup_uv.setup(project, dev=True, with_system_deps=True,
                          python="3.14", platform_name="linux") == 0
    commands = [command for command, _ in runner.calls]
    assert commands[0][-2:] == ["--extra", "dev"]
    assert commands[0][commands[0].index("--python") + 1] == "3.14"
    assert commands[1][1:] == ["-m", "patchright", "install", "--with-deps", "chromium"]


@pytest.mark.parametrize("backend", ["patchright", "playwright", "selenium"])
@pytest.mark.parametrize("dev", [False, True])
def test_selected_backend_controls_extra_and_browser_installer(monkeypatch, project, backend, dev):
    settings = project / "config/settings.json"
    settings.write_text(json.dumps({"mcp": {"browser_backend": backend}}), encoding="utf-8")
    runner = fake_commands(monkeypatch, project)
    assert setup_uv.setup(project, platform_name="linux", dev=dev) == 0
    commands = [command for command, _ in runner.calls]
    sync = commands[0]
    extras = [sync[index + 1] for index, value in enumerate(sync) if value == "--extra"]
    assert extras == (["dev"] if dev else []) + (["playwright"] if backend == "playwright" else [])
    browser_installs = [command for command in commands if command[1:2] == ["-m"]
                        and command[2:3] in (["patchright"], ["playwright"])]
    if backend == "selenium":
        assert browser_installs == []
    else:
        assert browser_installs == [[str(runner.python(project / ".venv")), "-m", backend, "install", "chromium"]]
    assert json.loads(settings.read_text(encoding="utf-8"))["mcp"]["browser_backend"] == backend


def test_explicit_playwright_extra_is_installed_even_when_browser_download_is_skipped(monkeypatch, project):
    (project / "config/settings.json").write_text('{"mcp":{"browser_backend":"playwright"}}', encoding="utf-8")
    runner = fake_commands(monkeypatch, project)
    assert setup_uv.setup(project, platform_name="linux", skip_browser=True) == 0
    commands = [command for command, _ in runner.calls]
    assert commands[0][-2:] == ["--extra", "playwright"]
    assert not any(command[1:3] == ["-m", "playwright"] for command in commands)


@pytest.mark.parametrize("content", [
    '{"PRIVATE_COOKIE_SENTINEL":', '[]', '{"mcp":[]}',
    '{"mcp":{"browser_backend":"PRIVATE_COOKIE_SENTINEL"}}',
    '{"mcp":{"browser_backend":["patchright"]}}',
])
def test_malformed_backend_configuration_fails_safely_before_any_install(monkeypatch, project, content, capsys):
    (project / "config/settings.json").write_text(content, encoding="utf-8")
    runner = fake_commands(monkeypatch, project)
    assert setup_uv.main([], project_root=project) == 1
    assert runner.calls == []
    output = capsys.readouterr().err
    assert "error:" in output and "PRIVATE_COOKIE_SENTINEL" not in output


def test_legacy_system_dependency_flag_is_rejected_before_any_install(monkeypatch, project):
    (project / "config/settings.json").write_text('{"mcp":{"browser_backend":"selenium"}}', encoding="utf-8")
    runner = fake_commands(monkeypatch, project)
    with pytest.raises(setup_uv.SetupError, match="applies to Patchright or Playwright"):
        setup_uv.setup(project, platform_name="linux", with_system_deps=True)
    assert runner.calls == []


def test_first_resolution_creates_lock_and_later_runs_assert_it(monkeypatch, project):
    (project / "uv.lock").unlink()
    runner = fake_commands(monkeypatch, project)
    setup_uv.setup(project, platform_name="linux", skip_browser=True)
    setup_uv.setup(project, platform_name="linux", skip_browser=True)
    syncs = [command for command, _ in runner.calls if command[1] == "sync"]
    assert "--locked" not in syncs[0]
    assert "--locked" in syncs[1]
    assert (project / "uv.lock").is_file()
    assert not any(command[1:3] in (["-m", "patchright"], ["-m", "playwright"])
                   for command, _ in runner.calls)


@pytest.mark.parametrize("stage", ["bootstrap", "uv_install", "sync", "browser"])
def test_failed_setup_step_stops_later_commands_with_actionable_error(monkeypatch, project, stage):
    predicates = {
        "bootstrap": lambda command: command[1:3] == ["-m", "venv"],
        "uv_install": lambda command: command[1:4] == ["-m", "pip", "install"],
        "sync": lambda command: len(command) > 1 and command[1] == "sync",
        "browser": lambda command: "patchright" in command,
    }
    runner = fake_commands(monkeypatch, project, uv=None if stage in ("bootstrap", "uv_install") else "uv",
                           fail=predicates[stage])
    with pytest.raises(setup_uv.SetupError, match="failed.*exit 7"):
        setup_uv.setup(project, platform_name="linux")
    assert not any(command[-1] == "doctor" for command, _ in runner.calls)
    assert predicates[stage](runner.calls[-1][0])


def test_sync_failure_explains_windows_locked_file_recovery_without_killing_processes(monkeypatch, project):
    runner = fake_commands(monkeypatch, project, platform="win32", fail=lambda command: command[1] == "sync")
    with pytest.raises(setup_uv.SetupError, match="close running x-use, Python, and browser-test processes"):
        setup_uv.setup(project, platform_name="win32")
    assert len(runner.calls) == 1


def test_doctor_issues_are_reported_and_strict_or_start_flags_return_failure(monkeypatch, project, capsys):
    runner = fake_commands(monkeypatch, project, doctor_status=1)
    assert setup_uv.setup(project, platform_name="linux") == 0
    assert "doctor reported configuration or environment issues" in capsys.readouterr().err
    assert setup_uv.setup(project, platform_name="linux", strict_doctor=True) == 1
    assert setup_uv.setup(project, platform_name="linux", start_mcp=True) == 1
    assert not any(command[-1] == "mcp" for command, _ in runner.calls)


def test_mcp_start_is_explicit_and_preserves_clean_stdio(monkeypatch, project, capsys):
    runner = fake_commands(monkeypatch, project)
    assert setup_uv.setup(project, platform_name="linux", start_mcp=True) == 0
    commands = [command for command, _ in runner.calls]
    assert commands[-2][1:] == ["-m", "xuse.cli", "doctor"]
    assert commands[-1][1:] == ["-m", "xuse.cli", "mcp"]
    assert all(kwargs["stdout"] is not None for _, kwargs in runner.calls[:-1])
    assert runner.calls[-1][1]["stdout"] is None
    assert capsys.readouterr().out == ""


def test_external_active_environment_cannot_redirect_project_install(monkeypatch, project):
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", "outside-project")
    monkeypatch.setenv("VIRTUAL_ENV", "outside-venv")
    monkeypatch.setenv("PYTHONPATH", "unrelated-code")
    monkeypatch.setenv("PYTHONHOME", "unrelated-python")
    runner = fake_commands(monkeypatch, project)
    setup_uv.setup(project, platform_name="linux")
    for _, kwargs in runner.calls:
        assert kwargs["env"]["UV_PROJECT_ENVIRONMENT"] == str(project / ".venv")
        assert all(name not in kwargs["env"] for name in ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME"))


def test_incomplete_or_retargeted_existing_environment_is_never_overwritten(monkeypatch, project):
    runner = fake_commands(monkeypatch, project)
    (project / ".venv").mkdir()
    with pytest.raises(setup_uv.SetupError, match="incomplete"):
        setup_uv.setup(project, platform_name="linux")
    assert runner.calls == []
    runner.python(project / ".venv")
    with pytest.raises(setup_uv.SetupError, match="existing .venv is retained"):
        setup_uv.setup(project, platform_name="linux", python="3.12")
    assert runner.calls == []


def test_external_cache_and_environment_paths_are_rejected(project):
    with pytest.raises(setup_uv.SetupError, match="outside this checkout"):
        setup_uv._inside_project(project.resolve(), project.parent / "external-environment")


@pytest.mark.parametrize("options", [
    {"platform_name": "unsupported"},
    {"platform_name": "win32", "with_system_deps": True},
    {"platform_name": "linux", "with_system_deps": True, "skip_browser": True},
    {"platform_name": "linux", "python": "--system"},
])
def test_invalid_options_fail_before_installation(monkeypatch, project, options):
    runner = fake_commands(monkeypatch, project)
    with pytest.raises(setup_uv.SetupError):
        setup_uv.setup(project, **options)
    assert runner.calls == []


def test_os_command_error_never_echoes_credentials(monkeypatch, project, capsys):
    monkeypatch.setattr(setup_uv.shutil, "which", lambda _: "uv")

    def failed(*args, **kwargs):
        raise OSError("PRIVATE_COOKIE_SENTINEL")

    monkeypatch.setattr(setup_uv.subprocess, "run", failed)
    assert setup_uv.main([], project_root=project) == 1
    output = capsys.readouterr().err
    assert "could not start" in output and "PRIVATE_COOKIE_SENTINEL" not in output
