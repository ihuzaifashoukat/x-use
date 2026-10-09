"""Bounded conversation reads from rendered native X cards.

DOM order is observation, not proof of ancestry. In particular, author identity,
visual adjacency and a displayed 'Replying to' handle cannot identify a parent
post. Return unknown parent IDs until a native post-specific edge is supported.
"""

import asyncio

from .errors import BrowserActionError


def _merge_order(order, batch):
    """Insert newly rendered IDs beside known anchors, retaining batch DOM order."""
    for index, identifier in enumerate(batch):
        if identifier in order:
            continue
        following = next((item for item in batch[index + 1:] if item in order), None)
        if following is not None:
            order.insert(order.index(following), identifier)
        else:
            preceding = next((item for item in reversed(batch[:index]) if item in order), None)
            if preceding is None:
                order.append(identifier)
            else:
                order.insert(order.index(preceding) + 1, identifier)


async def _snapshots(column):
    # One evaluation preserves the order of a single rendered sample even when
    # the timeline virtualizes cards. Inspect only visible primary-column cards;
    # exclude quote articles, ads, dialogs and sidebar material before parsing.
    from .page import _ELIGIBLE_CARD, _SNAPSHOT

    expression = "cards => cards.slice(0,100).filter(" + _ELIGIBLE_CARD + ").map(" + _SNAPSHOT + ")"
    return await column.locator('article[data-testid="tweet"]').evaluate_all(expression)


async def read_thread(browser, tweet_url, limit=20):
    """Return a partial context sample; never claim a complete connected thread.

    ``observed_count`` counts unique valid, eligible cards across all samples,
    including cards outside the returned window. ``position`` is zero based in
    the returned DOM order. The window always includes the verified focal post.
    """
    from .page import status_id, tweet_from_snapshot

    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise BrowserActionError("invalid_input")
    _, _, focal = await browser._target(tweet_url, wait_for_target=True)
    if status_id(browser.page.url) != focal:
        raise BrowserActionError("tweet_not_found")
    column = browser.page.get_by_test_id("primaryColumn")
    if await column.count() != 1 or not await column.is_visible():
        raise BrowserActionError("unsupported_dom")

    tweets, order, quiet = {}, [], 0
    stop_reason = "scroll_bound"
    for step in range(min(12, 3 + (limit + 7) // 8)):
        await browser.check_blocked()
        if status_id(browser.page.url) != focal:
            raise BrowserActionError("tweet_not_found")
        before = len(tweets)
        batch = []
        for data in await _snapshots(column):
            tweet = tweet_from_snapshot(data)
            if tweet is None or tweet.tweet_id in batch:
                continue
            batch.append(tweet.tweet_id)
            tweets.setdefault(tweet.tweet_id, tweet)
        _merge_order(order, batch)
        if step == 0 and focal not in tweets:
            # A quote/sidebar/hidden/promoted card cannot satisfy the focal proof.
            raise BrowserActionError("tweet_not_found")
        if len(tweets) >= limit:
            stop_reason = "limit_reached"
            break
        quiet = quiet + 1 if len(tweets) == before else 0
        if quiet >= 3:
            stop_reason = "no_growth"
            break
        if step + 1 < min(12, 3 + (limit + 7) // 8):
            # Hovering a tall timeline would scroll its center into view and
            # skip intermediate rows. Move inside its current viewport bounds.
            point = await column.evaluate("""column => {
              const b=column.getBoundingClientRect();
              return {x:Math.max(1,Math.min(innerWidth-1,b.left+b.width/2)),
                y:Math.max(1,Math.min(innerHeight-1,b.top+Math.min(b.height,innerHeight)/2))};
            }""")
            await browser.page.mouse.move(point["x"], point["y"])
            browser._reusable_navigation = None
            await browser.page.mouse.wheel(0, 800)
            # Wheel dispatch does not wait for scroll/hydration completion.
            await asyncio.sleep(0.25)

    await browser.check_blocked()
    if status_id(browser.page.url) != focal:
        raise BrowserActionError("tweet_not_found")
    focal_index = order.index(focal)
    start = max(0, focal_index - limit + 1)
    selected = order[start:start + limit]
    entries = [{"tweet_id": identifier, "position": position,
                "relation": "focal" if identifier == focal else "visible_context",
                "parent_tweet_id": None,
                "relationship_evidence": "exact_primary_permalink" if identifier == focal
                else "visible_primary_column_dom_order"}
               for position, identifier in enumerate(selected)]
    return {"focal_tweet_id": focal, "tweets": [tweets[item] for item in selected],
            "entries": entries, "partial": True, "stop_reason": stop_reason,
            "observed_count": len(tweets)}
