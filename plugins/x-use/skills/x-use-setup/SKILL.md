---
name: x-use-setup
description: Zero-knowledge onboarding for x-use: verify the install, register the MCP server, then interview the user to configure their first X account (cookies, niche, keywords, persona). Use when x-use is not yet configured, when adding an account, or when the user asks to get started.
---

# x-use setup

Goal: a working account, configured conversationally. The user should never
edit a config file by hand. Work through these steps in order, skipping any
that are already done, and confirm each before moving on.

## 1. Verify the install

- Run `x-use doctor`. It checks Chrome/driver, cookies, LLM key, proxies.
- If `x-use` is not found: `pip install x-use-mcp`, then re-run doctor.
- If the MCP server is not registered in this client yet, register it, then
  ask the user to restart the client:
  - Claude Desktop (`claude_desktop_config.json`):
    `{"mcpServers": {"x-use": {"command": "x-use", "args": ["mcp"]}}}`
  - Claude Code: `claude mcp add x-use -- x-use mcp`
  - Codex (`~/.codex/config.toml`):
    `[mcp_servers.x-use]` with `command = "x-use"`, `args = ["mcp"]`

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
