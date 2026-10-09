#!/usr/bin/env python3
"""Install the project, uv and its browser with one local Python command.

Requires Python 3.10+ with venv/ensurepip support. No packages are installed
into the caller's Python. Existing uv is reused; otherwise a pinned uv wheel
is installed into .cache/uv-bootstrap. Project dependencies use uv.lock when
present. A fresh environment reuses the invoking Python when it is Python
3.10-3.14; otherwise it uses Python 3.12. Existing .venv Python and user
configuration are preserved. Linux OS packages are opt-in only.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional, Sequence

# Deliberately pin the bootstrap tool; update alongside lock/tooling review.
UV_VERSION = "0.12.23"
DEFAULT_PYTHON = "3.12"
MIN_VALIDATED_PYTHON = (3, 10)
MAX_VALIDATED_PYTHON = (3, 14)
DEFAULT_SETTINGS = {
    "mcp": {
        "browser_backend": "patchright",
        "browser_headless": True,
        "draft_mode": True,
    }
}


class SetupError(Exception):
    """An actionable setup failure, without command/environment disclosure."""


def _config_root(root: Path) -> Path:
    """Return the same configuration root that the installed package will use."""
    configured = os.environ.get("X_USE_HOME")
    if not configured:
        return root
    path = Path(configured).expanduser()
    if not path.is_absolute() or "${" in configured:
        raise SetupError("X_USE_HOME must be an absolute local directory path.")
    if path.exists() and not path.is_dir():
        raise SetupError("X_USE_HOME must name a directory, not a file.")
    return path.absolute()


def _browser_backend(config_root: Path) -> str:
    """Read the backend before installation, without importing project packages."""
    path = config_root / "config" / "settings.json"
    if not path.exists():
        return "patchright"
    try:
        settings = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError):
        raise SetupError("config/settings.json is unreadable or contains invalid JSON. "
                         "Correct it before rerunning setup.") from None
    if not isinstance(settings, dict):
        raise SetupError("config/settings.json must contain a JSON object.")
    mcp = settings.get("mcp")
    mcp = {} if mcp is None else mcp
    if not isinstance(mcp, dict):
        raise SetupError("config/settings.json mcp must be a JSON object.")
    backend = mcp.get("browser_backend", "patchright")
    if backend not in ("patchright", "playwright", "selenium"):
        raise SetupError("mcp.browser_backend must be patchright, playwright, or selenium.")
    return backend


def _environment(root: Path) -> dict[str, str]:
    environment = dict(os.environ)
    for name in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
        environment.pop(name, None)
    environment.update(PYTHONNOUSERSITE="1", PYTHONUTF8="1",
                       UV_PROJECT_ENVIRONMENT=str(root / ".venv"))
    return environment


def _run(command: Sequence[str], root: Path, environment: dict[str, str],
         *, stage: str, hint: str, allow_failure: bool = False,
         stdio: bool = False) -> int:
    print(f"==> {stage}", file=sys.stderr, flush=True)
    try:
        result = subprocess.run(list(command), cwd=str(root), env=environment,
                                check=False, shell=False,
                                stdout=None if stdio else sys.stderr)
    except OSError:
        raise SetupError(f"{stage} could not start. {hint}") from None
    if result.returncode and not allow_failure:
        raise SetupError(f"{stage} failed (exit {result.returncode}). {hint}")
    return result.returncode


def _venv_python(directory: Path, platform_name: str) -> Path:
    return directory / ("Scripts/python.exe" if platform_name == "win32" else "bin/python")


def _python_target(requested: Optional[str], environment_python: Path) -> str:
    """Select an explicit target, retained environment, or validated caller Python."""
    if requested:
        return requested
    if environment_python.is_file():
        return str(environment_python)
    current = sys.version_info[:2]
    if current[0] == 3 and MIN_VALIDATED_PYTHON <= current <= MAX_VALIDATED_PYTHON:
        return sys.executable
    return DEFAULT_PYTHON


def _inside_project(root: Path, path: Path) -> None:
    # Do not silently replace an environment/cache symlink pointing elsewhere.
    if not path.resolve().is_relative_to(root):
        raise SetupError("The setup environment or cache points outside this checkout. "
                         "Choose a checkout with local .venv and .cache directories.")


def _ensure_uv(root: Path, environment: dict[str, str], platform_name: str) -> str:
    installed = shutil.which("uv")
    if installed:
        return installed
    bootstrap = root / ".cache" / "uv-bootstrap"
    _inside_project(root, bootstrap)
    python = _venv_python(bootstrap, platform_name)
    binary = python.parent / ("uv.exe" if platform_name == "win32" else "uv")
    if not python.is_file():
        if bootstrap.exists():
            raise SetupError("The local uv bootstrap environment is incomplete. "
                             "Repair .cache/uv-bootstrap before rerunning setup.")
        _run([sys.executable, "-m", "venv", str(bootstrap)], root, environment,
             stage="Create an isolated uv bootstrap environment",
             hint="Install Python with venv/ensurepip support, then rerun this command.")
    if not python.is_file():
        raise SetupError("The uv bootstrap Python was not created. Check filesystem permissions.")
    _run([str(python), "-m", "pip", "install", "--disable-pip-version-check",
          "--only-binary=:all:", f"uv=={UV_VERSION}"], root, environment,
         stage="Install uv into the local bootstrap environment",
         hint="Check your package-index connection and certificate/proxy configuration, then rerun.")
    if not binary.is_file():
        raise SetupError("The uv executable was not created. Repair the local bootstrap environment.")
    return str(binary)


def _bootstrap_config(root: Path, config_root: Optional[Path] = None) -> None:
    config_root = config_root or root
    for source_name, destination_name, default_bytes in (
        (".env.example", ".env", None),
        ("config/accounts.example.json", "config/accounts.json", None),
        (None, "config/settings.json",
         (json.dumps(DEFAULT_SETTINGS, indent=2) + "\n").encode("utf-8")),
    ):
        source = root / source_name if source_name else None
        destination = config_root / destination_name
        if destination.exists() or (source is not None and not source.is_file()):
            continue
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Exclusive creation never overwrites existing user configuration,
            # even when another installer creates it concurrently.
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(source.read_bytes() if source is not None else default_bytes)
        except FileExistsError:
            continue
        except OSError:
            raise SetupError("Sample configuration could not be created. Check filesystem permissions.") from None
        print(f"==> Created {destination_name}; existing configuration is retained", file=sys.stderr, flush=True)


def setup(root: Path, *, dev: bool = False, python: Optional[str] = None,
          with_system_deps: bool = False, skip_browser: bool = False,
          strict_doctor: bool = False, start_mcp: bool = False,
          platform_name: Optional[str] = None) -> int:
    """Perform an idempotent local installation; never start MCP by default."""
    if sys.version_info < (3, 10):
        raise SetupError("Setup requires Python 3.10 or newer.")
    root = root.resolve()
    platform_name = platform_name or sys.platform
    if platform_name not in ("win32", "darwin", "linux"):
        raise SetupError("This setup supports Windows, macOS, and Linux.")
    if not (root / "pyproject.toml").is_file() or not (root / "src/xuse/__init__.py").is_file():
        raise SetupError("Run this script from a complete x-use checkout containing pyproject.toml and src/xuse.")
    if with_system_deps and platform_name != "linux":
        raise SetupError("--with-system-deps is only needed on Linux; omit it on this platform.")
    if with_system_deps and skip_browser:
        raise SetupError("--with-system-deps and --skip-browser cannot be combined.")
    if python is not None and (not python.strip() or python.startswith("-")):
        raise SetupError("--python must name a Python version or interpreter.")
    config_root = _config_root(root)
    backend = _browser_backend(config_root)
    if backend == "selenium" and with_system_deps:
        raise SetupError("--with-system-deps applies to Patchright or Playwright. "
                         "Install the selected Selenium browser and driver separately.")
    environment_dir = root / ".venv"
    _inside_project(root, environment_dir)
    environment_python = _venv_python(environment_dir, platform_name)
    if environment_dir.exists() and not environment_python.is_file():
        raise SetupError("The existing .venv is incomplete. Repair it before running setup; it will not be overwritten.")
    if python and environment_python.is_file():
        raise SetupError("An existing .venv is retained. Omit --python to reuse it, or create a fresh checkout for another interpreter.")
    requested_python = _python_target(python, environment_python)
    environment = _environment(root)
    uv = _ensure_uv(root, environment, platform_name)
    command = [uv, "sync", "--project", str(root), "--python", requested_python,
               "--no-dev", "--no-default-groups"]
    if (root / "uv.lock").is_file():
        command.append("--locked")
    else:
        print("==> No uv.lock supplied; uv will resolve dependencies and create it once", file=sys.stderr, flush=True)
    if dev:
        command.extend(["--extra", "dev"])
    if backend == "playwright":
        command.extend(["--extra", "playwright"])
    _run(command, root, environment, stage="Synchronize the project environment with uv",
         hint="Check network access and the lockfile; update uv if an option is unsupported. "
              "For changed project metadata, run `uv lock` before rerunning setup. "
              "On Windows, close running x-use, Python, and browser-test processes that may lock "
              "environment files, then rerun the same setup command.")
    if not environment_python.is_file():
        raise SetupError("uv did not create the project Python in .venv. Check your uv version and filesystem permissions.")
    if not skip_browser and backend != "selenium":
        browser = [str(environment_python), "-m", backend, "install"]
        if with_system_deps:
            browser.append("--with-deps")
        browser.append("chromium")
        product = "Patchright" if backend == "patchright" else "Playwright"
        _run(browser, root, environment, stage=f"Install the browser matching the project's {product} version",
             hint="Check your browser-download connection, available disk space, and platform support, then rerun.")
    elif backend == "selenium":
        print("==> Selenium selected; doctor will check its separately installed browser and driver", file=sys.stderr, flush=True)
    if platform_name == "linux" and backend != "selenium" and not with_system_deps and not skip_browser:
        print("==> If browser shared libraries are missing, review OS package permissions and rerun with --with-system-deps", file=sys.stderr, flush=True)
    _bootstrap_config(root, config_root)
    doctor_status = _run([str(environment_python), "-m", "xuse.cli", "doctor"], root, environment,
                         stage="Run x-use doctor", hint="Fix the reported FAIL rows and rerun doctor.",
                         allow_failure=True)
    if doctor_status:
        print("==> Runtime installed; doctor reported configuration or environment issues. "
              "Complete account setup with `.venv/bin/x-use init` (Windows: `.venv\\Scripts\\x-use.exe init`) "
              "and fix the FAIL rows.", file=sys.stderr, flush=True)
        if strict_doctor or start_mcp:
            return doctor_status
    else:
        print("==> Installation and doctor checks completed successfully", file=sys.stderr, flush=True)
    if start_mcp:
        return _run([str(environment_python), "-m", "xuse.cli", "mcp"], root, environment,
                    stage="Start the MCP stdio server (runs until the client disconnects)",
                    hint="Review the MCP error output and server configuration.", allow_failure=True, stdio=True)
    print("==> Setup finished; the x-use command is available in .venv", file=sys.stderr, flush=True)
    return 0


def main(argv: Optional[Sequence[str]] = None, *, project_root: Optional[Path] = None) -> int:
    parser = argparse.ArgumentParser(description="Install uv, the project environment, and Chromium with one command.")
    parser.add_argument("--dev", action="store_true", help="Include the project's development extra.")
    parser.add_argument("--python", help="Python version/interpreter for a fresh environment (default: the invoking Python when 3.10-3.14, otherwise 3.12; existing .venv is reused).")
    parser.add_argument("--with-system-deps", action="store_true",
                        help="Allow the selected browser driver to install Linux OS packages; this may request administrator permission.")
    parser.add_argument("--skip-browser", action="store_true", help="Reuse an already-installed browser; omit the browser download.")
    parser.add_argument("--strict-doctor", action="store_true", help="Return a failure status when doctor reports incomplete configuration.")
    parser.add_argument("--start-mcp", action="store_true", help="Start the MCP stdio server after successful doctor checks.")
    args = parser.parse_args(argv)
    try:
        return setup(project_root or Path(__file__).resolve().parents[1], **vars(args))
    except SetupError as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 1
    except OSError:
        print("error: Setup could not access a required file. Check filesystem permissions and rerun.",
              file=sys.stderr, flush=True)
        return 1
    except KeyboardInterrupt:
        print("Setup interrupted. Rerun the same command to resume.", file=sys.stderr, flush=True)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
