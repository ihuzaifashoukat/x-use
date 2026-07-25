# Roadmap

**twitter-automation-ai is becoming `x-use`**, browser-native AI agents for X (Twitter): multi-account, MCP-ready, no API keys required. The engine you know today (Selenium + LLM orchestration, per-account configs, proxies, metrics) stays the foundation. What changes in v2 is how you use it: an installable Python package, a proper CLI with a setup wizard, and an MCP server so Claude Desktop, Claude Code, Cursor, and other MCP clients can drive your X accounts directly. This roadmap is open to input, if you want to influence priorities, propose a tool, or challenge a decision, open an issue or start a discussion.

Legend: `[x]` shipped · `[ ]` planned or in progress.

---

## Phase 1, v2.0 "x-use" relaunch (complete bar the directory listings)

The goal of v2.0: go from "clone the repo and run `python src/main.py`" to `pip install x-use-mcp`, a guided init, and first-class MCP support, without rewriting the engine.

### Rebrand & packaging

- [x] Rename the GitHub repository `twitter-automation-ai` → `x-use` (stars, forks, and old URLs are preserved by GitHub redirects)
- [x] Add `pyproject.toml` and move the codebase into a src-layout package: `src/xuse/` (`xuse/core`, `xuse/features`, `xuse/utils`, `xuse/models`), a structural move, not a rewrite; existing engine logic carries over
- [x] Raise the Python floor to 3.10+
- [x] Publish to PyPI as `x-use-mcp` (the bare `x-use` name was rejected by PyPI as too similar to an existing `xuse` project)

### CLI (Typer)

- [x] `x-use init`, interactive wizard: add accounts, import cookies, set LLM keys, pick a starting preset (the existing `presets/settings/` and `presets/accounts/` templates become wizard choices)
- [x] `x-use run [--account NAME] [--pipeline ...]`, replaces `python src/main.py` as the way to run automation cycles
- [x] `x-use mcp`, start the MCP server
- [x] `x-use doctor`, environment checks: Chrome/driver availability, cookie validity, LLM key configuration, proxy reachability

### MCP server (the Phase 1 flagship)

- [x] MCP server over stdio, built on the official MCP Python SDK, works with Claude Desktop, Claude Code, Cursor, and Windsurf out of the box
- [x] Tools wrapping the existing modules:
  - [x] `list_accounts()`, enumerate configured accounts
  - [x] `post_tweet(account, text, media?, community?)`, publish via the composer, including community posting
  - [x] `generate_and_post(account, topic)`, LLM content generation plus publish in one call
  - [x] `search_tweets(keywords, limit)`, keyword search via the scraper
  - [x] `reply_to_tweet(account, tweet_url, text | auto)`, manual or LLM-generated replies
  - [x] `engage(account, keywords, actions, max)`, likes / retweets / replies with per-call caps
  - [x] `run_cycle(account?, pipelines?)`, trigger a full orchestrator cycle
  - [x] `get_metrics(account)`, read per-account metrics summaries
- [x] **Draft / approval mode**, human-in-the-loop safety, on by default for MCP usage: write-tools return a draft object instead of posting; a separate `approve_draft(draft_id)` call executes it. Nothing goes live without an explicit approval unless you deliberately opt out
- [x] Lazy per-account browser session pool with idle timeout, so MCP calls stay fast and never hang the client
- [x] **v2.1 MCP expansion**, account management tools (add/update/pause/remove with validated atomic writes), a persistent scheduled-action queue (`queue_post`, `queue_engagement`, `process_queue`, opt-in `auto_drain` worker) with jittered pacing and daily caps, and support tools (draft listing/rejection, run status, account health)
- [x] **v2.2 agent-native LLM**, one OpenAI-compatible client (`llm`: api_key/base_url/model) replaces the gemini/openai/azure provider stack; interactive MCP use runs keyless (the calling agent writes the text, new `prepare_reply` context tool), server-side LLM only powers `"auto"` text and background automation
- [x] **v2.3 agent media + skills** *(released, see [CHANGELOG.md](CHANGELOG.md))*, typed media on tweets with photos returned as MCP `ImageContent` on the read tools (new `get_tweet`; `prepare_reply`/`search_tweets` gain `include_images`), freeform account `persona`, composite draft-staging tools (`research_and_stage`, `draft_post_variations`), proxy pool management tools (`list_proxies`/`add_proxy`/`remove_proxy`/`test_proxy`), and a bundled 5-skill agent pack with a plugin marketplace plus one-prompt setup (`docs/SETUP_PROMPT.md`). 25 → 32 tools
- [x] **v2.3 hardening wave**, a pass driven by live testing against a real X account and a full-surface audit: whole-userinfo credential masking everywhere, queue cancel/crash-recovery/paused-drain fixes, cancel-safe session lifecycle, config-write and metrics atomicity, LLM empty-completion retries with a 1200-token default, and engine truthfulness (no action reports success without confirmation)

### Config hygiene & docs

- [x] Ship `config/accounts.example.json` and stop tracking `config/accounts.json`; tighten `.gitignore` around cookies and `.env`
- [x] README relaunch: hero, 3-step quick start (one-line installer → `x-use init` → paste the MCP snippet into your client config), comparison with API-based alternatives, demo GIF
- [x] Public `ARCHITECTURE.md` and `docs/BEST_PRACTICES.md` (including a responsible-use section)
- [x] Official MCP Registry wiring: `server.json` at the repo root plus the `mcp-name` ownership marker in the README, published automatically by the `mcp-registry` job in `publish.yml` (GitHub OIDC, no secret, version taken from the release tag)
- [x] First registry publish, shipped with v2.3.1. The record is live under `io.github.ihuzaifashoukat/x-use`, and every future release republishes it automatically
- [x] PulseMCP, no submission needed. It ingests the official registry daily and processes weekly, so the listing follows from the registry publish above
- [ ] Community directories, submitted and waiting on review: [punkpeye/awesome-mcp-servers](https://github.com/punkpeye/awesome-mcp-servers/pull/10914) (PR), [mcp.so](https://github.com/chatmcp/mcpso/issues/3301) (issue), [LobeHub](https://github.com/lobehub/lobehub/issues/17601) (issue), Glama (submitted for review)
- [ ] Smithery is deploy-only now: every listing runs in their cloud. x-use drives Chrome on your own machine with your own cookies, so a hosted deployment is the wrong shape for it. Parked unless local listings come back

### Compatibility

- [x] Keep the legacy `python src/main.py` entry point working during a deprecation window (see [Versioning](#versioning))

---

## Phase 2, dashboard & deployment

Once the package and MCP layer are solid, make the system observable and deployable.

- [ ] FastAPI backend serving the data the engine already writes: `data/metrics/<account_id>.json` summaries and `logs/accounts/<account_id>.jsonl` event logs
- [ ] Lightweight web UI:
  - [ ] Draft-approval queue, review, edit, approve, or reject pending drafts from the browser
  - [ ] Per-account metrics charts (posts, replies, retweets, quote tweets, likes, errors over time)
  - [ ] Account status overview (last run, cookie health, active pipelines)
- [ ] Official Docker image and a `docker compose` setup for running the engine, MCP server, and dashboard together
- [ ] Dashboard-aware `x-use doctor` checks (port availability, data directory permissions)

---

## Phase 3, extensibility

Turn x-use from a tool into a platform you can extend without forking.

- [ ] **Persona preset library**, curated, shareable persona/preset bundles (tone, cadence, engagement style) building on today's `presets/` system; contributed personas welcome
- [ ] **Plugin system**, register custom pipelines and MCP tools from your own package, so niche workflows don't need to live in core
- [ ] **Selector self-healing**, headless smoke tests in CI that detect X UI selector breakage early, plus fallback selector strategies, so UI changes get caught by automation instead of by your accounts
- [ ] **Prometheus-compatible metrics endpoint**, scrape run counts, action outcomes, and error rates into Grafana or any Prometheus stack
- [ ] Community persona and preset contributions folded into releases on a regular cadence

---

## Phase 4, threads & video transcription

- [ ] Read and post threads (multi-tweet compositions) through the MCP tools
- [ ] Local video transcription with faster-whisper as an optional `[media]` extra, so agents can read video content keyless

---

## Phase 5, background workflows

- [ ] Recurring per-account routines (scheduled engagement/content runs without a client attached)
- [ ] OS scheduler integration via `x-use schedule install`
- [ ] Prometheus metrics endpoint for run counts, action outcomes, and error rates

---

## How to help

The fastest way to help right now:

- **Selector fixes.** X changes its DOM regularly. PRs that update or harden selectors in `src/xuse/features/scraper/` and `src/xuse/features/publisher/` are the most valuable contributions this project gets, especially with a DOM snippet in the PR description showing what changed.
- **Presets.** New account presets (`presets/accounts/`) and settings presets (`presets/settings/`) for real-world use cases, a good preset saves every new user an hour of configuration.
- **Docs.** Clarify setup steps, add troubleshooting entries, improve `docs/CONFIG_REFERENCE.md`. If something confused you during setup, it confuses others too.
- **MCP tool ideas.** Open an issue describing the tool call you wish existed (name, inputs, expected behavior). The Phase 1 tool list above is a starting set, not a ceiling.
- **Testing.** pytest coverage for pure logic (config merging, dedup keys, LLM JSON parsing) and, once the MCP layer lands, tool contract tests.

Look for issues labeled `good first issue` and `help wanted`. Before starting a large change, open an issue first so we can agree on the approach, see [CONTRIBUTING.md](CONTRIBUTING.md) for the workflow.

---

## Versioning

x-use follows [semantic versioning](https://semver.org/). Released changes are recorded in
[CHANGELOG.md](CHANGELOG.md).

- **v2.0.0** is a major release because it is a breaking restructure: the package moves to `src/xuse/`, imports change, and the primary entry point becomes the `x-use` CLI.
- The legacy `python src/main.py` entry point is still present and still works, with a warning pointing to `x-use run`. It is scheduled for removal in **v3.0**; the v2 series keeps it.
- Config file formats (`config/settings.json`, `config/accounts.json`) have remained compatible across the whole v2 series; any future schema change will ship with a migration note in the release notes.
- **Phases past the first carry no version number, on purpose.** Only Phase 1 names one, because v2.0 shipped and the phase is a record of what happened. Everything after it is ordering, not scheduling: scope moves between releases, and a number written here today is a number that is wrong in three months. Versions get assigned when a release is cut, and the shipped ones live in [CHANGELOG.md](CHANGELOG.md).

**Cutting a release.** `.github/workflows/publish.yml` publishes to PyPI via trusted
publishing when a GitHub release is published, and it takes the version from
`pyproject.toml`. So the order matters: bump `pyproject.toml` → commit → push → tag
`vX.Y.Z` → publish the GitHub release. Tagging before the bump uploads a wheel carrying the
previous version number.

---

*This roadmap reflects current intent, not a contract, priorities shift with feedback. Comment on the tracking issues for each phase, or open a discussion to propose something new.*
