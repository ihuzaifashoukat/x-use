Configuration Reference

Overview

- Summarizes key fields in `config/settings.json` and `config/accounts.json`.
- The app is tolerant of optional fields. Unknown keys are ignored by Pydantic models unless explicitly used.

settings.json

- llm
  - api_key, base_url, model — the single OpenAI-compatible LLM client. Every major provider (OpenAI, OpenRouter, Azure's OpenAI-compatible endpoint, Gemini's OpenAI-compatible endpoint, local vLLM/Ollama) speaks this API; base_url selects which one you are talking to.
  - Environment override: `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_MODEL` — from the process environment or a `.env` file at the project root (see `.env.example`) — take precedence over the values here: **env > settings.json**.
  - Placeholder values (e.g. `YOUR_OPENAI_API_KEY`) are rejected from BOTH sources; with no valid key the client is simply not initialized and server-side generation is disabled (interactive MCP use does not need it — the calling agent writes the text). Key values are never logged — logs state only which source supplied a key.
  - Only used by `"auto"` text generation, structured analysis, the composite MCP tools (`research_and_stage`, `draft_post_variations`), and background automation (run_cycle, pipelines). All other interactive MCP tools accept explicit text and work keyless.
- api_keys (legacy)
  - openai_api_key is honored as a last-resort fallback for the llm client key (after env and llm.api_key).
  - gemini_api_key, azure_openai_api_key, azure_openai_endpoint, azure_openai_deployment, azure_api_version: ignored with a one-time warning — move to the `llm` block.
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
  - Pool management surface: the `list_proxies`, `add_proxy`, `remove_proxy`, and `test_proxy` MCP tools manage these pools. Pools remain editable by hand; MCP writes are validated, backed up (`config/backups/`), and atomic, and every credential is masked (`scheme://***@host:port`) in tool responses.
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

- queue (settings for the scheduled-action queue MCP tools: `queue_post`, `queue_engagement`, `process_queue`)
  - store_file: JSONL persistence for queued actions (repo-relative or absolute). Default `data/engagement_queue.jsonl`.
  - max_actions_per_run: hard budget per `process_queue` call, per account. Default `5`.
  - min_delay_seconds / max_delay_seconds: uniform jitter range between executed actions. Defaults `90` / `240`.
  - max_attempts: attempts before an item is marked `failed`; failures requeue with linear backoff (`min_delay * attempts`). Default `2`.
  - daily_caps: per-account, per-action daily ceilings (UTC day), counted from the queue store itself. Default `{post: 5, reply: 15, like: 30, retweet: 10}`.
  - auto_drain.enabled: opt-in background worker inside the MCP server. Default `false`.
  - auto_drain.interval_seconds: seconds between auto-drain ticks. Default `900`.
  - auto_drain.max_actions_per_account: per-account budget per tick. Default `3`.
  - Queued items execute only via an explicit `process_queue` call or the auto-drain worker. Daily caps and pacing apply on both paths.

MCP media behavior (v2.3 read tools: `get_tweet`, `prepare_reply`, `search_tweets`)

- include_images: per-tool default — `true` for `get_tweet` and `prepare_reply`, `false` (opt-in) for `search_tweets`. When on, photos additionally attach as MCP image content so a vision-capable client sees them.
- Bounds: up to 4 photos per tweet; the first photo of up to 5 tweets per search.
- Re-encode: each fetched photo is converted to JPEG, max 1024px on the long edge, max 200KB; fetch timeout 5s. Failed fetches are skipped, never errors.
- Fallback: the JSON envelope always carries the typed `media` list (photos with alt text; videos as poster + URL only), so clients without image support lose nothing.

accounts.json (per account)

- account_id: string
- is_active: bool
- cookies: [ cookie objects ] (optional)
- cookie_file_path: string (recommended)
- proxy: URL or "pool:<pool_name>"
- post_to_community: bool, community_id: string?, community_name: string?
- target_keywords or target_keywords_override: [strings]
- persona: string (optional, max 4000 chars) — freeform markdown describing the account's voice/engagement style; returned by `get_tweet`/`prepare_reply` and prepended to server-side LLM prompts. Starter presets in `presets/personas/`.
- competitor_profiles or competitor_profiles_override: [profile URLs] (required for rewrite-based posting)
- news_sites(_override): [URLs] (optional)
- research_paper_sites(_override): [URLs] (optional)
- llm_settings_override: { model_name_override, max_tokens, temperature } (service_preference is deprecated and ignored — single OpenAI-compatible client)
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
  - LLM action settings: llm_settings_for_post / reply / thread_analysis (service_preference deprecated; model_name_override overrides the configured llm.model)
  - Keyword engagement: enable_keyword_retweets, max_retweets_per_keyword_run

Notes

- Unknown fields are generally ignored; keep to the provided keys for predictable behavior.
- `${VAR}` interpolation remains the mechanism for proxy strings only (e.g. `"http://user:${RESI_PASS}@host:port"` in `proxy_pools`); it reads the process environment and does not apply to any other config field. The LLM key uses the env-var override described under `llm` above.
- For pools with env vars, ensure your shell exports them before running (`export RESI_PASS=...`).
- Cookies must match `cookie_domain_url` domain; the app navigates there before injection.
- Community posting: the app opens the “Choose audience” menu and selects by `community_id` (preferred) or visible `community_name`. It scrolls virtualized lists and uses JS-click fallbacks when needed.
  - If selection still fails, confirm the account is a member of the community and that it appears under “My Communities”.
  - UI can change; open an issue with a DOM snapshot if selectors need tuning.
