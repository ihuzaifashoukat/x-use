---
name: x-use-setup
description: Zero-knowledge onboarding for x-use that verifies the install, registers the MCP server, then interviews the user to configure their first X account (cookies, niche, keywords, persona). Use when x-use is not yet configured, when adding an account, or when the user asks to get started.
---

# x-use setup

Configure the requested account conversationally. Skip completed steps and honor
the setup authorization for routine local changes; ask for missing account,
persona or credential choices. Do not use setup as authorization to publish.

## 1. Verify the install

- Run `x-use doctor`. It checks the selected Patchright/Playwright browser,
  cookies, LLM key and proxies without launching a browser.
- If working in a checkout, use `py -3 scripts/setup_uv.py` on Windows or
  `python3 scripts/setup_uv.py` on macOS/Linux and use the `.venv` executable.
  Otherwise install `pip install x-use-mcp` and
  `python -m patchright install chromium`, then re-run doctor.
- If the MCP server is not registered in this client yet, register it, then
  reload the connection if the client supports it, or ask the user to restart
  the client. Continue tool-dependent work only after tools appear.
  Use the absolute executable path and
  absolute data directory printed by the installer; set `X_USE_HOME` in the
  MCP server environment so configuration and state stay stable across client
  working directories:
  - Claude Desktop (`claude_desktop_config.json`):
    `{"mcpServers": {"x-use": {"command": "/absolute/path/to/x-use/.venv/bin/x-use", "args": ["mcp"], "env": {"X_USE_HOME": "/absolute/path/to/x-use-data"}}}}`
  - Cursor, Windsurf, and other clients using `mcpServers` JSON: use the same
    `command`, `args`, and `env` fields shown for Claude Desktop.
  - Claude Code: `claude mcp add --scope user x-use --env X_USE_HOME=/absolute/path/to/x-use-data -- /absolute/path/to/x-use/.venv/bin/x-use mcp`
  - Codex (`~/.codex/config.toml`):
    `[mcp_servers.x-use]` with the absolute `command` path and `args = ["mcp"]`,
    plus `[mcp_servers.x-use.env]` with `X_USE_HOME = "/absolute/path/to/x-use-data"`.

## 2. Account + cookies

Ask: "What should this account be called (a short id like `main` or
`brand`)?" Then explain cookie export:

1. Install a cookie-export browser extension (e.g. "Cookie-Editor").
2. Log into x.com in that browser, open the extension on x.com, Export as JSON.
3. Save the file somewhere on this machine and give me the path.

Then call `add_account(account_id, cookie_file=<path>)`. The file is
validated and copied server-side; cookie values never pass through the chat.
Verify with `get_account_health(account)`; it checks the local cookie file,
not whether X currently accepts the session. Inspect reported problems and use
a bounded browser read to verify login; stop for login or account challenges.

## 3. Niche and keywords

Ask for the niche, keywords, target profiles and the account's actual X handle.
Apply with
`update_account(account, target_keywords=[...],
competitor_profiles=[...], self_handles=["yourhandle"])`. `self_handles` enables
the own-post guard. Batch account edits where possible: each update closes its
warm browser session.

## 4. Persona

Ask how they want to sound. In a checkout, the personas in `presets/personas/`
(builder, founder, curator) can serve as starting points; these presets are not
included in the installed skill pack. Otherwise draft the requested voice, then
apply with
`update_account(account, persona=<text>)`. Keep it under 4000 chars,
markdown, covering: voice, topics, reply style, what to avoid, 1-2 example
replies.

## 5. Optional extras (only if relevant)

- Multi-account: repeat from step 2, and ask about proxies, added with
  `add_proxy(pool, proxy_url)` and assign with
  `update_account(account, proxy="pool:<name>")`.
- Server-generated posts/replies and unattended `"auto"` text need one
  OpenAI-compatible key in `config/settings.json` under `llm`
  (`api_key`, `base_url`, `model`). Interactive use needs no key at all.

## 6. Prove it works

With `mcp.draft_mode=true` (default), use x-use-engage or x-use-content to stage
one requested reply or post and show its full payload. Ordinary writes execute
immediately when draft mode is disabled. `approve_draft` executes reviewed
drafts; queued actions run via `process_queue` or opt-in auto-drain, and legacy
`run_cycle` runs immediately. Preparation alone does not authorize those gates.

Requested inbox verification uses `get_inbox(account=..., folder="inbox",
inbox_filter="unread", limit=5)`; requests and other folders are separate, and
results cover only visible conversations. Reads never accept requests.
`unread=null` means the UI supplied no reliable read-state evidence. Opening a
chat may mark it as read; visiting notifications can do the same. If
`pin_required`, use `unlock_inbox(account=..., pin_env_var="XUSE_INBOX_PIN")`
after the owner sets that server variable; never request a PIN in chat or store it
in drafts. Stop for unsupported UI or account challenges. Messages and follows
always need individual draft approval, and an uncertain write needs explicit
outcome reconciliation after inspecting X before any retry.
