# Local data and examples

This directory holds private runtime state and two tracked configuration
examples. The examples contain dummy values and cannot authenticate an
account or connect to a working proxy. Keep your own cookies, credentials,
browser state and operational records outside source control.

## Cookie shape

[`cookies/dummy_cookies_example.json`](cookies/dummy_cookies_example.json)
shows an array with `auth_token` and `ct0` for `.x.com`. Point an account's
`cookie_file_path` at your actual private export, for example
`data/cookies/personal.cookies.json`. Relative paths are checked against the
configuration directory first, then the project root; absolute paths are
also accepted.

The async Patchright/Playwright runtime requires exactly one nonempty, secure
cookie of each authentication name for `x.com` or `.x.com`, with path `/`.
The async loader expects a JSON array of cookie objects. Common
fields are `name`, `value`, `domain`, `path`, `secure`, `httpOnly`, `sameSite`
and `expires`; expiration aliases `expirationDate` and `expiry` are supported.
Expired authentication cookies are rejected. The dummy example omits expiry
so it remains a useful shape example; use the real expiry from your export.
Legacy Selenium imports the same array shape.

## Proxy examples

[`proxies/dummy_proxies.json`](proxies/dummy_proxies.json) contains illustrative
HTTP, HTTPS and unauthenticated SOCKS5 routes. Copy your actual URLs into
`browser_settings.proxy_pools`, choose `proxy_pool_strategy: "hash"`, and set
an account's `proxy` to `pool:<pool_name>`. A direct account URL is also
supported. Environment placeholders such as `${RESI_PASS}` must be set in the
server environment before use. The example hosts are placeholders.

Patchright/Playwright rejects authenticated SOCKS5, SOCKS4 and rotating pool
strategies. Round-robin pools belong to the legacy Selenium workflow. That
workflow creates `data/proxy_pools_state.json` when needed; there is no tracked
sample state file to copy. The proxy example file is reference material,
not an automatically loaded configuration source.

## Generated state

Runtime-created files include drafts (`drafts.jsonl`), the action queue
(`engagement_queue.jsonl`), the action ledger (`action_safety.sqlite3`),
outreach leads/campaigns (`outreach.sqlite3`), browser ownership locks and
legacy account metrics (`metrics/<account_id>.json`). SQLite sidecars and
browser/session directories are private too. The old tracked metrics snapshot
was removed because it was operational history rather than reusable input.

These stores are created by their consumers; a fresh checkout does not need
dummy ledgers, counters or browser profiles. Do not reset the action ledger to
recover an uncertain write: inspect X and use the documented reconciliation
workflow. Share the same account IDs and safety database across local clients
that must share budgets. See [browser runtime](../docs/BROWSER_RUNTIME.md).
