---
name: x-use
description: Install, register, and configure x-use, the browser-native MCP server for X (Twitter) automation that needs no X API key. Covers pip install, MCP client registration for Claude Desktop, Claude Code, Codex, Cursor and Windsurf, cookie-based account setup, keywords, and persona. Use when the user wants to set up x-use, add an X account, connect X or Twitter automation to their agent, or when x-use tools are unavailable or unconfigured.
---

# Set up x-use

x-use is an MCP server that automates X (Twitter) through a real logged-in
Chrome session instead of the paid X API. This skill takes a machine from
nothing to a configured, working account.

Read this first: **the steps split at the client restart.** Everything before it
is shell work, because x-use's MCP tools do not exist yet. Everything after it is
tool work. Do not try to call `add_account` before step 4 has completed.

Explain each step in one line as you go, and confirm before anything that changes
the user's system.

## 1. Check the prerequisites

Python 3.10 or newer, and Google Chrome. Check both:

```bash
python --version
```

If either is missing, say so and stop. There is no workaround: x-use drives a
real Chrome window, and that is the whole design.

## 2. Install

```bash
pip install x-use-mcp
```

This provides the `x-use` command. If the user is working inside a clone of the
repository instead, `pip install -e .` from the repo root does the same thing.

Then verify:

```bash
x-use doctor
```

`doctor` prints one PASS, FAIL, or SKIP line per check: config files, browser
binary, driver, per-account cookies, LLM key, proxies. On a fresh machine the
cookie and config checks are expected to fail, since nothing is configured yet.
Do not treat that as a broken install. A FAIL on the browser or driver line is
the one that actually blocks progress.

If `x-use` is not found after installing, it is a PATH problem, not a failed
install. `pip show -f x-use-mcp` locates the console script; use its full path
everywhere below, for example `venv/bin/x-use` or `venv\Scripts\x-use.exe`.

## 3. Install the workflow skills

```bash
x-use skills install
```

This writes five skills to `~/.claude/skills/` and `~/.agents/skills/`, so Claude
Code and Codex-style agents both pick them up. They cover engagement, content,
daily review, and account setup. `--force` overwrites existing copies, and
`x-use skills list` shows what landed.

## 4. Register the MCP server, then restart the client

Work out which client you are running inside and register x-use in that one. The
config is identical everywhere: command `x-use`, args `["mcp"]`.

- **Claude Code:**

  ```bash
  claude mcp add x-use -- x-use mcp
  ```

- **Claude Desktop** (`claude_desktop_config.json`), **Cursor**, **Windsurf**,
  and any other client taking JSON:

  ```json
  {
    "mcpServers": {
      "x-use": {
        "command": "x-use",
        "args": ["mcp"]
      }
    }
  }
  ```

- **Codex** (`~/.codex/config.toml`):

  ```toml
  [mcp_servers.x-use]
  command = "x-use"
  args = ["mcp"]
  ```

**Now tell the user to restart the client, and stop.** A stdio MCP server is
loaded at client startup, so nothing you do will make the tools appear in the
current session. When they come back, confirm the connection with
`list_accounts`. An empty list is the correct answer at this point.

## 5. Add the account

Everything from here is MCP tool calls.

Ask for a short account id, something like `main` or `brand`. It is an internal
label, not the X handle.

Then walk the user through the cookie export, which is how x-use authenticates:

1. Install a cookie-export browser extension, for example Cookie-Editor.
2. Log into x.com in that browser.
3. Open the extension while on x.com and export as JSON.
4. Save the file on this machine and give you the **path**.

```
add_account(account_id="main", cookie_file="/path/to/cookies.json")
```

The file is validated and copied server-side. **Never ask the user to paste
cookie contents into the conversation.** The path-only design exists precisely so
the values never cross the wire, and pasting them defeats it.

Verify with `get_account_health("main")`. Cookie status should be valid. If it is
not, the export is stale or from the wrong domain, so have them redo it while
actually logged in.

## 6. Configure the account

Ask what niche they are in, which keywords to watch, which profiles they want to
engage with, and their actual X handle.

```
update_account(
  account="main",
  target_keywords=["..."],
  competitor_profiles=["https://x.com/..."],
  self_handles=["theirhandle"],
)
```

`self_handles` matters more than it looks. Without it the own-post guard cannot
fire, and nothing can later find the posts this account published, because
`approve_draft` returns no URL.

## 7. Set the persona

Ask how they want to sound. The repo ships three starting points under
`presets/personas/` (builder, founder, curator), so offer them or write one
together. Cover voice, topics, reply style, what to avoid, and one or two example
replies. Under 4000 characters, markdown.

```
update_account(account="main", persona="...")
```

Batch the config edits where you can. Every `update_account` closes the warm
browser session, so the next action pays a cold start.

## 8. Optional, only if it applies

- **More accounts:** repeat from step 5. If they want separate network routes,
  `add_proxy(pool, proxy_url)` then
  `update_account(account, proxy="pool:<name>")`.
- **An LLM key:** interactive use needs none. You are the writer, and the server
  drives the browser. A key is only needed for unattended background automation
  and `"auto"` text, set under `llm` in `config/settings.json`.

## 9. Prove it works

Stage one real but harmless draft, a reply or a post, and show it with
`list_drafts`.

Then tell the user plainly: **write tools return a draft and change nothing on X.
Only `approve_draft(draft_id)` publishes.** That gate is on by default and it is
the reason this is safe to hand to an agent.

## When something fails

Run `x-use doctor`, read the actual error, fix that cause, and only then
continue. Every tool returns `{"ok": true, ...}` or
`{"ok": false, "error": {"type", "message"}}`. On an error envelope, explain it
and stop rather than retrying in a loop.

## After setup

The installed skills take over: **x-use-engage** for research and replies,
**x-use-content** for original posts, **x-use-review** for the daily digest, and
**x-use** for tool routing and conventions.

Clients without skill support get the same workflows as MCP prompts
(`research_niche`, `draft_replies`, `review_and_publish`, `daily_check`,
`setup_account`), plus read-only resources (`xuse://accounts`,
`xuse://accounts/{account_id}/persona`, `xuse://drafts/pending`) for context
worth attaching rather than fetching.

Full tool reference: [docs/MCP_GUIDE.md](docs/MCP_GUIDE.md).
