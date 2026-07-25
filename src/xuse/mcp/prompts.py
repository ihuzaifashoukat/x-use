"""MCP prompts: the workflows, in the protocol instead of in a skill file.

x-use ships SKILL.md files, but those only work in clients that implement Agent
Skills. Prompts are the portable equivalent: any MCP client can surface them,
so a Claude Desktop, Cursor, or Windsurf user gets the same workflows without
installing anything. The text below is deliberately the condensed version of
the bundled skills; when the two disagree, the skill file is the longer form
and this is the one that has to stay correct on its own.

Every prompt is read-only to produce. None of them call a tool. They return
instructions the client's model then follows, and the safety gates still apply
to whatever it does next.
"""
import logging

logger = logging.getLogger(__name__)

MAX_REPLY_CHARS = 270  # mirrors executor.MAX_REPLY_CHARS, the hard clamp on replies

GATES = """\
Two gates protect this account. Do not work around either one.
1. Write tools (post_tweet, reply_to_tweet, generate_and_post, engage) return a
   draft and change nothing on X. Only approve_draft(draft_id) publishes.
   If a write response has NO draft_id, draft mode is off on this server and the
   action ALREADY PUBLISHED. Stop and say so.
2. queue_post and queue_engagement only store work. Only process_queue runs it.
Never approve or process anything the user did not ask for by id."""


def _default_account(ctx) -> str:
    """First active configured account, or empty string. Prompt rendering must
    never fail: a missing config is a thing to tell the user about, not a crash."""
    try:
        for raw in ctx.config_loader.get_accounts_config():
            if isinstance(raw, dict) and raw.get("is_active", True) and raw.get("account_id"):
                return raw["account_id"]
    except Exception:  # pragma: no cover - defensive
        logger.warning("Could not resolve a default account for a prompt.", exc_info=True)
    return ""


def _resolve(ctx, account: str) -> str:
    return (account or "").strip() or _default_account(ctx) or "<no account configured>"


def register_prompts(server, ctx) -> None:
    """Register the five workflow prompts on the FastMCP server."""

    @server.prompt(
        description="Research what is worth engaging with on X for one account. Read-only: "
                    "searches keywords and watched profiles, reads candidates including their "
                    "images, and reports a scored shortlist. Stages nothing."
    )
    def research_niche(account: str = "", keywords: str = "", profiles: str = "") -> str:
        """Find posts worth replying to, without staging anything."""
        acc = _resolve(ctx, account)
        kw = keywords.strip() or "the account's own target_keywords"
        prof = profiles.strip() or "the profiles in the account's competitor_profiles"
        return f"""Research X for account `{acc}`. This is read-only. Stage nothing and post nothing.

1. `get_account("{acc}")` for persona, target_keywords, and competitor_profiles.
2. `list_drafts(account="{acc}", status="pending")` and `list_queue(account="{acc}")`.
   Anything already staged is out of scope; you would only create a duplicate.
3. Search {kw}. One `search_tweets(keywords=<single query>, limit=8, account="{acc}")`
   per query, at most 4 queries. Broad queries return noise; use focused ones.
4. Read {prof} with `search_profile(profile="@handle", limit=5, account="{acc}")`,
   at most 3. Profile timelines include pinned posts and reposts, so check
   `user_handle` on each result before treating it as that person's own writing.
5. For each promising post, `prepare_reply("{acc}", <tweet_url>)`. Look at the
   images it attaches. A chart or a screenshot changes what a good reply is.
6. Score each candidate out of 5: it is a question; the author is small enough
   that a reply gets read; it is fresh with few replies; the author posts on this
   topic regularly; you can name one concrete thing your reply would add.
   Drop anything below 3.

Report at most 5 candidates: author, one-line gist, score, and the specific angle
you would take. If nothing scores 3 or higher, say so. An honest empty result is
correct; never pad the list."""

    @server.prompt(
        description="Write and stage X replies or posts for one account in its persona. "
                    "Produces drafts or queued items for review and publishes nothing."
    )
    def draft_replies(account: str = "", tweet_urls: str = "", lane: str = "draft") -> str:
        """Stage replies or posts, written by you, in the account's voice."""
        acc = _resolve(ctx, account)
        targets = tweet_urls.strip() or "the candidates from the research step"
        queued = lane.strip().lower() == "queue"
        stage = ('`queue_engagement(account, action="reply", tweet_url=..., text=<yours>)`'
                 if queued else "`reply_to_tweet(account, tweet_url, text=<yours>)`")
        undo = "`cancel_queued_action(queue_id)`" if queued else "`reject_draft(draft_id)`"
        return f"""Stage X work for account `{acc}` on {targets}. You write every word.

{GATES}

Pick ONE lane and stay in it. This run is `{lane.strip().lower() or 'draft'}`.
Drafts and queued items live in separate stores with no bridge between them, so
staging the same target in both creates two records that collide later at the
server's dedup check and surface as a confusing failure.

1. `get_account("{acc}")` and follow its `persona`. No persona set? Say so and
   stop; anything you write will read as generic.
2. `prepare_reply("{acc}", <url>)` for any target you lack context on.
3. Write reply 1 yourself. Under {MAX_REPLY_CHARS} characters, which is the server's
   hard clamp; longer text is silently truncated. One concrete point: a number, a
   tradeoff, or a correction. No hashtags, no emoji, no links unless asked. Never
   open with praise.
4. Stage it with {stage}.
5. Show it to the user with its id. Take edits, re-stage, and {undo} whatever the
   edit replaced so the review list stays clean.
6. Repeat one at a time, at most 3 unless the user asks for more.

Never pass text="auto". It hands the writing to a server-side LLM that needs an
API key and has less context than you. Finish by listing what is staged and
reminding the user that nothing is public yet."""

    @server.prompt(
        description="Review everything staged for one X account and publish only what the "
                    "user approves by id. This is the workflow that makes things public."
    )
    def review_and_publish(account: str = "") -> str:
        """Approve, reject, or drain staged work, one item at a time."""
        acc = _resolve(ctx, account)
        return f"""Review and publish staged work for account `{acc}`.

THIS WORKFLOW PUBLISHES TO X. Approval is per item and per session; permission for
one batch never carries to the next.

1. `get_account_health("{acc}")` first. If `cookies.valid` is false, STOP.
   Approving against expired cookies burns the draft: it is marked failed and can
   never be re-approved. Point the user at re-exporting cookies and
   `update_account(account, cookie_file=...)`.
2. `list_drafts(account="{acc}", status="pending")` and `list_queue(account="{acc}")`.
3. Show both as numbered tables with the FULL text, not a preview. This is the
   last human read before it is public.
4. Ask which ones. "Looks good" is not an answer; get ids.
5. For each approved id, call `approve_draft(draft_id)` ONCE and report the result
   before the next call. Never loop over the whole list, even if asked for all of
   them: a mid-batch failure has to be visible, not buried.
6. `reject_draft(draft_id)` for the rest, and record the user's stated reason.
   Rejection reasons are the only voice correction this account ever gets.
7. For queued work, `process_queue(account="{acc}", max_actions=3)`. Warn first
   that pacing is 90 to 240 seconds per item and daily caps apply (5 posts,
   15 replies, 30 likes, 10 retweets). Always pass `account`; omitting it drains
   every configured account.
8. Report what published, what did not, and why.

Note: approve_draft returns no post URL. To find what published, read the account's
own timeline with `search_profile(profile="@<self handle>", account="{acc}")`."""

    @server.prompt(
        description="Daily digest for one X account: health, metrics, pending drafts, and "
                    "queue state in one read-only pass, then act only on the user's direction."
    )
    def daily_check(account: str = "") -> str:
        """One pass over an account's state, changing nothing by default."""
        acc = _resolve(ctx, account)
        return f"""Give a daily digest for account `{acc}`. Read-only unless the user directs otherwise.

1. `get_account_health("{acc}")`. Report cookie validity FIRST; nothing else works
   without it. Then config validity, warm session, queue depth, pending drafts.
2. `get_metrics("{acc}")` for counters and the last events. Summarize in two or
   three lines and always include errors.
3. `list_drafts(status="pending", account="{acc}")` as a numbered table.
4. `list_queue(account="{acc}")`. Call out anything `failed`, with its `last_error`.
   Items at max attempts are dead and need cancelling or re-staging.
5. If the account has `self_handles` set, `search_profile(profile="@<handle>",
   limit=10, account="{acc}")` to see what actually published recently, and
   `get_tweet` anything over a day old for its public counts.

Then ask what to do. Only on an explicit instruction: `approve_draft`,
`reject_draft`, `process_queue`, or `cancel_queued_action`. Change nothing on your
own initiative."""

    @server.prompt(
        description="Set up x-use from nothing: verify the install, add an X account from a "
                    "cookie export, and configure its niche, keywords, and persona."
    )
    def setup_account() -> str:
        """Conversational onboarding. The user never edits a config file."""
        return """Set up x-use with the user. They should never open a config file. Confirm each
step before moving on, and skip anything already done.

1. Install check. Run `x-use doctor` (browser/driver, cookies, LLM key, proxies).
   Not found? `pip install x-use-mcp`, then re-run. Note that interactive use
   needs no LLM key at all: the calling model writes the text.

2. Account and cookies. Ask for a short id like `main` or `brand`. Then explain
   the cookie export: install a cookie-export browser extension, log into x.com,
   export the x.com cookies as JSON, save the file, and give you the PATH.
   Call `add_account(account_id, cookie_file=<path>)`. The file is validated and
   copied server-side; cookie values never pass through the conversation, so do
   not ask the user to paste their contents. Verify with
   `get_account_health(account)`.

3. Niche. Ask what they work on, which keywords to watch, and whose posts they
   want to engage with. Apply with `update_account(account, target_keywords=[...],
   competitor_profiles=[...])`.

4. Self handles. Ask for their actual X handle and set
   `update_account(account, self_handles=["handle"])`. Without it the own-post
   guard cannot fire and nothing can find their published posts later.

5. Persona. Ask how they want to sound, then `update_account(account, persona=...)`.
   Under 4000 characters, markdown, covering voice, topics, reply style, what to
   avoid, and one or two example replies. Batch config edits: each update_account
   closes the warm browser session and the next action pays a cold start.

6. Prove it. Stage one real but harmless draft, show it with `list_drafts`, and
   tell them plainly that nothing publishes until they say approve_draft."""
