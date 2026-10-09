# Preset library

`x-use init` offers these files as setup choices. For a new configuration,
pressing Enter selects `beginner-patchright.json` and `reviewed_outreach.json`.
Existing configurations default to skip. You can also copy a settings file to
`config/settings.json` and an account file to `config/accounts.json` yourself;
back up existing configuration before replacing it.

## Current MCP setup

Use `settings/beginner-patchright.json` with `accounts/reviewed_outreach.json`
for the V3 MCP runtime. The settings select headless Patchright, reviewed
drafts, durable action limits and local outreach storage. Queue auto-draining
starts disabled. Install its matching browser with
`python -m patchright install chromium`, or set `mcp.browser_channel` to
`chrome` or `msedge` to use an installed browser.

The outreach account starts inactive. Replace its ID and cookie file path,
customize its persona and set `is_active` to `true` when ready. The wizard
activates the account when you configure its ID and cookie import. Granular
MCP tools accept text written by your client; they need no server LLM key.
Research a recipient's authored posts, prepare the exact message with
`prepare_outreach`, inspect its draft, then approve it individually. Campaign
records and bounded preparation also require individual draft approvals.

Optional server generation uses the single `llm` block. Set `OPENAI_API_KEY`,
`OPENAI_BASE_URL` and `OPENAI_MODEL` in your local environment or `.env` for
your chosen OpenAI-compatible provider. These values override the preset.
Action-specific LLM settings inherit that model; old provider selectors are
not needed.

## Retained legacy batch examples

These settings preserve the Selenium workflows used by `x-use run` and the
legacy MCP `run_cycle` tool. Each explicitly sets `mcp.browser_backend` to
`selenium`; Chrome/Firefox and stealth options belong to that runtime.

| Settings file | Legacy workflow |
| --- | --- |
| `beginner-defaults.json` | Headless Firefox, no proxies |
| `beginner-chrome-undetected.json` | Chrome with undetected-chromedriver and stealth |
| `beginner-proxies-hash.json` | Chrome with stable per-account proxy pool selection |
| `beginner-proxies-roundrobin.json` | Chrome with round-robin proxy pool selection |

The existing account scenarios remain available: `growth.json` for competitor
reposts and curation, `brand_safe.json` for on-topic engagement,
`replies_first.json` for support replies, `engagement_light.json` for light
engagement, and `community_posting.json` for a community batch workflow.
They use current account field names; historical `*_override` names still
load through the compatibility normalizer. Their `action_config` describes
batch behavior, not the async MCP action ledger's budgets.

Replace account IDs, cookie paths and community placeholders before use.
Competitor sources are needed for the legacy rewrite/repost pipeline. A
persona file in `personas/` is a starting point: copy its text into the
account's `persona` field; file paths are not loaded automatically.

Community audience writes currently return `community_unverified` with
Patchright or Playwright. Use the community preset only with the explicit
legacy workflow. Async MCP does not support `run_cycle` or rotating pools.

## Cookies, proxies and state

Use your own private cookie export, never the dummy values in `data/`.
See [the data guide](../data/README.md) for supported shapes and local storage.
The growth account has no mandatory proxy pool; to use the pool examples, set
its `proxy` to `pool:residential_eu` and replace the pool's sample URLs.
Async MCP accepts HTTP/HTTPS and unauthenticated SOCKS5 proxies with the
`hash` pool strategy. Set any referenced environment variables first.

The recommended settings file is for `x-use mcp`. The legacy `x-use run`
engine remains a separate workflow; choose a legacy settings/account scenario
deliberately when running it. See [browser runtime](../docs/BROWSER_RUNTIME.md)
and [configuration reference](../docs/CONFIG_REFERENCE.md) for exact contracts.
