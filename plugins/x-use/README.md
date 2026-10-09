# x-use Claude Code plugin

This plugin installs the x-use Agent Skills and connects Claude Code to the
`x-use` MCP server from this source checkout. The plugin is not yet available
from the public marketplace; use the local checkout steps below.

## Install from a local checkout

Run the following from the repository root. `setup_uv.py` installs the locked
environment, matching Patchright Chromium, and sample configuration without
overwriting existing account files.

Windows PowerShell:

```powershell
py -3 scripts/setup_uv.py
$checkout = (Get-Location).Path
$env:PATH = (Join-Path $checkout '.venv\Scripts') + ';' + $env:PATH
$env:X_USE_HOME = Join-Path $env:LOCALAPPDATA 'x-use'
& (Join-Path $checkout '.venv\Scripts\x-use.exe') init
claude --plugin-dir (Join-Path $checkout 'plugins\x-use')
```

macOS or Linux:

```bash
python3 scripts/setup_uv.py
export X_USE_HOME="$HOME/.local/share/x-use"
export PATH="$PWD/.venv/bin:$PATH"
x-use init
claude --plugin-dir "$PWD/plugins/x-use"
```

`X_USE_HOME` must be an absolute path and must be set in the environment that
launches Claude Code. The plugin passes it to the MCP server, so configuration
and local state do not depend on Claude Code's current project directory or the
plugin cache. To use the plugin from another working directory, pass the
absolute path to this checkout's `plugins/x-use` directory to `--plugin-dir`.

Restart Claude Code if needed, then check `/mcp` for the `x-use` server. Keep
the data directory private. Cookie exports should remain outside source
control; provide their file paths through x-use setup and never paste cookie
contents into chat.

After the plugin files are published to the GitHub repository, it can also be
installed from the marketplace with:

```text
/plugin marketplace add ihuzaifashoukat/x-use
/plugin install x-use@x-use
```

Claude Code plugin manifests and bundled MCP server configuration follow the
[plugin components documentation](https://code.claude.com/docs/en/plugins/components)
and [MCP documentation](https://code.claude.com/docs/en/mcp).
