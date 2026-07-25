# Changelog

All notable changes to **x-use** (`x-use-mcp` on PyPI) are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project follows [semantic versioning](https://semver.org/). See
[ROADMAP.md](ROADMAP.md) for what is planned next.

---

## [2.3.0] — 2026-07-25

Agent-native media, personas, composite draft staging, and proxy pool management, followed
by a hardening wave driven by live testing against a real X account. **25 → 32 MCP tools.**

### Added

- **`get_tweet(account, tweet_url, include_images=true)`** — read-only fetch of a single
  tweet: text, author, like/retweet/reply/view counts, typed media, and the account persona.
- **Typed media on scraped tweets** — `ScrapedTweet.media` carries photos with alt text
  (`tweetPhoto`) and video posters (`videoPlayer`), deduped by URL, with avatars excluded.
- **Image pipeline** — photos are returned to vision-capable clients as MCP `ImageContent`
  alongside the JSON envelope. Fetches are restricted to `*.twimg.com`, capped at 8 MB with
  a 5 s timeout, and re-encoded with Pillow to JPEG ≤1024 px. Any failure degrades to
  URL-only and never errors the tool. Adds Pillow as a dependency.
- **`include_images` on `search_tweets` and `prepare_reply`** — opt-in on search (first photo
  of up to 5 tweets), on by default for single-tweet reads.
- **Freeform `persona` per account** — set through `add_account` / `update_account`, readable
  via `get_account`, and prepended to reply and post prompts when present.
- **Composite draft-only tools** — `research_and_stage(account, keywords, max_items)` runs
  per-keyword search, dedupes by tweet id, applies a keyless relevance heuristic, sorts by
  like count, and stages persona-aware reply drafts; `draft_post_variations(account, topic,
  count)` stages N persona-aware post drafts. Both refuse paused accounts.
- **Proxy pool management over MCP** — `list_proxies`, `add_proxy`, `remove_proxy`,
  `test_proxy`. Members are masked in every response, and `settings.json` writes are
  validated, backed up, and replaced atomically.
- **Bundled agent skills pack** — 5 `SKILL.md` files shipped in the wheel as package data,
  installed by `x-use skills install [--force]` (never overwrites without `--force`) and
  offered at the end of `x-use init`. `x-use skills list` enumerates them.
- **Claude plugin marketplace** — `plugins/x-use/`, kept byte-identical to the shipped skills
  pack by `scripts/sync_skills.py` and a CI drift guard.
- **Persona starter presets** under `presets/personas/`.
- **Documentation** — `docs/MCP_GUIDE.md` (full 32-tool reference) and
  `docs/SETUP_PROMPT.md` (one-prompt setup).
- **`get_account_health`** now surfaces corrupt-on-disk `settings.json` / `accounts.json`
  parse errors instead of silently reporting healthy.

### Changed

- Default LLM `max_tokens` raised to **1200**. Reasoning models were spending the entire
  budget on reasoning tokens and returning empty replies.
- An empty completion with `finish_reason="length"` is now retried once at
  `max(2 × max_tokens, 1200)` instead of failing.
- Preset `max_tokens` pins raised to match.
- The cookie handshake no longer performs a post-cookie refresh — it was redundant, and
  dropping it removes one full page load from every cold start.

### Fixed

**Security**

- Mask the **entire** URL userinfo everywhere. The previous `user:***@` shape leaked
  usernames and, for token-style URLs (`scheme://TOKEN@host`), the whole credential.
- **Mask userinfo containing `/`, whitespace, or a second `@`.** The regex introduced with
  the masking work above could not match those characters, so `http://user:pa/ss@host` was
  emitted **completely unmasked** and `http://user:p@ss@host` leaked its tail — reachable
  through `list_proxies`, `get_account`, and any proxy error message, because
  `validate_proxy_url` accepts all three forms. Masking now scans the URL authority directly
  rather than pattern-matching it. The same regex had been copied into three modules, so
  fixing one left two leaking; `xuse.utils.sanitize.mask_url_userinfo` is now the single
  implementation and `proxy_manager.mask_proxy_url` / `proxy_tools.mask_proxy` delegate to it.
- `sanitize_text` now masks credentials embedded in free-form error prose, not only in
  strings that are entirely a URL.
- Sanitize the queue's `last_error` before it is persisted or reported.
- Mask credentials in all proxy tool error text.
- Restrict image fetches to an explicit `*.twimg.com` allowlist.

**Queue**

- A cancellation issued while the drain is sleeping between actions is now honored.
- Items stuck in `processing` after a crash are recovered to `pending` on load.
- Draining across all accounts now skips paused accounts.
- Dedup keys include media and community, so materially different posts no longer collide.

**Sessions and browser lifecycle**

- Cold start and close are cancel-safe.
- An unresponsive driver is force-quit rather than leaked.
- Cookie files are shape-validated before use.
- Fixed the proxy pool import that silently disabled `pool:<name>` resolution.

**Config writes**

- Backup failures are wrapped as `ConfigWriteError` and block the write, preserving the
  "backup before replace" guarantee.
- Proxy validation is shared between the MCP proxy tools and the accounts write gate.
- Orphaned cookie copies are cleaned up when a mutation fails.
- **A failed `add_account`/`update_account` no longer deletes the account's live cookie
  file.** Importing a `cookie_file` overwrites `config/<id>_cookies.json`, and the
  orphan-cleanup above then unlinked that path on a failed config write — so
  `update_account(cookie_file=…, proxy="bad-url")` returned `ok:false` *and* destroyed a
  working login that is backed up nowhere, while `accounts.json` still referenced it. The
  import now stashes any pre-existing cookie file and restores it on rollback, deleting only
  files the import actually created.
- A corrupt config file surfaces as an error instead of being treated as empty.

**Pacing and metrics**

- Pacing is applied on failed actions, not only successful ones.
- Added a per-account pacing lock.
- Dedup survives across day boundaries.
- Metrics writes are atomic, and write ordering is hardened.

**LLM, doctor, wizard, media**

- Cookie expiry is checked, not just cookie presence.
- Placeholder API keys are detected and reported as unconfigured.
- Prompt echo is stripped from completions.
- JSON scanning is more tolerant of surrounding prose.
- Pixel and stream gates added to the media path.
- Safety fixes in the init wizard.

**Engine truthfulness**

- No action reports success without confirmation.
- Navigation failures are respected instead of being swallowed.
- Fixed community targeting, and URL, encoding, and user-agent handling.

**Other**

- `test_proxy` fails cleanly on dead proxies: no egress IP is a probe failure rather than
  `ok: true` with a null IP, and the page-load wait is capped at 25 s.
- `images_for_tweet` filters images before applying the limit, so the cap is not consumed by
  entries that are later discarded.

### Notes

The two most severe fixes in this release — the cookie-file deletion and the userinfo
masking gap — were regressions introduced *within* this release cycle by the hardening
commits themselves (`3d0c608` and `b150c5e`). Neither ever reached a published version;
2.2.0 is unaffected. Both are now covered by regression tests
(`tests/mcp/test_cookie_import_rollback.py`, `tests/utils/test_sanitize_userinfo.py`).

Test suite: **599 passing**, up from 551, with no network or browser required.

---

## [2.2.0] — 2026-07-24

### Changed

- **Single OpenAI-compatible LLM client** replaces the gemini/openai/azure provider stack.
  Configure one `llm` block (`api_key`, `base_url`, `model`); the service returns `None`
  when unconfigured rather than failing.
- Interactive MCP use can now run keyless — the calling agent writes the text via the
  `prepare_reply` context tool. The server-side LLM is only needed for `"auto"` text and
  background automation.

### Fixed

- Six bugs found in live testing against a real X account.

---

## [2.1.0] — 2026-07-24

Released by version bump; not tagged.

### Added

- **Account management tools** — `add_account`, `update_account`, `set_account_active`,
  `remove_account`, with validated, backed-up, atomic writes to `accounts.json`.
- **Persistent scheduled-action queue** — `queue_post`, `queue_engagement`,
  `cancel_queued_action`, `process_queue`, plus an opt-in `auto_drain` worker, with
  `not_before` scheduling, jittered pacing, per-action daily caps, and crash recovery.
- **Support tools** — `list_drafts`, `get_draft`, `reject_draft`, `get_run_status`,
  `get_account_health`.
- Full 24-tool contract test.

### Fixed

- Paused accounts are respected on enqueue and on drain-all.
- Seven correctness bugs from the v2.1 audit.

---

## [2.0.0] — 2026-07-24

The **`twitter-automation-ai` → `x-use`** relaunch. Breaking: the package moves to a
src-layout at `src/xuse/`, imports change, and the primary entry point becomes the `x-use`
CLI.

### Added

- `pyproject.toml` and a src-layout package (`xuse/core`, `xuse/features`, `xuse/utils`,
  `xuse/models`). Published to PyPI as **`x-use-mcp`**.
- **CLI (Typer)** — `x-use init` (interactive wizard), `x-use run`, `x-use mcp`,
  `x-use doctor`.
- **MCP server over stdio** on the official MCP Python SDK, with `list_accounts`,
  `post_tweet`, `generate_and_post`, `search_tweets`, `reply_to_tweet`, `engage`,
  `run_cycle`, and `get_metrics`.
- **Draft / approval mode**, on by default: write tools return a draft; `approve_draft`
  executes it. Nothing goes live without explicit approval unless deliberately opted out.
- Lazy per-account browser session pool with idle timeout.
- `config/accounts.example.json`, tightened `.gitignore` around cookies and `.env`.
- Public `ARCHITECTURE.md` and `BEST_PRACTICES.md`.

### Changed

- Python floor raised to 3.10+.

### Deprecated

- `python src/main.py` still works, with a warning pointing to `x-use run`.

[2.3.0]: https://github.com/ihuzaifashoukat/x-use/compare/v2.2.0...HEAD
[2.2.0]: https://github.com/ihuzaifashoukat/x-use/compare/v2.0.0...v2.2.0
[2.1.0]: https://github.com/ihuzaifashoukat/x-use/compare/v2.0.0...v2.2.0
[2.0.0]: https://github.com/ihuzaifashoukat/x-use/releases/tag/v2.0.0
