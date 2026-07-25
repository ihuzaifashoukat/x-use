# x-use

<!-- mcp-name: io.github.ihuzaifashoukat/x-use -->

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

33 tools in six groups, full reference with signatures and examples: [docs/MCP_GUIDE.md](docs/MCP_GUIDE.md). Two safety gates: write tools run in draft mode by default (review, then `approve_draft`), and the queue only stores work until an explicit `process_queue` call.

| Group | Tools |
|---|---|
| Read-only & status | `list_accounts`, `get_account`, `get_metrics`, `search_tweets`, `search_profile`, `get_tweet`, `prepare_reply`, `list_queue`, `list_drafts`, `get_draft`, `reject_draft`, `get_run_status`, `get_account_health`, `list_proxies` |
| Write (draft-gated) | `post_tweet`, `generate_and_post`, `reply_to_tweet`, `engage`, `run_cycle`, `approve_draft` |
| Scheduled queue | `queue_post`, `queue_engagement`, `cancel_queued_action`, `process_queue` |
| Composite (server LLM) | `research_and_stage`, `draft_post_variations` |
| Account management | `add_account`, `update_account`, `set_account_active`, `remove_account` |
| Proxy management | `add_proxy`, `remove_proxy`, `test_proxy` |

Interactive use needs no LLM key: your MCP client (Claude, Codex, ...) does the thinking, sees tweet images via `get_tweet`/`prepare_reply`, and passes explicit text to the write tools. The optional server-side LLM (`llm` block) only powers the composite tools, `"auto"` text, and background automation.

Drafts persist in `data/drafts.jsonl`; the queue persists in `data/engagement_queue.jsonl`. Both survive restarts.

## Agent skills

`x-use init` (or `x-use skills install`) installs five agent skills for Claude Code and Codex: **x-use** (overview), **x-use-setup** (zero-knowledge onboarding interview), **x-use-engage** (research + reply workflow), **x-use-content** (content creation), **x-use-review** (daily digest). Claude Code users can also install from the marketplace: `/plugin marketplace add ihuzaifashoukat/x-use`.

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
| MCP server | 33 tools over stdio on the official MCP Python SDK (`FastMCP`, pinned `mcp>=1.6,<2`): draft-gated writes, a persistent scheduled-action queue with daily caps, account management, and a lazy per-account browser session pool. |
| Draft mode | On by default. Write tools build the full payload (including LLM-generated text), store a draft, and touch nothing until `approve_draft` runs. |
| Multi-account engine | Post (including communities and media), reply, repost/quote, like, keyword search, and relevance-gated engagement. Per-account overrides for keywords, LLM settings, and action behavior. |
| LLM generation | One OpenAI-compatible client (`llm`: api_key, base_url, model) covers OpenAI, OpenRouter, Azure, Gemini, and local servers. Only needed for `"auto"` text and background automation; interactive MCP use runs keyless. Keys resolve from env/`.env` first, then `config/settings.json`. |
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

An LLM key is optional: interactive MCP use needs none (your agent writes the text). For `"auto"` generation and background automation, set one OpenAI-compatible key (`llm.api_key` + `base_url` + `model`, or `OPENAI_API_KEY`/`OPENAI_BASE_URL`/`OPENAI_MODEL`); that is the only key involved.

## Configuration

- `config/accounts.json`: your accounts (gitignored). Start from [`config/accounts.example.json`](config/accounts.example.json) or let `x-use init` write it.
- `config/settings.json`: global defaults for browser, pacing, action caps, LLM, proxies, and the `mcp` section.
- `.env`: LLM API keys; overrides `settings.json` (env wins). See [`.env.example`](.env.example).

Full schema: [docs/CONFIG_REFERENCE.md](docs/CONFIG_REFERENCE.md). Starter templates: [`presets/`](presets/) (offered as wizard choices by `x-use init`).

## Recommended proxy provider

Multi-account automation needs quality residential proxies, X's per-IP detection kills datacenter IPs fast. We use and recommend ScrapingAnt (5% off with code `TWI_AUTO`):

<a href="https://scrapingant.com/residential-proxies?ref=mdkzote"><img src="https://i.ibb.co/mrK3tv4g/Screenshot-2026-05-08-at-14-17-29.png" alt="ScrapingAnt Residential Proxies" width="250" /></a>

## Responsible use

Browser automation of X carries real account risk. Read [docs/BEST_PRACTICES.md](docs/BEST_PRACTICES.md) before running anything: it covers conservative rate limits (the shipped defaults), account warm-up, relevance filters, cookie and credential hygiene, and X ToS considerations. Keep delays high, caps low, and draft mode on.

## FAQ

### Does x-use need an X (Twitter) API key?

No. x-use drives a real Chrome session authenticated with cookies you export from your own browser, so it never calls the X API and costs $0 in API fees. An LLM key is optional too: interactive MCP use is keyless, because your AI client writes the text and passes it to the tools.

### Which MCP clients work with x-use?

Any client that can run a stdio MCP server. Claude Desktop, Claude Code, Cursor, and Windsurf are the tested ones. The config is the same everywhere: `{"command": "x-use", "args": ["mcp"]}`.

### How many X accounts can x-use manage?

As many as you configure. Each account carries its own cookies, proxy, persona, keywords, and daily action caps in `config/accounts.json`, and gets its own browser session from a lazy pool that reaps idle sessions.

### Will automating X get my account suspended?

It can, and you should plan for that. x-use ships conservative defaults (jittered pacing, per-action daily caps, relevance filters) and keeps draft mode on so nothing publishes without your approval, but no tool can make browser automation risk-free. Read [docs/BEST_PRACTICES.md](docs/BEST_PRACTICES.md) first, warm accounts up slowly, and keep the caps low.

### What is the difference between x-use and twitter-automation-ai?

They are the same project. `twitter-automation-ai` was renamed to `x-use` for the v2 relaunch, which added the MCP server, the `x-use` CLI, draft mode, and the PyPI package. Old URLs still redirect, and stars, forks, and issues came across intact.

### Is x-use free?

Yes, MIT licensed, installed with `pip install x-use-mcp`. The only costs are optional: an LLM key if you want server-side text generation, and residential proxies if you run several accounts at once.

## Development

```bash
pip install -e '.[dev]'
pytest
```

599 tests cover config loading and merging, dedup keys, LLM JSON extraction and the single-client service, tweet parsing, proxy pool selection, the MCP tool contract, drafts, sessions, the action queue, account writes, credential masking, and the CLI; none of them needs a network or a browser. CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs the suite plus an import smoke check on Python 3.10/3.11/3.12.

x-use is published on PyPI and listed in the official MCP Registry as `io.github.ihuzaifashoukat/x-use`. Dashboard and Docker come next, then personas, plugins, and selector self-healing. See [ROADMAP.md](ROADMAP.md).

## Contributing

Contributions are welcome. Selector fixes, presets, docs, and MCP tool ideas (via issues) are the most valuable contributions right now. See [CONTRIBUTING.md](CONTRIBUTING.md) for the workflow and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for community expectations.

## Star history

[![Star History Chart](https://api.star-history.com/svg?repos=ihuzaifashoukat/x-use&type=Date)](https://star-history.com/#ihuzaifashoukat/x-use&Date)

## License

MIT. See [LICENSE](LICENSE).
