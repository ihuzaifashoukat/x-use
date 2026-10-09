---
name: x-use-setup
description: Zero-knowledge onboarding for x-use that verifies the install, registers the MCP server, then interviews the user to configure their first X account (cookies, niche, keywords, persona). Use when x-use is not yet configured, when adding an account, or when the user asks to get started.
---

# x-use setup

Goal: a working account, configured conversationally. The user should never
edit a config file by hand. Work through these steps in order, skipping any
that are already done, and confirm each before moving on.

## 1. Verify the install

- Run `x-use doctor`. It checks the selected Patchright/Playwright browser,
  cookies, LLM key and proxies without launching a browser.
- If working in a checkout, use `py -3 scripts/setup_uv.py` on Windows or
  `python3 scripts/setup_uv.py` on macOS/Linux and use the `.venv` executable.
  Otherwise install `pip install x-use-mcp` and
  `python -m patchright install chromium`, then re-run doctor.
- If the MCP server is not registered in this client yet, register it, then
  ask the user to restart the client. Use the absolute executable path and
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
Verify with `get_account_health(account)`; cookie status should be valid.

## 3. Niche and keywords

Ask: "What niche are you in, which keywords should I watch, and whose posts
do you want to engage with (profiles)?" Apply with
`update_account(account, target_keywords=[...],
competitor_profiles=[...])`.

## 4. Persona

Ask how they want to sound. Offer the three starter personas from
`presets/personas/` (builder, founder, curator) as starting points, let the
user pick or dictate their own, then apply with
`update_account(account, persona=<text>)`. Keep it under 4000 chars,
markdown, covering: voice, topics, reply style, what to avoid, 1-2 example
replies.

## 5. Optional extras (only if relevant)

- Multi-account: repeat from step 2, and ask about proxies, added with
  `add_proxy(pool, proxy_url)` and assign with
  `update_account(account, proxy="pool:<name>")`.
- Background automation (unattended `"auto"` text): needs one
  OpenAI-compatible key in `config/settings.json` under `llm`
  (`api_key`, `base_url`, `model`). Interactive use needs no key at all.

## 6. Prove it works

Stage something real but harmless: use x-use-engage to research one reply
draft, or x-use-content to stage one post draft. Show it via `list_drafts`.
Tell the user: nothing posts until you say `approve_draft(<id>)`.

Inbox verification uses `get_inbox`; results cover only visible conversations.
`unread=null` means the UI supplied no reliable read-state evidence. Opening a
chat may mark it as read. If `pin_required`, use the owner's server environment
variable with `unlock_inbox`; never request a PIN in tool arguments or store it
in drafts. Stop for unsupported UI or account challenges. Messages and follows
always need individual draft approval, and an uncertain write needs explicit
outcome reconciliation after inspecting X before any retry.
