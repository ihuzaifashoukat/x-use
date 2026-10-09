"""Credential-free smoke orchestration regressions, without a browser."""
import importlib.util
import os
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ci_browser_smoke.py"
SPEC = importlib.util.spec_from_file_location("ci_browser_smoke", SCRIPT)
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


@pytest.mark.asyncio
@pytest.mark.parametrize("inherited_home", [None, "unrelated-account-home"])
async def test_package_smoke_canonicalizes_temp_root_and_isolates_home(
    tmp_path, monkeypatch, inherited_home
):
    from xuse.core import local_state

    # A noncanonical temp alias exposes the regression on every OS; real
    # symlink aliases are separately exercised where the OS permits them.
    nested = tmp_path / "nested"
    nested.mkdir()
    temporary = nested / ".."
    if inherited_home is None:
        monkeypatch.delenv("X_USE_HOME", raising=False)
    else:
        monkeypatch.setenv("X_USE_HOME", inherited_home)
    previous = Path.cwd()
    observed = []

    @contextmanager
    def temporary_directory(**kwargs):
        yield str(temporary)

    def package_check(expect_installed, driver):
        assert os.environ["X_USE_HOME"] == str(tmp_path.resolve())
        assert Path.cwd() == tmp_path.resolve()
        observed.append("package")
        return {"cli_help": "passed"}

    def prepare_private_file(path):
        # Reject a noncanonical path just as private_state_file rejects
        # /var on macOS before its symlinked ancestor can be traversed.
        assert path == tmp_path.resolve() / "private-fixture" / ".bootstrap"
        path.parent.mkdir()
        observed.append("private")
        return path

    async def protocol_check(directory, expect_installed, driver):
        assert directory == tmp_path.resolve() / "private-fixture"
        assert Path.cwd() == directory
        assert os.environ["X_USE_HOME"] == str(directory)
        observed.append("protocol")
        return {"browser_started": False}

    def init_check(directory):
        assert directory == tmp_path.resolve() / "private-fixture"
        observed.append("init")
        return {"config_loader": "passed"}

    monkeypatch.setattr(smoke.tempfile, "TemporaryDirectory", temporary_directory)
    monkeypatch.setattr(smoke, "installed_package_check", package_check)
    monkeypatch.setattr(local_state, "private_state_file", prepare_private_file)
    monkeypatch.setattr(smoke, "package_protocol_check", protocol_check)
    monkeypatch.setattr(smoke, "installed_init_check", init_check)
    report = {"status": "failed"}
    await smoke.run(SimpleNamespace(mode="package", expect_installed=False, driver="patchright"), report)

    assert observed == ["package", "private", "init", "protocol"]
    assert report["status"] == "passed" and "stage" not in report
    assert Path.cwd() == previous
    assert os.environ.get("X_USE_HOME") == inherited_home


@pytest.mark.asyncio
async def test_smoke_restores_process_state_when_package_check_fails(tmp_path, monkeypatch):
    previous = Path.cwd()
    monkeypatch.setenv("X_USE_HOME", "unrelated-account-home")

    @contextmanager
    def temporary_directory(**kwargs):
        yield str(tmp_path)

    def fail(*args):
        assert os.environ["X_USE_HOME"] == str(tmp_path.resolve())
        raise RuntimeError("synthetic package failure")

    monkeypatch.setattr(smoke.tempfile, "TemporaryDirectory", temporary_directory)
    monkeypatch.setattr(smoke, "installed_package_check", fail)
    with pytest.raises(RuntimeError, match="synthetic package failure"):
        await smoke.run(SimpleNamespace(mode="package", expect_installed=False, driver="patchright"), {})
    assert Path.cwd() == previous
    assert os.environ["X_USE_HOME"] == "unrelated-account-home"


@pytest.mark.asyncio
async def test_smoke_accepts_os_temp_symlink_without_weakening_private_state(tmp_path, monkeypatch):
    from xuse.core.local_state import PrivateStateError, private_state_file

    target = tmp_path / "ordinary"
    target.mkdir()
    alias = tmp_path / "temp-alias"
    try:
        alias.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlinks are unavailable for this user.")
    # The production guard must continue refusing user-supplied linked paths.
    with pytest.raises(PrivateStateError):
        private_state_file(alias / "refused")

    @contextmanager
    def temporary_directory(**kwargs):
        yield str(alias)

    async def protocol_check(directory, *args):
        assert directory == target.resolve() / "private-fixture"
        assert (directory / ".bootstrap").is_file()
        return {"browser_started": False}

    monkeypatch.setattr(smoke.tempfile, "TemporaryDirectory", temporary_directory)
    monkeypatch.setattr(smoke, "installed_package_check", lambda *args: {})
    monkeypatch.setattr(smoke, "installed_init_check", lambda *args: {})
    monkeypatch.setattr(smoke, "package_protocol_check", protocol_check)
    report = {}
    await smoke.run(SimpleNamespace(mode="package", expect_installed=False, driver="patchright"), report)
    assert report["status"] == "passed"
