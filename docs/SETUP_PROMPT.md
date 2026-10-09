# Set up x-use with your AI client, the one prompt

Paste the prompt below into Claude Code, Claude Desktop, or Codex. The agent
installs x-use, registers the MCP server, verifies it, and interviews you to
configure your account. You never edit a config file by hand.

Everything below the line is the prompt, copy it verbatim.

---

Set up x-use (https://github.com/ihuzaifashoukat/x-use), the MCP server for
X/Twitter automation, on this machine, and configure it for me. Work through
these steps, explaining meaningful progress briefly. This request authorizes
local installation and client setup; ask only for missing information or actions
outside that scope. Preserve existing configuration and prior authorization:

1. Detect my OS and which AI client you are. Install x-use if missing:
   From a checkout use `py -3 scripts/setup_uv.py` on Windows or
   `python3 scripts/setup_uv.py` on macOS/Linux. This installs uv locally,
   locked dependencies and matching Chromium into an isolated environment.
   Otherwise use `pip install x-use-mcp` followed by
   `python -m patchright install chromium` (Python 3.10+ required).
   Verify: `x-use doctor`. Use the `.venv` executable for a checkout install.
2. Run `x-use skills install` to install the bundled agent skills.
3. Choose a stable absolute `X_USE_HOME` for private configuration and state.
   Use the same directory for setup and the MCP server. Register the server in
   this client using the actual installed executable path. Follow the current
   root `SKILL.md` or bundled `x-use-setup` skill for platform-specific examples:
   - Claude Desktop: add to claude_desktop_config.json:
     {"mcpServers": {"x-use": {"command": "ABSOLUTE_X_USE_EXECUTABLE", "args": ["mcp"], "env": {"X_USE_HOME": "ABSOLUTE_PRIVATE_DIRECTORY"}}}}
   - Claude Code: run `claude mcp add -e X_USE_HOME=ABSOLUTE_PRIVATE_DIRECTORY x-use -- ABSOLUTE_X_USE_EXECUTABLE mcp`
   - Codex: add to ~/.codex/config.toml:
     [mcp_servers.x-use]
     command = "ABSOLUTE_X_USE_EXECUTABLE"
     args = ["mcp"]
     [mcp_servers.x-use.env]
     X_USE_HOME = "ABSOLUTE_PRIVATE_DIRECTORY"
   Replace the placeholders and quote/escape paths for the target shell or
   config format. `scripts/mcp_config.py` renders the JSON form safely.
   Restart or reload the client if it needs that to discover the server.
4. Once the server responds (try `list_accounts`), follow the
   x-use-setup skill: interview me about my account, account id, cookie
   export file (guide me through exporting x.com cookies to a JSON file on
   disk), my niche and keywords, profiles I want to engage with, and my
   persona (offer the presets/personas templates or write one with me).
   Configure everything with the add_account/update_account tools.
5. If not already specified, ask whether I manage multiple accounts (repeat setup + proxy pools via
   add_proxy) and whether I need unattended background automation (only then
   is one OpenAI-compatible LLM key needed, interactive use needs none).
6. Check draft mode and auto-drain settings before demonstrating the workflow.
   With draft mode enabled, stage one local reply or post draft for review.
   Do not publish as part of setup. Ordinary direct-mode publishing, queue
   processing, opt-in auto-drain and legacy batch runs are separate execution
   paths; approval guidance must reflect the actual configuration.

Use the Patchright and reviewed-outreach starters for a fresh MCP setup.
Cookie format checks do not prove a live session is valid. Keep credentials
in private local files; do not print them or put them into tool arguments.
An inbox PIN or platform challenge needs the owner's recovery action. Stop
there without reload loops, credential guessing, or resetting action budgets.

If anything fails, run `x-use doctor`, read the error, fix the cause, and
only then continue.
