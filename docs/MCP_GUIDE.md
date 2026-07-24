# x-use MCP guide

x-use is an MCP server that drives a real, logged-in browser on X (Twitter):
post, reply, search, like, retweet, schedule, and manage multiple accounts —
no X API key required. This is the full reference for its 32 tools:
signatures, behavior, and gotchas.

## The two safety gates

1. **Draft gate (on by default).** Write tools (`post_tweet`,
   `generate_and_post`, `reply_to_tweet`, `engage`) build the full payload,
   store a draft, and change nothing on X. Only `approve_draft(draft_id)`
   executes a draft. Opt out with `"mcp": { "draft_mode": false }` in
   `config/settings.json`.
2. **Queue gate.** `queue_post` / `queue_engagement` only store work. Nothing
   runs until an explicit `process_queue` call (or the opt-in `auto_drain`
   worker), which applies jittered pacing and daily caps.

## Keyless interactive use vs. the `llm` block

Interactive use needs no LLM key: your MCP client (Claude, Codex, ...) does
the thinking, sees tweet images via `get_tweet`/`prepare_reply`, and passes
explicit text to the write tools. The optional server-side LLM (the `llm`
block in `config/settings.json`, or `OPENAI_API_KEY` / `OPENAI_BASE_URL` /
`OPENAI_MODEL`) only powers the composite tools (`research_and_stage`,
`draft_post_variations`), `"auto"` text generation, and background
automation.

## Install & client setup

```bash
pip install x-use-mcp    # Python 3.10+ and Chrome required
x-use doctor             # verify browser/driver, cookies, LLM key, proxies
```

Register the server in your client, then restart the client:

- Claude Desktop (`claude_desktop_config.json`):

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

- Claude Code: `claude mcp add x-use -- x-use mcp`
- Codex (`~/.codex/config.toml`):

  ```toml
  [mcp_servers.x-use]
  command = "x-use"
  args = ["mcp"]
  ```

If `x-use` is not on your client's PATH, use the full path the installer
printed (for example `venv/bin/x-use` or `venv\Scripts\x-use.exe`). Verify
the connection by asking the client to run `list_accounts`.

Zero-knowledge alternative: paste the prompt from
[SETUP_PROMPT.md](SETUP_PROMPT.md) into your AI client — it installs,
registers, verifies, and interviews you to configure your account.

## Conventions

- **Envelopes.** Every tool returns a JSON envelope: `{"ok": true, ...}` on
  success, `{"ok": false, "error": {"type", "message"}}` on failure. On an
  error envelope, read the message, explain it, and stop — do not retry in a
  loop.
- **Draft flow.** Write tools return a draft; review it, then
  `approve_draft(draft_id)` only what the user explicitly approves and
  `reject_draft(draft_id)` the rest. Drafts persist in `data/drafts.jsonl`.
- **Queue flow.** `queue_post` / `queue_engagement` store actions with full
  payloads (inspect with `list_queue`); `not_before` (ISO 8601) schedules.
  `process_queue` is the approval gate — it drains due items with pacing and
  daily caps. The queue persists in `data/engagement_queue.jsonl`.
- **Media.** Read tools return each tweet's typed `media` list (photos with
  alt text; videos as poster + URL). With `include_images=true` — the default
  for `get_tweet` and `prepare_reply`, opt-in for `search_tweets` — photos
  also attach as MCP image content so a vision-capable client sees them.
  Bounds: up to 4 photos per tweet; the first image of up to 5 tweets per
  search; re-encoded to JPEG, max 1024px, max 200KB, 5s fetch timeout.
  Clients without image support always have the URLs + alt text in the
  envelope.
- **Persona.** `get_tweet` and `prepare_reply` include the account's
  `persona` so your model writes in the account's voice. See
  [Personas](#personas).
- **Masked secrets.** Passwords, cookies, and proxy credentials are masked in
  every response (proxy URLs as `scheme://***@host:port`).
- **Paused accounts.** Accounts with `is_active=false` can be read but never
  written to; write and composite tools refuse them.

## Tool reference

All 32 tools, grouped as in the README summary table.

**Read-only & status**

### list_accounts()

List configured accounts with secrets stripped.

### get_account(account)

One account's masked config plus cookie-file status.

### get_metrics(account)

Counters and recent events for an account.

### search_tweets(keywords, limit=10, account?, include_images=false)

Search recent posts for a query. Each tweet carries its typed `media` list;
`include_images=true` additionally attaches the first photo of up to 5
tweets as image content.

### get_tweet(account, tweet_url, include_images=true)

Read-only fetch of one tweet: text, author (no `@@`), public counts, typed
`media` (photos with alt text; videos as poster + URL), and the account's
`persona`. With `include_images=true` (default) up to 4 photos attach as MCP
image content so a vision-capable client sees them; the envelope always
carries URLs + alt text. Never posts; works on paused accounts.

### prepare_reply(account, tweet_url, include_images=true)

Fetch a tweet's content plus the account context so your agent can write the
reply itself. No LLM, nothing posted. The response also carries the tweet's
typed `media` and the account's `persona`; with `include_images=true`
(default) photos attach as image content.

### list_queue(account?, status?)

Queued actions with full payloads (exactly what will fire) plus per-status
counts.

### list_drafts(status?, account?, limit=20)

Drafts, newest first, with filters.

### get_draft(draft_id)

One draft by id.

### reject_draft(draft_id)

Reject a pending draft (local status flip; nothing touches X).

### get_run_status(run_id?)

Poll `run_cycle` handles.

### get_account_health(account)

Config, cookie validity, metrics, session, queue, and draft state in one
call.

### list_proxies()

All pools (size, credential-masked members, rotation cursor), the global pool
strategy, each account's assignment, and the global fallback proxy. Read-only.

**Write (draft-gated)**

### post_tweet(account, text, media?, community?)

Post text/media, optionally into a community.

### generate_and_post(account, topic)

Generate a post with the server-side LLM (needs the `llm` block), then post
it. Prefer `post_tweet` with your own text when driving from an MCP client.

### reply_to_tweet(account, tweet_url, text="auto")

Reply with explicit text, or `"auto"` to generate from the tweet's content
via the server-side LLM.

### engage(account, keywords, actions=["like"], max_actions=5)

Relevance-gated likes/retweets on keyword results.

### run_cycle(account?, pipelines?)

Full orchestrator cycle in the background; returns a run handle.

### approve_draft(draft_id)

Execute a pending draft exactly once.

**Scheduled queue**

### queue_post(account, text? topic?, media?, community?, not_before?)

Queue a post for paced execution. `topic` generates the text now, so
`list_queue` shows the final payload. `not_before` schedules (ISO 8601).

### queue_engagement(account, action, tweet_url, text?)

Queue a like, retweet, or reply. Replies accept `"auto"` to generate the text
now.

### cancel_queued_action(queue_id)

Cancel a pending or failed item.

### process_queue(account?, max_actions=5)

The approval gate: drains due items through the shared pacing/dedup/metrics
path with daily caps.

**Composite (server LLM)**

### research_and_stage(account, keywords, max_items=3)

Searches each keyword, dedupes, keeps tweets the keyless heuristic scores
relevant (>= 0.5 against your keywords), writes a persona-aware reply per
tweet with the server-side LLM, and stages one reply draft per tweet (max 10).
Returns `{considered, skipped_irrelevant, drafts: [{draft_id, tweet_url,
author, text}]}`. Nothing is posted. Requires the `llm` block; refuses paused
accounts.

### draft_post_variations(account, topic, count=3)

Generates `count` (max 5) distinct persona-aware takes on a topic with the
server-side LLM and stages each as a post draft. Pick one with
`approve_draft`, `reject_draft` the rest. Requires the `llm` block.

**Account management**

### add_account(account_id, cookie_file?, proxy?, target_keywords?, persona?, is_active=true)

Add an account. Cookies import from a file path on the server, never inline.
`persona` is freeform markdown describing the account's voice/engagement
style (max 4000 chars).

### update_account(account, ...)

Partial update; closes the warm session so changes take effect. `persona`
sets the freeform persona text; pass an empty string to clear it.

### set_account_active(account, active)

Pause or resume an account without deleting it.

### remove_account(account, confirm)

Remove an account (requires `confirm=true`; backups in `config/backups/`).

**Proxy management**

### add_proxy(pool, proxy_url)

Validate (http/https/socks4/socks5; `${ENV_VAR}` URLs accepted), dedupe, and
append a proxy to a named pool (created if new). settings.json write is
validated + backed up (config/backups/). Assign with
`update_account(account, proxy="pool:<name>")` or a direct URL.

### remove_proxy(pool, proxy_url, confirm=false)

Remove a proxy from a pool. Requires `confirm=true`. Response includes
`affected_accounts` — accounts still assigned `pool:<name>` (emptying a
referenced pool is allowed but reported; those accounts fall back to no proxy).

### test_proxy(account?, proxy_url?)

Probe a proxy without risking an account: resolves the effective proxy
(explicit `proxy_url` wins; otherwise the account's direct URL or `pool:<name>`
assignment), launches a throwaway browser (no account cookies, never the warm
session, never x.com), fetches an IP-echo page, returns `{proxy (masked),
egress_ip, latency_ms}`. Failure returns a credential-masked error envelope.

## Personas

Each account has an optional freeform `persona` (markdown, max 4000 chars):
who the account is and how it engages — voice, topics, reply style,
avoid-list, an example reply or two. Set it with
`add_account(..., persona=...)` or `update_account(account, persona=...)`;
pass an empty string to clear it. `get_tweet` and `prepare_reply` return it
so the calling agent composes in-voice, and the server-side LLM prepends it
to its generation prompts.

Three starter presets ship in [`presets/personas/`](../presets/personas/):
`builder.md` (indie builder), `founder.md`, `curator.md`. The `x-use-setup`
skill offers them during onboarding and applies your pick (or one you
dictate) via `update_account`.

## Skills

Five agent skills ship inside the package and install into both Claude Code
and Codex skill directories:

- **x-use** — overview and routing: tool groups, the two safety gates,
  conventions.
- **x-use-setup** — zero-knowledge onboarding interview.
- **x-use-engage** — research + reply workflow (draft-first).
- **x-use-content** — content creation and staging.
- **x-use-review** — daily digest: health, metrics, drafts, queue.

```bash
x-use skills install   # copy the five skills into the client skill dirs
x-use skills list      # show what is installed where
```

`x-use init` offers the same install. Claude Code users can alternatively
install from the marketplace: `/plugin marketplace add ihuzaifashoukat/x-use`.

## Troubleshooting

- **Run `x-use doctor` first.** It checks Chrome/driver, cookie validity, LLM
  key configuration, and proxy reachability, and exits non-zero on failure.
- **Cookie expiry.** Symptoms: tools report the session is signed out or
  cookie status invalid in `get_account_health`. Re-export x.com cookies to a
  JSON file and apply with `update_account(account, cookie_file=<path>)`.
- **Chrome/driver version mismatch.** Chrome auto-updates can outrun the
  pinned undetected-chromedriver; `x-use doctor` reports the mismatch.
  Upgrade the package (`pip install -U x-use-mcp`) to pull a matching driver.
- **Proxy probe failures.** `test_proxy` never risks an account: failure
  returns a credential-masked error envelope. Check the scheme
  (http/https/socks4/socks5), that `${ENV_VAR}` variables are exported in the
  server's environment, and that the egress endpoint is reachable from your
  network.
- **Logs.** Per-account JSONL event logs live in
  `logs/accounts/<account_id>.jsonl`; metrics counters in
  `data/metrics/<account_id>.json`.
