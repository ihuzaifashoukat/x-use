"""Credential-free installed-package and synthetic browser checks for CI.

Run with the interpreter from a clean wheel environment, not with PYTHONPATH
pointing at src. Browser mode requires an explicitly installed matching
Chromium; an unavailable browser is a failure rather than a silent skip.
All browser requests are intercepted before network access. Screenshots and
traces contain only the fixed HTML fixture below, never a user's account.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib
import importlib.metadata as metadata
import importlib.resources as resources
import json
import logging
import os
import platform
import subprocess
import sys
import tempfile
from pathlib import Path


FIXTURE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Local browser fixture</title></head><body>
<button data-testid="SideNav_AccountSwitcher_Button">Fixture account</button>
<main><article data-testid="tweet">
  <div data-testid="User-Name"><span>Fixture Person</span><span>@fixture</span></div>
  <a href="/fixture/status/1234"><time datetime="2026-01-01T12:00:00Z">Today</time></a>
  <div data-testid="tweetText">Local fixture post #Testing</div>
  <div role="link"><a href="/quote/status/123"><time>Yesterday</time></a>
    <div data-testid="tweetText">Embedded quote</div></div>
  <button data-testid="like" onclick="this.dataset.testid='unlike'">1.2K</button>
  <a href="/fixture/status/1234/analytics">42</a>
</article></main>
</body></html>"""


def installed_package_check(expect_installed: bool, driver: str) -> dict:
    import xuse
    from xuse.browser.page import XBrowser
    from xuse.browser.sessions import PatchrightSessionPool, PlaywrightSessionPool
    from xuse.mcp.server import create_server

    distribution = metadata.distribution("x-use-mcp")
    assert xuse.__version__ == distribution.version, "Package and metadata versions disagree."
    assert PlaywrightSessionPool.backend == "playwright" and PatchrightSessionPool.backend == "patchright"
    if expect_installed:
        source = Path(__file__).resolve().parents[1] / "src"
        assert not Path(xuse.__file__).resolve().is_relative_to(source), "Smoke imported checkout source instead of the wheel."
        direct_url = distribution.read_text("direct_url.json")
        if direct_url:
            assert not json.loads(direct_url).get("dir_info", {}).get("editable"), "Wheel environment contains an editable install."
    entries = {entry.name: entry.value for entry in distribution.entry_points}
    assert entries.get("x-use") == "xuse.cli:app", "CLI entry point is absent."
    skill_root = resources.files("xuse.skills_pack")
    skills = [path.name for path in skill_root.iterdir() if path.is_dir() and (path / "SKILL.md").is_file()]
    assert {"x-use", "x-use-setup", "x-use-engage", "x-use-content", "x-use-review", "x-use-threads", "x-use-inbox"}.issubset(skills), "Bundled skill files are absent from the wheel."
    for name in skills:
        assert (skill_root / name / "SKILL.md").read_text(encoding="utf-8").startswith("---")
    environment = dict(os.environ, PYTHONUTF8="1", PYTHONNOUSERSITE="1")
    environment.pop("PYTHONPATH", None)
    cli = subprocess.run(
        [sys.executable, "-c", "from xuse.cli import app; app()", "--help"],
        capture_output=True, text=True, encoding="utf-8", timeout=20, env=environment,
    )
    assert cli.returncode == 0 and "mcp" in cli.stdout, "Installed CLI help failed."
    return {"version": distribution.version, "skill_count": len(skills), "cli_help": "passed",
            "driver": driver, "driver_version": metadata.version(driver)}


async def package_protocol_check(directory: Path, expect_installed: bool, driver: str) -> dict:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from xuse.core.config_loader import ConfigLoader
    from xuse.mcp.server import create_server, shutdown

    config = directory / "config"
    config.mkdir()
    settings = {
        "mcp": {"browser_backend": driver, "draft_mode": True,
                "drafts_file": str(directory / "drafts.jsonl"),
                "safety_file": str(directory / "safety.sqlite3"),
                "outreach_file": str(directory / "outreach.sqlite3")},
        "queue": {"store_file": str(directory / "queue.jsonl"), "auto_drain": {"enabled": False}},
    }
    settings_path, accounts_path = config / "settings.json", config / "accounts.json"
    settings_path.write_text(json.dumps(settings), encoding="utf-8")
    accounts_path.write_text("[]", encoding="utf-8")
    server = create_server(ConfigLoader(settings_path, accounts_path))
    try:
        declared = {tool.name for tool in await server.list_tools()}
        assert not list(server.xuse_ctx.session_pool.active_accounts)
        assert server.xuse_ctx.session_pool._runtime is None, "Introspection started a browser."
    finally:
        await shutdown(server)
    # Explicit empty configuration keeps this script safe even when a local
    # editable installation anchors PROJECT_ROOT to an existing checkout.
    bootstrap = (
        "from xuse.mcp.stdio import enforce_stdio_stdout_hygiene; "
        "enforce_stdio_stdout_hygiene(); "
        "from xuse.core.config_loader import ConfigLoader; "
        "from xuse.mcp.server import create_server; "
        "create_server(ConfigLoader('config/settings.json','config/accounts.json')).run()"
    )
    environment = dict(os.environ, PYTHONUTF8="1", PYTHONNOUSERSITE="1")
    environment.pop("PYTHONPATH", None)
    entrypoints = [("explicit_config", sys.executable, ["-c", bootstrap])]
    if expect_installed:
        launcher = Path(sys.executable).parent / ("x-use.exe" if os.name == "nt" else "x-use")
        assert launcher.is_file(), "Installed console launcher is absent."
        entrypoints = [("module", sys.executable, ["-m", "xuse.mcp.server"]),
                       ("cli", str(launcher), ["mcp"])]
    # No account material exists in the child. Keep its ordinary startup
    # warnings out of protocol stdout and do not upload arbitrary process logs.
    class ProtocolErrors(logging.Handler):
        count = 0

        def emit(self, record):
            if record.levelno >= logging.ERROR:
                self.count += 1

    client_logger = logging.getLogger("mcp.client.stdio")
    previous_propagation = client_logger.propagate
    counter = ProtocolErrors()
    client_logger.addHandler(counter)
    client_logger.propagate = False
    try:
        for label, command, arguments in entrypoints:
            parameters = StdioServerParameters(command=command, args=arguments,
                                               cwd=str(directory), env=environment)
            with (directory / (label + "-stderr.log")).open("w", encoding="utf-8") as errors:
                async with stdio_client(parameters, errlog=errors) as (read, write):
                    async with ClientSession(read, write) as session:
                        initialized = await session.initialize()
                        tools = await session.list_tools()
                        prompts = await session.list_prompts()
                        await session.list_resources()
            assert {tool.name for tool in tools.tools} == declared, "MCP entrypoint registries disagree."
    finally:
        client_logger.removeHandler(counter)
        client_logger.propagate = previous_propagation
    assert counter.count == 0, "MCP stdout contained invalid JSON-RPC messages."
    exposed = {tool.name for tool in tools.tools}
    assert exposed == declared, "MCP protocol and server tool registries disagree."
    required = {"list_accounts", "search_tweets", "get_inbox", "send_message", "follow_profile", "create_campaign"}
    assert required <= exposed, "Installed wheel is missing core MCP tools."
    assert all(tool.inputSchema.get("type") == "object" for tool in tools.tools)
    return {"server": initialized.serverInfo.name, "tool_count": len(exposed),
            "prompt_count": len(prompts.prompts), "browser_started": False,
            "entrypoints": [item[0] for item in entrypoints], "invalid_protocol_messages": counter.count}


async def browser_check(artifacts: Path | None, channel: str | None, driver: str) -> dict:
    async_playwright = importlib.import_module(driver + ".async_api").async_playwright
    from xuse.browser.errors import BrowserActionError, BrowserBlocked
    from xuse.browser.page import XBrowser

    requests = {"fulfilled": 0, "aborted": 0}
    if artifacts:
        artifacts.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as runtime:
        options = {"headless": True}
        if channel:
            options["channel"] = channel
        chromium = await runtime.chromium.launch(**options)
        context = await chromium.new_context(service_workers="block", accept_downloads=False)
        tracing = False
        try:
            if artifacts:
                await context.tracing.start(screenshots=True, snapshots=True, sources=False)
                tracing = True

            async def intercept(route):
                if route.request.url.startswith("https://x.com/"):
                    requests["fulfilled"] += 1
                    if "/rate-limit-fixture" in route.request.url:
                        await route.fulfill(status=429, content_type="text/html", body="Rate limit exceeded")
                    else:
                        await route.fulfill(status=200, content_type="text/html", body=FIXTURE)
                else:
                    requests["aborted"] += 1
                    await route.abort()

            await context.route("**/*", intercept)
            page = await context.new_page()
            browser = XBrowser(page, account={"self_handles": ["fixture"]})
            await browser.navigate("https://x.com/home")
            await browser.ensure_ready()
            tweet = (await browser.get_tweet("https://x.com/fixture/status/1234"))[0]
            assert tweet.tweet_id == "1234" and tweet.text_content == "Local fixture post #Testing"
            assert tweet.like_count == 1200 and tweet.view_count == 42
            try:
                await browser.get_tweet("https://x.com/fixture/status/123")
            except BrowserActionError as error:
                assert error.reason == "tweet_not_found"
            else:
                raise AssertionError("A quoted/prefix ID was incorrectly selected.")
            result = await browser.like("https://x.com/fixture/status/1234")
            assert result["success"] is True and result["evidence"]["observed"] == "unlike_control"
            if artifacts:
                await page.screenshot(path=str(artifacts / "fixture.png"), full_page=True)
            try:
                await browser.navigate("https://x.com/rate-limit-fixture")
            except BrowserBlocked as error:
                assert error.reason == "rate_limited"
            else:
                raise AssertionError("A rate-limited fixture was not blocked.")
            return {"driver": driver, "engine": "chromium", "channel": channel or "bundled",
                    "dom_parsing": "passed", "exact_tweet_match": "passed",
                    "observed_like": "passed", "rate_limit": "passed",
                    "network_requests_forwarded": 0, "intercepted_requests": requests}
        finally:
            if tracing:
                with contextlib.suppress(Exception):
                    await context.tracing.stop(path=str(artifacts / "fixture-trace.zip"))
            await context.close()
            await chromium.close()


async def run(args, report):
    report["stage"] = "installed_package"
    with tempfile.TemporaryDirectory(prefix="xuse-ci-empty-") as temporary:
        # macOS exposes its temp tree through /var -> /private/var. Resolve
        # this OS alias before handing paths to the strict private-state
        # checks, which correctly reject symlink ancestors in supplied paths.
        directory = Path(temporary).resolve()
        previous = Path.cwd()
        previous_home = os.environ.get("X_USE_HOME")
        try:
            os.chdir(directory)
            # Editable imports can otherwise anchor defaults to the checkout,
            # and inherited X_USE_HOME can point at real account material.
            os.environ["X_USE_HOME"] = str(directory)
            report["package"] = installed_package_check(args.expect_installed, args.driver)
            if args.mode in ("package", "all"):
                # The OS temp directory may grant other users access. Keep
                # the identity check above in an empty cwd, then atomically
                # create a private child for database/protocol fixtures.
                from xuse.core.local_state import private_state_file
                directory = directory / "private-fixture"
                private_state_file(directory / ".bootstrap")
                os.chdir(directory)
                os.environ["X_USE_HOME"] = str(directory)
                report["stage"] = "mcp_protocol"
                report["mcp"] = await asyncio.wait_for(package_protocol_check(directory, args.expect_installed, args.driver), timeout=45)
            if args.mode in ("browser", "all"):
                report["stage"] = "synthetic_browser"
                report["browser"] = await asyncio.wait_for(browser_check(args.artifacts, args.channel, args.driver), timeout=60)
        finally:
            os.chdir(previous)
            if previous_home is None:
                os.environ.pop("X_USE_HOME", None)
            else:
                os.environ["X_USE_HOME"] = previous_home
    report["status"] = "passed"
    report.pop("stage", None)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("package", "browser", "all"), default="all")
    parser.add_argument("--driver", choices=("patchright", "playwright"), default="patchright")
    parser.add_argument("--expect-installed", action="store_true")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--artifacts", type=Path)
    parser.add_argument("--channel", choices=("chrome", "msedge", "chromium"),
                        help="Explicit local development override; CI uses bundled Chromium.")
    args = parser.parse_args()
    if args.report:
        args.report = args.report.resolve()
    if args.artifacts:
        args.artifacts = args.artifacts.resolve()
    report = {"status": "failed", "mode": args.mode, "os": platform.system(),
              "architecture": platform.machine(), "python": platform.python_version()}
    try:
        asyncio.run(run(args, report))
    except Exception as error:
        # Even diagnostics use fixed labels. No traceback or arbitrary browser
        # exception text is saved or printed by this credential-free smoke.
        report["error_type"] = type(error).__name__
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
