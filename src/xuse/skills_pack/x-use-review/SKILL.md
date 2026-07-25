---
name: x-use-review
description: Daily digest for an x-use account covering health, metrics, pending drafts, and queued work in one pass, then approve/reject/process per the user's direction. Use for "how is my account doing", daily check-ins, or before any batch of approvals.
---

# x-use review

One pass: status -> pending work -> decisions.

## Workflow

1. **Health.** `get_account_health(account)`: cookie validity, config,
   session, queue, drafts. Flag anything red first (expired cookies mean
   nothing else will work, so point the user at re-exporting cookies and
   `update_account(account, cookie_file=...)`).
2. **Metrics.** `get_metrics(account)`: posts/replies/likes/errors counters
   and the last ~20 events. Summarize in 2-3 lines, including errors.
3. **Pending drafts.** `list_drafts(status="pending", account)`. Present a
   numbered table: action, target, text preview, draft_id.
4. **Queue.** `list_queue(account)`: what's staged, what's due, what failed.
5. **Decisions, always the user's.** Ask what to do, then:
   - approve specific drafts -> `approve_draft(draft_id)` one at a time
   - reject -> `reject_draft(draft_id)`
   - run due queued work -> `process_queue(account)` (daily caps apply)
   - cancel queued items -> `cancel_queued_action(queue_id)`

## Rules

- Read-only by default: this skill changes nothing unless the user directs
  an approve/reject/process/cancel.
- Keep the summary tight. The user wants the digest, not a data dump.
