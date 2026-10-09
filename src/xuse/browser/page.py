"""Bounded X DOM reads and single-attempt writes using native Playwright.

Selectors intentionally fail closed when X changes. Successful writes carry
observed DOM evidence; a click alone never constitutes success.
"""
import asyncio
import functools
import json
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

from xuse.models import MediaItem, ScrapedTweet

from .analytics import AnalyticsMixin
from .errors import BrowserActionError, BrowserBlocked, SessionError
from .messaging import MessagingMixin
from .notifications import NotificationsMixin

_HANDLE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
_STATUS = re.compile(r"^/(?:[A-Za-z0-9_]{1,15}|i/web)/status/([0-9]+)(?:/(?:photo|video)/[0-9]+)?/?$")
_COUNT = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*([KMB])?", re.I)
_CARD = 'article[data-testid="tweet"]'
_QUOTED_DESCENDANTS = '[data-testid="quoteTweet"] *, div[role="link"] *, article[data-testid="tweet"] article[data-testid="tweet"] *'
_PRIMARY_TIME = 'time:not(' + _QUOTED_DESCENDANTS + ')'
_ELIGIBLE_CARD = r"""card => {
  if (!card.getClientRects().length || card.parentElement.closest(
    'article[data-testid="tweet"], [data-testid="quoteTweet"], div[role="link"], ' +
    'aside, [data-testid="sidebarColumn"], [role="dialog"], ' +
    '[data-testid="placementTracking"], [data-testid="promotedTweet"]')) return false;
  for (let node=card; node; node=node.parentElement) {
    const style=getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' ||
        style.visibility === 'collapse' || style.opacity === '0') return false;
  }
  const markers=[...card.querySelectorAll(
    '[data-testid="promotedIndicator"], [data-testid="ad"], [data-testid="socialContext"]'
  )].filter(node => node.closest('article[data-testid="tweet"]') === card &&
    !node.closest('[data-testid="quoteTweet"], div[role="link"]'));
  return !markers.some(node => node.dataset.testid !== 'socialContext' ||
    /^(?:Ad|Promoted)$/i.test(node.textContent.trim()));
}"""


def validate_x_url(value):
    if not isinstance(value, str) or len(value) > 8192 or any(ord(c) < 32 for c in value):
        raise BrowserActionError("invalid_url")
    try:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or parsed.hostname != "x.com" or parsed.port not in (None, 443) or parsed.username is not None or parsed.password is not None:
            raise ValueError()
    except ValueError:
        raise BrowserActionError("invalid_url") from None
    return value


def status_id(value):
    """Extract only a genuine X permalink, with exact numeric ID boundaries."""
    if not isinstance(value, str):
        return None
    try:
        value = urljoin("https://x.com", value)
        validate_x_url(value)
        match = _STATUS.fullmatch(urlsplit(value).path)
        return match.group(1) if match else None
    except BrowserActionError:
        return None


def _handle(value):
    if not isinstance(value, str) or not _HANDLE.fullmatch(value.removeprefix("@")):
        raise BrowserActionError("invalid_input")
    return value.removeprefix("@")


def _limit(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
        raise BrowserActionError("invalid_input")
    return value


def _count(value):
    match = _COUNT.search(str(value or "").replace(",", ""))
    if not match:
        return 0
    try:
        return int(float(match.group(1)) * {"": 1, "k": 1000, "m": 1000000, "b": 1000000000}[(match.group(2) or "").lower()])
    except (ValueError, OverflowError):
        return 0


def _https(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        return value if parsed.scheme == "https" and parsed.hostname else None
    except ValueError:
        return None


def tweet_from_snapshot(data):
    """Convert a credential-free card snapshot to the existing ScrapedTweet model."""
    if not isinstance(data, dict):
        return None
    permalink = urljoin("https://x.com", str(data.get("status_url") or ""))
    tweet_id = status_id(permalink)
    text = data.get("text")
    if not tweet_id or not isinstance(text, str):
        return None
    created = None
    if data.get("created_at"):
        try:
            created = datetime.fromisoformat(data["created_at"].replace("Z", "+00:00"))
        except (ValueError, TypeError, AttributeError):
            pass
    media = []
    seen = set()
    for item in (data.get("media") or [])[:16]:
        if not isinstance(item, dict):
            continue
        url = _https(item.get("url"))
        if url and url not in seen and item.get("type") in ("image", "video"):
            try:
                media.append(MediaItem(type=item["type"], url=url, alt_text=item.get("alt_text"),
                                       poster_url=_https(item.get("poster_url")),
                                       source_url=_https(item.get("source_url"))))
                seen.add(url)
            except ValueError:
                continue
    handle = data.get("handle")
    path = urlsplit(permalink).path.split("/")
    path_handle = path[1] if path[1:3] != ["i", "web"] else None
    if (isinstance(handle, str) and _HANDLE.fullmatch(handle.removeprefix("@"))
            and path_handle is not None and handle.removeprefix("@").lower() != path_handle.lower()):
        return None
    if not isinstance(handle, str) or not _HANDLE.fullmatch(handle.removeprefix("@")):
        handle = "@" + path_handle if path_handle and _HANDLE.fullmatch(path_handle) else None
    try:
        return ScrapedTweet(
            tweet_id=tweet_id, tweet_url=permalink,
            user_handle=handle, user_name=data.get("name"), text_content=text,
            user_is_verified=bool(data.get("verified")), created_at=created,
            reply_count=_count(data.get("reply_count")), retweet_count=_count(data.get("retweet_count")),
            like_count=_count(data.get("like_count")), view_count=_count(data.get("view_count")),
            profile_image_url=_https(data.get("profile_image_url")), media=media,
            embedded_media_urls=[item.url for item in media],
            tags=re.findall(r"(?<!\w)#[\w]+", text),
            mentions=re.findall(r"(?<!\w)@[A-Za-z0-9_]{1,15}\b", text),
            is_thread_candidate=bool(re.search(r"(?:\bthread\b|\b1/\d+\b|ðŸ§µ)", text, re.I)),
        )
    except (ValueError, TypeError):
        return None


# A time's parent permalink identifies the primary tweet. Selecting arbitrary
# /status/ links can accidentally operate on an embedded quoted post.
_SNAPSHOT = r"""card => {
  const own = node => node.closest('article[data-testid="tweet"]') === card &&
    !node.closest('[data-testid="quoteTweet"], div[role="link"]');
  const first = selector => [...card.querySelectorAll(selector)].find(own);
  const time = first('time');
  const primary = time && time.closest('a[href*="/status/"]');
  const text = first('[data-testid="tweetText"]');
  const user = first('[data-testid="User-Name"]');
  const strings = user ? [...user.querySelectorAll('span')].map(x => x.textContent) : [];
  const handle = strings.find(x => /^@[A-Za-z0-9_]{1,15}$/.test(x)) || null;
  const count = name => {
    const node = first(`[data-testid="${name}"]`);
    return node ? (node.textContent || node.getAttribute('aria-label') || '') : '';
  };
  const media = [...card.querySelectorAll('[data-testid="tweetPhoto"] img')]
    .filter(own)
    .map(x => ({type:'image',url:x.src,alt_text:x.alt || null}));
  for (const video of card.querySelectorAll('video')) {
    if (own(video)) {
      const src = video.currentSrc || video.src ||
        (video.querySelector('source[src]') || {}).src || null;
      const source = src && src.startsWith('https://') ? src : null;
      media.push({type:'video',url:video.poster || source,poster_url:video.poster || null,
        source_url:source,alt_text:video.getAttribute('aria-label') || null});
    }
  }
  const avatar = first('[data-testid^="UserAvatar-Container"] img');
  const analytics = first('a[href$="/analytics"]');
  return {status_url:primary && primary.getAttribute('href'),
    text:text ? text.innerText : '', handle,
    name:strings.find(x => x && !x.startsWith('@') && x !== 'Â·') || null,
    created_at:time && time.getAttribute('datetime'),
    verified:!!(user && user.querySelector('[data-testid="icon-verified"]')),
    reply_count:count('reply'),retweet_count:count('retweet') || count('unretweet'),
    like_count:count('like') || count('unlike'),view_count:analytics ? analytics.textContent : '',
    profile_image_url:avatar && avatar.src, media};
}"""
_VISIBLE_SNAPSHOT = "card => (" + _ELIGIBLE_CARD + ")(card) ? (" + _SNAPSHOT + ")(card) : null"


def _safe(reason="read_failed"):
    def decorate(function):
        @functools.wraps(function)
        async def invoke(*args, **kwargs):
            try:
                return await function(*args, **kwargs)
            except SessionError:
                raise
            except Exception:
                raise BrowserActionError(reason) from None
        return invoke
    return decorate


class XBrowser(NotificationsMixin, AnalyticsMixin, MessagingMixin):
    backend = "playwright"
    action_confirmation_timeout_ms = 15000
    navigation_reuse_seconds = 30

    def __init__(self, page, account=None):
        self.page = page
        self.account = account or {}
        self._reusable_navigation = None

    @staticmethod
    def _result(action, status="confirmed", **evidence):
        return {"action": action, "success": True, "status": status, "evidence": evidence}

    def _before_write(self):
        validator = getattr(self, "action_validator", None)
        if validator is not None:
            validator()

    async def _is_visible(self, locator):
        return await locator.count() > 0 and await locator.first.is_visible()

    async def _wait_visible(self, locator, timeout_ms=10000):
        try:
            await locator.first.wait_for(state="visible", timeout=timeout_ms)
            return locator.first
        except Exception:
            await self.check_blocked()
            raise BrowserActionError("unsupported_dom") from None

    @_safe()
    async def ensure_ready(self):
        await self.check_blocked()
        await self._wait_visible(self.page.get_by_test_id("SideNav_AccountSwitcher_Button"), timeout_ms=20000)
        return self._result("session_ready", authenticated_navigation=True)

    async def verify_session(self):
        """Read-only recovery probe; never clears a policy pause itself."""
        await self.navigate("https://x.com/home")
        return await self.ensure_ready()

    @_safe("navigation_failed")
    async def navigate(self, url, *, refresh=True, ready=None):
        """Navigate freshly unless a recent document and current DOM prove reuse.

        Reuse is opt-in, query-sensitive, and never extends the freshness window.
        Shared inbox/session callers retain their existing full-navigation default.
        ``ready`` is a non-waiting async predicate proving the operation's identity.
        Returns True only when the proven document was reused; fresh loads retain
        the existing None return value.
        """
        validate_x_url(url)
        if not isinstance(refresh, bool) or (ready is not None and not callable(ready)):
            raise BrowserActionError("invalid_input")
        if not refresh and ready is not None and self.page.url == url:
            # A stale/missing reuse record is not permission to reload away a
            # challenge on the same route. Explicit refresh retains its meaning.
            await self.check_blocked()
        previous = self._reusable_navigation
        if not refresh and ready is not None and previous is not None:
            route, loaded_at, document = previous
            if (url == route == self.page.url and
                    0 <= time.monotonic() - loaded_at < self.navigation_reuse_seconds):
                if document == await self._document_identity() and await ready():
                    await self.check_blocked()
                    if self.page.url == url and document == await self._document_identity():
                        return True
        self._reusable_navigation = None
        response = await self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
        if response is not None and response.status == 429:
            raise BrowserBlocked("rate_limited")
        if response is not None and response.status in (401, 403):
            raise BrowserBlocked("login_required" if response.status == 401 else "challenge")
        try:
            validate_x_url(self.page.url)
        except BrowserActionError:
            raise BrowserBlocked("external_redirect") from None
        await self.check_blocked()
        if ready is not None and self.page.url == url:
            document = await self._document_identity()
            if document is not None:
                self._reusable_navigation = (url, time.monotonic(), document)

    async def _document_identity(self):
        try:
            # A URL can survive a reload or SPA rewrite. Retain the document's
            # navigation epoch and viewport, rather than trusting the URL alone.
            value = await self.page.evaluate("() => [performance.timeOrigin, scrollX, scrollY]")
            return value if isinstance(value, list) and len(value) == 3 else None
        except Exception:
            return None  # Unavailable proof keeps full navigation enabled.

    @_safe()
    async def check_blocked(self):
        path = urlsplit(self.page.url).path.lower()
        if path.startswith(("/i/flow/login", "/login", "/logout")):
            raise BrowserBlocked("login_required")
        if path.startswith(("/account/access", "/i/flow/verify", "/challenge")):
            raise BrowserBlocked("challenge")
        checks = [
            ("challenge", self.page.locator('iframe[src*="captcha"], iframe[src*="arkoselabs"], [data-testid="ocfEnterTextTextInput"]')),
            ("challenge", self.page.get_by_text(re.compile(r"^(?:Verify you are human|Authenticate your account|Help us keep your account safe)$", re.I))),
            ("account_locked", self.page.get_by_text(re.compile(r"^(?:Your account is locked|Your account is suspended|Account suspended|Your account has been locked)$", re.I))),
            ("rate_limited", self.page.get_by_text(re.compile(r"^(?:Rate limit exceeded|You are rate limited|You have reached your daily limit.*)$", re.I))),
            ("login_required", self.page.get_by_test_id("loginButton")),
        ]
        if path.startswith(("/messages", "/i/chat")):
            checks.extend([
                ("pin_required", self.page.locator('input[name="pin"], input[autocomplete="one-time-code"], input[data-testid*="pin"], input[type="password"]')),
                ("pin_required", self.page.get_by_text(re.compile(r"^(?:Enter (?:your )?(?:PIN|passcode)|Unlock your messages)$", re.I))),
            ])
        for reason, locator in checks:
            if await self._is_visible(locator):
                raise BrowserBlocked(reason)

    async def _snapshots(self):
        cards = self.page.locator(_CARD)
        samples = [await cards.nth(index).evaluate(_VISIBLE_SNAPSHOT) for index in range(min(await cards.count(), 100))]
        return [data for data in samples if isinstance(data, dict)]

    async def _collect(self, limit, *, author=None, route=None):
        expected_route = route or self.page.url

        async def verify_route():
            await self.check_blocked()
            if self.page.url != expected_route:
                raise BrowserActionError("profile_mismatch" if author is not None else "unsupported_dom")

        await verify_route()
        samples = await self._snapshots()
        if not samples and not await self._is_visible(self.page.locator(_CARD)):
            empty = self.page.get_by_text(re.compile(r"^(?:No results for.*|No posts yet|This account doesn.t exist|These posts are protected)$", re.I))
            if await self._is_visible(empty):
                await verify_route()
                return []
            await self._wait_visible(self.page.locator(_CARD))
            samples = await self._snapshots()
        tweets, quiet = {}, 0
        for step in range(min(12, 2 + (limit + 7) // 8)):
            await verify_route()
            before = len(tweets)
            if step:
                samples = await self._snapshots()
            await verify_route()
            for data in samples:
                tweet = tweet_from_snapshot(data)
                if tweet and (author is None or str(tweet.user_handle or "").lstrip("@").lower() == author.lower()):
                    tweets.setdefault(tweet.tweet_id, tweet)
                    if len(tweets) >= limit:
                        return list(tweets.values())[:limit]
            quiet = quiet + 1 if len(tweets) == before else 0
            if quiet >= 2:
                break
            self._reusable_navigation = None
            await self.page.mouse.wheel(0, 800)
            await asyncio.sleep(0.25)
            await verify_route()
        await verify_route()
        return list(tweets.values())[:limit]

    @_safe()
    async def search_tweets(self, query, limit=20):
        _limit(limit)
        if not isinstance(query, str) or not query.strip() or len(query) > 1000:
            raise BrowserActionError("invalid_input")
        target = "https://x.com/search?q=" + quote(query, safe="") + "&src=typed_query&f=live"
        await self.navigate(target)
        return await self._collect(limit, route=target)

    @_safe()
    async def search_profile(self, handle, limit=20):
        _limit(limit)
        handle = _handle(handle)
        target = "https://x.com/" + handle
        await self._navigate_profile(handle)
        return await self._collect(limit, route=target)

    async def _target(self, url, *, wait_for_target=False):
        validate_x_url(url)
        target = status_id(url)
        if not target:
            raise BrowserActionError("invalid_url")
        candidate = None

        async def ready():
            nonlocal candidate
            candidate = await self._find_target(target) if status_id(self.page.url) == target else None
            return candidate is not None

        reused = await self.navigate(url, refresh=False, ready=ready)
        if status_id(self.page.url) != target:
            raise BrowserActionError("tweet_not_found")
        if not reused:
            candidate = await self._find_target(target)
        if candidate is None:
            await self._wait_visible(self.page.locator(_CARD))
        deadline = time.monotonic() + (1.5 if wait_for_target else 0)
        while True:
            found = candidate
            if found is not None:
                if not reused:
                    await self.check_blocked()
                if status_id(self.page.url) != target:
                    raise BrowserActionError("tweet_not_found")
                return found[0], found[1], target
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.25)
            await self.check_blocked()
            candidate = await self._find_target(target)
        raise BrowserActionError("tweet_not_found")

    async def _find_target(self, target):
        cards = self.page.locator(_CARD)
        matches = []
        for index in range(min(await cards.count(), 100)):
            card = cards.nth(index)
            data = await card.evaluate(_VISIBLE_SNAPSHOT)
            if not isinstance(data, dict):
                continue
            if status_id(data.get("status_url")) == target:
                # Bind the current card and primary permalink, never a recyclable
                # positional row or an embedded quote's controls.
                primary = self.page.locator('a[href=' + json.dumps(data["status_url"]) + '] ' + _PRIMARY_TIME)
                bound = card.filter(has=primary)
                if await bound.count() == 1 and await bound.is_visible():
                    matches.append((bound, data))
        return matches[0] if len(matches) == 1 else None

    async def _verify_action_target(self, card, target):
        if status_id(self.page.url) != target or await card.count() != 1 or not await card.is_visible():
            raise BrowserActionError("tweet_not_found")
        if status_id((await card.evaluate(_SNAPSHOT)).get("status_url")) != target or status_id(self.page.url) != target:
            raise BrowserActionError("tweet_not_found")
        if not await card.evaluate(_ELIGIBLE_CARD):
            raise BrowserActionError("tweet_not_found")

    @staticmethod
    def _target_control(card, testid):
        return card.locator('[data-testid="' + testid + '"]:not(' + _QUOTED_DESCENDANTS + ')')

    async def _verify_profile_target(self, header, handle):
        if await header.count() != 1 or not await header.is_visible() or self.page.url.rstrip("/").lower() != "https://x.com/" + handle.lower():
            raise BrowserActionError("profile_mismatch")

    @_safe()
    async def get_tweet(self, url):
        _, data, _ = await self._target(url)
        tweet = tweet_from_snapshot(data)
        return [tweet] if tweet else []

    @_safe()
    async def get_thread(self, tweet_url, limit=20):
        """Read ordered, bounded visible conversation context around an exact post."""
        from .threads import read_thread

        return await read_thread(self, tweet_url, limit)

    @_safe()
    async def get_home_feed(self, limit=20):
        _limit(limit)
        await self.navigate("https://x.com/home")
        return await self._collect(limit, route="https://x.com/home")

    @_safe()
    async def get_profile(self, handle):
        handle = _handle(handle)
        await self._navigate_profile(handle)
        name = self.page.get_by_test_id("UserName").filter(has=self.page.get_by_text(re.compile("^@" + re.escape(handle) + "$", re.I), exact=True))
        await self._wait_visible(name)
        if await name.count() != 1 or urlsplit(self.page.url).path.rstrip("/").lower() != "/" + handle.lower():
            raise BrowserActionError("profile_mismatch")
        biography = self.page.get_by_test_id("UserDescription")
        raw_name = await name.inner_text()
        result = {"handle": handle, "name": raw_name,
                  "display_name": re.split("@" + re.escape(handle), raw_name, maxsplit=1, flags=re.I)[0].strip() or None,
                  "bio": await biography.inner_text() if await biography.count() == 1 else "",
                  "url": "https://x.com/" + handle}
        for field, testid in (("location", "UserLocation"), ("website", "UserUrl"), ("joined", "UserJoinDate")):
            locator = self.page.get_by_test_id(testid)
            result[field] = await locator.inner_text() if await locator.count() == 1 and await locator.is_visible() else None
        message = self.page.get_by_test_id("sendDMFromProfile")
        result["can_message"] = bool(await message.count() == 1 and await message.is_visible() and await message.is_enabled())
        result["follows_you"] = bool(await self._is_visible(name.get_by_text("Follows you", exact=True)))
        for relationship in ("followers", "following"):
            link = self.page.locator(f'a[href="/{handle}/{relationship}"]')
            result[relationship + "_count"] = _count(await link.inner_text()) if await link.count() == 1 and await link.is_visible() else None
        await self.check_blocked()
        await self._verify_profile_target(name, handle)
        return result

    async def _navigate_profile(self, handle, suffix=""):
        if suffix:
            # The profile header proves the owner, not the selected replies or
            # media panel. Keep tab reads fresh until native tab proof is known.
            await self.navigate("https://x.com/" + handle + suffix)
            return

        async def ready():
            identity = self.page.get_by_test_id("UserName").filter(
                has=self.page.get_by_text(re.compile("^@" + re.escape(handle) + "$", re.I), exact=True))
            return await identity.count() == 1 and await identity.is_visible()

        await self.navigate("https://x.com/" + handle + suffix, refresh=False, ready=ready)

    @_safe()
    async def get_profile_context(self, handle, post_limit=5):
        """Read an exact profile and bounded authored posts in one operation."""
        handle = _handle(handle)
        _limit(post_limit)
        profile = await self.get_profile(handle)
        posts = await self._collect(post_limit, author=handle, route="https://x.com/" + handle)
        return {"profile": profile, "posts": posts, "partial": True,
                "pagination": "bounded_scroll", "source": "browser_dom"}

    @_safe()
    async def get_profile_posts(self, handle, feed="posts", limit=10):
        """Read authored content from one profile tab, excluding repost authors."""
        handle = _handle(handle)
        _limit(limit)
        suffix = {"posts": "", "replies": "/with_replies", "media": "/media"}.get(feed)
        if suffix is None:
            raise BrowserActionError("invalid_input")
        target = "https://x.com/" + handle + suffix
        await self._navigate_profile(handle, suffix)
        if self.page.url.rstrip("/").lower() != target.lower():
            raise BrowserActionError("profile_mismatch")
        identity = self.page.get_by_test_id("UserName").filter(has=self.page.get_by_text(re.compile("^@" + re.escape(handle) + "$", re.I), exact=True))
        await self._wait_visible(identity)
        if await identity.count() != 1:
            raise BrowserActionError("profile_mismatch")
        posts = await self._collect(limit, author=handle, route=target)
        return {"handle": handle, "feed": feed, "posts": posts, "count": len(posts),
                "partial": True, "pagination": "bounded_scroll", "source": "browser_dom"}

    @_safe()
    async def get_profile_connections(self, handle, relationship="following", limit=20):
        """Read visible public relationship rows; never infer private contacts."""
        handle = _handle(handle)
        _limit(limit)
        if relationship not in {"followers", "following"}:
            raise BrowserActionError("invalid_input")
        target = f"https://x.com/{handle}/{relationship}"
        await self.navigate(target)
        if self.page.url.rstrip("/").lower() != target.lower():
            raise BrowserActionError("profile_mismatch")
        column = self.page.get_by_test_id("primaryColumn")
        await self._wait_visible(column)
        if await column.count() != 1:
            raise BrowserActionError("unsupported_dom")
        rows = column.get_by_test_id("UserCell")
        if not await self._is_visible(rows):
            empty = column.get_by_test_id("emptyState")
            if await empty.count() == 1 and await empty.is_visible():
                return {"handle": handle, "relationship": relationship, "connections": [], "count": 0,
                        "partial": True, "pagination": "visible_only", "source": "browser_dom"}
            await self._wait_visible(rows)
        connections, seen = [], set()
        for index in range(min(await rows.count(), 100)):
            row = rows.nth(index)
            if not await row.is_visible():
                continue
            identities = set()
            links = row.locator("a[href]")
            for link_index in range(min(await links.count(), 20)):
                href = await links.nth(link_index).get_attribute("href")
                parsed = urlsplit(urljoin("https://x.com", href or ""))
                candidate = parsed.path.strip("/")
                if parsed.scheme == "https" and parsed.netloc == "x.com" and not parsed.query and not parsed.fragment and _HANDLE.fullmatch(candidate) and candidate.lower() not in {"home", "i", "search", "messages", "settings", "explore"}:
                    identities.add(candidate.lower())
            if len(identities) != 1:
                raise BrowserActionError("unsupported_dom")
            candidate = identities.pop()
            if candidate in seen:
                continue
            seen.add(candidate)
            connections.append({"handle": candidate, "url": "https://x.com/" + candidate,
                                "visible_text": await row.inner_text()})
            if len(connections) >= limit:
                break
        return {"handle": handle, "relationship": relationship, "connections": connections,
                "count": len(connections), "partial": True, "pagination": "visible_only", "source": "browser_dom"}

    @_safe("outcome_unknown")
    async def like(self, url):
        card, _, target = await self._target(url)
        inverse = self._target_control(card, "unlike")
        if await self._is_visible(inverse):
            await self._verify_action_target(card, target)
            return self._result("like", "already_done", tweet_id=target, observed="unlike_control")
        button = await self._wait_visible(self._target_control(card, "like"))
        await self._verify_action_target(card, target)
        self._before_write()
        await button.click()
        try:
            await inverse.wait_for(state="visible", timeout=10000)
        except Exception:
            await self.check_blocked()
            raise BrowserActionError("outcome_unknown") from None
        return self._result("like", tweet_id=target, observed="unlike_control")

    @_safe("outcome_unknown")
    async def retweet(self, url):
        card, _, target = await self._target(url)
        inverse = self._target_control(card, "unretweet")
        if await self._is_visible(inverse):
            await self._verify_action_target(card, target)
            return self._result("retweet", "already_done", tweet_id=target, observed="unretweet_control")
        button = await self._wait_visible(self._target_control(card, "retweet"))
        await self._verify_action_target(card, target)
        await button.click()
        confirm = await self._wait_visible(self.page.get_by_test_id("retweetConfirm"))
        await self._verify_action_target(card, target)
        self._before_write()
        await confirm.click()
        try:
            await inverse.wait_for(state="visible", timeout=10000)
        except Exception:
            await self.check_blocked()
            raise BrowserActionError("outcome_unknown") from None
        return self._result("retweet", tweet_id=target, observed="unretweet_control")

    @_safe("outcome_unknown")
    async def follow(self, handle):
        handle = _handle(handle)
        await self.navigate("https://x.com/" + handle)
        header = self.page.get_by_test_id("UserName").filter(has=self.page.get_by_text(re.compile("^@" + re.escape(handle) + "$", re.I), exact=True))
        await self._wait_visible(header)
        region = header.first.locator('xpath=ancestor::div[.//button[substring(@data-testid, string-length(@data-testid)-6)="-follow" or substring(@data-testid, string-length(@data-testid)-8)="-unfollow"]][1]')
        await self._wait_visible(region)
        await self._verify_profile_target(header, handle)
        inverse = region.locator('button[data-testid$="-unfollow"]')
        if await self._is_visible(inverse):
            return self._result("follow", "already_done", handle=handle, observed="unfollow_control")
        button = region.locator('button[data-testid$="-follow"]')
        if await button.count() != 1:
            raise BrowserActionError("unsupported_dom")
        await self._verify_profile_target(header, handle)
        self._before_write()
        await button.click()
        try:
            await inverse.wait_for(state="visible", timeout=10000)
        except Exception:
            await self.check_blocked()
            raise BrowserActionError("outcome_unknown") from None
        return self._result("follow", handle=handle, observed="unfollow_control")

    @staticmethod
    def _text(text):
        if not isinstance(text, str) or not text.strip() or len(text) > 25000 or "\x00" in text:
            raise BrowserActionError("invalid_input")
        return text

    async def _observe_submission(self, before, text, action, target=None):
        deadline = time.monotonic() + self.action_confirmation_timeout_ms / 1000
        own = {str(value).removeprefix("@").lower() for value in (self.account.get("self_handles") or [])}
        while time.monotonic() < deadline:
            await self.check_blocked()
            for data in await self._snapshots():
                tweet = tweet_from_snapshot(data)
                if not tweet or tweet.tweet_id in before or tweet.text_content != text:
                    continue
                if own and str(tweet.user_handle).removeprefix("@").lower() not in own:
                    continue
                if not own:
                    # Without a known account handle, another author's matching
                    # text is insufficient evidence. Use the submission toast.
                    continue
                return self._result(action, tweet_id=tweet.tweet_id, tweet_url=str(tweet.tweet_url),
                                    reply_to=target, observed="new_own_permalink_and_text")
            toast = self.page.get_by_test_id("toast")
            if await self._is_visible(toast):
                text_value = await toast.first.inner_text()
                if re.search(r"\bYour (?:post|reply) was sent\b", text_value, re.I):
                    links = toast.first.locator('a[href*="/status/"]')
                    for index in range(min(await links.count(), 8)):
                        href = await links.nth(index).get_attribute("href")
                        identifier = status_id(href)
                        if identifier and identifier not in before:
                            return self._result(action, tweet_id=identifier,
                                                tweet_url=urljoin("https://x.com", href), reply_to=target,
                                                observed="submission_toast_and_new_permalink")
            await asyncio.sleep(0.25)
        raise BrowserActionError("outcome_unknown")

    async def _submission_baseline(self):
        identifiers = {status_id(data.get("status_url")) for data in await self._snapshots()}
        toast = self.page.get_by_test_id("toast")
        if await self._is_visible(toast):
            links = toast.first.locator('a[href*="/status/"]')
            for index in range(min(await links.count(), 8)):
                identifiers.add(status_id(await links.nth(index).get_attribute("href")))
        return identifiers

    async def _verify_reply_target(self, composer, target, original=None, route=None):
        if route is not None and self.page.url != route:
            raise BrowserActionError("tweet_not_found")
        originals = composer.locator(_CARD + ':visible:not(' + _QUOTED_DESCENDANTS + ')')
        count = await originals.count()
        if count != 1:
            raise BrowserActionError("tweet_not_found")
        previews = [await originals.nth(index).evaluate(_SNAPSHOT) for index in range(min(count, 8))]
        identifiers = {status_id(data.get("status_url")) for data in previews} - {None}
        if target in identifiers:
            return
        if identifiers or original is None or count != 1 or not await originals.is_visible():
            raise BrowserActionError("tweet_not_found")
        verified = tweet_from_snapshot(original)
        if verified is None or verified.tweet_id != target or not verified.user_handle:
            raise BrowserActionError("tweet_not_found")
        expected_author = verified.user_handle.removeprefix("@").lower()
        user = originals.get_by_test_id("User-Name")
        if await user.count() != 1:
            raise BrowserActionError("tweet_not_found")
        preview = previews[0]
        preview_author = preview.get("handle")
        if preview_author and preview_author.removeprefix("@").lower() != expected_author:
            raise BrowserActionError("tweet_not_found")
        links = user.locator("a[href]")
        link_count = await links.count()
        if link_count == 0:
            # Current native previews link the avatar while the displayed
            # User-Name handle is plain text. Require both identities to agree.
            if not isinstance(preview_author, str) or not _HANDLE.fullmatch(preview_author.removeprefix("@")):
                raise BrowserActionError("tweet_not_found")
            handles = user.get_by_text(re.compile(r"^@[A-Za-z0-9_]{1,15}$"), exact=True)
            if await handles.count() != 1 or not await handles.is_visible() or (await handles.inner_text()).strip().removeprefix("@").lower() != expected_author:
                raise BrowserActionError("tweet_not_found")
            avatar = originals.locator('[data-testid^="UserAvatar-Container-"]:visible')
            if await avatar.count() != 1 or (await avatar.get_attribute("data-testid") or "").lower() != "useravatar-container-" + expected_author:
                raise BrowserActionError("tweet_not_found")
            links = avatar.locator("a[href]")
            link_count = await links.count()
            if link_count != 1 or not await links.is_visible():
                raise BrowserActionError("tweet_not_found")
        if not 1 <= link_count <= 8:
            raise BrowserActionError("tweet_not_found")
        authors = set()
        for index in range(link_count):
            href = urljoin("https://x.com", await links.nth(index).get_attribute("href") or "")
            try:
                validate_x_url(href)
            except BrowserActionError:
                raise BrowserActionError("tweet_not_found") from None
            parsed = urlsplit(href)
            handle = parsed.path.strip("/")
            if _HANDLE.fullmatch(handle) and not parsed.query and not parsed.fragment:
                authors.add(handle.lower())
        if authors != {expected_author}:
            raise BrowserActionError("tweet_not_found")
        source_text = " ".join(verified.text_content.split())
        preview_text = " ".join(str(preview.get("text") or "").split())
        if not source_text:
            raise BrowserActionError("tweet_not_found")
        if preview_text == source_text:
            return
        # Native media previews can append a displayed pic.x.com URL. This
        # suffix is accepted only for media on the independently verified post.
        if verified.media and re.fullmatch(re.escape(source_text) + r"\s+https://\s*pic\.x\.com/[A-Za-z0-9]+", preview_text):
            return
        raise BrowserActionError("tweet_not_found")

    async def _submit(self, text, composer, action, *, media=None, media_manifest=None, target=None, reply_snapshot=None, reply_route=None, submission_route=None):
        from .uploads import reviewed_uploads
        async with reviewed_uploads(media, media_manifest) as uploads:
            return await self._submit_prepared(text, composer, action, media=uploads, target=target,
                                               reply_snapshot=reply_snapshot, reply_route=reply_route,
                                               submission_route=submission_route)

    async def _submit_prepared(self, text, composer, action, *, media=None, target=None, reply_snapshot=None, reply_route=None, submission_route=None):
        if submission_route is not None and self.page.url != submission_route:
            raise BrowserActionError("unsupported_dom")
        if target is not None:
            await self._verify_reply_target(composer, target, reply_snapshot, reply_route)
        textbox = await self._wait_visible(composer.get_by_test_id("tweetTextarea_0"))
        if (await textbox.inner_text()).strip():
            raise BrowserActionError("composer_not_empty")
        if media:
            if not isinstance(media, (list, tuple)) or not 1 <= len(media) <= 4:
                raise BrowserActionError("media_invalid")
            paths = []
            for item in media:
                if not isinstance(item, str) or not Path(item).is_file():
                    raise BrowserActionError("media_invalid")
                paths.append(str(Path(item).resolve()))
            upload = composer.locator('input[type="file"]')
            if await upload.count() != 1:
                raise BrowserActionError("unsupported_dom")
            await upload.set_input_files(paths)
            await self._wait_visible(composer.get_by_test_id("attachments"))
        await textbox.fill(text)
        button = await self._wait_visible(composer.get_by_test_id("tweetButton"))
        if not await button.is_enabled():
            raise BrowserActionError("send_disabled")
        before = await self._submission_baseline()
        if target is not None:
            await self._verify_reply_target(composer, target, reply_snapshot, reply_route)
        if reply_route is not None and self.page.url != reply_route:
            raise BrowserActionError("tweet_not_found")
        if submission_route is not None:
            if (await composer.locator('[data-testid="tweetTextarea_0"]:visible').count() != 1
                    or await composer.locator('[data-testid="tweetButton"]:visible').count() != 1
                    or self.page.url != submission_route):
                raise BrowserActionError("unsupported_dom")
        self._before_write()
        await button.click()
        return await self._observe_submission(before, text, action, target)

    @_safe("outcome_unknown")
    async def post(self, text, media=None, community=None, media_manifest=None):
        text = self._text(text)
        if community is not None:
            # Audience UI does not expose a stable numeric community identity.
            # Refuse to publish publicly when the requested audience cannot be
            # independently proven. No heuristic community-name substitution.
            raise BrowserActionError("community_unverified")
        await self.navigate("https://x.com/compose/tweet")
        submission_route = self.page.url
        if urlsplit(submission_route).path not in {"/compose/tweet", "/compose/post"}:
            raise BrowserActionError("unsupported_dom")
        # The outer dialog can inherit hidden visibility while its nested
        # native composer overrides it. Bind to the one visible submit control
        # and its nearest dialog instead of the first accessible-role match.
        buttons = self.page.locator('[data-testid="tweetButton"]:visible')
        await self._wait_visible(buttons)
        if await buttons.count() != 1:
            raise BrowserActionError("unsupported_dom")
        composer = buttons.locator('xpath=ancestor::*[@role="dialog"][1]')
        if await composer.count() != 1 or not await composer.is_visible():
            raise BrowserActionError("unsupported_dom")
        textboxes = composer.locator('[data-testid="tweetTextarea_0"]:visible')
        await self._wait_visible(textboxes)
        if await textboxes.count() != 1 or self.page.url != submission_route:
            raise BrowserActionError("unsupported_dom")
        return await self._submit(text, composer, "post", media=media, media_manifest=media_manifest,
                                  submission_route=submission_route)

    @_safe("outcome_unknown")
    async def reply(self, url, text, media=None, media_manifest=None):
        text = self._text(text)
        card, original, target = await self._target(url)
        source_route = self.page.url
        if status_id(source_route) != target:
            raise BrowserActionError("tweet_not_found")
        if await self.page.locator('[data-testid="tweetButton"]:visible').count():
            raise BrowserActionError("composer_not_empty")
        button = await self._wait_visible(self._target_control(card, "reply"))
        await self._verify_action_target(card, target)
        await button.click()
        # X can nest a visible reply dialog inside a hidden outer dialog.
        # The modal submit control distinguishes it from the inline composer.
        buttons = self.page.locator('[data-testid="tweetButton"]:visible')
        await self._wait_visible(buttons)
        if await buttons.count() != 1:
            raise BrowserActionError("unsupported_dom")
        composer = buttons.locator('xpath=ancestor::*[@role="dialog"][1]')
        if await composer.count() != 1 or not await composer.is_visible():
            raise BrowserActionError("unsupported_dom")
        textboxes = composer.locator('[data-testid="tweetTextarea_0"]:visible')
        await self._wait_visible(textboxes)
        if await textboxes.count() != 1:
            raise BrowserActionError("unsupported_dom")
        reply_route = self.page.url
        validate_x_url(reply_route)
        if reply_route not in (source_route, "https://x.com/compose/post"):
            raise BrowserActionError("tweet_not_found")
        return await self._submit(text, composer, "reply", target=target, media=media, media_manifest=media_manifest,
                                  reply_snapshot=original, reply_route=reply_route)
