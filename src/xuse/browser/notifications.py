"""Bounded notification snapshots from observed X tabs and article rows.

Event types use only observed action copy outside post previews. Unread
indicators and stable notification IDs remain unknown without a DOM contract.
"""
import asyncio
from datetime import datetime
from urllib.parse import urljoin, urlsplit

from .errors import BrowserActionError, BrowserBlocked
from .messaging import _limit, _observed_at, _profile_handle, _safe_dom_errors

_VIEWS = {"all": ("https://x.com/notifications", "All"),
          "mentions": ("https://x.com/notifications/mentions", "Mentions")}
_ACTION_TYPES = {"followed you": "follow", "liked your post": "like",
                 "liked your reply": "like", "liked 2 of your posts": "like"}

_SCOPE = r"""(root, route) => {
  const visible = node => {
    if (!node.getClientRects().length) return false;
    for (let p=node; p; p=p.parentElement) {
      const s=getComputedStyle(p);
      if (s.display==='none' || ['hidden','collapse'].includes(s.visibility) || s.opacity==='0') return false;
    }
    return true;
  };
  if (location.href!==route || !root.isConnected || !visible(root)) return false;
  const roots=[...document.querySelectorAll('[data-testid="primaryColumn"]')].filter(visible);
  if (roots.length!==1 || roots[0]!==root) return false;
  const name=route.endsWith('/mentions') ? 'Mentions' : 'All';
  const tabs=[...root.querySelectorAll('[role="tab"]')].filter(visible);
  const expected=tabs.filter(tab=>tab.innerText.trim()===name);
  const selected=tabs.filter(tab=>tab.getAttribute('aria-selected')==='true');
  return expected.length===1 && selected.length===1 && expected[0]===selected[0] &&
    expected[0].getAttribute('href')===new URL(route).pathname;
}"""

_WINDOW = r"""(root, args) => {
  const scopeReady = """ + _SCOPE + r""";
  if (!scopeReady(root,args.route)) return {rows:[],scanned:0,scan_truncated:false};
  const selector = 'article[data-testid="notification"],article[data-testid="tweet"]';
  const visible = node => {
    if (!node.getClientRects().length) return false;
    for (let p=node; p; p=p.parentElement) {
      const s=getComputedStyle(p);
      if (s.display==='none' || ['hidden','collapse'].includes(s.visibility) || s.opacity==='0') return false;
    }
    return true;
  };
  const cards=[...root.querySelectorAll(selector)];
  const rows=[];
  let scanned=0;
  for (const card of cards.slice(0,200)) {
    scanned++;
    if (!visible(card) || card.parentElement.closest(selector+',aside,[data-testid="sidebarColumn"],[role="dialog"],[data-testid="placementTracking"],[data-testid="quoteTweet"]')) continue;
    const own = node => node.closest(selector)===card && visible(node) && !node.closest('[data-testid="quoteTweet"]');
    if ([...card.querySelectorAll('[data-testid="promotedIndicator"],[data-testid="ad"],[data-testid="promotedTweet"]')].some(own)) continue;
    const links=[...card.querySelectorAll('a[href]')].filter(own).slice(0,40);
    const avatars=[...card.querySelectorAll('[data-testid^="UserAvatar-Container-"]')].filter(own).slice(0,20);
    const actors=avatars.map(avatar => {
      const href=avatar.closest('a[href]')?.getAttribute('href') || avatar.querySelector('a[href]')?.getAttribute('href');
      const names=[...new Set(links.filter(a=>a.getAttribute('href')===href && !a.contains(avatar) && a.innerText.trim())
        .map(a=>a.innerText.trim().slice(0,200)))];
      return {href:href || null,avatar_handle:avatar.getAttribute('data-testid').slice('UserAvatar-Container-'.length),
        name:names.length===1 ? names[0] : null};
    });
    const times=[...card.querySelectorAll('time[datetime]')].filter(own).slice(0,2);
    const texts=[...card.querySelectorAll('[data-testid="tweetText"]')].filter(own).slice(0,2);
    const media=[...card.querySelectorAll('[data-testid="tweetPhoto"] img')].filter(own).slice(0,4)
      .map(img=>({type:'image',url:img.currentSrc || img.src,alt_text:img.alt.slice(0,1000) || null}));
    for (const video of [...card.querySelectorAll('video')].filter(own).slice(0,1)) {
      media.push({type:'video',url:video.poster || null,poster_url:video.poster || null});
    }
    const kind=card.getAttribute('data-testid')==='tweet' ? 'post' : 'notification';
    // These exact leaf-span phrases were observed as notification action copy.
    // Never classify a post, preview, quoted post, link, time or media caption.
    const phrases=new Set(['followed you','liked your post','liked your reply','liked 2 of your posts']);
    const actionCopies=kind==='notification' ? [...new Set([...card.querySelectorAll('span')]
      .filter(node=>own(node) && !node.children.length && !node.closest('a,time,[data-testid="tweetText"],[data-testid="tweetPhoto"],figcaption'))
      .map(node=>node.innerText.trim()).filter(text=>phrases.has(text)))] : [];
    const primary=kind==='post' && times.length===1 ? times[0].closest('a[href*="/status/"]')?.getAttribute('href') : null;
    const postLinks=kind==='post' ? (primary ? [primary] : []) : [...new Set(links.map(a=>a.getAttribute('href')).filter(h=>h?.includes('/status/')))].slice(0,10);
    rows.push({row_kind:kind,text:card.innerText.trim().slice(0,12000),actors,
      action_copy:actionCopies.length===1 ? actionCopies[0] : null,
      created_at:times.length===1 ? times[0].getAttribute('datetime') : null,
      timestamp_text:times.length===1 ? (times[0].parentElement.getAttribute('aria-label') || times[0].innerText).slice(0,200) : null,
      post_text:texts.length===1 ? texts[0].innerText.slice(0,10000) : null,post_links:postLinks,media,
      post_context_partial:texts.length>1 || (kind==='post' && !primary)});
    if (rows.length>=args.maximum) break;
  }
  return {rows,scanned,scan_truncated:cards.length>200};
}"""


def _media(items):
    result = []
    for item in items[:5] if isinstance(items, list) else []:
        if not isinstance(item, dict) or item.get("type") not in ("image", "video"):
            continue
        url = item.get("url")
        if not isinstance(url, str) or len(url) > 2048:
            continue
        try:
            parsed = urlsplit(url)
            if (parsed.scheme != "https" or parsed.hostname not in {"pbs.twimg.com", "video.twimg.com"}
                    or parsed.username or parsed.password or parsed.port not in (None, 443)):
                continue
        except ValueError:
            continue
        result.append({"type": item["type"], "url": url,
                       "alt_text": item.get("alt_text") if isinstance(item.get("alt_text"), str) else None,
                       "poster_url": url if item["type"] == "video" else None,
                       "video_analysis": "poster_only" if item["type"] == "video" else None})
    return result


def parse_notification_snapshot(snapshot):
    """Normalize only observed row facts; notification semantics stay unknown."""
    from .page import status_id
    actors, seen = [], set()
    for actor in snapshot.get("actors", [])[:20]:
        if not isinstance(actor, dict):
            continue
        handle = _profile_handle(actor.get("href"))
        avatar_handle = actor.get("avatar_handle")
        evidenced_handle = _profile_handle("/" + avatar_handle) if isinstance(avatar_handle, str) else None
        if handle and evidenced_handle == handle and handle not in seen:
            seen.add(handle)
            actors.append({"handle": handle, "url": "https://x.com/" + handle,
                           "name": actor.get("name") if isinstance(actor.get("name"), str) else None,
                           "source": "visible_avatar_profile_link"})
    posts, seen = [], set()
    for link in snapshot.get("post_links", [])[:10]:
        identifier = status_id(link)
        if identifier and identifier not in seen:
            seen.add(identifier)
            url = urljoin("https://x.com", link)
            canonical = "https://x.com" + urlsplit(url).path.split("/status/")[0] + "/status/" + identifier
            posts.append({"post_id": identifier, "url": canonical,
                          "text": None, "media": [], "source": "visible_post_permalink"})
    post_text = snapshot.get("post_text")
    media = _media(snapshot.get("media"))
    if isinstance(post_text, str) or media:
        if snapshot.get("row_kind") == "post" and len(posts) == 1:
            posts[0].update(text=post_text, media=media)
        else:
            posts.append({"post_id": None, "url": None, "text": post_text, "media": media,
                          "source": "visible_notification_post_preview"})
    created = snapshot.get("created_at")
    try:
        if not isinstance(created, str) or len(created) > 64 or datetime.fromisoformat(created.replace("Z", "+00:00")).tzinfo is None:
            created = None
    except ValueError:
        created = None
    action = snapshot.get("action_copy")
    event_type = _ACTION_TYPES.get(action, "unknown") if snapshot.get("row_kind") == "notification" and actors else "unknown"
    evidence = {"source": "visible_notification_action_copy", "text": action} if event_type != "unknown" else None
    return {"notification_id": None, "notification_id_source": "not_exposed",
            "type": event_type, "type_evidence": evidence, "row_kind": snapshot.get("row_kind"),
            "text": snapshot.get("text", ""), "actors": actors, "actors_partial": True,
            "related_posts": posts, "post_context_partial": bool(snapshot.get("post_context_partial")) or
                any(post["post_id"] is None for post in posts),
            "created_at": created, "created_at_source": "time_datetime" if created else "not_exposed",
            "timestamp_text": snapshot.get("timestamp_text"), "unread": None, "unread_source": "not_exposed"}


class NotificationsMixin:
    notifications_timeout_ms = 10_000

    async def _notifications_scope(self, view):
        route = _VIEWS[view][0]
        if self.page.url != route:
            return None
        roots = await self._visible(self.page.locator('[data-testid="primaryColumn"]'))
        if len(roots) != 1:
            return None
        root = roots[0]
        ready = await root.evaluate(_SCOPE, route)
        return root if ready and self.page.url == route else None

    @_safe_dom_errors("read_failed")
    async def get_notifications(self, limit=20, *, view="all"):
        _limit(limit, 50)
        if not isinstance(view, str) or view not in _VIEWS:
            raise ValueError("view must be all or mentions.")
        route = _VIEWS[view][0]
        try:
            await self.check_blocked()
        except BrowserBlocked as exc:
            # The evidenced recovery route gates encrypted inbox reads. A
            # notification read can leave it without clearing the policy pause.
            current = urlsplit(self.page.url)
            if (exc.reason != "pin_required" or current.scheme != "https"
                    or current.netloc != "x.com" or current.path != "/i/chat/pin/recovery"):
                raise
        await self.navigate(route, refresh=False, ready=lambda: self._notifications_ready(view))
        deadline = asyncio.get_running_loop().time() + self.notifications_timeout_ms / 1000
        window = None
        while True:
            await self.check_blocked()
            root = await self._notifications_scope(view)
            if root is not None:
                window = await root.evaluate(_WINDOW, {"maximum": limit + 1, "route": route})
                if window["rows"]:
                    break
            if asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(0.1)
        await self.check_blocked()
        if (await self._notifications_scope(view) is None or self.page.url != route
                or not window or not window["rows"]):
            raise BrowserActionError("unsupported_dom")
        return {"notifications": [parse_notification_snapshot(row) for row in window["rows"][:limit]],
                "count": min(len(window["rows"]), limit), "limit": limit, "view": view, "view_verified": True,
                "status": "ok", "partial": True, "coverage": "visible_only", "pagination": "visible_only",
                "truncated": len(window["rows"]) > limit or window["scan_truncated"], "scanned": window["scanned"],
                "source": "browser_dom", "observed_at": _observed_at(),
                "unread_side_effects": "Visiting notifications may mark them read on X. Unread state describes only the observed view."}

    async def _notifications_ready(self, view):
        return await self._notifications_scope(view) is not None
