---
name: x-use-content
description: Create original X content in the user's persona — research what works in the niche, draft posts, stage and polish them one by one for review. Use when the user wants post ideas, content creation, or a posting pipeline.
---

# x-use content

Research -> draft -> polish -> stage. The user picks what ships.

## Workflow

1. **Load the voice.** `get_account(account)` — read `persona` and
   `target_keywords`. No persona set? Ask the user a couple of questions and
   set one via `update_account(account, persona=...)` first (see x-use-setup).
2. **Research the niche.** `search_tweets` per keyword (limit 5-10). Note
   which formats earn engagement: hot takes, how-tos, charts, contrarian
   questions, build-in-public numbers. `get_tweet` on standouts to see their
   media.
3. **Pick 3 angles.** Tell the user the angles you see and let them steer.
4. **Draft one at a time.** Write the post yourself in the persona (under
   280 chars unless the user wants long-form), then
   `post_tweet(account, text=...)` to stage it as a draft. Show it. Take the
   user's edits and re-stage if needed — polish each draft before starting
   the next.
5. **Review.** End with `list_drafts(account)` and let the user choose:
   `approve_draft` to post now, `queue_post`-style scheduling via
   `queue_post(account, text, not_before=...)` for later, or `reject_draft`.

## Rules

- Original text in the user's voice — never repost someone else's wording.
- Media for posts: only local file paths the user provides
  (`post_tweet(..., media=[path])`). Never invent image files.
- Threads are not supported yet (coming in v2.4) — stage parts as separate
  drafts and tell the user.
- Server-side generation (`generate_and_post`, `draft_post_variations`)
  exists for the API tier, but YOUR writing is the default — it's free and
  it's better.
