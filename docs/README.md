# X-Use documentation

X-Use is a Python MCP server and CLI for X (Twitter). Use these guides to
install it, try a bounded research workflow, review drafts, or contribute a fix.

**Version 3.3.0 contains 82 tools**, the same set as 3.1.0, which added runtime
diagnostics, campaign previews and publishing/inbox controls to the 70 tools in 3.0.0. For release-specific behavior, read
the documentation at the matching Git tag and consult the [changelog](../CHANGELOG.md).

## Start here

| I want to… | Read |
|---|---|
| Install X-Use and connect an MCP client | [Installation and quickstart](../README.md) |
| Have my agent configure the server | [Setup prompt](SETUP_PROMPT.md) |
| Try research, inbox, outreach or threads | [Practical MCP workflows](WORKFLOWS.md) |
| Find a tool, prompt, resource or error convention | [MCP reference](MCP_GUIDE.md) |
| Choose configuration and account settings | [Configuration reference](CONFIG_REFERENCE.md) |
| Understand the account score and recovery advice | [Account health](ACCOUNT_HEALTH.md) |
| Start from an existing configuration | [Presets and personas](../presets/README.md) |
| Understand local files and example data | [Data directory](../data/README.md) |

Set one stable, private `X_USE_HOME` before account setup and use that same
directory in your MCP client configuration. Run one MCP server process per
data home. Credentials belong in private local files, outside the repository.

## Browser behavior and evidence

| Guide | What it explains |
|---|---|
| [Browser runtime and outreach](BROWSER_RUNTIME.md) | Sessions, inbox folders, local campaigns, thread progress, action recovery and limits |
| [Publishing and inbox controls](CONTROLS.md) | Bookmarks, reviewed edits/deletions, engagement reversal and supported conversation actions |
| [Responsible operation](BEST_PRACTICES.md) | Conservative pacing, account policy and credential handling |
| [Tool validation](TOOL_VALIDATION.md) | Which tools have isolated, protocol or live-account evidence |
| [Patchright hardening](PATCHRIGHT_HARDENING.md) | Browser lifecycle, cookie import, account isolation and validation evidence |
| [Parallel upgrade validation](PARALLEL_UPGRADE_VALIDATION.md) | Earlier 82-tool evidence, queue recovery, inbox reliability and native Selenium checks |
| [Initial 3.1.0 validation](RELEASE_3_1_0_VALIDATION.md) | Development unlock allowance and earlier live inbox retest |
| [October 12 audit](AUDIT_2026-10-12.md) | Earlier 72-tool installation checks, fixes and test evidence |
| [Control expansion audit](CONTROL_TEST_AUDIT_2026-10-12.md) | Earlier 82-tool evidence, new controls and bookmark redirect fix |
| [Earlier resilience audit](RESILIENCE_AUDIT.md) | Historical findings and the controls they motivated |
| [Architecture](../ARCHITECTURE.md) | Runtime components, storage and compatibility boundaries |

Visible X results are partial. A passing local fixture does not prove that every
tool works on every live account, and an unavailable counter is not zero.
Opening a conversation or notifications can change X's native read state.

## Contribute and share

- [Contributing](../CONTRIBUTING.md): development setup, fictional browser
  fixtures, focused verification and release preparation.
- [Roadmap](../ROADMAP.md): current capabilities and proposed work.
- [Security reporting](../SECURITY.md): the private channel for sensitive bugs.
- [Code of Conduct](../CODE_OF_CONDUCT.md): community expectations.
- [Claude Code plugin](../plugins/x-use/README.md): bundled skills and plugin setup.
- [Brand assets](BRAND.md): the project icon, social preview and usage guidance.

Public destinations: [GitHub source and issues](https://github.com/ihuzaifashoukat/x-use)
and [the PyPI package](https://pypi.org/project/x-use-mcp/). The official MCP
Registry identifier is `io.github.ihuzaifashoukat/x-use`.
