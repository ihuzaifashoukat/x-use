"""Reviewed multi-post threads, with durable progress and no blanket retries."""
import asyncio
import hashlib
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from PIL import Image
from xuse.core.config_loader import PROJECT_ROOT
from . import actions, executor as ex
from .annotations import LOCAL_WRITE, LOCAL_WRITE_IDEMPOTENT, PUBLISHES_TO_X, READ_ONLY_LOCAL
from .browser_bridge import uses_playwright
from .executor import ToolError
from .safety import PolicyError
from .thread_store import ThreadStore

MAX_THREAD_PARTS = 20
_PERMALINK = re.compile(r"https://x\.com/(?:[A-Za-z0-9_]{1,15}|i/web)/status/([0-9]+)")


def canonical_post_url(value):
    if not isinstance(value, str) or not _PERMALINK.fullmatch(value):
        raise ToolError("A canonical https://x.com author/status/numeric-id URL is required.")
    return value


def _media_manifest(paths):
    """Validate every file and bind reviewed media bytes to the draft.

    Reads are bounded and streamed. No arbitrary text/config files can be
    attached. Image decoding verifies the format rather than trusting a suffix.
    """
    if not isinstance(paths, list) or len(paths) > 4 or any(not isinstance(p, str) or not p for p in paths):
        raise ToolError("Thread media must be a list of at most four local media paths.")
    manifests = []
    for value in paths:
        path = Path(value).resolve()
        suffix = path.suffix.lower()
        if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".mov"} or not path.is_file():
            raise ToolError("Thread media must be existing local image or video files.")
        video = suffix in {".mp4", ".mov"}
        size = path.stat().st_size
        if not 0 < size <= (512 * 1024 * 1024 if video else 20 * 1024 * 1024):
            raise ToolError("Thread media exceeds its local file size limit.")
        if (video or suffix == ".gif") and len(paths) != 1:
            raise ToolError("A thread segment can attach one video/GIF or up to four images.")
        try:
            if video:
                with path.open("rb") as stream:
                    if stream.read(12)[4:8] != b"ftyp":
                        raise ValueError()
            else:
                with Image.open(path) as image:
                    if image.format not in {"JPEG", "PNG", "WEBP", "GIF"}:
                        raise ValueError()
                    if image.format == "GIF" and len(paths) != 1:
                        raise ValueError()
                    image.verify()
            digest = hashlib.sha256()
            total = 0
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    total += len(chunk)
                    if total > (512 * 1024 * 1024 if video else 20 * 1024 * 1024):
                        raise ValueError()
                    digest.update(chunk)
            if total != size:
                raise ValueError()
        except Exception:
            raise ToolError("Thread media could not be validated.") from None
        manifests.append({"path": str(path), "sha256": digest.hexdigest()})
    return manifests


def _store(ctx):
    store = getattr(ctx, "thread_store", None)
    if store is None:
        draft_path = getattr(ctx.draft_store, "_path", None)
        directory = Path(draft_path).parent if draft_path else PROJECT_ROOT / "data"
        store = ThreadStore(directory / "thread_runs.sqlite3")
        ctx.thread_store = store
    return store


def thread_result(run):
    published = [s["published_url"] for s in run["segments"] if s["state"] == "confirmed"]
    next_index = next((s["position"] for s in run["segments"] if s["state"] != "confirmed"), len(run["segments"]))
    return {"state": run["state"], "run_id": run["run_id"], "draft_id": run["draft_id"],
            "account": run["account"], "published": published, "next_index": next_index,
            "error": run["error"], "segments": run["segments"],
            "success": run["state"] == "complete"}


def _error(exc, fallback="preflight_failed"):
    error = {"reason": getattr(exc, "reason", None) or fallback,
             "message": ex.sanitize_text(str(exc))}
    for field in ("action_id", "retry_after_seconds"):
        if getattr(exc, field, None) is not None:
            error[field] = getattr(exc, field)
    return error


def _check_account_backend(ctx, account):
    _, raw, _ = ex.resolve_account(ctx, account)
    ex.require_active(raw, account)
    if not uses_playwright(ctx) or ctx.safety_store is None:
        raise ToolError("Threads require the native async browser and durable safety store.")


async def _check_media(post):
    if await asyncio.to_thread(_media_manifest, post["media"]) != post["media_manifest"]:
        raise ToolError("Reviewed thread media changed; create and approve a new draft.")


async def _preflight(ctx, run):
    _check_account_backend(ctx, run["account"])
    # Check all remaining files before the next external write, including files
    # attached to later segments. Changed media needs a newly reviewed draft.
    for post in run["payload"]["posts"][len([s for s in run["segments"] if s["state"] == "confirmed"]):]:
        await _check_media(post)


def _confirmed_url(result, run, parent):
    if not isinstance(result, dict) or result.get("success") is not True or result.get("status") != "confirmed":
        raise ToolError("Thread segment has no confirmed newly created post evidence.")
    evidence = result.get("evidence")
    if not isinstance(evidence, dict):
        raise ToolError("Thread segment has no canonical newly created post URL.")
    url = canonical_post_url(evidence.get("tweet_url"))
    identifier = _PERMALINK.fullmatch(url).group(1)
    forbidden = [s["published_url"] for s in run["segments"] if s["published_url"]] + ([parent] if parent else [])
    if str(evidence.get("tweet_id")) != identifier or any(_PERMALINK.fullmatch(old).group(1) == identifier for old in forbidden):
        raise ToolError("Thread segment confirmation reused a parent or previous post ID.")
    return url


async def _execute_run(ctx, store, run_id):
    run = store.get(run_id)
    if run["state"] in ("complete", "uncertain", "cancelled"):
        return thread_result(run)
    try:
        await _preflight(ctx, run)
    except Exception as exc:
        store.block(run_id, _error(exc))
        return thread_result(store.get(run_id))
    for segment in run["segments"]:
        if segment["state"] == "confirmed":
            continue
        position = segment["position"]
        post = run["payload"]["posts"][position]
        parent = run["segments"][position - 1]["published_url"] if position else run["payload"]["reply_to"]
        try:
            _check_account_backend(ctx, run["account"])
            await _check_media(post)
        except Exception as exc:
            store.block(run_id, _error(exc))
            return thread_result(store.get(run_id))
        store.start(run_id, position, parent)
        result = None
        try:
            context = {"draft_id": run["draft_id"]}
            if parent:
                kwargs = {"action_context": context}
                if post["media"]:
                    kwargs["media"] = post["media"]
                    kwargs["media_manifest"] = post["media_manifest"]
                result = await actions.exec_reply(ctx, run["account"], parent, post["text"], **kwargs)
            else:
                kwargs = {"media": post["media"] or None, "action_context": context}
                if post["media"]:
                    kwargs["media_manifest"] = post["media_manifest"]
                result = await actions.exec_post(ctx, run["account"], post["text"], **kwargs)
            url = _confirmed_url(result, run, parent)
        except BaseException as exc:
            # Only a policy refusal *without* a reservation proves that the
            # browser never wrote. Every other exception is an unknown outcome.
            known_unsent = isinstance(exc, PolicyError) and not getattr(exc, "action_id", None)
            error = _error(exc, "outcome_unknown")
            action_id = getattr(exc, "action_id", None) or (result.get("action_id") if isinstance(result, dict) else None)
            if action_id:
                error["action_id"] = action_id
            store.finish(run_id, position, state="pending" if known_unsent else "uncertain", action_id=action_id, error=error)
            if not isinstance(exc, Exception):
                raise
            return thread_result(store.get(run_id))
        store.finish(run_id, position, state="confirmed", published_url=url, action_id=result.get("action_id"))
        run = store.get(run_id)
    return thread_result(run)


async def execute_thread_draft(ctx, draft):
    """Approval dispatcher contract: complete, blocked, or uncertain result.

    Approval owns the draft status transition. A blocked run retains durable
    authorization; continue_thread never grants authorization by itself.
    """
    draft = draft.model_copy(deep=True)
    store = _store(ctx)
    run_id = draft.payload.get("run_id")
    with store.execution_lock(run_id):
        store.validate_draft(run_id, draft)
        if draft.status != "approved":
            raise ToolError("Thread execution requires an approved draft.")
        store.authorize(run_id)
        return await _execute_run(ctx, store, run_id)


def register_thread_tools(server, ctx):
    from .tools import draft_response, guard, ok_

    @server.tool(annotations=LOCAL_WRITE)
    @guard
    async def prepare_thread(account: str, posts: List[Dict[str, Any]], reply_to: Optional[str] = None) -> Dict[str, Any]:
        """Stage 1-20 exact text/media posts for review; always creates a draft.
        Optional reply_to starts a reply thread. Every later post replies to
        the immediately preceding confirmed new post. No LLM is called."""
        account_id, _, _ = ex.resolve_account(ctx, account)
        _check_account_backend(ctx, account_id)
        if not 1 <= len(posts) <= MAX_THREAD_PARTS:
            raise ToolError("A thread requires between 1 and 20 posts.")
        if reply_to is not None:
            reply_to = canonical_post_url(reply_to)
        prepared = []
        for post in posts:
            if not isinstance(post, dict) or set(post) - {"text", "media"}:
                raise ToolError("Each thread post accepts only text and optional media.")
            text = post.get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > 280 or "\x00" in text:
                raise ToolError("Thread post text must contain 1–280 characters and no NUL bytes.")
            paths = post.get("media", [])
            manifest = await asyncio.to_thread(_media_manifest, [] if paths is None else paths)
            prepared.append({"text": text, "media": [m["path"] for m in manifest], "media_manifest": manifest})
        payload = {"run_id": uuid4().hex, "posts": prepared, "reply_to": reply_to}
        preview = f"Publish {len(prepared)} posts as @{account_id}"
        if reply_to:
            preview += f" replying first to {reply_to}"
        preview += "\n" + "\n".join(f"{i + 1}/{len(prepared)}: {p['text']}" + ("\nMedia: " + ", ".join(p["media"]) if p["media"] else "") for i, p in enumerate(prepared))
        draft = ctx.draft_store.create(account_id, "publish_thread", payload, preview)
        run = _store(ctx).create(draft)
        return {**draft_response(draft), **thread_result(run)}

    @server.tool(annotations=READ_ONLY_LOCAL)
    @guard
    async def get_thread_run(run_id: str) -> Dict[str, Any]:
        """Read durable local segment states, exact published URLs and next index."""
        return ok_(**thread_result(_store(ctx).get(run_id)))

    @server.tool(annotations=PUBLISHES_TO_X)
    @guard
    async def continue_thread(run_id: str) -> Dict[str, Any]:
        """Resume an approved run only at a known unsubmitted segment after
        spacing/budget/preflight recovery. Uncertain segments are never resent."""
        store = _store(ctx)
        with store.execution_lock(run_id):
            run = store.get(run_id)
            if run["state"] == "cancelled":
                return ok_(**thread_result(run), status="rejected")
            if not run["authorized"]:
                raise ToolError("Approve the thread draft before continuing.")
            try:
                draft = ctx.draft_store.get(run["draft_id"])
            except KeyError:
                raise ToolError("The reviewed thread draft is unavailable.") from None
            store.validate_draft(run_id, draft)
            if draft.status in ("rejected", "failed"):
                raise ToolError("The thread draft cannot be continued in its current status.")
            result = await _execute_run(ctx, store, run_id)
            status = "executed" if result["state"] == "complete" else "uncertain" if result["state"] == "uncertain" else "partial" if result["published"] else "pending"
            ctx.draft_store.set_status(draft.draft_id, status)
            return ok_(**result, status=status)

    @server.tool(annotations=LOCAL_WRITE_IDEMPOTENT)
    @guard
    async def cancel_thread(run_id: str) -> Dict[str, Any]:
        """Cancel remaining thread segments locally. Retains confirmed URLs and
        uncertain evidence; never deletes posts or clears safety uncertainty.
        An active send must finish before cancellation can be accepted."""
        store = _store(ctx)
        with store.execution_lock(run_id):
            run = store.cancel(run_id)
            if run["state"] == "cancelled":
                # Durable revocation comes first; even a missing draft or failed
                # draft update cannot make the run resumable again.
                try:
                    ctx.draft_store.get(run["draft_id"])
                except KeyError:
                    pass
                else:
                    ctx.draft_store.set_status(run["draft_id"], "rejected")
            return ok_(**thread_result(run), status="executed" if run["state"] == "complete" else "rejected")
