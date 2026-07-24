# x-use

**Browser-native AI agents for X (Twitter). Multi-account, MCP-ready, no X API key required.**

x-use drives a real, stealth-hardened browser instead of the paid X API. It posts, replies, searches, and engages across as many accounts as you configure, writes content with your own LLM, and exposes everything as MCP tools, so Claude Desktop, Claude Code, Cursor, and other MCP clients can run your X presence directly.

The X API's pricing tiers put write access out of reach for exactly the people who want to automate a couple of accounts. x-use sidesteps the API entirely: if a logged-in browser can do it, an MCP client can ask for it. And because write actions go through a draft-approval step by default, an agent can prepare work all day while nothing reaches X until a human says yes.

> x-use is the v2 relaunch of **twitter-automation-ai**. The repository was renamed; old URLs keep redirecting, and stars, forks, and issues came along intact.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python Version](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![CI](https://github.com/ihuzaifashoukat/x-use/actions/workflows/ci.yml/badge.svg)](https://github.com/ihuzaifashoukat/x-use/actions/workflows/ci.yml)
[![Issues](https://img.shields.io/github/issues/ihuzaifashoukat/x-use)](https://github.com/ihuzaifashoukat/x-use/issues)
[![Forks](https://img.shields.io/github/forks/ihuzaifashoukat/x-use)](https://github.com/ihuzaifashoukat/x-use/network/members)
[![Stars](https://img.shields.io/github/stars/ihuzaifashoukat/x-use)](https://github.com/ihuzaifashoukat/x-use/stargazers)

<!-- DEMO GIF PLACEHOLDER: record `x-use init`, paste the MCP snippet, make a draft, approve it, then embed the GIF here. -->

---

## Install

From PyPI (CLI and MCP server):

```bash
pip install x-use-mcp
```

For the full repo setup (presets, example configs, docs), the one-line installer clones the repo, installs x-use into its own virtual environment, and finishes with `x-use doctor` so you can see what is left to configure.

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

Requires Python 3.10+ and Chrome. Any of these gives you the `x-use` command.

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

If `x-use` is not on your client's PATH, use the full path the installer printed (for example `venv/bin/x-use` or `venv\Scripts\x-use.exe`). Restart the client, then ask it to `list_accounts`.

**Draft mode is on by default.** Write tools return a reviewable draft and change nothing until you call `approve_draft` with the returned `draft_id`. Opt out with `"mcp": { "draft_mode": false }` in `config/settings.json`.

## MCP tools

24 tools in four groups. Two safety gates: the immediate write tools run in draft mode by default (review, then `approve_draft`), and the queue only stores work until an explicit `process_queue` call (or the opt-in `auto_drain` worker) executes it with jittered pacing and daily caps.

Read-only and status:

| Tool | What it does |
|---|---|
| `list_accounts()` | List configured accounts with secrets stripped. |
| `get_account(account)` | One account's masked config plus cookie-file status. |
| `get_metrics(account)` | Counters and recent events for an account. |
| `search_tweets(keywords, limit=10, account?)` | Search recent posts for a query. |
| `list_queue(account?, status?)` | Queued actions with full payloads (exactly what will fire) plus per-status counts. |
| `list_drafts(status?, account?, limit=20)` | Drafts, newest first, with filters. |
| `get_draft(draft_id)` | One draft by id. |
| `reject_draft(draft_id)` | Reject a pending draft (local status flip; nothing touches X). |
| `get_run_status(run_id?)` | Poll `run_cycle` handles. |
| `get_account_health(account)` | Config, cookie validity, metrics, session, queue, and draft state in one call. |

Write tools (draft-gated):

| Tool | What it does |
|---|---|
| `post_tweet(account, text, media?, community?)` | Post text/media, optionally into a community. |
| `generate_and_post(account, topic)` | Generate a post with the configured LLM, then post it. |
| `reply_to_tweet(account, tweet_url, text="auto")` | Reply with explicit text, or `"auto"` to generate from the tweet's content. |
| `engage(account, keywords, actions=["like"], max_actions=5)` | Relevance-gated likes/retweets on keyword results. |
| `run_cycle(account?, pipelines?)` | Full orchestrator cycle in the background; returns a run handle. |
| `approve_draft(draft_id)` | Execute a pending draft exactly once. |

Scheduled queue (the second gate):

| Tool | What it does |
|---|---|
| `queue_post(account, text? topic?, media?, community?, not_before?)` | Queue a post for paced execution. `topic` generates the text now, so `list_queue` shows the final payload. `not_before` schedules (ISO 8601). |
| `queue_engagement(account, action, tweet_url, text?)` | Queue a like, retweet, or reply. Replies accept `"auto"` to generate the text now. |
| `cancel_queued_action(queue_id)` | Cancel a pending or failed item. |
| `process_queue(account?, max_actions=5)` | The approval gate: drains due items through the shared pacing/dedup/metrics path with daily caps. |

Account management (validated, backed up, atomic):

| Tool | What it does |
|---|---|
| `add_account(account_id, cookie_file?, proxy?, target_keywords?, is_active=true)` | Add an account. Cookies import from a file path on the server, never inline. |
| `update_account(account, ...)` | Partial update; closes the warm session so changes take effect. |
| `set_account_active(account, active)` | Pause or resume an account without deleting it. |
| `remove_account(account, confirm)` | Remove an account (requires `confirm=true`; backups in `config/backups/`). |

Drafts persist in `data/drafts.jsonl`; the queue persists in `data/engagement_queue.jsonl`. Both survive restarts.

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

The legacy `python src/main.py` entry point still works via a deprecation shim (removal no earlier than v2.1).

## Features

| Area | What you get |
|---|---|
| MCP server | 24 tools over stdio on the official MCP Python SDK (`FastMCP`, pinned `mcp>=1,<2`): draft-gated writes, a persistent scheduled-action queue with daily caps, account management, and a lazy per-account browser session pool. |
| Draft mode | On by default. Write tools build the full payload (including LLM-generated text), store a draft, and touch nothing until `approve_draft` runs. |
| Multi-account engine | Post (including communities and media), reply, repost/quote, like, keyword search, and relevance-gated engagement. Per-account overrides for keywords, LLM settings, and action behavior. |
| LLM generation | OpenAI, Azure OpenAI, or Gemini with structured JSON prompting and strict extraction. Keys resolve from env/`.env` first, then `config/settings.json`. |
| Stealth | undetected-chromedriver, selenium-stealth, randomized user agents, headless support. |
| Proxies | Per-account proxy, named pools, hash or round-robin rotation, `${VAR}` env interpolation in proxy strings. |
| Metrics | Per-account counters in `data/metrics/<account_id>.json` plus JSONL event logs in `logs/accounts/<account_id>.jsonl`. |

## x-use vs. API-based X MCP servers

| | **x-use** | API-based X/Twitter MCP servers |
|---|---|---|
| X API cost | **$0**: cookie auth, no X API key needed | Paid X API tier required |
| Multi-account | Built-in: per-account config, cookies, proxies | Typically one account |
| Proxies | Per-account proxies, named pools, hash/round-robin rotation | N/A |
| Stealth | undetected-chromedriver + selenium-stealth, randomized user agents | N/A (official API) |
| Write safety | Draft mode **on by default**, explicit `approve_draft` gate | Usually posts directly |
| Metrics | Per-account counters + JSONL event logs, readable via MCP | Varies |

You still bring your own LLM key (OpenAI, Azure OpenAI, or Gemini) for content generation and analysis; that is the only key involved.

## Configuration

- `config/accounts.json`: your accounts (gitignored). Start from [`config/accounts.example.json`](config/accounts.example.json) or let `x-use init` write it.
- `config/settings.json`: global defaults for browser, pacing, action caps, LLM, proxies, and the `mcp` section.
- `.env`: LLM API keys; overrides `settings.json` (env wins). See [`.env.example`](.env.example).

Full schema: [docs/CONFIG_REFERENCE.md](docs/CONFIG_REFERENCE.md). Starter templates: [`presets/`](presets/) (offered as wizard choices by `x-use init`).

## Recommended proxy providers

X's per-IP detection kills automation at scale. A quality proxy provider keeps a multi-account setup running reliably. The providers below are tested and recommended.

### ScrapingAnt (featured)

Private residential network with premium-grade IPs, not resold traffic. Supports the sticky sessions that X's IP consistency checks require.

<table>
<tr>
<td align="center">
<a href="https://scrapingant.com/residential-proxies?ref=mdkzote">
<img src="https://i.ibb.co/mrK3tv4g/Screenshot-2026-05-08-at-14-17-29.png" alt="ScrapingAnt Residential Proxies" width="350" />
</a>  <br/><a href="https://scrapingant.com/residential-proxies?ref=mdkzote">Top-notch, fast residential ScrapingAnt’s proxies for best performance</a>

</td>
<td align="center">
<a href="https://scrapingant.com/datacenter-proxies?ref=mdkzote">
<img src="https://i.ibb.co/ch0cFtPm/Screenshot-2026-05-08-at-14-17-46.png" alt="ScrapingAnt Datacenter Proxies" width="350" />
</a>
  <br/><a href="https://scrapingant.com/datacenter-proxies?ref=mdkzote">Affordable datacenter proxies for cost-effective operations</a>

</td>
</tr>
</table>
> 5% discount for x-use users: apply code `TWI_AUTO` at checkout.

### RapidProxy

[RapidProxy](https://www.rapidproxy.io/?ref=aut)

<img src="https://i.ibb.co/TqYSs4yr/image-10.png" alt="RapidProxy Banner" width="300" />

Dynamic and static residential proxies with free testing and unlimited, non-expiring traffic.

## Responsible use

Browser automation of X carries real account risk. Read [BEST_PRACTICES.md](BEST_PRACTICES.md) before running anything: it covers conservative rate limits (the shipped defaults), account warm-up, relevance filters, cookie and credential hygiene, and X ToS considerations. Keep delays high, caps low, and draft mode on.

## Development

```bash
pip install -e '.[dev]'
pytest
```

231 tests cover config loading and merging, dedup keys, LLM JSON extraction, tweet parsing, proxy pool selection, the MCP tool contract, drafts, sessions, the action queue, account writes, and the CLI; none of them needs a network or a browser. CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs the suite plus an import smoke check on Python 3.10/3.11/3.12.

The v2.0 relaunch is merged; PyPI publishing and MCP directory submissions are the remaining Phase 1 items. Dashboard and Docker are Phase 2; personas, plugins, and selector self-healing are Phase 3. See [ROADMAP.md](ROADMAP.md).

## Contributing

Contributions are welcome. Selector fixes, presets, docs, and MCP tool ideas (via issues) are the most valuable contributions right now. See [CONTRIBUTING.md](CONTRIBUTING.md) for the workflow and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for community expectations.

## Star history

[![Star History Chart](https://api.star-history.com/svg?repos=ihuzaifashoukat/x-use&type=Date)](https://star-history.com/#ihuzaifashoukat/x-use&Date)

## License

MIT. See [LICENSE](LICENSE).
