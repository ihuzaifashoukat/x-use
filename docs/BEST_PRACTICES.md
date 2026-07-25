# Best practices

How to run x-use without getting your account locked. Written against v2.3: Python 3.10+,
Selenium plus an OpenAI-compatible LLM, driven either by the `x-use` CLI or by the MCP
server from an agent client.

Read section 6 before you run anything. The rest is operational detail.

## 1. Account safety and rate limiting

X profiles automation aggressively. The most effective protection is patience: low action
volume, human-scale delays, and filters that skip marginal engagements.

Keep the conservative defaults. Delays are randomized between a minimum and maximum, per-run
caps are low, and keyword scraping is bounded. The presets in `presets/accounts/` show
sensible profiles. `engagement_light.json` and `brand_safe.json` widen delays and cap replies
and likes to low single digits per run. Even `growth.json`, the most aggressive one, stays at
90 to 240 second delays. If you edit these numbers, move them up rather than down.

Use recency filters. `reply_only_to_recent_tweets_hours` stops the bot replying to stale
tweets. Necro-replies are a classic automation signal and they get reported.

The queue adds a second layer. When you schedule work through `queue_post` or
`queue_engagement`, the drain applies jittered pacing and per-action daily caps on top of
whatever the account config says, and it refuses to run two drains at once.

Why aggressive settings get accounts flagged: X correlates action frequency, action-to-browse
ratio, IP reputation, and fingerprint consistency. A fresh account liking 50 tweets in ten
minutes from a datacenter IP is trivially detectable. Detection escalates through captcha
challenges, temporary action blocks, shadow restrictions, then suspension. By the time you
notice, the account has already been scored.

Warm up new accounts. For the first week or two on a new or newly automated account, start
from `presets/accounts/engagement_light.json`, run once or twice a day rather than
continuously, keep relevance filters on so the account only touches on-topic content, and
raise caps gradually while watching `data/metrics/<account_id>.json` for error spikes.

## 2. Cookies and credentials

Each account in `config/accounts.json` points at a cookie file through `cookie_file_path`.
The file is a Selenium-compatible array of cookie objects for `x.com`. A working login needs
valid `auth_token` and `ct0` cookies. Both. A file with only `auth_token` fails validation.

Never commit real cookies. An `auth_token` is a full session credential: anyone holding it
can act as your account. `config/accounts.json` and cookie files are gitignored, and
`config/accounts.example.json` is the committed template. This matters more than it sounds.
A contributor recently opened a PR against this repo containing their live x.com cookies,
which stayed readable in the diff even after the PR was closed.

If a cookie leaks, log the account out of all sessions on X. That invalidates `auth_token`.
Then log back in and re-export. Deleting the commit does not un-leak it, so assume anything
that touched a public branch is compromised.

API keys come from the environment or from `config/settings.json`, with environment winning.
Copy `.env.example` to `.env` and fill in `OPENAI_API_KEY`, or use the `llm` block in
settings. Placeholder values like `YOUR_OPENAI_API_KEY` are rejected from both sources, and
with no valid key the client is simply not initialized. Interactive MCP use needs no key at
all, because your agent writes the text.

Cookies expire. `x-use doctor` and the `get_account_health` tool check actual expiry dates
rather than just file presence, so run one of them when logins start failing.

## 3. Proxies

One account, one stable IP. X checks IP consistency per session, so the reliable pattern is
a dedicated IP or sticky residential session per account, kept stable across runs. Never
route several accounts through one exit IP, and avoid moving an established account to a new
IP without a reason.

Set `proxy` on the account object to override the global `browser_settings.proxy`. Chrome
applies it through `--proxy-server`. Proxy auth prompts are not handled interactively, so
credentials go in the URL.

Named pools live under `browser_settings.proxy_pools` and accounts reference them as
`"proxy": "pool:<name>"`. Two strategies:

- `hash` maps an `account_id` to a pool entry deterministically, so each account keeps the
  same IP run after run. This is the one you want for accounts that post.
- `round_robin` rotates across runs and persists its cursor. Use it for scraping-heavy work
  where session identity matters less.

Residential IPs with sticky sessions are strongly preferred for any account that posts,
replies, or likes. Datacenter ranges are widely fingerprinted and score poorly. They are fine
for cost-sensitive read-only scraping.

Keep proxy passwords out of config. Proxy strings support `${VAR}` interpolation, so
`http://user:${RESI_PASS}@eu1.proxy.local:8080` expands at runtime from the process
environment. Note that this is proxy-strings-only and separate from the LLM key mechanism.

If your password contains characters that are not legal in a URL, percent-encode them:
`/` as `%2F`, `@` as `%40`, a space as `%20`. Masking and validation both expect encoded
userinfo.

You can manage pools over MCP with `list_proxies`, `add_proxy`, `remove_proxy`, and
`test_proxy`. Every response masks credentials. `test_proxy` drives a throwaway cookieless
browser and reports the egress IP, so it tells you whether a proxy actually works before you
attach it to an account.

## 4. Stealth

Prefer Chrome with undetected-chromedriver, which is the shipped default
(`use_undetected_chromedriver: true`). You can layer `enable_stealth: true` on top for
selenium-stealth's fingerprinting tweaks. `presets/settings/beginner-chrome-undetected.json`
is a ready-made starting point.

Keep the user agent consistent per account. The default `user_agent_generation: "random"`
picks a new UA each session, and a login whose browser changes every run looks synthetic.
For long-lived accounts set `user_agent_generation: "custom"` with a realistic
`custom_user_agent` and leave it alone. Pair it with a fixed `window_size`.

Never run parallel logins to the same account. Two Selenium sessions sharing a cookie file
means one account acting from two browsers, possibly two IPs, at once. That is a near-certain
flag and it corrupts session state. The MCP server keeps one warm browser per account and
serializes actions within it. Keep that invariant: no overlapping runs against one
`accounts.json`, and never point two account entries at the same cookie file.

For warming up accounts or debugging logins, run headed rather than headless. It is both less
detectable and easier to watch.

## 5. LLM usage

One OpenAI-compatible client covers OpenAI, OpenRouter, Azure, Gemini, and local servers like
Ollama and vLLM, since they all speak the same chat-completions API. Configure `api_key`,
`base_url`, and `model`.

The interactive path needs no key. Tools like `prepare_reply` return the tweet, its media,
your account persona, and the character limit, and your agent writes the reply. The
server-side LLM only powers `"auto"` text and background automation.

Tune relevance thresholds rather than disabling them. The analyzer scores tweets for
relevance and sentiment, and the filters live in `analysis_config` globally or per account in
`action_config_override`. Raise thresholds when the account engages with off-topic content,
lower them slightly if nearly everything is filtered out. Quoting should always carry the
highest bar, because it puts words on your own timeline.

Keep cheap work cheap. Relevance checks run on every scraped tweet while generation runs only
on the few that pass, so keep analysis on a fast model with a low token cap and spend on the
model that writes text people actually read. One caveat learned the hard way: reasoning
models spend their token budget on reasoning before emitting anything, so a low `max_tokens`
returns empty completions. The default is 1200 for that reason.

## 6. Responsible use and X terms of service

Be clear-eyed about what this is. Browser automation of X can violate X's Terms of Service
and automation rules, and accounts running it risk temporary locks, action blocks, or
permanent suspension. Conservative settings reduce that risk. Nothing eliminates it. Do not
automate an account you cannot afford to lose.

This project is for people automating their own accounts: scheduling their own content,
engaging on topics they actually care about, managing accounts they legitimately operate. It
is not a spam tool, and the design enforces that:

- Low per-run caps, long randomized delays, relevance filters, and recency limits ship
  conservative by default.
- No mass-DM, follow-churn, or vote-manipulation features. They are deliberately absent and
  contributions adding them will not be accepted.
- Draft mode is on by default for MCP. Write tools return a draft for review and nothing
  posts until you call `approve_draft`. Queued work executes only on an explicit
  `process_queue`, or through the auto-drain worker if you deliberately enable it.
- Paused accounts are refused on every write path.

You are responsible for how you use this: compliance with X's terms and automation policies,
with the laws where you live, including platform-manipulation and disclosure rules, and with
basic decency toward other users. The maintainers provide the tool. The operator owns the
consequences.

## 7. Operations

Read your metrics. Every run writes two streams per account. `data/metrics/<account_id>.json`
holds counters for posts, replies, retweets, quote tweets, likes, and errors plus last-run
timestamps. `logs/accounts/<account_id>.jsonl` holds one JSON line per action attempt. Tail
the log during a run and grep it afterwards. `get_account_health` folds config validity,
cookie validity, metrics, and session state into one call.

Watch error categories, because they map to specific fixes. Click-interception errors usually
mean X changed its DOM, so selectors need updating. Login and cookie errors mean expired
cookies, so re-export. LLM 429 or 500 errors mean throttling, so reduce volume or switch
provider. A sudden wall of failures on a previously healthy account can mean an X-side
restriction: stop automating and check the account by hand before resuming.

Run `x-use doctor` before enabling actions on a new setup. It checks Chrome and driver
availability, cookie validity and expiry, LLM key configuration, and proxy reachability.

Back up config and state. Operational state lives in `config/`, `data/`, and `logs/`. Cookie
files are credentials, so treat backups with the same care as the originals and keep them
encrypted or access-controlled. Restoring is just copying files back.
`data/proxy_pools_state.json` can be reset safely if lost, since round-robin counters simply
restart.

## 8. Contributing

- Conventional commits: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:` with a
  concise imperative subject.
- No AI attribution. Do not add AI co-author trailers, "generated by" notes, or tool
  attributions to commits, PRs, or comments.
- Test pure logic with pytest. Config merging, dedup keys, and LLM JSON parsing are
  deterministic and should be covered by tests that need no browser and no network. The suite
  runs in about 40 seconds precisely because nothing in it touches the network.
- Keep selectors centralized. X's DOM changes often. Scraper selectors belong in
  `src/xuse/features/scraper/selectors.py` and publisher interactions in
  `src/xuse/features/publisher/`. A UI change should be fixable in one place.
- Keep PRs focused on one change. Say what you tested. Include a DOM snippet when fixing
  selectors. Update `docs/CONFIG_REFERENCE.md` when you add or change a config key. Never
  include real cookies, API keys, or populated account configs in diffs, fixtures, or
  screenshots.

See [CONTRIBUTING.md](../CONTRIBUTING.md) and the Code of Conduct for the full guidelines.
