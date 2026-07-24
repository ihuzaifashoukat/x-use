"""x-use command line interface (Phase 1 — see ROADMAP.md)."""
import asyncio
from typing import Optional

import typer

from xuse.pipelines import ALL_PIPELINE_ENABLE_FLAGS, PIPELINE_FLAGS

app = typer.Typer(
    help="x-use - browser-native AI agents for X (Twitter).",
    no_args_is_help=True,
)


def _apply_pipeline_override(orchestrator, pipeline: str) -> None:
    """Restrict this run to one pipeline by forcing all other enable_* flags off.

    In-memory only: mutates the account dicts already loaded by the orchestrator;
    config/accounts.json on disk is never touched.
    """
    from xuse.models import ActionConfig

    global_ac = (
        (orchestrator.global_settings.get("twitter_automation", {}) or {})
        .get("action_config", {}) or {}
    )
    for acc in orchestrator.accounts_data:
        merged = dict(global_ac)
        merged.update(acc.get("action_config_override") or {})
        merged.update(acc.get("action_config") or {})
        for flag in ALL_PIPELINE_ENABLE_FLAGS:
            merged[flag] = False
        merged[PIPELINE_FLAGS[pipeline]] = True
        acc["action_config"] = ActionConfig(**merged).model_dump()


@app.command()
def run(
    account: Optional[str] = typer.Option(
        None, help="Run a single account only (account_id from config/accounts.json)."),
    pipeline: Optional[str] = typer.Option(
        None, help=f"Run a single pipeline only: {', '.join(sorted(PIPELINE_FLAGS))}."),
):
    """Run automation cycles (default: all active accounts, concurrent).

    TwitterOrchestrator itself has no scoping support; --account/--pipeline are
    applied here as in-memory filters/overrides on the loaded config.
    """
    # Lazy import: pulls in selenium and configures logging at module import time.
    from xuse.orchestrator import TwitterOrchestrator

    if pipeline is not None and pipeline not in PIPELINE_FLAGS:
        typer.secho(f"Unknown pipeline '{pipeline}'. Choose from: {', '.join(sorted(PIPELINE_FLAGS))}",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(2)

    orchestrator = TwitterOrchestrator()

    if account is not None:
        matched = [a for a in orchestrator.accounts_data if a.get("account_id") == account]
        if not matched:
            available = ", ".join(str(a.get("account_id")) for a in orchestrator.accounts_data) or "<none>"
            typer.secho(f"Account '{account}' not found. Available: {available}",
                        fg=typer.colors.RED, err=True)
            raise typer.Exit(2)
        if not matched[0].get("is_active", True):
            typer.secho(f"Note: account '{account}' is marked inactive; the orchestrator will skip it.",
                        fg=typer.colors.YELLOW)
        orchestrator.accounts_data = matched

    if pipeline is not None:
        _apply_pipeline_override(orchestrator, pipeline)
        typer.echo(f"Pipeline override (this run only): {pipeline}")

    try:
        asyncio.run(orchestrator.run())
    except KeyboardInterrupt:
        typer.echo("Run interrupted by user.")


@app.command()
def init():
    """Interactive setup wizard: presets, account + cookie import, LLM keys."""
    from xuse.init_wizard import run_wizard
    run_wizard()


@app.command()
def mcp():
    """Start the MCP stdio server (draft mode default-on)."""
    try:
        from xuse.mcp import server as mcp_server
    except ModuleNotFoundError as e:
        if e.name and e.name.startswith("xuse.mcp"):
            typer.secho(
                "The MCP server (xuse.mcp.server) is not available in this checkout yet —\n"
                "it ships later in Phase 1 (see ROADMAP.md). The other commands work today.",
                fg=typer.colors.YELLOW, err=True,
            )
        else:
            typer.secho(f"MCP server failed to import (missing dependency: {e}).", err=True)
        raise typer.Exit(1)

    entry = None
    for name in ("main", "run"):
        candidate = getattr(mcp_server, name, None)
        if callable(candidate):
            entry = candidate
            break
    if entry is None and callable(getattr(getattr(mcp_server, "mcp", None), "run", None)):
        entry = mcp_server.mcp.run
    if entry is None:
        typer.secho("xuse.mcp.server was found but exposes no entry point (main/run).",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    entry()


@app.command()
def doctor():
    """Environment checks: browser/driver, cookies, LLM keys, proxies."""
    from xuse.doctor import run_checks
    raise typer.Exit(run_checks())


skills_app = typer.Typer(
    help="Manage the bundled agent skills (Claude Code, Codex).",
    no_args_is_help=True,
)
app.add_typer(skills_app, name="skills")


@skills_app.command("install")
def skills_install(
    force: bool = typer.Option(False, "--force", help="Overwrite existing skills."),
):
    """Copy the bundled SKILL.md pack to ~/.claude/skills and ~/.agents/skills."""
    from xuse.skills_installer import install_skills
    try:
        result = install_skills(force=force)
    except Exception as e:
        # Partial/broken install (e.g. xuse.skills_pack absent): same
        # remediation message as the empty-pack branch, never a traceback.
        typer.secho(f"No bundled skills found (broken install?). ({e})",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    for path in result["installed"]:
        typer.echo(f"installed: {path}")
    for path in result["skipped"]:
        typer.echo(f"skipped (exists — use --force): {path}")
    if not result["installed"] and not result["skipped"]:
        typer.secho("No bundled skills found (broken install?).",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(1)


@skills_app.command("list")
def skills_list():
    """Show which bundled skills are installed in each client dir."""
    from xuse.skills_installer import SKILL_TARGETS, list_skills
    for name, targets in list_skills().items():
        marks = "  ".join(f"{t}: {'yes' if present else 'no'}"
                           for t, present in targets.items())
        typer.echo(f"{name}: {marks}")
    typer.echo(f"(targets: {', '.join(SKILL_TARGETS)})")


if __name__ == "__main__":
    app()
