# Contributing to x-use

x-use is a Python MCP server and CLI for X. Start with a reproducible problem,
keep changes scoped, and include evidence for the behavior being changed.
Development targets Python 3.10–3.14 on Windows, Linux, and macOS.

## Development setup

Clone your fork and create a working branch. With uv installed:

```bash
uv sync --locked --extra dev
uv run --locked --extra dev x-use --help
```

The checkout also supports `python3 scripts/setup_uv.py --dev` (Windows:
`py -3 scripts/setup_uv.py --dev`). This installs matching Chromium and runs
diagnostics. Account setup is separate; ordinary development and CI do not
need your X cookies or an LLM key.

## Where changes belong

| Area | Files |
|---|---|
| Current browser behavior, inbox, notifications, threads | `src/xuse/browser/` |
| MCP schemas, workflows, approval and recovery | `src/xuse/mcp/` |
| Local leads, campaigns and suppression | `src/xuse/outreach/` |
| Private state, configuration and platform permissions | `src/xuse/core/` |
| Legacy Selenium batch engine | `src/xuse/features/`, `src/xuse/orchestrator.py` |
| Reusable configuration and writing personas | `presets/` |
| Canonical bundled agent skills | `src/xuse/skills_pack/` |
| Installation and CI checks | `scripts/`, `.github/workflows/` |

`data/` is primarily runtime state, not a library of configuration presets.
Only the documented dummy examples belong in Git. See [presets](presets/README.md)
and [data](data/README.md) before changing either directory.

## Tests

Run the regression suite:

```bash
uv run --locked --extra dev pytest -q -m "not smoke"
```

Tests use temporary state and synthetic content. Some tests launch a local
browser or use loopback networking; they do not use a real X account. Browser
fixtures may skip when their executable is absent. To require the bundled
Patchright browser fixtures, install Chromium and set
`XUSE_TEST_BROWSER_DRIVER=patchright`, `XUSE_TEST_BROWSER_CHANNEL=chromium`, and
`XUSE_REQUIRE_BROWSER_TESTS=1` in your shell:

```bash
uv run --locked --extra dev python -m patchright install chromium
uv run --locked --extra dev pytest -q tests/browser
```

Linux may need `patchright install --with-deps chromium` with permission to
install OS packages. CI separately runs native scenarios against a clean
installed wheel, so an editable source import cannot hide packaging mistakes.
`scripts/ci_browser_smoke.py` checks CLI/MCP startup and synthetic browser
behavior; it is different from the reserved `smoke` marker for authorized
live-account tests.

For a bug, add a regression for the observable failure: wrong target, stale
conversation, duplicate write, lost recovery identity, or leaked account state.
Do not weaken safety assertions to make an unsupported environment pass. DOM
fixtures must use fictional identities and messages. Never include a real inbox
dump, decrypted conversation, PIN, cookie, token, or browser profile.

## MCP and skills

New tools need explicit annotations, bounded input/output, structured errors,
and updates to `tests/mcp/test_contract.py` and the public tool documentation.
Keep exact target verification and durable action identity through cancellation
and timeouts. An uncertain external write must not be automatically replayed.
Local draft/queue changes and external submissions are distinct effects.

Edit packaged skills in `src/xuse/skills_pack/`, then run
`uv run python scripts/sync_skills.py` to update the plugin copies. The root
`SKILL.md` is a separate installation skill. Validate with
`tests/test_skills_pack.py`, `tests/test_skills_sync.py`, and
`tests/test_install_manifests.py`. Guidance must respect existing authorization,
direct-mode settings, and queue auto-drain without promising universal approval
gates or complete visible-page history.

## Pull requests and releases

- Explain the problem, resulting behavior, tests, and remaining limitations.
- Use focused commits and include related tests with the change.
- Keep credentials, account journals, `.env`, and local diagnostics untracked.
- Run relevant tests and `git diff --check`; let the supported-platform CI finish.
- Update the changelog and affected documentation. A passing fixture is not proof
  that every tool worked on a live account.

The version source is `src/xuse/__init__.py`; package metadata reads it
dynamically. A release preparation also updates `server.json`, plugin product
versions in both marketplaces, the plugin manifest, `uv.lock`, and the changelog.
Run `tests/test_version_integrity.py`, build with `uv run python -m build`, and
check with `uv run python -m twine check --strict dist/*` (a POSIX shell expands
the glob; use explicit distribution paths in shells that do not).
Tagging and publishing are maintainer actions after review and passing CI.
The release workflow verifies the tag and runs CI before PyPI/MCP publication.

## Reports and community

Search [existing issues](https://github.com/ihuzaifashoukat/x-use/issues) before
opening a bug report. Include Python/OS/browser versions, backend, sanitized
errors, and a minimal reproduction. Report security-sensitive issues privately
to [ihuzaifashoukat@gmail.com](mailto:ihuzaifashoukat@gmail.com), without sending
account credentials. Do not publish exploit details or private account data in
an issue while a report is being investigated.

Participation follows the [Code of Conduct](CODE_OF_CONDUCT.md). Contributions
are distributed under the existing [MIT license](LICENSE).