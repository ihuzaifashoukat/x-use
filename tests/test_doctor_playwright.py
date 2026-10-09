"""Doctor checks the selected MCP runtime offline, with legacy compatibility."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import xuse.doctor as doctor


def credentials(**overrides):
    return [dict(name=name, value="PRIVATE_COOKIE_SENTINEL", domain=".x.com", path="/",
                 secure=True, **overrides) for name in ("auth_token", "ct0")]


def probe_stub(monkeypatch, path, backend="patchright"):
    calls = []
    monkeypatch.setattr(doctor, f"_probe_{backend}_browser",
                        lambda channel, headless: calls.append((channel, headless)) or path)
    return calls


def test_default_mcp_inspects_headless_patchright_and_missing_browser_hint(monkeypatch, tmp_path):
    calls = probe_stub(monkeypatch, tmp_path / "headless_shell.exe")
    checks = doctor._check_mcp_browser({})
    assert calls == [(None, True)]
    assert checks[0].status == "PASS"
    assert "backend=patchright" in checks[0].detail
    assert checks[1].status == "PASS"
    assert checks[1].name == "mcp:patchright-runtime"
    assert checks[2].name == "mcp:browser:chromium-headless-shell"
    assert checks[2].status == "FAIL"
    assert "python -m patchright install chromium" in checks[2].hint


def test_explicit_playwright_uses_only_vanilla_probe(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor, "_probe_patchright_browser", lambda *args: pytest.fail("wrong driver selected"))
    calls = probe_stub(monkeypatch, tmp_path / "headless_shell.exe", backend="playwright")
    checks = doctor._check_mcp_browser({"mcp": {"browser_backend": "playwright"}})
    assert calls == [(None, True)]
    assert checks[1].name == "mcp:playwright-runtime"
    assert "python -m playwright install chromium" in checks[-1].hint


@pytest.mark.parametrize("channel", ["chrome", "msedge", "chromium"])
def test_selected_channel_binary_is_checked_without_system_path_substitution(monkeypatch, tmp_path, channel):
    executable = tmp_path / "browser.exe"
    executable.write_bytes(b"test executable marker")
    calls = probe_stub(monkeypatch, executable)
    checks = doctor._check_mcp_browser({"mcp": {"browser_channel": channel}})
    assert calls == [(channel, True)]
    assert checks[-1].status == "PASS"
    assert checks[-1].name == f"mcp:browser:{channel}"
    assert checks[-1].detail == str(executable)


def test_mcp_channel_fallback_and_explicit_null_match_runtime(monkeypatch, tmp_path):
    calls = probe_stub(monkeypatch, tmp_path / "browser")
    doctor._check_mcp_browser({"browser_settings": {"channel": "chrome"}})
    doctor._check_mcp_browser({"mcp": {"browser_channel": None, "browser_headless": False},
                               "browser_settings": {"channel": "chrome", "headless": True}})
    assert calls == [("chrome", True), (None, False)]


@pytest.mark.parametrize("settings,field", [
    ({"mcp": []}, "mcp"),
    ({"mcp": {"browser_backend": "other"}}, "browser_backend"),
    ({"mcp": {"browser_backend": ["patchright"]}}, "browser_backend"),
    ({"mcp": {"browser_channel": "PRIVATE_COOKIE_SENTINEL"}}, "browser_channel"),
    ({"mcp": {"browser_headless": "yes"}}, "browser_headless"),
    ({"browser_settings": "invalid"}, "browser_settings"),
])
def test_malformed_config_is_distinct_from_missing_runtime_and_safe(monkeypatch, settings, field):
    monkeypatch.setattr(doctor, "_probe_browser_driver",
                        lambda *args: pytest.fail("malformed config must not start a probe"))
    checks = doctor._check_mcp_browser(settings)
    assert len(checks) == 1 and checks[0].status == "FAIL"
    assert checks[0].name == "mcp:browser-config"
    assert field in checks[0].detail
    assert "PRIVATE_COOKIE_SENTINEL" not in repr(checks)


@pytest.mark.parametrize("backend,product", [("patchright", "Patchright"), ("playwright", "Playwright")])
def test_missing_package_and_broken_driver_are_distinguished_without_error_data(monkeypatch, backend, product):
    def missing(*args):
        raise ModuleNotFoundError("PRIVATE_COOKIE_SENTINEL")

    monkeypatch.setattr(doctor, f"_probe_{backend}_browser", missing)
    settings = {"mcp": {"browser_backend": backend}}
    checks = doctor._check_mcp_browser(settings)
    assert checks[-1].detail == f"{product} is not importable"
    assert f"pip install {backend}" in checks[-1].hint

    def broken(*args):
        raise RuntimeError("PRIVATE_COOKIE_SENTINEL")

    monkeypatch.setattr(doctor, f"_probe_{backend}_browser", broken)
    checks = doctor._check_mcp_browser(settings)
    assert checks[-1].detail == f"{product} driver inspection failed"
    assert "PRIVATE_COOKIE_SENTINEL" not in repr(checks)


def test_selenium_skips_async_driver_probe_and_preserves_legacy_browser_api(monkeypatch):
    monkeypatch.setattr(doctor, "_probe_browser_driver", lambda *args: pytest.fail("legacy backend"))
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "test/chrome" if name == "chrome" else None)
    settings = {"mcp": {"browser_backend": "selenium"}, "browser_settings": {"chrome_driver_path": "test/driver"}}
    legacy = doctor._check_browser(settings)
    assert [(check.name, check.status) for check in legacy] == [
        ("browser:chrome", "PASS"), ("driver:chromedriver", "PASS")]
    modern = doctor._check_browser(settings, include_mcp=True)
    assert modern[0].status == "SKIP"
    assert modern[1:] == legacy


def test_default_doctor_does_not_fail_for_irrelevant_missing_selenium_browser(monkeypatch, tmp_path):
    executable = tmp_path / "headless-shell"
    executable.write_bytes(b"test marker")
    probe_stub(monkeypatch, executable)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    monkeypatch.setattr(doctor, "_windows_browser_paths", lambda name: [])
    checks = doctor._check_browser({}, include_mcp=True)
    assert all(check.status != "FAIL" for check in checks)
    assert checks[-1].name == "legacy-browser/driver" and checks[-1].status == "SKIP"
    assert doctor._check_browser({})[0].status == "FAIL"


@pytest.mark.parametrize("installed", [True, False])
def test_run_checks_reports_selected_mcp_browser_and_install_hint(
    monkeypatch, tmp_path, make_config_loader, capsys, installed,
):
    loader = make_config_loader(settings={"mcp": {}}, accounts=[])
    monkeypatch.setattr(doctor, "ConfigLoader", lambda: loader)
    monkeypatch.setattr("xuse.utils.env.load_env", lambda: None)
    monkeypatch.setattr(doctor, "_check_config_files", lambda _: [doctor.Check("config", "PASS")])
    monkeypatch.setattr(doctor, "_check_llm_keys", lambda _: [doctor.Check("llm", "SKIP")])
    monkeypatch.setattr(doctor, "_check_proxies", lambda *_: [doctor.Check("proxy", "SKIP")])
    executable = tmp_path / "headless-shell"
    if installed:
        executable.write_bytes(b"test marker")
    probe_stub(monkeypatch, executable)
    assert doctor.run_checks() == (0 if installed else 1)
    output = capsys.readouterr().out
    assert "mcp:browser:chromium-headless-shell" in output
    assert "mcp:cookies" in output
    if not installed:
        assert "python -m patchright install chromium" in output


@pytest.mark.parametrize("layout", ["legacy", "bundle"])
@pytest.mark.parametrize("backend", ["patchright", "playwright"])
@pytest.mark.parametrize("channel,headless,selected", [
    (None, True, "chromium-headless-shell"), (None, False, "chromium"),
    ("chromium", True, "chromium"), ("chrome", True, "chrome"), ("msedge", True, "msedge"),
])
def test_probe_uses_driver_registry_and_never_launches_a_browser(
    monkeypatch, tmp_path, layout, backend, channel, headless, selected,
):
    package = tmp_path / "package"
    registry_path = (package / "lib/server/registry/index.js" if layout == "legacy"
                     else package / "lib/coreBundle.js")
    registry_path.parent.mkdir(parents=True)
    registry_path.write_text("test registry marker", encoding="utf-8")
    node = tmp_path / "node"
    driver = SimpleNamespace(compute_driver_executable=lambda: (str(node), str(package / "cli.js")),
                             get_driver_env=lambda: {"PLAYWRIGHT_BROWSERS_PATH": "selected-cache"})
    imports = []

    def fake_import(name):
        imports.append(name)
        return driver if name == f"{backend}._impl._driver" else SimpleNamespace()

    monkeypatch.setattr(doctor.importlib, "import_module", fake_import)
    commands = []

    def fake_run(command, **kwargs):
        commands.append((command, kwargs))
        return SimpleNamespace(stdout=json.dumps({"path": str(tmp_path / "selected-browser")}))

    monkeypatch.setattr(doctor.subprocess, "run", fake_run)
    probe = getattr(doctor, f"_probe_{backend}_browser")
    assert probe(channel, headless) == tmp_path / "selected-browser"
    assert imports == [f"{backend}.async_api", f"{backend}._impl._driver"]
    command, options = commands[0]
    assert command[0] == str(node) and command[1] == "-e"
    assert command[-2:] == [str(registry_path), selected]
    assert "executablePath()" in command[2]
    assert ".launch(" not in command[2] and ".install(" not in command[2]
    assert options["timeout"] == 10 and options["check"] is True
    assert options["env"]["PLAYWRIGHT_BROWSERS_PATH"] == "selected-cache"


def test_generic_probe_rejects_untrusted_module_name_before_import(monkeypatch):
    monkeypatch.setattr(doctor.importlib, "import_module", lambda *_: pytest.fail("must not import arbitrary modules"))
    with pytest.raises(ValueError, match="unsupported browser driver"):
        doctor._probe_browser_driver("PRIVATE_COOKIE_SENTINEL", None, True)


def test_strict_mcp_cookie_rules_detect_export_that_legacy_helper_accepts(make_config_loader):
    payload = credentials()
    payload[0]["domain"] = "x.com.attacker.invalid"
    assert doctor.check_cookie_data(payload)[0] is True
    loader = make_config_loader(accounts=[{"account_id": "a", "cookies": payload}])
    checks = doctor._check_mcp_cookies(loader, loader.accounts, {})
    assert checks[0].status == "FAIL"
    assert "domain" in checks[0].detail
    assert "PRIVATE_COOKIE_SENTINEL" not in repr(checks)


def test_strict_cookie_file_uses_runtime_accounts_directory_and_never_echoes_values(
    make_config_loader,
):
    loader = make_config_loader(accounts=[{"account_id": "a", "cookie_file_path": "cookies.json"}])
    path = loader.accounts_file.parent / "cookies.json"
    path.write_text(json.dumps(credentials()), encoding="utf-8")
    checks = doctor._check_mcp_cookies(loader, loader.accounts, {})
    assert checks[0].status == "PASS"
    assert "PRIVATE_COOKIE_SENTINEL" not in repr(checks)
    path.write_text('{"PRIVATE_COOKIE_SENTINEL":', encoding="utf-8")
    checks = doctor._check_mcp_cookies(loader, loader.accounts, {})
    assert checks[0].status == "FAIL"
    assert "PRIVATE_COOKIE_SENTINEL" not in repr(checks)


def test_strict_cookie_expiry_missing_inline_empty_and_legacy_backend(make_config_loader):
    accounts = [
        {"account_id": "expired", "cookies": credentials(expires=1)},
        {"account_id": "missing"},
        {"account_id": "empty", "cookies": []},
    ]
    loader = make_config_loader(accounts=accounts)
    checks = doctor._check_mcp_cookies(loader, accounts, {})
    assert len(checks) == 3 and all(check.status == "FAIL" for check in checks)
    assert checks[0].detail == "authentication cookies have expired"
    assert "missing" in checks[1].detail
    assert "PRIVATE_COOKIE_SENTINEL" not in repr(checks)
    skipped = doctor._check_mcp_cookies(loader, accounts, {"mcp": {"browser_backend": "selenium"}})
    assert skipped[0].status == "SKIP"


def test_cookie_helper_legacy_api_keeps_permissive_historical_semantics(make_config_loader):
    account = {"account_id": "legacy", "cookies": [{"name": "auth_token", "value": "x"},
                                                     {"name": "ct0", "value": "y"}]}
    assert doctor._check_cookies([account])[0].status == "PASS"
    loader = make_config_loader(accounts=[account])
    checks = doctor._check_cookies([account], include_mcp=True, loader=loader, settings={})
    assert checks[0].status == "PASS" and checks[1].status == "FAIL"
