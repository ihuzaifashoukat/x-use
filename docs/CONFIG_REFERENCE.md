Configuration Reference

Overview

- Summarizes key fields in `config/settings.json` and `config/accounts.json`.
- The app is tolerant of optional fields. Unknown keys are ignored by Pydantic models unless explicitly used.

settings.json

- api_keys
  - openai_api_key, gemini_api_key, azure_openai_api_key, azure_openai_endpoint, azure_openai_deployment, azure_api_version
  - Environment override: `OPENAI_API_KEY`, `GEMINI_API_KEY`, and `AZURE_OPENAI_API_KEY` — from the process environment or a `.env` file at the project root (see `.env.example`) — take precedence over the values here: **env > settings.json**. The override applies only to the three API keys; Azure endpoint/deployment/api_version stay in settings.json.
  - Placeholder values (e.g. `YOUR_OPENAI_API_KEY`) are rejected from BOTH sources; a provider with no valid key is simply not initialized. Key values are never logged — logs state only which source (env var name or settings.json) supplied a key.
- twitter_automation
  - response_interval_seconds: Base delay between actions.
  - media_directory: Folder for downloaded media.
  - processed_tweets_file: CSV for processed action keys.

Community Engagement (in `twitter_automation.action_config` or per-account override)
- enable_community_engagement: Enable engaging with posts from the configured `community_id` timeline.
- enable_community_likes: Toggle liking community posts.
- enable_community_retweets: Toggle retweeting community posts (quotes collapsed to retweet).
- max_community_engagements_per_run: Cap on total community actions per run (like/retweet/quote/reply).
- enable_community_replies: Enable generating and posting replies in the community.
- max_community_replies_per_run: Cap for community replies per run.
- community_reply_only_recent_tweets_hours: Optional age limit (in hours) for replying to community posts.

Notes
- Community engagement uses the same relevance and action decision heuristics configured in `engagement_decision` and the per-account `ActionConfig` thresholds. When the decision output is `repost`, the orchestrator maps it to a retweet for community posts.
  - For cookie-based login, ensure JSON contains valid `auth_token` and `ct0` for the same domain as `cookie_domain_url`. If cookies are invalid/expired, set `browser_settings.login_wait_seconds` (e.g., 60–120) and complete manual login; the run will continue once signed-in is detected.
  - analysis_config
    - enable_relevance_filter: { competitor_reposts, likes, keyword_replies }
    - thresholds: { competitor_reposts_min, likes_min, keyword_replies_min? }
  - engagement_decision
    - enabled, use_sentiment
    - thresholds: { quote_min, retweet_min, repost_min }
  - action_config
    - min_delay_between_actions_seconds, max_delay_between_actions_seconds
    - enable_competitor_reposts, max_posts_per_competitor_run, repost_only_tweets_with_media,
      min_likes_for_repost_candidate, min_retweets_for_repost_candidate,
      competitor_post_interaction_type, prompt_for_quote_tweet_from_competitor
    - enable_keyword_replies, max_replies_per_keyword_run, reply_only_to_recent_tweets_hours, avoid_replying_to_own_tweets
    - enable_keyword_retweets, max_retweets_per_keyword_run
    - enable_content_curation_posts, max_curated_posts_per_run
    - enable_liking_tweets, max_likes_per_run, like_tweets_from_keywords, like_tweets_from_feed
    - enable_thread_analysis
    - llm_settings_for_post, llm_settings_for_reply, llm_settings_for_thread_analysis
- logging: level, format, (optional file/console handlers)
- browser_settings
  - type: chrome|firefox, headless, user_agent_generation, custom_user_agent
  - proxy: global proxy URL (overridden per account)
  - proxy_pools: { name: [ "http://user:${ENV}@host:port", ... ] }
  - proxy_pool_strategy: hash|round_robin
  - proxy_pool_state_file: path for round-robin counters
  - window_size, driver_options, timeouts
  - use_undetected_chromedriver: bool (Chrome only)
  - enable_stealth: bool (Chrome only)
  - cookie_domain_url: base URL for cookie domain navigation (e.g., https://x.com)
  - login_wait_seconds: Optional. If > 0, after applying cookies the browser opens X home and waits up to this many seconds for a signed-in state. Use this to complete manual login once when cookies are missing/expired.
  - chrome_driver_path / gecko_driver_path (optional): use a specific local driver binary. The app prefers local drivers if found.
- mcp (settings for the `x-use mcp` server)
  - draft_mode: bool, default `true`. When on, write tools (`post_tweet`, `generate_and_post`, `reply_to_tweet`, `engage`) build the full payload, store a draft, and change nothing until `approve_draft(draft_id)` is called. Set to `false` to let write tools act immediately.
  - session_idle_timeout_seconds: idle timeout for the per-account browser session pool before sessions are reaped. Default `600`.
  - cold_start_timeout_seconds: max wait for a browser session to cold-start before the tool call fails with a structured error. Default `180`.
  - drafts_file: path of the persistent draft store (JSONL). Default `data/drafts.jsonl` under the project root.

### `queue` (optional)

Scheduled-action queue used by the queue MCP tools (`queue_post`, `queue_engagement`, `process_queue`). All keys optional; defaults shown.

```json
"queue": {
  "store_file": "data/engagement_queue.jsonl",
  "max_actions_per_run": 5,
  "min_delay_seconds": 90,
  "max_delay_seconds": 240,
  "max_attempts": 2,
  "daily_caps": { "post": 5, "reply": 15, "like": 30, "retweet": 10 },
  "auto_drain": { "enabled": false, "interval_seconds": 900, "max_actions_per_account": 3 }
}
```

| Key | Default | Meaning |
|---|---|---|
| `store_file` | `data/engagement_queue.jsonl` | JSONL persistence for queued actions (repo-relative or absolute). |
| `max_actions_per_run` | `5` | Hard budget per `process_queue` call, per account. |
| `min_delay_seconds` / `max_delay_seconds` | `90` / `240` | Uniform jitter range between executed actions. |
| `max_attempts` | `2` | Attempts before an item is marked `failed`. Failures requeue with linear backoff (`min_delay * attempts`). |
| `daily_caps` | `{post: 5, reply: 15, like: 30, retweet: 10}` | Per-account, per-action daily ceilings (UTC day), counted from the queue store itself. |
| `auto_drain.enabled` | `false` | Opt-in background worker inside the MCP server. |
| `auto_drain.interval_seconds` | `900` | Seconds between auto-drain ticks. |
| `auto_drain.max_actions_per_account` | `3` | Per-account budget per tick. |

Queued items execute only via an explicit `process_queue` call or the auto-drain worker. Daily caps and pacing apply on both paths.

accounts.json (per account)

- account_id: string
- is_active: bool
- cookies: [ cookie objects ] (optional)
- cookie_file_path: string (recommended)
- proxy: URL or "pool:<pool_name>"
- post_to_community: bool, community_id: string?, community_name: string?
- target_keywords or target_keywords_override: [strings]
- competitor_profiles or competitor_profiles_override: [profile URLs] (required for rewrite-based posting)
- news_sites(_override): [URLs] (optional)
- research_paper_sites(_override): [URLs] (optional)
- llm_settings_override: { service_preference, model_name_override, max_tokens, temperature }
- action_config_override: ActionConfig subset to override defaults, including:
  - enable_competitor_reposts, max_posts_per_competitor_run, repost_only_tweets_with_media,
    min_likes_for_repost_candidate, min_retweets_for_repost_candidate
  - enable_keyword_replies, max_replies_per_keyword_run, reply_only_to_recent_tweets_hours, avoid_replying_to_own_tweets
  - enable_content_curation_posts, max_curated_posts_per_run
  - enable_liking_tweets, max_likes_per_run
  - enable_thread_analysis
  - Relevance filters: enable_relevance_filter_(competitor_reposts|likes|keyword_replies) and relevance_threshold_*
  - Decision logic: enable_engagement_decision, use_sentiment_in_decision,
    decision_quote_min, decision_retweet_min, decision_repost_min
  - LLM action settings: llm_settings_for_post / reply / thread_analysis
  - Keyword engagement: enable_keyword_retweets, max_retweets_per_keyword_run

Notes

- Unknown fields are generally ignored; keep to the provided keys for predictable behavior.
- `${VAR}` interpolation remains the mechanism for proxy strings only (e.g. `"http://user:${RESI_PASS}@host:port"` in `proxy_pools`); it reads the process environment and does not apply to any other config field. LLM API keys use the env-var override described under `api_keys` above.
- For pools with env vars, ensure your shell exports them before running (`export RESI_PASS=...`).
- Cookies must match `cookie_domain_url` domain; the app navigates there before injection.
- Community posting: the app opens the “Choose audience” menu and selects by `community_id` (preferred) or visible `community_name`. It scrolls virtualized lists and uses JS-click fallbacks when needed.
  - If selection still fails, confirm the account is a member of the community and that it appears under “My Communities”.
  - UI can change; open an issue with a DOM snapshot if selectors need tuning.
