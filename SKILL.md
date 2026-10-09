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

Explain each step briefly as you go. The setup request authorizes routine local
installation and registration; ask only for missing choices or credentials.

## 1. Check the prerequisites

Python 3.10 or newer with venv support. Check Python:

```bash
python --version
```

If Python is missing, explain the prerequisite. Setup installs the Chromium
browser matching the default Patchright driver. Installed Chrome or Edge is
also supported through `mcp.browser_channel`.

## 2. Install

```bash
pip install x-use-mcp
python -m patchright install chromium
```

This provides the `x-use` command and matching Chromium. Inside a checkout,
prefer the one-command uv setup: `py -3 scripts/setup_uv.py` on Windows or
`python3 scripts/setup_uv.py` on macOS/Linux. It installs missing uv locally,
uses the dependency lock and creates `.venv`; use that environment's full
`x-use` executable path for registration. Linux system libraries are explicit
with `--with-system-deps`, which can require OS permission.

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
everywhere below, for example `.venv/bin/x-use` or `.venv\Scripts\x-use.exe`.

## 3. Install the workflow skills

```bash
x-use skills install
```

This writes seven skills to `~/.claude/skills/` and `~/.agents/skills/`, so Claude
Code and Codex-style agents both pick them up. They cover engagement, content,
daily review, inbox/messages/notifications, account setup, and X thread workflows.
Use **x-use-inbox** for reading incoming activity and sending contextual DMs.
`--force` overwrites existing copies, and
`x-use skills list` shows what landed.

## 4. Register the MCP server, then restart the client

Work out which client you are running inside and register x-use in that one.
Use the full executable path and absolute data directory printed by the
installer. Set `X_USE_HOME` in the server environment so settings, accounts
and state stay stable when the client starts from another working directory.

- **Claude Code:**

  ```bash
  claude mcp add --scope user x-use --env X_USE_HOME=/absolute/path/to/x-use-data -- /absolute/path/to/x-use/.venv/bin/x-use mcp
  ```

- **Claude Desktop** (`claude_desktop_config.json`), **Cursor**, **Windsurf**,
  and any other client taking JSON:

  ```json
  {
    "mcpServers": {
      "x-use": {
        "command": "/absolute/path/to/x-use/.venv/bin/x-use",
        "args": ["mcp"],
        "env": {"X_USE_HOME": "/absolute/path/to/x-use-data"}
      }
    }
  }
  ```

- **Codex** (`~/.codex/config.toml`):

  ```toml
  [mcp_servers.x-use]
  command = "/absolute/path/to/x-use/.venv/bin/x-use"
  args = ["mcp"]

  [mcp_servers.x-use.env]
  X_USE_HOME = "/absolute/path/to/x-use-data"
  ```

If this client cannot reload MCP servers in the current session, tell the user
to restart the client and pause tool-dependent setup until the tools appear.
When connected, confirm with `list_accounts`. An empty list is the correct
answer before the first account is added.

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

Verify with `get_account_health(account="main")`. This checks the local cookie
file and configuration without logging into X; valid cookie structure does not
prove a live authenticated session. Inspect reported problems, then use a
bounded browser read to verify the session. Stop for a login or account challenge.

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
fire, and x-use cannot identify the account's historical posts from this
configuration. For a published thread, use `get_thread_run` to retrieve the
confirmed post URLs.

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
  and `"auto"` text, set under `llm` in the data home's `config/settings.json`.

## 9. Prove it works

With the default `mcp.draft_mode=true`, stage one requested reply or post and
show its full payload with `get_draft` or `list_drafts`. If draft mode has been
disabled, ordinary post/reply tools execute immediately and cannot serve as a
draft-only smoke check. `prepare_thread`, messages, and follows always stage,
but do not create unrelated work solely to test the installation.

Explain the execution boundary: ordinary writes draft by default, and
`approve_draft(draft_id)` executes a reviewed draft. Messages, follows and
threads always require that approval. Queued work executes through
`process_queue` or an explicitly enabled auto-drain worker; the legacy
`run_cycle` executes immediately. Honor prior authorization for an exact action
and payload; setup alone does not authorize publication or outreach.

For requested inbox verification, `get_inbox(account="main", folder="inbox",
inbox_filter="unread", limit=5)` reads only the visible folder. Requests and
other folders are separate; reads do not accept requests. Unknown read state
stays unknown. Opening a conversation or visiting `get_notifications` may mark
items read. An encrypted inbox uses `unlock_inbox(account="main",
pin_env_var="XUSE_INBOX_PIN")` after the owner sets the PIN in the server
environment; never request the PIN in chat.

## When something fails

Run `x-use doctor`, read the actual error, fix that cause, and only then
continue. Tools carry `{"ok": true, ...}` or
`{"ok": false, "error": {"type", "message"}}`; media reads can also attach images.
Inspect returned state even on success envelopes: partial context, unavailable
analytics or a blocked thread is not completed work. For an uncertain write,
read `get_account_safety`, inspect X and its action ID, and use
`resolve_action_outcome` only with observed `succeeded` or `not_sent` evidence.
Do not automatically retry a timeout or unknown outcome.

## After setup

The installed skills take over: **x-use-engage** for research and replies,
**x-use-content** for original posts, **x-use-review** for the daily digest, and
**x-use** for tool routing and conventions.

Clients without skill support get the same workflows as MCP prompts
(`research_niche`, `draft_replies`, `review_and_publish`, `daily_check`,
`setup_account`, `outreach_message`, `thread_workflow`), plus read-only resources
(`xuse://accounts`, `xuse://accounts/{account_id}`,
`xuse://accounts/{account_id}/persona`, `xuse://drafts/pending`) for context
worth attaching rather than fetching.

Full tool reference: [docs/MCP_GUIDE.md](docs/MCP_GUIDE.md).
