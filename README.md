# x-use

<!-- mcp-name: io.github.ihuzaifashoukat/x-use -->

**Browser-native AI agents for X (Twitter). Multi-account, MCP-ready, no X API key required.**

x-use uses an async Patchright Chromium browser authenticated with your own X session. Its MCP tools cover posts, replies, searches, inbox reads, reviewed messages, follows, local leads, and campaign drafts. Isolated account contexts, browser ownership locks, durable local action budgets, and uncertain-outcome tracking protect shared sessions.

No official X API is used. Write tools stage drafts by default; messages and follows always require individual draft approval. Browser challenges and rate limits pause work for operator recovery. [Browser runtime and outreach guide](docs/BROWSER_RUNTIME.md) describes setup, limits, recovery, and current limitations.

The detailed [runtime audit](docs/RESILIENCE_AUDIT.md) records the controls, test evidence and remaining compatibility gaps. The [tool validation matrix](docs/TOOL_VALIDATION.md) distinguishes live-account results from isolated tests for every registered tool.

> **Educational-use disclaimer:** intended for personal, educational, noncommercial experiments on accounts you own or are authorized to manage. Do not use it for unsolicited bulk outreach or to bypass platform protections. X's rules still apply, and this project makes no promise of undetectable activity or protection from account restrictions.

> x-use is the v2 relaunch of **twitter-automation-ai**. The repository was renamed; old URLs keep redirecting, and stars, forks, and issues came along intact.

[![MCP Badge](https://lobehub.com/badge/mcp/ihuzaifashoukat-x-use)](https://lobehub.com/mcp/ihuzaifashoukat-x-use)
[![Glama score](https://glama.ai/mcp/servers/ihuzaifashoukat/x-use/badges/score.svg)](https://glama.ai/mcp/servers/ihuzaifashoukat/x-use)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python Version](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![CI](https://github.com/ihuzaifashoukat/x-use/actions/workflows/ci.yml/badge.svg)](https://github.com/ihuzaifashoukat/x-use/actions/workflows/ci.yml)
[![Issues](https://img.shields.io/github/issues/ihuzaifashoukat/x-use)](https://github.com/ihuzaifashoukat/x-use/issues)
[![Forks](https://img.shields.io/github/forks/ihuzaifashoukat/x-use)](https://github.com/ihuzaifashoukat/x-use/network/members)
[![Stars](https://img.shields.io/github/stars/ihuzaifashoukat/x-use)](https://github.com/ihuzaifashoukat/x-use/stargazers)

---

## Install

From PyPI (CLI and MCP server):

```bash
pip install x-use-mcp
```

From an existing checkout, one command installs uv if needed, synchronizes the locked dependencies into `.venv`, installs matching Chromium, creates missing sample configuration and runs `x-use doctor`:

```powershell
py -3 scripts/setup_uv.py
```

On macOS/Linux use `python3 scripts/setup_uv.py`. Requires Python 3.10+ with `venv`/`ensurepip` and network access for downloads. A fresh environment uses the invoking Python when it is Python 3.10-3.14, and falls back to Python 3.12 otherwise; an existing `.venv` is reused. Add `--dev` for development tools. Linux hosts missing browser libraries can explicitly use `--with-system-deps`, which may require OS administrator permission. The platform installers set `X_USE_HOME` to a stable per-user data directory (or retain your existing value), and print the same value in their MCP config snippet. Setup creates a minimal default `config/settings.json` and an inactive sample account only when those files are missing; run `x-use init` to add your own account and cookies.

To clone and set up the full repo in one command, the platform installers delegate to the same uv setup:

Windows (PowerShell):

```powershell
iex "& { $(irm https://raw.githubusercontent.com/ihuzaifashoukat/x-use/main/install.ps1) }"
```

macOS / Linux / Git Bash:

```bash
curl -fsSL https://raw.githubusercontent.com/ihuzaifashoukat/x-use/main/install.sh | bash
```

Or the manual way:

```bash
git clone https://github.com/ihuzaifashoukat/x-use.git
cd x-use
pip install -e .
```

Requires Python 3.10+ and a compatible Chromium browser. Install the default driver's Chromium with `python -m patchright install chromium`, or set `mcp.browser_channel` to `chrome` or `msedge` to use an installed browser. Optional `mcp.browser_backend="playwright"` requires the `playwright` package extra and its own matching browser installation. The legacy CLI batch engine remains available through Selenium.

## Automatic setup

Point any capable agent at this repository and it can install and configure x-use
itself. [`SKILL.md`](SKILL.md) at the root is the install-and-configure skill: it
covers prerequisites, `pip install`, MCP client registration for Claude Desktop,
Claude Code, Codex, Cursor, and Windsurf, the client restart, cookie-based
account setup, keywords, and persona.

```bash
npx skills add ihuzaifashoukat/x-use
```

Or paste the one-shot prompt in [docs/SETUP_PROMPT.md](docs/SETUP_PROMPT.md) into
your client. Prefer to drive it yourself? Keep reading.

## Set up

```bash
x-use init     # interactive wizard: presets, account + cookie import, LLM keys
x-use doctor   # verify browser/driver, cookies, LLM keys, proxies
```

Then connect your AI client. Paste this into `claude_desktop_config.json` (Claude Desktop > Settings > Developer > Edit Config); the same `command`/`args` pair works for any MCP client that runs stdio servers:

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

If `x-use` is not on your client's PATH, use the full path the installer printed (for example `.venv/bin/x-use` or `.venv\Scripts\x-use.exe`). Restart the client, then ask it to `list_accounts`.

**Draft mode is on by default.** Write tools return a reviewable draft and change nothing until you call `approve_draft` with the returned `draft_id`. Opt out with `"mcp": { "draft_mode": false }` in `config/settings.json`.

## MCP tools

The MCP server exposes 70 tools for account and browser status, public timeline and profile reads, inbox, visible analytics availability, reviewed outreach, lead and campaign drafts, action recovery, and resumable X thread workflows. Start profile research with `get_profile_context`, then use `prepare_outreach` to stage one personalized DM for review. Use `get_thread` to read bounded visible conversation context and `prepare_thread` to stage a multi-post draft. Full references: [docs/MCP_GUIDE.md](docs/MCP_GUIDE.md) and [browser, outreach, and thread guide](docs/BROWSER_RUNTIME.md). Writes use draft review by default; queued work executes through an explicit `process_queue` call unless the operator enabled auto-drain.

| Group | Tools |
|---|---|
| Read-only & status | `list_accounts`, `get_account`, `get_metrics`, `get_account_analytics`, `search_tweets`, `search_profile`, `get_tweet`, `prepare_reply`, `list_queue`, `list_drafts`, `get_draft`, `get_run_status`, `get_account_health`, `list_proxies` |
| Write (draft mode by default) | `post_tweet`, `generate_and_post`, `reply_to_tweet`, `engage`, `approve_draft` |
| Draft management | `reject_draft` |
| Legacy batch (Selenium only; executes directly) | `run_cycle` |
| Scheduled queue | `queue_post`, `queue_engagement`, `cancel_queued_action`, `process_queue` |
| Composite (server LLM) | `research_and_stage`, `draft_post_variations` |
| Account management | `add_account`, `update_account`, `set_account_active`, `remove_account` |
| Proxy management | `add_proxy`, `remove_proxy`, `test_proxy` |
| X threads | `get_thread`, `prepare_thread`, `get_thread_run`, `continue_thread`, `cancel_thread` |

Inbox reads are partial and limited to the currently visible conversations and
messages. `get_inbox` and `search_conversations` accept
`folder="inbox"|"requests"|"other"`, `inbox_filter="all"|"unread"|"read"`, and
`unread_first`. Native Unread selection and visible unread markers carry their
evidence in the result; unknown unread state remains unknown. Other requests
may be spam or lower priority. Listing requests does not accept them.
For older messages in a
conversation, pass `next_before_message_id` from the result as the next call's
`before_message_id`. This cursor only pages the current visible conversation
window. Opening a conversation can mark it read.

`get_notifications(account="personal", view="all", limit=20)` reads structured
notifications; use `view="mentions"` for the Mentions tab. Results include actors,
related posts or previews, timestamps, and supported event-type evidence.
Unread state and notification IDs stay `null` when X does not expose them.
Reads are bounded and partial, and visiting notifications may mark them read.

`get_account_analytics` reports only evidenced signed-in-owner analytics
availability. The observed screen is an X Premium paywall; it returns
`premium_required` without metrics. Unsupported layouts return
`unsupported_dom`. It does not expose an analytics dashboard or substitute
local action metrics from `get_metrics`.

Interactive use needs no LLM key: your MCP client (Claude, Codex, ...) does the thinking, sees tweet images via `get_tweet`/`prepare_reply`, and passes explicit text to the write tools. The optional server-side LLM (`llm` block) only powers the composite tools, `"auto"` text, and background automation.

Drafts persist in `data/drafts.jsonl`; the queue persists in `data/engagement_queue.jsonl`. Both survive restarts.

## MCP prompts and resources

Tools are what the model calls. Prompts and resources are the other two halves of the protocol, and x-use serves both.

**Prompts** are the workflows, usable in any MCP client. The bundled `SKILL.md` files only work in clients that implement Agent Skills; these need no installation and show up wherever your client surfaces prompts.

| Prompt | Arguments | What it does |
|---|---|---|
| `research_niche` | `account`, `keywords`, `profiles` | Read-only sweep of keywords and watched profiles, scored shortlist, stages nothing |
| `draft_replies` | `account`, `tweet_urls`, `lane` | Stage replies in the account's persona, into drafts or the queue |
| `review_and_publish` | `account` | Review what is staged and publish only what you approve by id |
| `daily_check` | `account` | Health, metrics, drafts, and queue in one read-only pass |
| `setup_account` | none | Conversational onboarding from nothing |
| `outreach_message` | `account`, `profile` | Verify one profile and prepare one reviewed DM draft |
| `thread_workflow` | `account`, `tweet_url` | Read bounded visible context, prepare a reviewed thread, and inspect durable progress |

**Resources** are read-only context your client can attach without spending a turn. None of them start a browser, and all run the same masking as the tools, so no cookie, password, or proxy credential leaves through them.

| Resource | Type | Contents |
|---|---|---|
| `xuse://accounts` | JSON | Every configured account, secrets stripped |
| `xuse://accounts/{account_id}` | JSON | One account's full masked config |
| `xuse://accounts/{account_id}/persona` | Markdown | The account's voice. Attach before writing anything as it |
| `xuse://drafts/pending` | JSON | Everything awaiting approval, with the exact text that would publish |

## Agent skills

`x-use init` (or `x-use skills install`) installs six agent skills for Claude Code and Codex: **x-use** (overview), **x-use-setup** (onboarding), **x-use-engage** (research and replies), **x-use-content** (content creation), **x-use-review** (daily digest), and **x-use-threads** (thread reading, review, and continuation). For Claude Code, use the [local plugin setup guide](plugins/x-use/README.md) to load the MCP server and skills from this checkout with an absolute data directory. Marketplace installation is available after the new plugin files are published.

**Zero-knowledge setup:** paste the prompt from [docs/SETUP_PROMPT.md](docs/SETUP_PROMPT.md) into your AI client, it installs, registers, verifies, and interviews you to configure your account.

## CLI

```bash
x-use init                            # interactive setup wizard
x-use run                             # all active accounts, concurrent
x-use run --account my_account        # one account only
x-use run --pipeline keyword_replies  # one pipeline only (in-memory; config files untouched)
x-use doctor                          # environment checks; exits non-zero on failure
x-use mcp                             # start the MCP stdio server
```

Pipelines for `--pipeline`: `community_engagement`, `competitor_reposts`, `content_curation`, `keyword_replies`, `keyword_retweets`, `likes`. The MCP `run_cycle` tool accepts the same names.

The legacy `python src/main.py` entry point still works via a deprecation shim. It is scheduled for removal in v3.0; the whole v2 series keeps it.

## Features

| Area | What you get |
|---|---|
| MCP server | Tools over stdio on the MCP Python SDK (`FastMCP`, `mcp>=1.30,<2` for tool annotations and structured results), including profile context, public connections, inbox, reviewed outreach, persistent leads/campaigns, session recovery, action budgets, and resumable thread workflows. |
| Draft mode | On by default. Write tools build the full payload (including LLM-generated text), store a draft, and touch nothing until `approve_draft` runs. |
| Multi-account engine | Post (including communities and media), reply, repost/quote, like, keyword search, and relevance-gated engagement. Per-account overrides for keywords, LLM settings, and action behavior. |
| LLM generation | One OpenAI-compatible client (`llm`: api_key, base_url, model) covers OpenAI, OpenRouter, Azure, Gemini, and local servers. Only needed for `"auto"` text and background automation; interactive MCP use runs keyless. Keys resolve from env/`.env` first, then `config/settings.json`. |
| Browser runtime | Async Patchright, isolated contexts, account ownership locks, bounded sessions and explicit challenge recovery. |
| Proxies | Per-account routes and named pools; async MCP uses stable account hashing, while round-robin is a legacy batch setting. Proxy strings support `${VAR}` interpolation. |
| Metrics | Per-account counters in `data/metrics/<account_id>.json` plus JSONL event logs in `logs/accounts/<account_id>.jsonl`. |

## x-use vs. API-based X MCP servers

| | **x-use** | API-based X/Twitter MCP servers |
|---|---|---|
| X API cost | **$0**: cookie auth, no X API key needed | Paid X API tier required |
| Multi-account | Built-in: per-account config, cookies, proxies | Typically one account |
| Proxies | Per-account proxies, named pools, hash/round-robin rotation | N/A |
| Browser management | Isolated Chromium contexts, per-account serialization, cross-process ownership locks | N/A (official API) |
| Write safety | Draft mode **on by default**, explicit `approve_draft` gate | Usually posts directly |
| Metrics | Per-account counters + JSONL event logs, readable via MCP | Varies |

An LLM key is optional: interactive MCP use needs none (your agent writes the text). For `"auto"` generation and background automation, set one OpenAI-compatible key (`llm.api_key` + `base_url` + `model`, or `OPENAI_API_KEY`/`OPENAI_BASE_URL`/`OPENAI_MODEL`); that is the only key involved.

## Configuration

- `config/accounts.json`: your accounts (gitignored). Start from [`config/accounts.example.json`](config/accounts.example.json) or let `x-use init` write it.
- `config/settings.json`: global defaults for browser, pacing, action caps, LLM, proxies, and the `mcp` section.
- `.env`: LLM API keys; overrides `settings.json` (env wins). See [`.env.example`](.env.example).

Full schema: [docs/CONFIG_REFERENCE.md](docs/CONFIG_REFERENCE.md). Starter templates: [`presets/`](presets/) (offered as wizard choices by `x-use init`).

## Optional proxy provider

A proxy is optional and does not make an authenticated account anonymous or
prevent platform restrictions. If your network needs one, [ScrapingAnt offers
residential proxy service](https://scrapingant.com/residential-proxies?ref=mdkzote).

## Responsible use

Browser automation of X carries real account risk. Read [docs/BEST_PRACTICES.md](docs/BEST_PRACTICES.md) before running anything: it covers conservative rate limits (the shipped defaults), account warm-up, relevance filters, cookie and credential hygiene, and X ToS considerations. Keep delays high, caps low, and draft mode on.

## FAQ

### Does x-use need an X (Twitter) API key?

No. x-use drives a real Chrome session authenticated with cookies you export from your own browser, so it never calls the X API and costs $0 in API fees. An LLM key is optional too: interactive MCP use is keyless, because your AI client writes the text and passes it to the tools.

### Which MCP clients work with x-use?

Any client that can run a stdio MCP server can use the documented configuration. Claude Desktop, Claude Code, Cursor, and Windsurf are supported by their stdio MCP configuration flows: `{"command": "x-use", "args": ["mcp"]}`.

### How many X accounts can x-use manage?

As many as you configure. Each account carries its own cookies, proxy, persona, keywords, and daily action caps in `config/accounts.json`, and gets its own browser session from a lazy pool that reaps idle sessions.

### Will automating X get my account suspended?

It can, and you should plan for that. x-use ships conservative action spacing and per-action daily caps, with draft review enabled by default. These controls do not guarantee freedom from account restrictions. Read [docs/BEST_PRACTICES.md](docs/BEST_PRACTICES.md) and keep the caps low.

### What is the difference between x-use and twitter-automation-ai?

They are the same project. `twitter-automation-ai` was renamed to `x-use` for the v2 relaunch, which added the MCP server, the `x-use` CLI, draft mode, and the PyPI package. Old URLs still redirect, and stars, forks, and issues came across intact.

### Is x-use free?

Yes, MIT licensed, installed with `pip install x-use-mcp`. The only costs are optional: an LLM key if you want server-side text generation, and residential proxies if you run several accounts at once.

## Development

```bash
pip install -e '.[dev]'
pytest
```

The regression suite covers MCP contracts, approvals, budgets, uncertainty recovery, browser ownership, inbox state, campaign suppression, setup and the legacy engine. Synthetic browser fixtures use local intercepted responses without personal accounts. CI is configured in [`.github/workflows/ci.yml`](.github/workflows/ci.yml) for Windows, macOS and Linux on Python 3.10/3.11/3.12/3.13/3.14, locked uv installs, distribution validation, clean-wheel MCP/CLI startup, Chromium smoke runs, and a non-root container. Local Windows checks have been run; remote matrix and container results require a GitHub workflow run.

x-use is published on PyPI and listed in the official MCP Registry as `io.github.ihuzaifashoukat/x-use`. Container targets and a Claude Code plugin are included; dashboard work and further selector recovery remain on the [roadmap](ROADMAP.md).

## Contributing

Contributions are welcome. Selector fixes, presets, docs, and MCP tool ideas (via issues) are the most valuable contributions right now. See [CONTRIBUTING.md](CONTRIBUTING.md) for the workflow and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for community expectations.

## Star history

[![Star History Chart](https://api.star-history.com/svg?repos=ihuzaifashoukat/x-use&type=Date)](https://star-history.com/#ihuzaifashoukat/x-use&Date)

## License

MIT. See [LICENSE](LICENSE).
