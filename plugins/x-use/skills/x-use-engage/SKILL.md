---
name: x-use-engage
description: Research the user's niche on X and draft persona-voice replies for review: search keywords or profiles, read tweets AND their images, compose replies, stage drafts. Use when the user wants to engage, reply, grow reach, or "find posts worth replying to".
---

# x-use engage

Draft-first engagement. You research and write; the user approves; nothing
posts on its own.

## Workflow

1. **Scope.** Default to the account's own keywords: `get_account(account)`
   shows `target_keywords`. If the user gave URLs or topics instead, use
   those. Confirm the account with `list_accounts` if unsure.
2. **Search.** `search_tweets(keywords=<one query>, limit=5, account)`.
   One query per call; prefer 2-3 focused queries over one broad one.
   To work a specific person or competitor instead of a topic, use
   `search_profile(profile="@handle", limit=5, account)`. Profile timelines
   include pinned posts and reposts, so check `user_handle` on each result
   before treating it as theirs.
3. **Read candidates properly.** For each promising tweet call
   `prepare_reply(account, tweet_url)`. You receive the text, the images
   (look at them, because chart screenshots, memes, and UI shots change what a good
   reply is), the author's handle, the account's persona, and keywords.
4. **Filter.** Skip: tweets you can't add value to, pure announcements,
   anything off-persona, and the account's own posts. Keep at most 3-5.
5. **Compose.** Write each reply yourself, in the account's persona, under
   270 chars: concrete, adds one useful point or question, no hashtags/links/
   emoji spam, no "Great post!".
6. **Stage.** `reply_to_tweet(account, tweet_url, text=<your text>)` returns
   a draft. Repeat per candidate.
7. **Review with the user.** Show a compact list: author, tweet gist, your
   reply, draft_id. Only call `approve_draft(draft_id)` for the ones the user
   explicitly approves; `reject_draft` the rest.

## Rules

- Never approve on your own initiative. Never batch-approve.
- If the user wants volume later, suggest `queue_engagement` +
  `process_queue` (same review model, paced, daily caps).
- No server LLM key? Everything above still works. You are the writer.
