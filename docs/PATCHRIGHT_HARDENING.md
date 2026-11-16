# Browser, account and proxy hardening — 3.1.0

This review covers X-Use's browser lifecycle, account ownership and isolation,
cookie imports, proxy routing and local health scoring in 3.1.0, with 82 tools.
The evidence below records the local audit before the release PR and publication;
cross-platform release checks are recorded in [GitHub Actions](https://github.com/ihuzaifashoukat/x-use/actions).

## What changed

| Area | Implemented behavior |
|---|---|
| Idle lifecycle | Close unused contexts, then retire an empty idle browser and driver. Later work may start fresh after process exit is verified. |
| Startup and shutdown | Serialize idle retirement with startup; protect active actions; clean up a runtime even when account authentication fails. |
| Ownership | Distinguish genuine lock contention from filesystem/locking failures. Verify Windows process-handle exit alongside Job accounting; allow a ten-second exit-polling window after termination and refresh pending identities. Retain ownership when cleanup is uncertain. |
| Session diagnostics | Report cold, starting, ready, busy, closing, disconnected or closed with local transport flags. No fresh authentication claim. |
| Cookie health | Use the same strict loader, source precedence and account-relative path resolution as async startup. |
| Cookie formats | Accept arrays and cookies/storage-state envelopes, importing cookies only. Preserve explicit host-only scope. |
| Cookie validation | Reject duplicate JSON keys, ambiguous scopes, conflicting expiry and unsupported partition metadata in `auth_token`/`ct0` or the file itself; for optional cookies these problems skip the cookie (reported by name and rule). Bound input and normalized output size. |
| Cookie replacement | Normalize through private temporary files and atomic replacement, including same-path imports. Restore the exact prior file if account configuration fails. |
| Runtime diagnostics | Describe configured channel/headless options and the ephemeral profile policy without starting a browser. |
| Account health | Report a weighted operational score out of 100 with unknown evidence, readiness blockers and recovery recommendations. Live authentication remains unverified. |
| Configuration isolation | Store imported cookies beside the selected accounts file; preserve separate credentials for the same account ID in different configuration homes. Reject duplicate account IDs before saving. |
| Configuration writes | Reject stale updates to removed accounts. Roll back imported credentials if staging fails. Return safe field and length errors without echoing submitted credentials. |
| Multiple accounts | Invalidate older startup work on account changes; reject stale queued acquisitions and validate cold requests before capacity eviction. Retain session closure after a saved account change even if its caller cancels. |
| Proxy routes | Reject invalid or unavailable configured routes without a direct fallback. Validate backend-specific capabilities and keep proxy probes separate from account sessions. |

Local `ready` and valid cookie exports do not prove a current X login. Login expiry,
challenges, restrictions, PIN gates and rate limits retain their existing recovery
flows. An interrupted write is never automatically replayed.

## Runtime design

The async backends keep separate account contexts within one Chromium process, plus account
ownership and data-home locks. It uses explicit cookie exports and ephemeral contexts. Automatic
OS-browser cookie extraction, saved browser profiles, decrypted inbox state,
cross-process browser handoff and automatic login repair were not added. These
would require a separate account identity, storage and recovery design.

The default browser channel remains unchanged. Configuring `chromium` explicitly
selects full bundled Chromium; leaving the channel unset uses the driver's
default selection. Diagnostics describe configuration rather than claim which
binary is currently running. No user-agent or fingerprint injection was added.

## Verification

| Partition | Passed | Skipped |
|---|---:|---:|
| Combined source suite | 2,068 | 8 |
| Installed-wheel affected paths and native browsers | 682 | 4 |
| Native multi-account lifecycle regression | 10 | 0 |
| Account health scoring and diagnostics | 90 | 0 |
| Proxy cleanup regression | 53 | 0 |

All final partitions passed with zero failures/errors. Partitions overlap;
do not add them as unique tests. [Machine-readable evidence](audit-2026-10-12/patchright-summary.json)
records counts, platform skips and distribution hashes. The source partition
excludes native DOM/smoke cases, which run separately. The combined source suite,
installed-wheel suite and package checks ran sequentially after the runtime
changes were frozen.

The [installed-wheel MCP audit](audit-2026-10-12/patchright-protocol.json) made
172 calls across all 82 tools without X credentials. Together with
the [functional trace](audit-2026-10-12/patchright-functional.json), every tool has
positive automated evidence; the [tool matrix](audit-2026-10-12/patchright-tool-coverage.csv)
names each test. This is not a live-X success percentage.

The [package smoke](audit-2026-10-12/patchright-package-smoke.json) verified three
entrypoints, seven skills and seven prompts. All 114 installed Python files match
source and the tested wheel. Wheel/sdist metadata and private-note exclusions
passed. No package version or dependency upgrade was required.

Native tests exposed a Windows cleanup race: Job accounting reached zero while
pinned process handles still showed termination in progress. Cleanup now requires
both empty accounting and confirmed handle exit. A deterministic regression and
the final native failure matrix cover that fix. Abrupt driver-exit testing also
showed that the previous three-second process-drain deadline could expire before
owned processes exited. The exit-polling allowance is now ten seconds; exhausted
waits still retain ownership. Other close phases and OS scheduling can make total
shutdown longer. An ownership fixture's child-import budget was increased from
30 to 90 seconds for loaded Windows hosts; its cross-process exclusion and
post-release acquisition assertions remain unchanged.
Pending transient process identities are also refreshed during drainage; a
deterministic regression covers stale uncertainty after those identities exit.
All browser fixtures use synthetic accounts/cookies and local or intercepted
pages. This review performs no live X mutations or personal-browser cookie reads.
Proxy cases use synthetic routes and injected responses; these results do not
establish connectivity through a personal or commercial proxy service.

Cookie scope tests ask Chromium's `Network.getCookies` for URL eligibility.
The Playwright-compatible `context.cookies(url)` convenience filter can include
host-only cookies for subdomains and is not sufficient evidence of send scope.
The tests also verify secure transport, expiry, ignored origin storage and
rejection before partial cookie injection.

Native process tests use pinned OS identities to verify cleanup. Browser API
success alone is insufficient. Windows native results do not establish native
Linux/macOS support; the existing cross-platform CI remains a release requirement.

The native test harness additionally verifies owned-Job membership and process
creation ordering before including any descendant in its assertions or cleanup.
This corrects a test-only stale-parent-ID attribution bug; parent IDs alone are
not reliable process ownership evidence.

For configuration and recovery, see [Browser runtime](BROWSER_RUNTIME.md),
[MCP reference](MCP_GUIDE.md) and [Configuration reference](CONFIG_REFERENCE.md).
The preceding [parallel upgrade report](PARALLEL_UPGRADE_VALIDATION.md) remains
historical evidence for the earlier queue, inbox and Selenium work.
