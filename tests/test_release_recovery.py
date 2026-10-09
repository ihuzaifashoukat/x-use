"""Run the publication guard against real Git histories without external APIs."""
import io
import json
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/publish.yml").read_text(encoding="utf-8"))
GUARD_STEP = next(step for step in WORKFLOW["jobs"]["release_guard"]["steps"]
                  if step.get("id") == "release")
GUARD = GUARD_STEP["run"].split("python - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]


def git(repo, *arguments):
    return subprocess.check_output(["git", "-C", str(repo), *arguments],
                                   stderr=subprocess.PIPE).decode().strip()


def commit(repo, path, content="changed\n"):
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    git(repo, "add", "--", path)
    git(repo, "commit", "-m", "Synthetic correction")
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def release_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.name", "Release fixture")
    git(repo, "config", "user.email", "release@example.invalid")
    git(repo, "config", "commit.gpgsign", "false")
    for name, text in {
        "src/xuse/__init__.py": '__version__ = "3.0.0"\n',
        "src/xuse/runtime.py": "original runtime\n",
        "README.md": "original package description\n",
        "tests/test_fixture.py": "original fixture\n",
        ".github/workflows/ci.yml": "original verification\n",
        "pyproject.toml": "original packaging\n",
    }.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "Immutable release")
    git(repo, "tag", "-a", "v3.0.0", "-m", "Published release")
    return repo


def run_guard(repo, monkeypatch, *, event="workflow_dispatch", ref="refs/heads/main",
              tag="v3.0.0", release=None, unavailable=False, caller=None, default_branch="main"):
    monkeypatch.chdir(repo)
    environment = {
        "RELEASE_TAG": tag,
        "CALLER_SHA": caller or git(repo, "rev-parse", "HEAD"),
        "DEFAULT_BRANCH": default_branch,
        "GITHUB_EVENT_NAME": event,
        "GITHUB_REF": ref,
        "GITHUB_API_URL": "https://api.github.com",
        "GITHUB_REPOSITORY": "fixture/repository",
        "GH_TOKEN": "synthetic-token",
        "GITHUB_OUTPUT": str(repo.parent / "guard-output"),
    }
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    published = {"tag_name": "v3.0.0", "draft": False,
                 "published_at": "2026-10-09T00:00:00Z"}

    def published_release(request, timeout):
        assert request.full_url == "https://api.github.com/repos/fixture/repository/releases/tags/" + tag
        assert timeout == 30
        if unavailable:
            raise urllib.error.HTTPError(request.full_url, 404, "Not found", {}, None)
        return io.BytesIO(json.dumps(published if release is None else release).encode())

    monkeypatch.setattr(urllib.request, "urlopen", published_release)
    exec(compile(GUARD, "<publication guard>", "exec"), {"__name__": "__main__"})
    return dict(line.split("=", 1) for line in Path(environment["GITHUB_OUTPUT"]).read_text().splitlines())


@pytest.mark.parametrize("event,ref", [("release", "refs/tags/v3.0.0"),
                                      ("workflow_dispatch", "refs/heads/main")])
def test_guard_preserves_annotated_release_commit_with_allowed_corrections(release_repo, monkeypatch, event, ref):
    original = git(release_repo, "rev-parse", "v3.0.0^{commit}")
    for path in ("tests/test_fixture.py", "tests/new_fixture.py", "CHANGELOG.md",
                 "CONTRIBUTING.md", ".github/workflows/publish.yml"):
        commit(release_repo, path)
    assert git(release_repo, "rev-parse", "HEAD") != original
    assert run_guard(release_repo, monkeypatch, event=event, ref=ref) == {
        "release_sha": original, "release_tag": "v3.0.0", "version": "3.0.0",
    }
    assert git(release_repo, "rev-parse", "v3.0.0^{commit}") == original


@pytest.mark.parametrize("path", [
    "src/xuse/runtime.py", "src/xuse/__init__.py", "pyproject.toml", "uv.lock",
    "README.md", "server.json", ".github/workflows/ci.yml",
    ".github/workflows/another.yml", "scripts/release_guard.py", "config/accounts.example.json",
])
def test_guard_rejects_non_recovery_changes(release_repo, monkeypatch, path):
    commit(release_repo, path)
    with pytest.raises(SystemExit, match="forbidden paths"):
        run_guard(release_repo, monkeypatch)
    assert not (release_repo.parent / "guard-output").exists()


@pytest.mark.parametrize("operation", ["rename", "delete"])
def test_guard_cannot_hide_protected_files_as_tests_or_deletions(release_repo, monkeypatch, operation):
    if operation == "rename":
        git(release_repo, "mv", "src/xuse/runtime.py", "tests/moved_runtime.py")
    else:
        git(release_repo, "rm", "src/xuse/runtime.py")
    git(release_repo, "commit", "-m", "Forbidden removal")
    with pytest.raises(SystemExit, match="src/xuse/runtime.py"):
        run_guard(release_repo, monkeypatch)


@pytest.mark.parametrize("release", [
    {"tag_name": "v3.0.0", "draft": True, "published_at": "2026-10-09"},
    {"tag_name": "v3.0.0", "draft": False, "published_at": None},
    {"tag_name": "v2.0.0", "draft": False, "published_at": "2026-10-09"},
    [],
])
def test_guard_requires_an_existing_published_release(release_repo, monkeypatch, release):
    with pytest.raises(SystemExit, match="published GitHub release"):
        run_guard(release_repo, monkeypatch, release=release)


def test_guard_rejects_unavailable_release(release_repo, monkeypatch):
    with pytest.raises(SystemExit, match="published GitHub release"):
        run_guard(release_repo, monkeypatch, unavailable=True)


@pytest.mark.parametrize("tag", ["--all", "v3.0.0/path", "v3.0.0\nversion=evil", "3.0.0", "v03.0.0"])
def test_guard_rejects_unsafe_tag_inputs(release_repo, monkeypatch, tag):
    with pytest.raises(SystemExit, match="safe v-prefixed"):
        run_guard(release_repo, monkeypatch, tag=tag)


@pytest.mark.parametrize("ref", ["refs/heads/feature", "refs/tags/v3.0.0"])
def test_manual_guard_requires_repository_default_branch(release_repo, monkeypatch, ref):
    with pytest.raises(SystemExit, match="default branch"):
        run_guard(release_repo, monkeypatch, ref=ref)


def test_guard_requires_exact_caller_checkout(release_repo, monkeypatch):
    with pytest.raises(SystemExit, match="exact caller commit"):
        run_guard(release_repo, monkeypatch, caller="0" * 40)


def test_manual_guard_uses_repository_default_branch_metadata(release_repo, monkeypatch):
    result = run_guard(release_repo, monkeypatch, ref="refs/heads/trunk", default_branch="trunk")
    assert result["release_sha"] == git(release_repo, "rev-parse", "v3.0.0^{commit}")


def test_guard_requires_tag_to_resolve_to_an_existing_commit(release_repo, monkeypatch):
    git(release_repo, "tag", "-d", "v3.0.0")
    with pytest.raises(subprocess.CalledProcessError):
        run_guard(release_repo, monkeypatch)
    assert not (release_repo.parent / "guard-output").exists()


def test_guard_checks_package_version_at_release_commit(release_repo, monkeypatch):
    commit(release_repo, "src/xuse/__init__.py", '__version__ = "3.0.1"\n')
    git(release_repo, "tag", "-d", "v3.0.0")
    git(release_repo, "tag", "v3.0.0")
    with pytest.raises(SystemExit, match="package version at the release commit"):
        run_guard(release_repo, monkeypatch)


def test_recovery_workflow_requires_full_ci_and_publishes_only_guarded_source():
    jobs = WORKFLOW["jobs"]
    guard_checkout = next(step for step in jobs["release_guard"]["steps"] if step.get("uses", "").startswith("actions/checkout@"))
    assert guard_checkout["with"]["ref"] == "${{ github.sha }}"
    assert GUARD_STEP["env"]["DEFAULT_BRANCH"] == "${{ github.event.repository.default_branch }}"
    for output in ("release_sha", "release_tag", "version"):
        assert jobs["release_guard"]["outputs"][output] == "${{ steps.release.outputs." + output + " }}"
    assert jobs["verify"]["needs"] == "release_guard"
    assert jobs["verify"]["uses"] == "./.github/workflows/ci.yml"
    assert "if" not in jobs["verify"]
    assert jobs["build"]["needs"] == ["release_guard", "verify"]
    assert jobs["publish"]["needs"] == "build"
    assert jobs["publish"]["environment"]["name"] == "pypi"
    assert jobs["publish"]["permissions"]["id-token"] == "write"
    assert jobs["mcp-registry"]["needs"] == ["release_guard", "publish"]
    for job in ("build", "mcp-registry"):
        checkout = next(step for step in jobs[job]["steps"] if step.get("uses", "").startswith("actions/checkout@"))
        assert checkout["with"]["ref"] == "${{ needs.release_guard.outputs.release_sha }}"
    build_check = next(step for step in jobs["build"]["steps"] if step.get("name") == "Check the release tag against package metadata")
    assert build_check["env"]["RELEASE_VERSION"] == "${{ needs.release_guard.outputs.version }}"
    publish = next(step for step in jobs["publish"]["steps"] if step.get("name") == "Publish to PyPI")
    assert publish["with"]["skip-existing"] is False
    assert WORKFLOW["concurrency"] == {"group": "${{ github.workflow }}-publication", "cancel-in-progress": False}
    assert "GITHUB_REF_NAME" not in (ROOT / ".github/workflows/publish.yml").read_text()
    # YAML 1.1 parses the unquoted GitHub Actions `on` key as True.
    triggers = WORKFLOW.get("on", WORKFLOW.get(True))
    assert triggers["release"]["types"] == ["published"]
    assert triggers["workflow_dispatch"]["inputs"]["release_tag"]["required"] is True
