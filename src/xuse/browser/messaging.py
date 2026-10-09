"""Visible-DOM messaging adapter for X.

Reads intentionally describe only the items currently rendered by X. Writes
require an identified conversation, a unique composer, and evidence of a new
outgoing message. Unsupported or encrypted UI fails closed; this module never
uses private endpoints, guesses a PIN, or automatically retries a send.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import uuid
from functools import wraps
from collections import Counter
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlsplit

from .errors import BrowserActionError, BrowserBlocked


_NUMERIC_CONVERSATION = re.compile(r"[0-9]{1,30}-[0-9]{1,30}\Z")
_HANDLE = re.compile(r"@?([A-Za-z0-9_]{1,15})\Z")
_HOSTS = {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}
_NON_PROFILE_ROUTES = {"home", "messages", "settings", "compose", "explore", "search", "notifications", "i", "login", "logout", "tos", "privacy"}
_VISIBLE_HANDLE = re.compile(r"^@[A-Za-z0-9_]{1,15}$")
_MESSAGE_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_INBOX = (
    '[data-testid="DMInbox"], [data-testid="conversationList"], '
    '[data-testid="chatConversationList"], [data-testid="chat-conversation-list"]'
    ', [data-testid="dm-inbox-panel"]'
)
_CONVERSATION_ROWS = (
    '[data-testid="conversation"], [data-testid="conversation-item"], '
    '[data-testid="chatConversationItem"], [data-testid="DMConversationItem"], '
    '[data-testid^="dm-conversation-item-"]'
)
_REQUEST_ROWS = '[data-testid^="dm-message-request-item-"]'
_REQUEST_ROOT = '[data-testid="dm-message-requests"]'
_REQUEST_SCROLLER = '[data-testid="dm-message-requests-scroller"]'
_REQUEST_PROMPT = '[data-testid="dm-message-request-prompt"]'
_FOLDER_PATHS = {"inbox": "/i/chat", "requests": "/i/chat/requests", "other": "/i/chat/requests/other"}
_NATIVE_ROW_ID = re.compile(r"dm-(?:conversation-item|message-request-item)-[0-9]{1,30}:[0-9]{1,30}\Z")
_REQUEST_INFO = "Message requests from accounts you don’t follow live here. To reply to their messages, you need to accept the request."
_CONVERSATION_LINKS = 'a[href^="/messages/"], a[href^="/i/chat/"]'
_UNREAD_MARKER = (
    '[data-testid="DMUnreadBadge"], [data-testid="unreadBadge"], '
    '[data-testid="UnreadIndicator"], [data-testid="chatUnreadIndicator"], '
    '[aria-label="Unread"], [aria-label="Unread conversation"]'
)
_CONVERSATION = (
    '[data-testid="DMConversationView"], [data-testid="conversationDetail"], '
    '[data-testid="chatConversation"], [data-testid="chat-conversation"]'
    ', [data-testid="dm-conversation-panel"]'
)
_HEADER = (
    '[data-testid="DMConversationHeader"], [data-testid="conversationHeader"], '
    '[data-testid="chatConversationHeader"], [data-testid="chat-conversation-header"]'
    ', [data-testid="dm-conversation-header"]'
)
_ENTRIES = (
    '[data-testid="messageEntry"], [data-testid="chatMessage"], '
    '[data-testid="message"], [data-testid="outgoingMessage"], '
    '[data-dm-message-row]'
)
_ENTRY_TEXT = (
    '[data-testid="messageEntryText"], [data-testid="messageText"], '
    '[data-testid="chatMessageText"], [data-dm-message-focus-target]'
)
_COMPOSER = (
    '[data-testid="dmComposerTextInput"], [data-testid="chatComposerTextInput"], '
    '[data-testid="messageComposer"]'
    ', [data-testid="dm-composer-textarea"]'
)
_SEND = (
    '[data-testid="dmComposerSendButton"], [data-testid="chatComposerSendButton"], '
    '[data-testid="messageSendButton"], [data-testid="dm-composer-send-button"]'
)
_PIN_INPUT = (
    'input[type="password"][inputmode="numeric"], '
    'input[name="pin"], input[data-testid="pinInput"], '
    'input[autocomplete="one-time-code"][inputmode="numeric"], '
    'input[aria-label*="PIN" i], input[placeholder*="PIN" i], '
    'input[aria-label*="passcode" i]'
)
_PIN_SEGMENT = (
    'input[type="text"][inputmode="numeric"][maxlength="1"]'
    '[pattern="[0-9]*"][aria-label^="Digit "]'
)
_PASSCODE_PROMPT = re.compile(r"^Enter Passcode$", re.I)
_PASSCODE_ERROR = re.compile(r"^(?:Incorrect|Invalid|Wrong) passcode(?:\.?|\. Try again\.?|, try again\.?)$", re.I)
_EMPTY_INBOX = re.compile(
    r"^(?:No messages(?: yet)?|Your inbox is empty|Welcome to your inbox!|"
    r"No conversations(?: yet)?|No results(?: found)?)$", re.I
)
_EMPTY_CONVERSATION = re.compile(
    r"^(?:No messages(?: yet)?|Start a conversation|Send your first message)$", re.I
)

# The observed modern body contains authored text plus two timestamp layout
# decorations. Select its authored subtree rather than trimming time strings.
_MODERN_BODY_TEXT = r"""body => {
  const visible = node => !!node.getClientRects().length && !['hidden','collapse'].includes(getComputedStyle(node).visibility);
  const authored = [...body.querySelectorAll('span[dir="auto"].whitespace-pre-wrap')].filter(visible)
    .filter(node => !node.parentElement?.closest('span[dir="auto"].whitespace-pre-wrap'));
  if (authored.length === 1) return authored[0].innerText;
  if (authored.length > 1 || body.querySelector('[aria-hidden="true"].user-select-none, .absolute.bottom-0.inset-e-0 .text-subtext3')) return null;
  return body.innerText;
}"""

# A read-only message window is captured in one DOM turn. Virtualized row
# positions cannot shift between separate attribute/text driver round trips.
# Send verification retains its existing independently tested snapshot path.
_READ_MESSAGE_WINDOW = r"""(entries, args) => {
  const authoredText = __XUSE_BODY_TEXT__;
  const visible = node => !!node.getClientRects().length && !['hidden','collapse'].includes(getComputedStyle(node).visibility);
  const nodes = entries.slice(-args.maximum).filter(visible);
  const result = [];
  for (const entry of nodes) {
    const attr = name => entry.getAttribute(name);
    const textNodes = [...entry.querySelectorAll(args.textSelector)].filter(visible);
    let text = textNodes.length === 1 ? textNodes[0].innerText : entry.innerText.trim();
    let id = attr('data-message-id') || attr('data-id') || attr('id');
    const modern = attr('data-dm-message-row') !== null;
    if (modern) {
      id = attr('data-dm-message-row');
      if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(id || '') ||
          attr('data-testid') !== 'message-' + id ||
          entry.parentElement?.closest('[data-dm-item-key]')?.getAttribute('data-dm-item-key') !== id) id = null;
      if (id) {
        const body = [...entry.querySelectorAll('[data-dm-message-focus-target] [data-testid="message-text-'+id+'"]')].filter(visible);
        if (body.length !== 1 || body[0].closest('[data-dm-message-row]') !== entry) return {error:'unsupported_dom'};
        text = authoredText(body[0]);
        if (text === null) return {error:'unsupported_dom'};
      }
    }
    const direction = attr('data-direction'), own = attr('data-is-outgoing');
    const outgoing = direction === 'outgoing' || own === 'true' || attr('data-testid') === 'outgoingMessage' ||
      /^(?:You:|Message sent by you\b)/i.test(attr('aria-label') || '');
    const incoming = direction === 'incoming' || own === 'false';
    const status = attr('data-status') || attr('data-send-status');
    const pending = ['pending','sending','failed','error'].includes(status) ||
      [...entry.querySelectorAll('[data-testid="messageSending"], [data-testid="messageError"]')].some(visible);
    result.push({message_id:id, text, direction:outgoing && incoming ? 'unknown' : outgoing ? 'outgoing' : incoming ? 'incoming' : 'unknown', pending_or_failed:pending});
  }
  const counts = new Map();
  for (const row of result) if (row.message_id) counts.set(row.message_id,(counts.get(row.message_id)||0)+1);
  for (const row of result) if (row.message_id && (counts.get(row.message_id)!==1 || row.message_id.length>200 || /[\x00-\x20\x7f]/.test(row.message_id))) row.message_id=null;
  return {messages:result,dom_count:entries.length};
}""".replace("__XUSE_BODY_TEXT__", _MODERN_BODY_TEXT)

_READ_INBOX_ROWS = r"""(entries, args) => {
  const visible = node => !!node.getClientRects().length && !['hidden','collapse'].includes(getComputedStyle(node).visibility);
  const rows = [];
  for (const row of entries.slice(0,200).filter(visible)) {
    let href = row.getAttribute('href');
    if (href === null) {
      const links = [...row.querySelectorAll(args.links)].filter(visible);
      if (links.length === 1) href = links[0].getAttribute('href');
    }
    const flag = row.getAttribute('data-unread'), label = row.getAttribute('aria-label');
    const native = row.closest('[data-testid^="dm-conversation-item-"], [data-testid^="dm-message-request-item-"]');
    const nativeId = native?.getAttribute('data-testid') || null;
    const nativeRow = /^dm-(?:conversation-item|message-request-item)-[0-9]{1,30}:[0-9]{1,30}$/.test(nativeId || '');
    const nativeIcon = nativeRow && [...row.querySelectorAll('svg[data-icon="icon-circle-fill"][role="img"][aria-hidden="true"]')].some(icon =>
      visible(icon) && icon.classList.contains('text-chat-accent') && icon.classList.contains('shrink-0') &&
      icon.classList.contains('text-[8px]') && icon.querySelector('circle'));
    const marker = nativeIcon || ['Unread','Unread conversation'].includes(label) ||
      [...row.querySelectorAll(args.unread)].some(visible);
    const unread = flag === 'false' && marker ? null : ['true','false'].includes(flag) ? flag === 'true' : marker ? true : null;
    rows.push({href, visible_text:row.innerText.trim(), unread, native_row_id:nativeId,
      unread_source:nativeIcon?'visible_native_unread_indicator':marker?'visible_unread_marker':flag==='true'||flag==='false'?'explicit_row_attribute':'unknown'});
  }
  return {rows, count:entries.length};
}"""


# Observe visible DOM changes only. No UI clicks, endpoint calls, or DOM writes
# are injected. A trusted native Send click starts the observation; bare sent
# rows never establish authorship, and failed transitions remain uncertain.
_ARM_SEND_OBSERVER = r"""(scope, args) => {
  const authoredText = __XUSE_BODY_TEXT__;
  const visible = node => !!node.getClientRects().length && getComputedStyle(node).visibility !== 'hidden';
  const controls = selector => [...scope.querySelectorAll(selector)].filter(visible);
  const sendSelector = '[data-testid="dm-composer-send-button"]';
  const composerSelector = 'textarea[data-testid="dm-composer-textarea"]';
  if (controls(sendSelector).length !== 1 || controls(composerSelector).length !== 1) throw new Error('Unsupported composer');
  const old = new Set(args.ids);
  const state = {started:false, pending:new Set(), failed:new Set(), confirmation:null};
  let clickedSend = null, clickedComposer = null;
  const id = row => {
    const value = row.getAttribute('data-dm-message-row');
    return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value || '')
      && row.getAttribute('data-testid') === 'message-' + value
      && row.parentElement?.closest('[data-dm-item-key]')?.getAttribute('data-dm-item-key') === value ? value : null;
  };
  const candidate = row => {
    const value = id(row);
    if (!value || old.has(value) || !scope.contains(row)) return null;
    const rows = [...scope.querySelectorAll('[data-dm-message-row]')];
    if (rows.filter(node=>id(node)===value).length!==1) return null;
    if (args.anchor) {
      const positions = rows.map((node,index)=>id(node)===args.anchor?index:-1).filter(index=>index>=0);
      if (positions.length!==1 || rows.indexOf(row)<=positions[0]) return null;
    }
    return value;
  };
  const eligible = row => {
    const value = candidate(row);
    if (!value || !visible(row)) return null;
    const body=[...row.querySelectorAll('[data-dm-message-focus-target] [data-testid="message-text-'+value+'"]')].filter(visible);
    if (body.length!==1 || body[0].closest('[data-dm-message-row]')!==row || authoredText(body[0])!==args.text) return null;
    return value;
  };
  const scan = records => {
    if (!state.started) return;
    for (const record of records) {
      if (record.type!=='attributes' || record.attributeName!=='data-send-status') continue;
      const row = record.target.closest('[data-dm-message-row]');
      // Preserve a proven UUID's post-click status transition while its body
      // hydrates. Full visible identity/body checks still gate confirmation.
      const value = row && candidate(row);
      if (!value) continue;
      if (['pending','sending'].includes(record.oldValue)) state.pending.add(value);
      if (['failed','error'].includes(record.oldValue)) state.failed.add(value);
    }
    state.confirmation = null;
    for (const row of scope.querySelectorAll('[data-dm-message-row]')) {
      const value = candidate(row);
      if (!value) continue;
      const status = row.getAttribute('data-send-status');
      if (['pending','sending'].includes(status)) state.pending.add(value);
      if (['failed','error'].includes(status)) state.failed.add(value);
      if (state.pending.has(value) && !state.failed.has(value) && ['sent','delivered','read'].includes(status) && eligible(row)) state.confirmation = {message_id:value};
    }
  };
  const clicked = event => {
    if (!event.isTrusted) return;
    const sends = controls(sendSelector), composers = controls(composerSelector);
    if (state.started || !scope.isConnected || sends.length !== 1 || composers.length !== 1 || !event.composedPath().includes(sends[0])) return;
    // Exclude anything that appeared before the native click, even if it was
    // rendered after the initial history snapshot.
    observer.takeRecords();
    for (const row of scope.querySelectorAll('[data-dm-message-row]')) {
      const value=id(row); if (value) old.add(value);
    }
    clickedSend = sends[0]; clickedComposer = composers[0]; state.started=true;
  };
  // Scope delegation follows a remounted Send control but accepts only the
  // exact unique visible control in this identified conversation's click path.
  scope.addEventListener('click',clicked,{capture:true});
  const observer = new MutationObserver(scan);
  observer.observe(scope,{subtree:true,childList:true,characterData:true,attributes:true,
    attributeFilter:['data-send-status','data-dm-message-row','data-testid'],attributeOldValue:true});
  const composerEmpty = () => {
    const current = controls(composerSelector);
    return scope.isConnected && current.length === 1 ? current[0].value.trim() === '' : null;
  };
  window[args.key] = {state,read:()=>{scan([]);return composerEmpty()===true?state.confirmation:null;},
    diagnostics:()=>({observer_started:state.started,composer_empty:composerEmpty(),pending_count:state.pending.size,
      failed_count:state.failed.size,scope_connected:scope.isConnected,
      composer_connected:clickedComposer?.isConnected ?? null,send_control_connected:clickedSend?.isConnected ?? null}),
    dispose:()=>{observer.disconnect();scope.removeEventListener('click',clicked,true);}};
}""".replace("__XUSE_BODY_TEXT__", _MODERN_BODY_TEXT)


def conversation_url(value: str) -> tuple[str, str]:
    """Validate a conversation ID/URL before any browser navigation.

    Legacy numeric pairs may be supplied as IDs. Opaque chat IDs must come
    from an allowlisted /i/chat/<id> URL, retaining their route information.
    Encoded paths, credentials, query strings, and fragments are rejected.
    """
    if not isinstance(value, str) or not value or value != value.strip() or re.search(r"[\x00-\x20\x7f]", value):
        raise ValueError("A valid X conversation ID or URL is required.")
    if _NUMERIC_CONVERSATION.fullmatch(value):
        return f"https://x.com/messages/{value}", value
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.netloc.lower() != parsed.hostname
        or parsed.query
        or parsed.fragment
        or "%" in parsed.path
        or "\\" in value
    ):
        raise ValueError("A valid X conversation ID or URL is required.")
    legacy = re.fullmatch(r"/messages/([0-9]{1,30}-[0-9]{1,30})/?", parsed.path)
    chat = re.fullmatch(r"/i/chat/([A-Za-z0-9_-]{1,200})/?", parsed.path)
    request = re.fullmatch(r"/i/chat/requests/(?:other/)?([0-9]{1,30}-[0-9]{1,30})/?", parsed.path)
    match = legacy or chat or request
    if match is None or match.group(1).lower() in {"new", "compose", "requests", "settings", "pin"}:
        raise ValueError("A valid X conversation ID or URL is required.")
    return f"https://x.com{parsed.path.rstrip('/')}", match.group(1)


def _limit(value: int, maximum: int = 200) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"limit must be an integer between 1 and {maximum}.")
    return value


def _inbox_options(inbox_filter: str, unread_first: bool, folder: str = "inbox") -> None:
    if not isinstance(inbox_filter, str) or inbox_filter not in {"all", "unread", "read"}:
        raise ValueError("inbox_filter must be all, unread, or read.")
    if not isinstance(unread_first, bool):
        raise ValueError("unread_first must be a boolean.")
    if not isinstance(folder, str) or folder not in _FOLDER_PATHS:
        raise ValueError("folder must be inbox, requests, or other.")


def _inbox_row_reference(href: str | None, native_row_id: str | None, folder: str) -> dict[str, Any] | None:
    """Retain observed request routes separately from openable conversations."""
    reference = urljoin("https://x.com", href or "")
    if folder == "inbox":
        try:
            url, identifier = conversation_url(reference)
        except ValueError:
            return None
        result = {"conversation_id": identifier, "url": url}
    else:
        parsed = urlsplit(reference)
        prefix = _FOLDER_PATHS[folder]
        if (parsed.scheme != "https" or parsed.hostname not in _HOSTS or parsed.netloc.lower() != parsed.hostname
                or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment
                or "%" in parsed.path or "\\" in reference
                or not re.fullmatch(re.escape(prefix) + r"/[0-9]{1,30}-[0-9]{1,30}/?", parsed.path)):
            return None
        url, identifier = conversation_url(reference)
        result = {"conversation_id": identifier, "url": url,
                  "conversation_supported": True, "reference_type": "message_request"}
    if isinstance(native_row_id, str) and _NATIVE_ROW_ID.fullmatch(native_row_id):
        result["native_row_id"] = native_row_id
    return result


def _message_cursor(value: str | None) -> None:
    if value is not None and (not isinstance(value, str) or not value or len(value) > 200
                              or value != value.strip() or re.search(r"[\x00-\x20\x7f]", value)):
        raise ValueError("before_message_id must be a nonempty message ID of at most 200 characters.")


def _observed_at() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_recipient(value: str) -> str:
    """Canonical handle or validated conversation reference for drafts."""
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("recipient must be a conversation ID/URL or @handle.")
    if _NUMERIC_CONVERSATION.fullmatch(value):
        return value
    handle = _HANDLE.fullmatch(value)
    if handle:
        return f"@{handle.group(1).lower()}"
    profile = _profile_handle(value)
    if profile is not None:
        return f"@{profile}"
    return conversation_url(value)[0]


def _profile_handle(href: str | None) -> str | None:
    """Recognize only direct allowlisted profile links, without URL tricks."""
    if not isinstance(href, str) or "\\" in href or re.search(r"[\x00-\x20\x7f]", href):
        return None
    parsed = urlsplit(urljoin("https://x.com", href))
    match = re.fullmatch(r"/([A-Za-z0-9_]{1,15})/?", parsed.path)
    if (
        parsed.scheme != "https" or parsed.hostname not in _HOSTS
        or parsed.netloc.lower() != parsed.hostname or parsed.username is not None
        or parsed.password is not None or parsed.query or parsed.fragment or not match
    ):
        return None
    handle = match.group(1).lower()
    return handle if handle not in _NON_PROFILE_ROUTES else None


def _safe_dom_errors(reason: str):
    """Suppress Playwright call logs, which can include private message text."""
    def decorate(method):
        @wraps(method)
        async def guarded(*args, **kwargs):
            try:
                return await method(*args, **kwargs)
            except (BrowserActionError, BrowserBlocked, ValueError):
                raise
            except Exception:
                raise BrowserActionError(reason) from None
        return guarded
    return decorate


class MessagingMixin:
    """Requires an async Playwright page, navigate(), and check_blocked()."""

    messaging_timeout_ms = 60_000
    messaging_confirmation_timeout_ms = 10_000

    async def _visible(self, locator: Any) -> list[Any]:
        return [locator.nth(i) for i in range(await locator.count()) if await locator.nth(i).is_visible()]

    async def _any_visible(self, locator: Any, maximum: int = 200) -> bool:
        for i in range(min(await locator.count(), maximum)):
            if await locator.nth(i).is_visible():
                return True
        return False

    async def _one(self, locator: Any, reason: str = "unsupported_dom") -> Any:
        visible = await self._visible(locator)
        if len(visible) != 1:
            raise BrowserActionError(reason)
        return visible[0]

    async def _wait_one(self, locator: Any, reason: str = "unsupported_dom") -> Any:
        deadline = asyncio.get_running_loop().time() + self.messaging_timeout_ms / 1000
        while True:
            await self._messaging_blocked()
            visible = await self._visible(locator)
            if len(visible) == 1:
                return visible[0]
            if len(visible) > 1 or asyncio.get_running_loop().time() >= deadline:
                raise BrowserActionError(reason)
            await asyncio.sleep(0.1)

    async def _messaging_blocked(self) -> None:
        await self.check_blocked()
        if await self._visible(self.page.locator(_PIN_INPUT)):
            raise BrowserBlocked("pin_required")

    async def _wait_messaging_view(self, conversation: bool = False) -> None:
        deadline = asyncio.get_running_loop().time() + self.messaging_timeout_ms / 1000
        while True:
            await self._messaging_blocked()
            selectors = f"{_ENTRIES}, {_COMPOSER}, {_REQUEST_PROMPT}" if conversation else f"{_CONVERSATION_ROWS}, {_REQUEST_ROWS}, {_CONVERSATION_LINKS}"
            if await self._any_visible(self.page.locator(selectors)):
                return
            empty = _EMPTY_CONVERSATION if conversation else _EMPTY_INBOX
            if await self._visible(self.page.get_by_text(empty, exact=True)):
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise BrowserActionError("unsupported_dom")
            await asyncio.sleep(0.1)

    async def _open_inbox(self) -> None:
        current = urlsplit(self.page.url)
        ready_route = current.scheme == "https" and current.netloc == "x.com" and current.path.rstrip("/") in {"/messages", *_FOLDER_PATHS.values()}
        if not ready_route:
            try:
                conversation_url(self.page.url)
                ready_route = bool(await self._visible(self.page.locator(_INBOX)))
            except ValueError:
                pass
        # Reusing the live SPA keeps the owner's unlocked encryption state.
        # A page.goto after unlock can replace its in-memory key/router state.
        if not ready_route:
            await self.navigate("https://x.com/i/chat")
        await self._wait_messaging_view()

    def _inbox_path(self) -> str:
        parsed = urlsplit(self.page.url)
        return parsed.path.rstrip("/") if parsed.scheme == "https" and parsed.netloc == "x.com" else ""

    def _native_main_inbox_route(self) -> bool:
        """A selected native conversation can retain the main inbox sidebar."""
        path = self._inbox_path()
        if path == "/i/chat":
            return True
        if not path.startswith("/i/chat/") or path.startswith("/i/chat/requests/"):
            return False
        try:
            conversation_url(self.page.url)
        except ValueError:
            return False
        return True

    def _request_folder(self) -> str | None:
        """Classify a validated request route, including a selected request."""
        path = self._inbox_path()
        for folder in ("other", "requests"):
            base = _FOLDER_PATHS[folder]
            if path == base:
                return folder
            if path.startswith(base + "/"):
                try:
                    conversation_url(self.page.url)
                except ValueError:
                    return None
                return folder
        return None

    async def _wait_inbox_condition(self, predicate: Any) -> None:
        deadline = asyncio.get_running_loop().time() + self.messaging_timeout_ms / 1000
        while True:
            await self._messaging_blocked()
            if await predicate():
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise BrowserActionError("unsupported_dom")
            await asyncio.sleep(0.1)

    async def _native_inbox_menu(self, value: str) -> bool:
        trigger_selector = '[data-testid="dm-inbox-dropdown-trigger"]'
        triggers = await self._visible(self.page.locator(trigger_selector))
        if not triggers:
            return False
        if len(triggers) != 1:
            raise BrowserActionError("unsupported_dom")
        menu_selector = '[data-testid="dm-inbox-dropdown-content"][role="menu"]'
        if not await self._visible(self.page.locator(menu_selector)):
            await triggers[0].click()
        menu = await self._wait_one(self.page.locator(menu_selector))
        item_selector = f'[data-testid="dm-inbox-dropdown-{value}"][role="menuitemradio"]'
        item = await self._one(menu.locator(item_selector))
        checked = await item.get_attribute("aria-checked")
        if checked not in {"true", "false"}:
            raise BrowserActionError("unsupported_dom")
        if checked == "false" or value == "requests":
            await item.click()
            if value == "requests":
                async def arrived():
                    return self._inbox_path() == _FOLDER_PATHS["requests"] and bool(await self._visible(self.page.locator(_REQUEST_ROOT)))
                await self._wait_inbox_condition(arrived)
                return True
            expected = "Unread" if value == "unread" else "All"
            async def selected():
                current = await self._visible(self.page.locator(trigger_selector))
                return (self._native_main_inbox_route() and len(current) == 1
                        and (await current[0].inner_text()).strip() == expected
                        and not await self._visible(self.page.locator(menu_selector)))
            await self._wait_inbox_condition(selected)
            # Confirm native selected state after the transition, rather than
            # trusting the requested click or a caption alone.
            await (await self._one(self.page.locator(trigger_selector))).click()
            menu = await self._wait_one(self.page.locator(menu_selector))
            item = await self._one(menu.locator(item_selector))
        selected_items = await self._visible(menu.locator('[role="menuitemradio"][aria-checked="true"]'))
        if len(selected_items) != 1 or await item.get_attribute("aria-checked") != "true":
            raise BrowserActionError("unsupported_dom")
        await (await self._one(self.page.locator(trigger_selector))).click()
        async def closed():
            return not await self._visible(self.page.locator(menu_selector))
        await self._wait_inbox_condition(closed)
        return True

    async def _dismiss_request_info(self) -> None:
        dialogs = await self._visible(self.page.locator('[data-testid="dm-message-requests-info-sheet"][role="dialog"]'))
        if not dialogs:
            return
        if len(dialogs) != 1:
            raise BrowserActionError("unsupported_dom")
        dialog = dialogs[0]
        await self._one(dialog.get_by_text("Message Requests", exact=True))
        # Both apostrophe forms have been observed in the same informational sheet.
        await self._one(dialog.get_by_text(re.compile(
            "^" + re.escape(_REQUEST_INFO).replace("’", "['’]") + "$"
        ), exact=True))
        await (await self._one(dialog.get_by_role("button", name="Dismiss", exact=True))).click()
        async def dismissed():
            return not await self._visible(self.page.locator('[data-testid="dm-message-requests-info-sheet"][role="dialog"]'))
        await self._wait_inbox_condition(dismissed)

    async def _prepare_inbox_view(self, folder: str, inbox_filter: str) -> tuple[Any, str, str | None, str]:
        await self._open_inbox()
        desired_path = _FOLDER_PATHS[folder]
        while self._request_folder() is not None and self._request_folder() != folder:
            if folder == "other" and self._request_folder() == "requests":
                break
            previous = self._request_folder()
            await self._dismiss_request_info()
            root = await self._one(self.page.locator(_REQUEST_ROOT))
            await (await self._one(root.locator('[data-testid="dm-message-requests-back"]'))).click()
            async def moved():
                expected = _FOLDER_PATHS["requests"] if previous == "other" else _FOLDER_PATHS["inbox"]
                return self._inbox_path() == expected
            await self._wait_inbox_condition(moved)
        if folder != "inbox":
            if self._request_folder() is None:
                if not await self._native_inbox_menu("requests"):
                    raise BrowserActionError("unsupported_dom")
            await self._dismiss_request_info()
            if folder == "other" and self._request_folder() == "requests":
                root = await self._one(self.page.locator(_REQUEST_ROOT))
                await (await self._one(root.locator('[data-testid="dm-message-requests-other-button"]'))).click()
                async def other_arrived():
                    return self._inbox_path() == desired_path
                await self._wait_inbox_condition(other_arrived)
            if self._request_folder() != folder:
                raise BrowserActionError("conversation_mismatch")
            root = await self._wait_one(self.page.locator(_REQUEST_ROOT))
            scope = await self._wait_one(root.locator(_REQUEST_SCROLLER))
            return scope, _REQUEST_ROWS, None, self.page.url
        native_filter = "unread" if inbox_filter == "unread" else "all"
        native = await self._native_inbox_menu(native_filter)
        await self._wait_messaging_view()
        roots = await self._visible(self.page.locator(_INBOX))
        if len(roots) > 1:
            raise BrowserActionError("unsupported_dom")
        return roots[0] if roots else self.page, _CONVERSATION_ROWS, native_filter if native else None, self.page.url

    async def _verify_inbox_view(self, route: str, folder: str, native_filter: str | None) -> None:
        await self._messaging_blocked()
        if self.page.url != route:
            raise BrowserActionError("conversation_mismatch")
        if folder != "inbox":
            if self._request_folder() != folder:
                raise BrowserActionError("conversation_mismatch")
            root = await self._one(self.page.locator(_REQUEST_ROOT))
            await self._one(root.locator(_REQUEST_SCROLLER))
        elif native_filter is not None:
            trigger = await self._one(self.page.locator('[data-testid="dm-inbox-dropdown-trigger"]'))
            if not self._native_main_inbox_route() or (await trigger.inner_text()).strip() != ("Unread" if native_filter == "unread" else "All"):
                raise BrowserActionError("conversation_mismatch")
        if self.page.url != route:
            raise BrowserActionError("conversation_mismatch")

    async def _inbox_snapshot(self, scope: Any = None, *, row_selector: str = _CONVERSATION_ROWS,
                              folder: str = "inbox") -> tuple[list[dict[str, Any]], bool]:
        if scope is None:
            roots = await self._visible(self.page.locator(_INBOX))
            if len(roots) > 1:
                raise BrowserActionError("unsupported_dom")
            scope = roots[0] if len(roots) == 1 else self.page
        locator = scope.locator(row_selector)
        if hasattr(locator, "evaluate_all"):
            args = {"links": _CONVERSATION_LINKS, "unread": _UNREAD_MARKER}
            snapshot = await locator.evaluate_all(_READ_INBOX_ROWS, args)
            if not snapshot["rows"] and folder == "inbox":
                snapshot = await scope.locator(_CONVERSATION_LINKS).evaluate_all(_READ_INBOX_ROWS, args)
            results = []
            seen: dict[str, dict[str, Any]] = {}
            for row in snapshot["rows"]:
                reference = _inbox_row_reference(row["href"], row["native_row_id"], folder)
                if reference is None:
                    continue
                url = reference["url"]
                if url in seen:
                    if seen[url]["unread"] != row["unread"]:
                        seen[url]["unread"] = None
                    continue
                parsed = {**reference, "visible_text": row["visible_text"], "unread": row["unread"],
                          "unread_source": row["unread_source"]}
                seen[url] = parsed
                results.append(parsed)
            if not results and not await self._visible(scope.get_by_text(_EMPTY_INBOX, exact=True)):
                raise BrowserActionError("unsupported_dom")
            return results, snapshot["count"] > 200
        count = await locator.count()
        rows = [locator.nth(i) for i in range(min(count, 200)) if await locator.nth(i).is_visible()]
        # Some versions expose ordinary links instead of a conversation testid.
        if not rows and folder == "inbox":
            locator = scope.locator(_CONVERSATION_LINKS)
            count = await locator.count()
            rows = [locator.nth(i) for i in range(min(count, 200)) if await locator.nth(i).is_visible()]
        results: list[dict[str, Any]] = []
        seen: dict[str, dict[str, Any]] = {}
        for row in rows:
            href = await row.get_attribute("href")
            if href is None:
                links = await self._visible(row.locator(_CONVERSATION_LINKS))
                if len(links) == 1:
                    href = await links[0].get_attribute("href")
            reference = _inbox_row_reference(href, await row.get_attribute("data-testid"), folder)
            if reference is None:
                continue
            url = reference["url"]
            text = (await row.inner_text()).strip()
            unread_attr = await row.get_attribute("data-unread")
            label = await row.get_attribute("aria-label") or ""
            unread_marker = label in {"Unread", "Unread conversation"} or bool(await self._visible(row.locator(_UNREAD_MARKER)))
            unread = unread_attr == "true" if unread_attr in {"true", "false"} else True if unread_marker else None
            if unread_attr == "false" and unread_marker:
                unread = None
            if url in seen:
                if seen[url]["unread"] != unread:
                    seen[url]["unread"] = None
                continue
            parsed = {**reference, "visible_text": text, "unread": unread}
            seen[url] = parsed
            results.append(parsed)
        if not results and not await self._visible(scope.get_by_text(_EMPTY_INBOX, exact=True)):
            raise BrowserActionError("unsupported_dom")
        return results, count > 200

    @staticmethod
    def _select_inbox(rows: list[dict[str, Any]], limit: int, inbox_filter: str,
                      unread_first: bool, scan_truncated: bool) -> dict[str, Any]:
        selected = [row for row in rows if inbox_filter == "all"
                    or row["unread"] is (inbox_filter == "unread")]
        if unread_first:
            # Preserve DOM order within unread and all remaining states.
            selected.sort(key=lambda row: row["unread"] is not True)
        return {
            "conversations": selected[:limit], "count": min(len(selected), limit), "limit": limit,
            "inbox_filter": inbox_filter, "ordering": "unread_first_then_dom" if unread_first else "dom",
            "partial": True, "pagination": "visible_only", "source": "browser_dom",
            "coverage": "visible_only", "observed_at": _observed_at(),
            "scanned_count": len(rows), "scan_limit": 200, "scan_truncated": scan_truncated,
            "matching_count": len(selected),
            "truncated": scan_truncated or len(selected) > limit,
            "unread_unknown_count": sum(row["unread"] is None for row in rows),
            "empty": not selected, "empty_scope": "matching_visible_conversation_summaries",
        }

    @_safe_dom_errors("read_failed")
    async def get_inbox(self, limit: int = 20, *, inbox_filter: str = "all",
                        unread_first: bool = False, folder: str = "inbox") -> dict[str, Any]:
        """Return currently visible conversation summaries, without scrolling."""
        limit = _limit(limit)
        _inbox_options(inbox_filter, unread_first, folder)
        scope, selector, native_filter, route = await self._prepare_inbox_view(folder, inbox_filter)
        await self._verify_inbox_view(route, folder, native_filter)
        rows, scan_truncated = await self._inbox_snapshot(scope, row_selector=selector, folder=folder)
        if native_filter == "unread":
            for row in rows:
                row["unread"] = None if row["unread"] is False else True
                row["unread_source"] = "native_unread_selection" if row["unread"] is True else "conflicting_evidence"
        await self._verify_inbox_view(route, folder, native_filter)
        return {**self._select_inbox(rows, limit, inbox_filter, unread_first, scan_truncated),
                "folder": folder, "folder_provenance": "observed_route_and_scoped_dom" if folder != "inbox" else "visible_inbox",
                "native_filter": native_filter,
                "filter_application": "native_unread_selection" if native_filter == "unread" else "visible_row_evidence"}

    @_safe_dom_errors("read_failed")
    async def search_conversations(self, query: str, limit: int = 20, *, inbox_filter: str = "all",
                                   unread_first: bool = False, folder: str = "inbox") -> dict[str, Any]:
        """Match rendered conversation summaries; this is not history search."""
        limit = _limit(limit)
        _inbox_options(inbox_filter, unread_first, folder)
        if not isinstance(query, str) or not query.strip() or len(query) > 200:
            raise ValueError("query must contain between 1 and 200 characters.")
        scope, selector, native_filter, route = await self._prepare_inbox_view(folder, inbox_filter)
        await self._verify_inbox_view(route, folder, native_filter)
        rendered, scan_truncated = await self._inbox_snapshot(scope, row_selector=selector, folder=folder)
        if native_filter == "unread":
            for row in rendered:
                row["unread"] = None if row["unread"] is False else True
                row["unread_source"] = "native_unread_selection" if row["unread"] is True else "conflicting_evidence"
        rows = [row for row in rendered if query.strip().casefold() in row["visible_text"].casefold()]
        result = self._select_inbox(rows, limit, inbox_filter, unread_first, scan_truncated)
        await self._verify_inbox_view(route, folder, native_filter)
        return {**result, "query": query, "search_scope": "visible_conversation_summaries",
                "scanned_count": len(rendered), "query_matching_count": len(rows), "folder": folder,
                "folder_provenance": "observed_route_and_scoped_dom" if folder != "inbox" else "visible_inbox",
                "native_filter": native_filter,
                "filter_application": "native_unread_selection" if native_filter == "unread" else "visible_row_evidence"}

    async def _conversation_scope(self) -> Any:
        roots = await self._visible(self.page.locator(_CONVERSATION))
        if len(roots) == 1:
            return roots[0]
        if len(roots) > 1:
            raise BrowserActionError("unsupported_dom")
        # Legacy DmActivityViewport contains messages but excludes the composer.
        # Its enclosing main is the common scope for both; ambiguity fails closed.
        root = await self._one(self.page.locator('main, [role="main"]'))
        if not await self._visible(root.locator(f"{_ENTRIES}, {_COMPOSER}, {_HEADER}")):
            raise BrowserActionError("unsupported_dom")
        return root

    async def _conversation_transition_snapshot(self) -> dict[str, Any] | None:
        """Capture the visible header and history together before/after a row click."""
        if not await self._visible(self.page.locator(f"{_CONVERSATION}, {_HEADER}, {_ENTRIES}")):
            return None
        scope = await self._conversation_scope()
        if hasattr(scope.locator(_ENTRIES), "evaluate_all"):
            return await scope.evaluate(r"""(scope, args) => {
              const authoredText = __XUSE_BODY_TEXT__;
              const visible = node => !!node.getClientRects().length && !['hidden','collapse'].includes(getComputedStyle(node).visibility);
              const nodes = selector => [...scope.querySelectorAll(selector)].filter(visible);
              const headers = nodes(args.header).slice(0,2).map(node => [node.innerText.trim(),
                [...node.querySelectorAll('a[href]')].filter(visible).map(link=>link.getAttribute('href'))]);
              const rows = nodes(args.entries).slice(-200).map(node => {
                const id = node.getAttribute('data-dm-message-row') || node.getAttribute('data-message-id') || node.getAttribute('data-id') || node.id;
                const body = [...node.querySelectorAll(args.text)].filter(visible);
                let text = body.length === 1 ? body[0].innerText : node.innerText.trim();
                if (node.hasAttribute('data-dm-message-row')) {
                  const modern = [...node.querySelectorAll('[data-testid^="message-text-"]')].filter(visible);
                  text = modern.length === 1 ? authoredText(modern[0]) : null;
                }
                return {key: id ? 'id:' + id : 'text:' + text, text};
              });
              const empty = !rows.length && [scope, ...scope.querySelectorAll('*')].filter(visible)
                .some(node=>/^(?:No messages(?: yet)?|Start a conversation|Send your first message)$/.test((node.innerText || '').trim()));
              return {headers, rows, empty, request: nodes(args.request).length === 1};
            }""".replace("__XUSE_BODY_TEXT__", _MODERN_BODY_TEXT), {
                "header": _HEADER, "entries": _ENTRIES, "text": _ENTRY_TEXT,
                "request": _REQUEST_PROMPT,
            })
        # Locator-only offline fixtures do not expose a browser DOM evaluator.
        headers = []
        for header in (await self._visible(scope.locator(_HEADER)))[:2]:
            links = await self._visible(header.locator('a[href]'))
            headers.append([(await header.inner_text()).strip(),
                            [await link.get_attribute("href") for link in links]])
        messages = await self._message_snapshot(scope, maximum=200)
        return {"headers": headers, "rows": [
            {"key": "id:" + row["message_id"] if row["message_id"] else "text:" + row["text"],
             "text": row["text"]} for row in messages],
            "empty": bool(await self._visible(scope.get_by_text(_EMPTY_CONVERSATION, exact=True))),
            "request": len(await self._visible(scope.locator(_REQUEST_PROMPT))) == 1}

    async def _wait_conversation_transition(self, url: str, previous: dict[str, Any] | None,
                                            deadline: float) -> None:
        """URL changes alone cannot associate an existing panel with a new chat."""
        old_keys = {row["key"] for row in previous["rows"]} if previous else set()
        old_profiles = {_profile_handle(href) for header in previous["headers"] for href in header[1]} if previous else set()
        old_profiles.discard(None)
        while True:
            await self._messaging_blocked()
            if self.page.url.rstrip("/") == url:
                current = await self._conversation_transition_snapshot()
                if current is not None:
                    profiles = {_profile_handle(href) for header in current["headers"] for href in header[1]}
                    profiles.discard(None)
                    header_changed = True
                    if old_profiles:
                        header_changed = bool(profiles) and profiles != old_profiles
                    elif previous and previous["headers"]:
                        header_changed = bool(current["headers"]) and current["headers"][0][0] != previous["headers"][0][0]
                    header_ready = len(current["headers"]) <= 1 and (
                        not previous or not previous["headers"] or (
                            len(current["headers"]) == 1 and header_changed))
                    content_ready = (bool(current["rows"]) and all(
                        row["text"] is not None and row["text"].strip() and row["key"] not in old_keys
                        for row in current["rows"])) or (
                            not current["rows"] and (current["empty"] or current["request"]))
                    changed = not previous or current != previous
                    # Recheck after the awaited snapshot; the route can move
                    # while the driver is reading the panel.
                    if header_ready and content_ready and changed and self.page.url.rstrip("/") == url:
                        return
            if asyncio.get_running_loop().time() >= deadline:
                raise BrowserActionError("conversation_mismatch")
            await asyncio.sleep(0.1)

    async def _open_conversation(self, value: str) -> tuple[Any, str, str]:
        url, conversation_id = conversation_url(value)
        numeric_id = bool(_NUMERIC_CONVERSATION.fullmatch(value))
        selected_url = None
        if numeric_id:
            try:
                current_url, current_id = conversation_url(self.page.url)
            except ValueError:
                pass
            else:
                if current_id == conversation_id and await self._visible(self.page.locator(_CONVERSATION)):
                    selected_url = current_url
        matching_links = []
        exact_links = []
        for link in await self._visible(self.page.locator(_CONVERSATION_LINKS)):
            href = await link.get_attribute("href")
            try:
                target, target_id = conversation_url(urljoin("https://x.com", href or ""))
            except ValueError:
                continue
            if target == url or (numeric_id and target_id == conversation_id):
                matching_links.append((target, link))
        if numeric_id:
            # Numeric pairs appear in both UIs. Bind an ID returned by the
            # inbox to its observed native route rather than translating it
            # back to /messages and replacing the unlocked SPA document.
            native = [item for item in matching_links if urlsplit(item[0]).path.startswith("/i/chat/")]
            if selected_url is not None:
                url = selected_url
            elif native:
                url = native[0][0]
            elif not matching_links:
                path = urlsplit(self.page.url).path
                if path == "/i/chat" or path.startswith("/i/chat/"):
                    raise BrowserActionError("conversation_mismatch")
                if path != "/messages" and not path.startswith("/messages/"):
                    await self._open_inbox()
                    return await self._open_conversation(value)
        exact_links = [link for target, link in matching_links if target == url]
        if self.page.url.rstrip("/") == url and await self._visible(self.page.locator(_CONVERSATION)):
            await self._messaging_blocked()
            pending = getattr(self, "_pending_conversation_transition", None)
            if pending and pending[0] == url:
                deadline = asyncio.get_running_loop().time() + self.messaging_timeout_ms / 1000
                await self._wait_conversation_transition(url, pending[1], deadline)
                self._pending_conversation_transition = None
        elif len(exact_links) > 1:
            raise BrowserActionError("unsupported_dom")
        elif exact_links:
            await self._messaging_blocked()
            previous_url = self.page.url
            previous = await self._conversation_transition_snapshot()
            if self.page.url != previous_url:
                raise BrowserActionError("conversation_mismatch")
            if previous and len(previous["headers"]) > 1:
                raise BrowserActionError("unsupported_dom")
            try:
                _, previous_id = conversation_url(previous_url)
            except ValueError:
                previous_id = None
            if previous_id is not None and previous_id != conversation_id and previous is None:
                # A selected but hidden/unmounted detail gives no baseline
                # against which to reject its stale reappearance after click.
                raise BrowserActionError("conversation_mismatch")
            if previous_id == conversation_id:
                # Accepted requests can keep their request route while the
                # inbox offers the main route for the very same conversation.
                pending = getattr(self, "_pending_conversation_transition", None)
                previous = pending[1] if pending else None
            deadline = asyncio.get_running_loop().time() + self.messaging_timeout_ms / 1000
            self._pending_conversation_transition = (url, previous)
            await exact_links[0].click()
            await self._wait_conversation_transition(url, previous, deadline)
            self._pending_conversation_transition = None
        else:
            await self.navigate(url)
        await self._wait_messaging_view(conversation=True)
        try:
            actual_url, actual_id = conversation_url(self.page.url)
        except ValueError:
            raise BrowserActionError("conversation_mismatch") from None
        if actual_id != conversation_id:
            raise BrowserActionError("conversation_mismatch")
        return await self._conversation_scope(), actual_url, actual_id

    async def _message_snapshot(self, scope: Any, maximum: int | None = None) -> list[dict[str, Any]]:
        locator = scope.locator(_ENTRIES)
        count = await locator.count()
        start = max(0, count - maximum) if maximum is not None else 0
        entries = [locator.nth(i) for i in range(start, count) if await locator.nth(i).is_visible()]
        result: list[dict[str, Any]] = []
        for entry in entries:
            text_nodes = await self._visible(entry.locator(_ENTRY_TEXT))
            text = await text_nodes[0].inner_text() if len(text_nodes) == 1 else (await entry.inner_text()).strip()
            message_id = (
                await entry.get_attribute("data-message-id")
                or await entry.get_attribute("data-id")
                or await entry.get_attribute("id")
            )
            direction = await entry.get_attribute("data-direction")
            own = await entry.get_attribute("data-is-outgoing")
            testid = await entry.get_attribute("data-testid")
            label = await entry.get_attribute("aria-label") or ""
            outgoing = direction == "outgoing" or own == "true" or testid == "outgoingMessage" or bool(re.match(r"^(?:You:|Message sent by you\b)", label, re.I))
            incoming = direction == "incoming" or own == "false"
            resolved_direction = "unknown" if outgoing and incoming else "outgoing" if outgoing else "incoming" if incoming else "unknown"
            modern_row = await entry.get_attribute("data-dm-message-row") is not None
            if modern_row:
                message_id = await self._modern_row_id(entry)
                if message_id:
                    # The focus wrapper also contains the timestamp. Read only
                    # the unique visible body tied to this verified row UUID.
                    # A message can have another focus target for a preview.
                    # Bind the unique authored body to this verified row UUID.
                    body = await self._one(entry.locator(
                        f'[data-dm-message-focus-target] [data-testid="message-text-{message_id}"]'
                    ))
                    owner = body.locator('xpath=ancestor::*[@data-dm-message-row][1]')
                    if await owner.get_attribute("data-dm-message-row") != message_id:
                        raise BrowserActionError("unsupported_dom")
                    text = await body.evaluate(_MODERN_BODY_TEXT)
                    if text is None:
                        raise BrowserActionError("unsupported_dom")
            status = await entry.get_attribute("data-status") or await entry.get_attribute("data-send-status")
            pending = status in {"pending", "sending", "failed", "error"} or bool(await self._visible(entry.locator('[data-testid="messageSending"], [data-testid="messageError"]')))
            result.append({
                "message_id": message_id, "text": text,
                "direction": resolved_direction,
                "pending_or_failed": pending,
            })
        ids = Counter(row["message_id"] for row in result if row["message_id"])
        for row in result:
            identifier = row["message_id"]
            if identifier and (ids[identifier] != 1 or len(identifier) > 200
                               or re.search(r"[\x00-\x20\x7f]", identifier)):
                row["message_id"] = None
        return result

    async def _modern_row_id(self, entry: Any) -> str | None:
        """Use only the observed matching UUID row, testid, and virtualizer key."""
        identifier = await entry.get_attribute("data-dm-message-row")
        if not identifier or not _MESSAGE_UUID.fullmatch(identifier):
            return None
        if await entry.get_attribute("data-testid") != f"message-{identifier}":
            return None
        parent = entry.locator('xpath=ancestor::*[@data-dm-item-key][1]')
        if await parent.count() != 1 or await parent.get_attribute("data-dm-item-key") != identifier:
            return None
        return identifier

    async def _read_message_window(self, scope: Any) -> tuple[list[dict[str, Any]], int]:
        locator = scope.locator(_ENTRIES)
        if hasattr(locator, "evaluate_all"):
            snapshot = await locator.evaluate_all(_READ_MESSAGE_WINDOW, {
                "maximum": 200, "textSelector": _ENTRY_TEXT,
            })
            if snapshot.get("error"):
                raise BrowserActionError("unsupported_dom")
            return snapshot["messages"], snapshot["dom_count"]
        # The small locator-only seam used by offline unit fixtures.
        return await self._message_snapshot(scope, maximum=200), await locator.count()

    @_safe_dom_errors("read_failed")
    async def get_conversation(self, conversation_id: str, limit: int = 50, *,
                               before_message_id: str | None = None) -> dict[str, Any]:
        """Read a bounded window of rendered messages, preserving DOM order.

        A cursor references the current visible window only; no scrolling or
        complete-history claim. Opening a conversation may clear X unread state.
        """
        limit = _limit(limit)
        _message_cursor(before_message_id)
        scope, url, resolved_id = await self._open_conversation(conversation_id)
        deadline = asyncio.get_running_loop().time() + self.messaging_timeout_ms / 1000
        while True:
            await self._messaging_blocked()
            messages, dom_count = await self._read_message_window(scope)
            if messages or await self._visible(scope.get_by_text(_EMPTY_CONVERSATION, exact=True)):
                break
            if asyncio.get_running_loop().time() >= deadline:
                raise BrowserActionError("unsupported_dom")
            await asyncio.sleep(0.1)
        actual_url, actual_id = conversation_url(self.page.url)
        if actual_url != url or actual_id != resolved_id:
            raise BrowserActionError("conversation_mismatch")
        snapshot_count = len(messages)
        if before_message_id is not None:
            positions = [i for i, row in enumerate(messages) if row["message_id"] == before_message_id]
            if len(positions) != 1:
                raise ValueError("before_message_id is absent or ambiguous in the current visible window.")
            messages = messages[:positions[0]]
        selected = messages[-limit:]
        cursor = selected[0]["message_id"] if selected and len(messages) > limit else None
        try:
            participants = sorted(await self._participant_handles(scope))
        except BrowserActionError as exc:
            if exc.reason != "recipient_unverified":
                raise
            participants = []
        reply_state = await self._conversation_reply_state(scope)
        if self.page.url.rstrip("/") != url:
            raise BrowserActionError("conversation_mismatch")
        return {
            "conversation_id": resolved_id, "url": url, "messages": selected,
            "count": len(selected), "limit": limit,
            "partial": True, "pagination": "visible_only", "source": "browser_dom",
            "history_coverage": "visible_only", "ordering": "dom", "observed_at": _observed_at(),
            "snapshot_count": snapshot_count, "scan_limit": 200, "scan_truncated": dom_count > 200,
            "truncated": dom_count > 200 or len(messages) > limit,
            "before_message_id": before_message_id, "next_before_message_id": cursor,
            "message_id_scope": "bounded_visible_snapshot",
            "participants": participants, "participants_verified": bool(participants),
            "missing_message_id_count": sum(row["message_id"] is None for row in selected),
            "unknown_direction_count": sum(row["direction"] == "unknown" for row in selected),
            "empty": not selected, "empty_scope": "current_visible_window",
            **reply_state,
        }

    async def _conversation_reply_state(self, scope: Any) -> dict[str, Any]:
        """Report observed request/composer state without accepting a request."""
        prompts = await self._visible(scope.locator(_REQUEST_PROMPT))
        composers = await self._visible(scope.locator(_COMPOSER))
        if len(prompts) > 1 or len(composers) > 1 or (prompts and composers):
            raise BrowserActionError("unsupported_dom")
        if prompts:
            await self._one(prompts[0].locator('[data-testid="dm-message-request-accept-button"]'))
            return {"acceptance_required": True, "can_reply": False,
                    "reply_state_source": "visible_request_prompt"}
        if composers:
            composer = composers[0]
            writable = (await composer.is_enabled()
                        and await composer.get_attribute("readonly") is None
                        and await composer.get_attribute("aria-disabled") != "true"
                        and await composer.get_attribute("contenteditable") != "false")
            return {"acceptance_required": False, "can_reply": writable,
                    "reply_state_source": "visible_composer"}
        return {"acceptance_required": None, "can_reply": None,
                "reply_state_source": "unknown"}

    async def _exact_handle(self, scope: Any, handle: str) -> bool:
        # Restrict identity evidence to the conversation header or recipient row.
        if await self._visible(scope.get_by_text(re.compile(rf"^@{re.escape(handle)}$", re.I), exact=True)):
            return True
        links = await self._visible(scope.locator("a[href]"))
        for link in links:
            href = await link.get_attribute("href")
            if _profile_handle(href) == handle.lower():
                return True
        return False

    async def _participant_handles(self, scope: Any, expected_handle: str | None = None) -> set[str]:
        """Resolve participants only inside the identified conversation header."""
        header = await self._one(scope.locator(_HEADER), "recipient_unverified")
        handles: set[str] = set()
        for node in await self._visible(header.get_by_text(_VISIBLE_HANDLE, exact=True)):
            value = (await node.inner_text()).strip()
            if _VISIBLE_HANDLE.fullmatch(value):
                handles.add(value[1:].lower())
        for line in (await header.inner_text()).splitlines():
            value = line.strip()
            if _VISIBLE_HANDLE.fullmatch(value):
                handles.add(value[1:].lower())
        for link in await self._visible(header.locator("a[href]")):
            handle = _profile_handle(await link.get_attribute("href"))
            if handle:
                handles.add(handle)
        if not handles:
            raise BrowserActionError("recipient_unverified")
        # A collapsed group header does not identify its hidden participants.
        # Do not let a single visible member make such a group send eligible.
        text = await header.inner_text()
        counts = re.findall(r"\b([0-9]+)\s+(?:people|members|participants)\b", text, re.I)
        attr_count = await header.get_attribute("data-participant-count")
        if attr_count is not None:
            if not attr_count.isdigit():
                raise BrowserActionError("recipient_unverified")
            counts.append(attr_count)
        if any(int(count) > len(handles) for count in counts) or re.search(r"\b(?:and\s+[0-9]+\s+others|more participants)\b", text, re.I):
            raise BrowserActionError("recipient_unverified")
        if expected_handle is not None:
            known_self = {value.lower().lstrip("@") for value in (getattr(self, "account", {}).get("self_handles") or []) if isinstance(value, str)}
            if handles - known_self != {expected_handle.lower()}:
                raise BrowserActionError("recipient_mismatch")
        return handles

    async def _validate_participants(self, validator: Any, handles: set[str]) -> None:
        if not callable(validator):
            raise BrowserActionError("recipient_unverified")
        for handle in sorted(handles):
            try:
                result = validator(handle)
                if inspect.isawaitable(result):
                    result = await result
                if result is False:
                    raise BrowserActionError("recipient_blocked")
            except Exception:
                # Local policy errors can contain private lead data. Never
                # expose their text, arguments, or exception chain to clients.
                raise BrowserActionError("recipient_blocked") from None

    async def _wait_participants(self, scope: Any, expected_handle: str | None = None) -> set[str]:
        """Wait for initial header identity hydration without reloading chat."""
        deadline = asyncio.get_running_loop().time() + self.messaging_timeout_ms / 1000
        while True:
            await self._messaging_blocked()
            try:
                return await self._participant_handles(scope, expected_handle)
            except BrowserActionError as exc:
                if exc.reason != "recipient_unverified" or asyncio.get_running_loop().time() >= deadline:
                    raise
            await asyncio.sleep(0.1)

    def _assert_send_route(self, expected_url: str) -> None:
        try:
            actual_url, _ = conversation_url(self.page.url)
        except ValueError:
            raise BrowserActionError("conversation_mismatch") from None
        if actual_url != expected_url:
            raise BrowserActionError("conversation_mismatch")

    async def _new_conversation(self, handle: str) -> Any:
        # A native profile Message action may already have opened the exact
        # chat. Keep that unlocked SPA selection instead of opening a new modal.
        try:
            conversation_url(self.page.url)
        except ValueError:
            pass
        else:
            roots = await self._visible(self.page.locator(_CONVERSATION))
            if len(roots) == 1:
                try:
                    await self._wait_participants(roots[0], handle)
                except BrowserActionError as exc:
                    if exc.reason != "recipient_mismatch":
                        raise
                else:
                    await self._messaging_blocked()
                    return roots[0]
        if _profile_handle(self.page.url) == handle.lower():
            return await self._profile_conversation(handle)
        await self._open_inbox()
        buttons = self.page.locator('[data-testid="NewDM_Button"], [data-testid="chatNewMessageButton"]')
        if not await self._visible(buttons):
            return await self._profile_conversation(handle)
        button = await self._one(buttons)
        await button.click()
        dialog = await self._wait_one(self.page.get_by_role("dialog"))
        search = dialog.locator('[data-testid="searchPeople"], input[placeholder="Search people"]')
        if not await self._visible(search):
            search = dialog.get_by_role("combobox", name=re.compile(r"Search people", re.I))
        await (await self._one(search)).fill(handle)
        deadline = asyncio.get_running_loop().time() + self.messaging_timeout_ms / 1000
        selected = None
        while True:
            await self._messaging_blocked()
            candidates = await self._visible(dialog.locator('[data-testid="UserCell"], [data-testid="recipientResult"]'))
            exact = [row for row in candidates if await self._exact_handle(row, handle)]
            if len(exact) == 1:
                selected = exact[0]
                break
            if len(exact) > 1 or asyncio.get_running_loop().time() >= deadline:
                raise BrowserActionError("recipient_mismatch")
            await asyncio.sleep(0.1)
        await selected.click()
        next_button = dialog.get_by_role("button", name=re.compile(r"^(?:Next|Start chat)$", re.I))
        next_button = await self._one(next_button)
        if not await next_button.is_enabled():
            raise BrowserActionError("send_disabled")
        await next_button.click()
        await self._wait_messaging_view(conversation=True)
        scope = await self._conversation_scope()
        header = await self._wait_one(scope.locator(_HEADER))
        if not await self._exact_handle(header, handle):
            raise BrowserActionError("recipient_mismatch")
        # X must have assigned an allowlisted conversation route before sending.
        try:
            conversation_url(self.page.url)
        except ValueError:
            raise BrowserActionError("conversation_mismatch") from None
        return scope

    async def _profile_conversation(self, handle: str) -> Any:
        if _profile_handle(self.page.url) != handle.lower():
            await self.navigate("https://x.com/" + handle)
        profile = await self._wait_one(self.page.locator('[data-testid="UserName"]'), "recipient_unverified")
        if not await self._exact_handle(profile, handle):
            raise BrowserActionError("recipient_mismatch")
        button = await self._one(self.page.locator('[data-testid="sendDMFromProfile"]'))
        if not await button.is_enabled():
            raise BrowserActionError("send_disabled")
        await self._messaging_blocked()
        if _profile_handle(self.page.url) != handle.lower():
            raise BrowserActionError("recipient_mismatch")
        await button.click()
        await self._wait_messaging_view(conversation=True)
        scope = await self._conversation_scope()
        await self._wait_participants(scope, handle)
        try:
            conversation_url(self.page.url)
        except ValueError:
            raise BrowserActionError("conversation_mismatch") from None
        return scope

    async def _composer(self, scope: Any) -> tuple[Any, Any]:
        composer = await self._one(scope.locator(_COMPOSER))
        if not await composer.is_enabled():
            raise BrowserActionError("send_disabled")
        # The observed modern UI shows a voice button while blank and mounts
        # its Send button only after native typing.
        send = None if await composer.get_attribute("data-testid") == "dm-composer-textarea" else await self._one(scope.locator(_SEND))
        return composer, send

    async def _composer_value(self, composer: Any) -> str:
        tag = await composer.evaluate("element => element.tagName.toLowerCase()")
        return await composer.input_value() if tag in {"input", "textarea"} else await composer.inner_text()

    @staticmethod
    def _new_outgoing(before: list[dict[str, Any]], after: list[dict[str, Any]], text: str) -> dict[str, Any] | None:
        old_ids = {message["message_id"] for message in before if message["message_id"]}
        anchor_index = -1
        if before:
            anchor = before[-1]["message_id"]
            if not anchor:
                return None
            positions = [index for index, message in enumerate(after) if message["message_id"] == anchor]
            if len(positions) != 1:
                return None
            anchor_index = positions[0]
        for index in range(len(after) - 1, anchor_index, -1):
            message = after[index]
            if message["text"] != text or message["direction"] != "outgoing" or message["pending_or_failed"]:
                continue
            message_id = message["message_id"]
            if message_id and message_id not in old_ids:
                return message
        return None

    async def _send_unconfirmed(self, stage: str, observer_key: str | None,
                                composer: Any) -> BrowserActionError:
        """Capture primitive diagnostics before disposing an uncertain send.

        Inspection failure remains unknown. No browser exceptions, URLs, DOM
        texts, recipient identifiers, message IDs, or composer contents escape.
        """
        diagnostics = {"schema_version": 1, "stage": stage,
                       "observer_armed": observer_key is not None,
                       "observer_started": None, "composer_empty": None,
                       "pending_count": None, "failed_count": None,
                       "scope_connected": None, "composer_connected": None,
                       "send_control_connected": None}
        try:
            if observer_key is not None:
                snapshot = await self.page.evaluate("key => window[key]?.diagnostics?.() || null", observer_key)
                if isinstance(snapshot, dict):
                    for field in ("observer_started", "composer_empty", "scope_connected",
                                  "composer_connected", "send_control_connected"):
                        value = snapshot.get(field)
                        if isinstance(value, bool):
                            diagnostics[field] = value
                    for field in ("pending_count", "failed_count"):
                        value = snapshot.get(field)
                        if type(value) is int and 0 <= value <= 1000:
                            diagnostics[field] = value
            else:
                diagnostics["composer_empty"] = not (await self._composer_value(composer)).strip()
        except Exception:
            pass
        error = BrowserActionError("send_unconfirmed")
        error.diagnostics = diagnostics
        return error

    @_safe_dom_errors("outcome_unknown")
    async def send_message(self, recipient: str, text: str) -> dict[str, Any]:
        """Send once and confirm a new outgoing bubble; never retry a write."""
        if not isinstance(text, str) or not text.strip() or len(text) > 10_000:
            raise ValueError("text must contain between 1 and 10000 characters.")
        recipient = normalize_recipient(recipient)
        handle_match = _HANDLE.fullmatch(recipient) if recipient.startswith("@") else None
        if handle_match:
            scope = await self._new_conversation(handle_match.group(1))
        else:
            scope, _, _ = await self._open_conversation(recipient)
        await self._messaging_blocked()
        send_url, _ = conversation_url(self.page.url)
        validator = getattr(self, "recipient_validator", None)
        expected_handle = handle_match.group(1) if handle_match else None
        verify_identity = validator is not None or expected_handle is not None
        participants = await self._wait_participants(scope, expected_handle) if verify_identity else None
        composer, send = await self._composer(scope)
        # Do not overwrite a human's existing draft.
        if (await self._composer_value(composer)).strip():
            raise BrowserActionError("composer_not_empty")
        before = await self._message_snapshot(scope)
        await composer.fill(text)
        await self._messaging_blocked()
        self._assert_send_route(send_url)
        if send is None:
            send = await self._wait_one(scope.locator(_SEND))
        if not await send.is_enabled():
            raise BrowserActionError("send_disabled")
        modern = await send.get_attribute("data-testid") == "dm-composer-send-button"
        if modern and before and not before[-1]["message_id"]:
            raise BrowserActionError("unsupported_dom")
        if verify_identity:
            current_participants = await self._participant_handles(scope, expected_handle)
            if current_participants != participants:
                raise BrowserActionError("recipient_mismatch")
            if validator is not None:
                await self._validate_participants(validator, current_participants)
            if await self._participant_handles(scope, expected_handle) != participants:
                raise BrowserActionError("recipient_mismatch")
        observer_key = None
        try:
            if modern:
                observer_key = "__xuse_send_" + uuid.uuid4().hex
                await scope.evaluate(_ARM_SEND_OBSERVER, {
                    "key": observer_key, "text": text,
                    "ids": [message["message_id"] for message in before if message["message_id"]],
                    "anchor": before[-1]["message_id"] if before else None,
                })
            if verify_identity:
                current_participants = await self._participant_handles(scope, expected_handle)
                if current_participants != participants:
                    raise BrowserActionError("recipient_mismatch")
            # Recheck all local policy after the final awaited DOM inspection.
            # The only subsequent DOM operation before confirmation is the
            # single native Send click.
            before_write = getattr(self, "_before_write", None)
            if before_write is not None:
                result = before_write()
                if inspect.isawaitable(result):
                    await result
            if validator is not None:
                await self._validate_participants(validator, current_participants)
            self._assert_send_route(send_url)
            await send.click()
            deadline = asyncio.get_running_loop().time() + self.messaging_confirmation_timeout_ms / 1000
            while True:
                await self._messaging_blocked()
                try:
                    current_url, current_id = conversation_url(self.page.url)
                except ValueError:
                    raise await self._send_unconfirmed("route_changed", observer_key, composer) from None
                if current_url != send_url:
                    raise await self._send_unconfirmed("route_changed", observer_key, composer)
                if modern:
                    evidence = await self.page.evaluate("key => window[key]?.read() || null", observer_key)
                else:
                    evidence = self._new_outgoing(before, await self._message_snapshot(scope), text)
                if evidence and (modern or not (await self._composer_value(composer)).strip()):
                    if self.page.url.rstrip("/") != current_url:
                        raise await self._send_unconfirmed("route_changed", observer_key, composer)
                    return {
                        "success": True, "action": "send_message", "status": "confirmed", "source": "browser_dom",
                        "conversation_id": current_id, "url": current_url,
                        "recipient": recipient, "message_id": evidence["message_id"],
                        "confirmation": "new_local_pending_to_sent_message_and_cleared_composer" if modern else "new_outgoing_message_and_cleared_composer",
                    }
                if asyncio.get_running_loop().time() >= deadline:
                    raise await self._send_unconfirmed("confirmation_timeout", observer_key, composer)
                await asyncio.sleep(0.1)
        finally:
            if observer_key is not None:
                try:
                    await self.page.evaluate("key => {const observation=window[key]; if(observation){observation.dispose(); delete window[key];}}", observer_key)
                except Exception:
                    pass

    def _on_passcode_recovery(self) -> bool:
        current = urlsplit(self.page.url)
        return current.scheme == "https" and current.netloc == "x.com" and current.path == "/i/chat/pin/recovery"

    async def _segmented_passcode_fields(self) -> list[Any]:
        """Recognize the observed existing-keys gate, including open shadow DOM."""
        if not self._on_passcode_recovery():
            return []
        prompts = await self._visible(self.page.get_by_text(_PASSCODE_PROMPT, exact=True))
        if len(prompts) != 1:
            return []
        fields = await self._visible(self.page.locator(_PIN_SEGMENT))
        if len(fields) > 4:
            raise BrowserActionError("unsupported_dom")
        by_label = {await field.get_attribute("aria-label"): field for field in fields}
        labels = [f"Digit {index} of 4" for index in range(1, 5)]
        if len(by_label) != len(fields) or not set(by_label).issubset(labels):
            raise BrowserActionError("unsupported_dom")
        # The recovery route and prompt can mount before all four fields.
        # Partial, correctly labelled fields are hydration, never permission
        # to enter any digit. Unknown or duplicate labels still fail closed.
        if len(fields) != 4 or not self._on_passcode_recovery():
            return []
        return [by_label[label] for label in labels]

    @_safe_dom_errors("unlock_failed")
    async def unlock_messages(self, pin: str) -> dict[str, Any]:
        """Submit an explicitly supplied PIN once; never persist or report it."""
        if not isinstance(pin, str) or not re.fullmatch(r"[0-9]{4,12}", pin):
            raise ValueError("A numeric message PIN is required.")
        try:
            current = urlsplit(self.page.url)
            if self._on_passcode_recovery() or (current.scheme == "https" and current.netloc == "x.com" and current.path in {"/i/chat", "/messages"}):
                await self.check_blocked()
            else:
                await self.navigate("https://x.com/i/chat")
        except BrowserBlocked as exc:
            if exc.reason != "pin_required":
                raise
        fields = []
        segmented = False
        deadline = asyncio.get_running_loop().time() + self.messaging_timeout_ms / 1000
        while not fields:
            try:
                await self.check_blocked()
            except BrowserBlocked as exc:
                if exc.reason != "pin_required":
                    raise
            segments = await self._segmented_passcode_fields()
            if segments:
                if len(pin) != 4 or await self._visible(self.page.locator(_PIN_INPUT)):
                    raise BrowserActionError("unsupported_dom")
                fields = segments
                segmented = True
                break
            standard = await self._visible(self.page.locator(_PIN_INPUT))
            gate_pending = (self._on_passcode_recovery()
                            or bool(await self._visible(self.page.get_by_text(_PASSCODE_PROMPT, exact=True)))
                            or bool(await self._visible(self.page.locator(_PIN_SEGMENT))))
            if standard and (self._on_passcode_recovery() or await self._visible(self.page.locator(_PIN_SEGMENT))):
                raise BrowserActionError("unsupported_dom")
            if len(standard) == 1:
                fields = standard
                break
            if len(standard) > 1:
                raise BrowserActionError("unsupported_dom")
            if not gate_pending and await self._visible(self.page.locator(_INBOX)):
                # The inbox shell remains mounted behind the recovery gate.
                # Require rendered rows or an explicit empty state, and keep
                # waiting if its PIN prompt appears during these DOM reads.
                ready = (await self._visible(self.page.locator(f"{_CONVERSATION_ROWS}, {_CONVERSATION_LINKS}"))
                         or await self._visible(self.page.get_by_text(_EMPTY_INBOX, exact=True)))
                if ready:
                    try:
                        await self._messaging_blocked()
                    except BrowserBlocked as exc:
                        if exc.reason != "pin_required":
                            raise
                    else:
                        return {"success": True, "action": "unlock_messages", "status": "already_unlocked", "source": "browser_dom"}
            if asyncio.get_running_loop().time() >= deadline:
                raise BrowserActionError("unsupported_dom")
            await asyncio.sleep(0.1)
        try:
            if any([not await field.is_enabled() for field in fields]):
                raise BrowserActionError("unlock_failed")
            if segmented:
                # The observed gate submits automatically after the fourth
                # input. Address each numbered field directly despite focus
                # changes, and never click Forgot passcode or retry a PIN.
                for index, digit in enumerate(pin):
                    try:
                        await self.check_blocked()
                    except BrowserBlocked as exc:
                        if exc.reason != "pin_required":
                            raise
                    current_fields = await self._segmented_passcode_fields()
                    if len(current_fields) != 4:
                        raise BrowserActionError("unlock_failed")
                    await current_fields[index].fill(digit)
            else:
                await fields[0].fill(pin)
                buttons = self.page.get_by_role("button", name=re.compile(r"^(?:Unlock|Continue|Confirm|Next)$", re.I))
                submit = await self._one(buttons)
                if not await submit.is_enabled():
                    raise BrowserActionError("unlock_failed")
                await submit.click()
            deadline = asyncio.get_running_loop().time() + self.messaging_timeout_ms / 1000
            while True:
                prompt = await self._visible(self.page.get_by_text(_PASSCODE_PROMPT, exact=True)) if segmented else []
                if not await self._visible(self.page.locator(f"{_PIN_INPUT}, {_PIN_SEGMENT}")) and not prompt:
                    await self._wait_messaging_view()
                    return {"success": True, "action": "unlock_messages", "status": "confirmed", "source": "browser_dom"}
                if await self._visible(self.page.get_by_text(_PASSCODE_ERROR, exact=True)):
                    raise BrowserActionError("unlock_failed")
                # Keep rate limits/login/challenges authoritative, allowing only
                # the already known PIN gate while waiting for its dismissal.
                try:
                    await self.check_blocked()
                except BrowserBlocked as exc:
                    if exc.reason != "pin_required":
                        raise
                if asyncio.get_running_loop().time() >= deadline:
                    raise BrowserActionError("unlock_failed")
                await asyncio.sleep(0.1)
        except BrowserBlocked:
            raise
        except Exception:
            # Playwright fill failures may include the supplied value in their
            # call log. Suppress that exception chain at this secret boundary.
            raise BrowserActionError("unlock_failed") from None
        finally:
            for field in fields:
                try:
                    if await field.is_visible():
                        await field.fill("")
                except Exception:
                    pass
