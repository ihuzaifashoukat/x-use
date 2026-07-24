# Set up x-use with your AI client — the one prompt

Paste the prompt below into Claude Code, Claude Desktop, or Codex. The agent
installs x-use, registers the MCP server, verifies it, and interviews you to
configure your account. You never edit a config file by hand.

Everything below the line is the prompt — copy it verbatim.

---

Set up x-use (https://github.com/ihuzaifashoukat/x-use), the MCP server for
X/Twitter automation, on this machine, and configure it for me. Work through
these steps, explaining what you're doing in one line each, and ask before
anything that changes my system:

1. Detect my OS and which AI client you are. Install x-use if missing:
   `pip install x-use-mcp` (Python 3.10+ and Chrome required — tell me if
   either is missing and stop). Verify: `x-use doctor`.
2. Run `x-use skills install` to install the bundled agent skills.
3. Register the MCP server in YOU (this client), then tell me to restart you:
   - Claude Desktop: add to claude_desktop_config.json:
     {"mcpServers": {"x-use": {"command": "x-use", "args": ["mcp"]}}}
   - Claude Code: run `claude mcp add x-use -- x-use mcp`
   - Codex: add to ~/.codex/config.toml:
     [mcp_servers.x-use]
     command = "x-use"
     args = ["mcp"]
   If `x-use` is not on PATH, use the full path `pip show x-use-mcp` implies.
4. After I confirm the server responds (try `list_accounts`), follow the
   x-use-setup skill: interview me about my account — account id, cookie
   export file (guide me through exporting x.com cookies to a JSON file on
   disk), my niche and keywords, profiles I want to engage with, and my
   persona (offer the presets/personas templates or write one with me).
   Configure everything with the add_account/update_account tools.
5. Ask whether I manage multiple accounts (repeat setup + proxy pools via
   add_proxy) and whether I need unattended background automation (only then
   is one OpenAI-compatible LLM key needed — interactive use needs none).
6. Finish by staging one harmless draft (a reply or post) so I can see the
   review flow. Remind me: nothing posts until I say approve_draft(<id>).

If anything fails, run `x-use doctor`, read the error, fix the cause, and
only then continue.
