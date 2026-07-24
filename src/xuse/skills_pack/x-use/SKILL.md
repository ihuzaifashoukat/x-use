---
name: x-use
description: Overview and routing for the x-use X (Twitter) automation MCP server — tool groups, the two safety gates, and usage conventions. Use whenever you work with x-use tools or the user mentions their X/Twitter account automation.
---

# x-use

x-use is an MCP server that drives a real, logged-in browser on X (Twitter):
post, reply, search, like, retweet, schedule, and manage multiple accounts.
It needs no X API key. Interactive use needs no LLM key either — YOU are the
writer; the server drives the browser.

## The two safety gates (never bypass them)

1. **Draft gate.** Write tools (`post_tweet`, `reply_to_tweet`,
   `generate_and_post`, `engage`) return a *draft* by default and change
   nothing on X. Present the draft to the user; only `approve_draft(draft_id)`
   executes it — and only when the user explicitly says to approve.
   `run_cycle` is the legacy batch path — it executes immediately and is not
   draft-gated.
2. **Queue gate.** `queue_post` / `queue_engagement` only store work. Nothing
   runs until an explicit `process_queue` call (daily caps apply).

## Tool groups (32 tools)

- **Read-only:** `list_accounts`, `get_account`, `get_metrics`,
  `search_tweets`, `get_tweet`, `prepare_reply`, `list_queue`, `list_drafts`,
  `get_draft`, `reject_draft`, `get_run_status`, `get_account_health`,
  `list_proxies`
- **Write (draft-gated):** `post_tweet`, `generate_and_post`,
  `reply_to_tweet`, `engage`, `run_cycle`, `approve_draft`
- **Queue:** `queue_post`, `queue_engagement`, `cancel_queued_action`,
  `process_queue`
- **Composite (need the server's `llm` block):** `research_and_stage`,
  `draft_post_variations` — prefer doing this work yourself with the
  read-only tools when the user has no LLM key configured
- **Accounts:** `add_account`, `update_account`, `set_account_active`,
  `remove_account` (mutate config; validated + backed up)
- **Proxies:** `add_proxy`, `remove_proxy`, `test_proxy` (plus
  `list_proxies` above)

## Conventions

- **You write the text.** Always prefer explicit `text` over `"auto"`. Call
  `prepare_reply(account, tweet_url)` first: you get the tweet's text AND its
  images (as image content — look at them), the account's keywords, and its
  persona. Follow the persona when composing.
- **Small batches.** Keep `max_actions` / `limit` small (<= 5) unless the
  user asks for more. X rate-limits aggressively; the server's pacing and
  caps exist to protect the account.
- **Drafts are the default review loop.** Stage work, show the user
  `list_drafts`, and let them pick. `reject_draft` what they dislike.
- **Paused accounts** (`is_active=false`) can be read but never written to.
- Every response is a JSON envelope: `{"ok": true, ...}` or
  `{"ok": false, "error": {"type", "message"}}`. On error, explain and stop.

## Workflow skills

- Setting up or adding an account → use **x-use-setup**
- Researching and replying in the user's niche → **x-use-engage**
- Creating and staging original content → **x-use-content**
- Daily review: metrics, drafts, queue → **x-use-review**
