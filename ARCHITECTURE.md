# Architecture

x-use 3.0.0 exposes browser-native X workflows through an MCP stdio server.
The current runtime uses async Patchright Chromium. The Selenium batch engine
remains an explicitly selected compatibility path. Both use the same account
configuration, but they do not have identical capabilities or policy boundaries.

## Runtime and module boundaries

| Path | Responsibility |
|---|---|
| `src/xuse/cli.py` | `init`, `doctor`, `mcp`, `run`, and skills commands |
| `src/xuse/mcp/server.py` | FastMCP construction, dependency injection, tool registration and shutdown |
| `src/xuse/mcp/stdio.py` | Reserve stdout for MCP protocol messages |
| `src/xuse/mcp/browser_bridge.py` | Account/action policy, browser dispatch, timeouts and recovery metadata |
| `src/xuse/browser/sessions.py` | Shared browser process, isolated contexts, account ownership, capacity and cleanup |
| `src/xuse/browser/page.py` and mixins | Verified native navigation, reads, composers, inbox, notifications and threads |
| `src/xuse/mcp/safety.py` | Durable action reservations, budgets, pauses and uncertain outcomes |
| `src/xuse/mcp/drafts.py`, `thread_store.py` | Reviewed payloads and durable per-segment thread progress |
| `src/xuse/outreach/` | Account-scoped leads, campaigns, suppression and delivery tracking |
| `src/xuse/queue/` | Scheduled work, durable state transitions and opt-in draining |
| `src/xuse/core/local_state.py`, `windows_permissions.py` | Private state creation, permission checks and link refusal |
| `src/xuse/core/config_loader.py` | Settings, accounts, override normalization and `X_USE_HOME` |
| `src/xuse/core/llm_service/` | Optional OpenAI-compatible server-side generation |
| `src/xuse/features/`, `orchestrator.py` | Legacy Selenium scraping, publishing, engagement and batch cycles |
| `src/xuse/skills_pack/` | Canonical skills included in distributions |
| `plugins/x-use/` | Claude Code plugin, MCP configuration and mirrored skills |
| `scripts/` | Setup, skill synchronization and credential-free CI checks |

## A browser-backed MCP call

1. The typed tool validates the account, inputs and requested operation.
2. The browser bridge takes the account operation lock and checks local policy.
   It durably reserves an action identity before browser acquisition.
3. The session pool reuses a healthy account context or starts one within
   capacity. Cookie validation, proxy resolution and process ownership happen
   before an authenticated page is exposed to a tool.
4. The browser adapter verifies the native page and exact target. Supported
   same-page calls reuse current state; a conversation transition waits for its
   header and message content rather than accepting a URL change alone.
5. A read returns bounded structured evidence. A write requires native success
   evidence; unsupported layouts, login gates, challenges or ambiguity stop work.
6. The action journal records the result. Cancellation, timeout and uncertain
   submission keep a recovery identity instead of making an automatic retry.
   MCP errors include a structured envelope and protocol `isError=true`.

Local account/configuration, draft, queue and journal inspection can run without
starting a browser. Profile, inbox and notification reads may start a context.
Reading a conversation or visiting notifications can update native read state.

## Sessions and process ownership

One Chromium process serves bounded, isolated in-memory account contexts.
Cookies and configured proxy routes belong to those contexts; there is no shared
persistent user profile or saved decrypted inbox state. A healthy warm context
preserves its unlocked inbox until closure, idle expiry or shutdown. A restart
uses the operator's original cookie export and may require renewed login or PIN
entry. The server does not fabricate refreshed credentials.

An OS-held authentication-identity lock coordinates local processes, including
aliases for the same account. Operations are serialized per account. Windows
uses a private Job Object for owned children; Linux can use pinned process
descriptors. Other supported hosts can verify natural exit; unsupported forced
signaling retains ownership when cleanup cannot be proved. Process identity is
never inferred solely from an executable name.

Account locks coordinate one host, not a distributed fleet. Context isolation
does not isolate the shared browser process or host. Runtime state has private
filesystem permissions but is not encrypted.

## Drafts, threads and queue execution

Ordinary publishing tools use draft mode by default. Operators can explicitly
disable that mode. Messages and follows always create individually approved
drafts, and thread publication starts from a reviewed thread draft.

A thread journal binds the reviewed payload and attachment digests to segment
progress. Each confirmed post becomes the next segment's exact parent. Explicit
continuation submits only known unsubmitted work; uncertain segments require
inspection, and cancellation preserves already-published evidence.

Queue insertion stores work locally. `process_queue` executes it, and an
operator can enable automatic draining. Queue execution and the legacy batch
runner are separate from ordinary draft approval. Journal updates are persisted
before in-memory transitions. Interrupted approved drafts remain uncertain;
interrupted processing queue items stop for inspection instead of replaying.

## Structured data and configuration

`get_inbox` supports main, Requests and Other folders, all/unread/read filters,
and unread-first ordering. Missing or conflicting unread evidence is unknown.
Conversation output reports observed participants, message IDs, reply
availability and partial coverage. It does not imply a complete archive.

Notifications bind a verified tab and return bounded actors, related posts,
media and evidence-backed event types. Thread reads distinguish focal and quoted
posts, images and video posters; a poster is not video transcription.
Account analytics availability is separate from locally recorded action metrics.

| Location | Purpose |
|---|---|
| `config/settings.json` | Global runtime, policy, LLM and batch settings |
| `config/accounts.json` | Private operator account records; ignored by Git |
| `presets/settings/` | Current Patchright MCP starter and labeled legacy batch alternatives |
| `presets/accounts/` | Inactive reviewed-outreach starter and optional batch scenarios |
| `presets/personas/` | Reusable writing guidance |
| `data/` | Private journals, metrics and state; only documented dummy examples are tracked |
| `logs/` | Local diagnostics and account event logs; ignored by Git |

An absolute `X_USE_HOME` supplies a stable configuration/data root independent
of the MCP client's working directory. Without it, source installs find the
project marker; installed packages can fall back to the working directory.
Presets are copied into configuration; editing a preset does not reconfigure an
existing account. See [configuration](docs/CONFIG_REFERENCE.md),
[presets](presets/README.md), and [data](data/README.md).

## Compatibility and extension

The legacy `x-use run`/Selenium `run_cycle` path uses account pipelines, its own
pacing, metrics and processed-action keys. Select its backend explicitly; it is
not interchangeable with the async MCP policy path.

Browser selector changes belong in the corresponding adapter with a synthetic
DOM regression. New MCP tools need annotations, bounded schemas, explicit
effects, recovery behavior, contract coverage and documentation. New configuration
fields should preserve existing account overrides or provide migration notes.
Skills are edited in the packaged source and synchronized to plugin copies.

CI checks Python 3.10–3.14 across Windows, Linux and macOS, locked installs,
installed-wheel native fixtures for both async browser drivers on Python 3.12,
containers and dependency advisories. It does
not use real account credentials. [The audit](docs/RESILIENCE_AUDIT.md) and
[tool matrix](docs/TOOL_VALIDATION.md) separate controlled live evidence from
synthetic coverage. [Contributing](CONTRIBUTING.md) documents local commands and
release preparation.
