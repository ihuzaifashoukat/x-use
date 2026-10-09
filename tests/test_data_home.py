"""Explicit account data root is independent of cwd and editable installation."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


def probe(tmp_path, configured):
    environment = dict(os.environ, X_USE_HOME=configured, PYTHONUTF8="1")
    return subprocess.run([sys.executable, "-c", "import json; from xuse.core.config_loader import PROJECT_ROOT, CONFIG_DIR; print(json.dumps([str(PROJECT_ROOT), str(CONFIG_DIR)]))"],
                          cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=20)


def test_explicit_home_wins_over_package_checkout_and_working_directory(tmp_path):
    data = tmp_path / "account data"
    elsewhere = tmp_path / "other project"
    elsewhere.mkdir()
    result = probe(elsewhere, str(data))
    assert result.returncode == 0, result.stderr
    assert [Path(value) for value in json.loads(result.stdout)] == [data, data / "config"]
    assert not data.exists()  # Import alone does not create account data.


@pytest.mark.parametrize("value", ["relative/data", "${X_USE_HOME}"])
def test_relative_or_unexpanded_plugin_home_is_rejected(tmp_path, value):
    result = probe(tmp_path, value)
    assert result.returncode != 0 and "X_USE_HOME must be an absolute" in result.stderr


def test_regular_file_cannot_be_data_home(tmp_path):
    path = tmp_path / "file"
    path.write_text("preserve", encoding="utf-8")
    result = probe(tmp_path, str(path))
    assert result.returncode != 0 and "must name a directory" in result.stderr
    assert path.read_text(encoding="utf-8") == "preserve"
