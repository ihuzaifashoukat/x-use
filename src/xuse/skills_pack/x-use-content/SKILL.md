---
name: x-use-content
description: Create original X content in the user's persona by researching what works in the niche, drafting posts, and staging them one by one for review. Use when the user wants post ideas, content creation, or a posting pipeline.
---

# x-use content

Research -> draft -> polish -> stage. Ordinary post tools stage only when
`mcp.draft_mode` is enabled (default); with it disabled they publish immediately.

## Workflow

1. **Load the voice.** `get_account(account)`, read `persona` and
   `target_keywords`. No persona set? Ask the user a couple of questions and
   set one via `update_account(account, persona=...)` first (see x-use-setup).
2. **Research the niche.** `search_tweets` per keyword (limit 5). Note
   which formats earn engagement: hot takes, how-tos, charts, contrarian
   questions, build-in-public numbers. `get_tweet` on standouts to see their
   media.
3. **Pick 3 angles.** Tell the user the angles you see and let them steer.
4. **Draft one at a time.** Write the post yourself in the persona (at most
   280 chars; do not assume long-form account support), then
   `post_tweet(account, text=...)` to stage it as a draft. Show it. Take the
   user's edits and re-stage if needed; reject the superseded pending draft to
   avoid approving the old text later. Polish each draft before the next.
5. **Review.** End with `list_drafts(account)` and let the user choose:
   `approve_draft` to post now, `queue_post`-style scheduling via
   `queue_post(account=..., text=..., not_before=<ISO 8601 timestamp>)` for later,
   or `reject_draft`. Scheduling stores a separate final payload; inspect it
   via `list_queue` and reject the superseded draft. Due work runs through
   `process_queue`, or the opt-in auto-drain worker; enqueueing is not approval
   to publish unless the user's scheduling authorization covers it.

## Rules

- Original text in the user's voice. Never repost someone else's wording.
- Media for posts: only local file paths the user provides
  (`post_tweet(..., media=[path])`). Never invent image files.
- For X thread composition, use **x-use-threads** so the sequence is staged and
  its durable progress can be reviewed safely.
- Server-side generation (`generate_and_post`, `draft_post_variations`)
  needs the configured `llm` block. Prefer explicit text for interactive work;
  no server LLM key is needed when you write it yourself.
