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
2. **Metrics.** `get_metrics(account)`: local posts/replies/likes/errors counters
   and recent events. These are recorded x-use actions, not X owner analytics
   or full engagement statistics. `get_account_analytics` reports observed
   availability: Premium paywalls and unsupported layouts return no metrics.
3. **Pending drafts.** `list_drafts(status="pending", account=...)`. Present a
   numbered table: action, target, text preview, draft_id. Read
   `get_account_safety(account)` and inspect uncertain or partial drafts before
   approving more work; pending drafts alone do not describe recovery state.
4. **Queue.** `list_queue(account)`: what's staged, what's due, what failed.
   Follow `next_offset` as the next call's `offset` until it is `null`.
5. **Decisions, always the user's.** Ask what to do, then:
   - approve specific drafts -> inspect full `get_draft` payloads, then
     `approve_draft(draft_id)` one at a time within the user's authorization
   - reject -> `reject_draft(draft_id)`
   - run due queued work -> `process_queue(account)` (daily caps apply)
   - cancel queued items -> `cancel_queued_action(queue_id)`

For requested incoming activity, read `get_inbox` before opening chats so unread
evidence is preserved; requests and other folders must be inspected separately.
`get_notifications(account=..., view="all"|"mentions", limit=5)` returns
bounded structured notification rows, with unknown IDs, type or unread state
when evidence is absent. Visiting notifications may mark them read. Treat
both surfaces as partial; empty supported windows do not establish full history.
Use **x-use-inbox** for conversation context, notifications, and authorized DMs;
use **x-use-engage** for public-post replies and profile outreach research.

## Rules

- Read-only by default: this skill changes nothing unless the user directs
  an approve/reject/process/cancel. Browser reads can update X read state;
  the opt-in auto-drain worker may execute queued items independently.
- Keep the summary tight. The user wants the digest, not a data dump.
