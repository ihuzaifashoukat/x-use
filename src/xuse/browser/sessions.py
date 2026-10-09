"""Lazy, bounded browser contexts with per-account and process ownership locks."""
import asyncio
import hashlib
import ipaddress
import inspect
import math
import os
import re
import select
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, Optional
from urllib.parse import unquote, urlsplit

from xuse.core.config_loader import normalize_account_dict

from .cookies import load_account_cookies
from .errors import SessionError


class _ProcessInspector:
    """OS identities only; never discover or signal a process by image name."""

    def __init__(self):
        self.kernel = None
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            self.ctypes, self.types = ctypes, wintypes
            self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            for name, args, result in (
                ("OpenProcess", [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
                ("WaitForSingleObject", [wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
                ("TerminateProcess", [wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
                ("CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
                ("GetProcessTimes", [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4, wintypes.BOOL),
                ("CreateToolhelp32Snapshot", [wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE),
            ):
                method = getattr(self.kernel, name)
                method.argtypes, method.restype = args, result

    def cutoff(self):
        if sys.platform.startswith("linux"):
            return float(Path("/proc/uptime").read_text().split()[0])
        return time.time()

    def snapshot(self):
        if self.kernel:
            c, t = self.ctypes, self.types

            class Entry(c.Structure):
                _fields_ = [("size", t.DWORD), ("usage", t.DWORD), ("pid", t.DWORD),
                            ("heap", c.c_size_t), ("module", t.DWORD), ("threads", t.DWORD),
                            ("parent", t.DWORD), ("priority", t.LONG), ("flags", t.DWORD),
                            ("image", t.WCHAR * 260)]

            snapshot = self.kernel.CreateToolhelp32Snapshot(2, 0)
            if snapshot == c.c_void_p(-1).value:
                raise OSError()
            row = Entry()
            row.size = c.sizeof(row)
            result = {}
            try:
                for name in ("Process32FirstW", "Process32NextW"):
                    method = getattr(self.kernel, name)
                    method.argtypes = [t.HANDLE, c.POINTER(Entry)]
                    method.restype = t.BOOL
                found = self.kernel.Process32FirstW(snapshot, c.byref(row))
                while found:
                    result[row.pid] = (row.parent, None)
                    found = self.kernel.Process32NextW(snapshot, c.byref(row))
                return result
            finally:
                self.kernel.CloseHandle(snapshot)
        if sys.platform.startswith("linux"):
            result = {}
            ticks = os.sysconf("SC_CLK_TCK")
            for path in Path("/proc").iterdir():
                if path.name.isdigit():
                    try:
                        fields = (path / "stat").read_text().rsplit(")", 1)[1].split()
                        result[int(path.name)] = (int(fields[1]), int(fields[19]) / ticks)
                    except (OSError, ValueError, IndexError):
                        continue
            return result
        # macOS and older POSIX platforms can verify natural drainage, but
        # cannot atomically signal a creation identity. No PID kill fallback.
        output = subprocess.run(["ps", "-axo", "pid=,ppid=,lstart="],
                                capture_output=True, text=True, timeout=3, check=True,
                                env={**os.environ, "LC_ALL": "C"}).stdout
        result = {}
        for line in output.splitlines():
            parts = line.split(None, 2)
            if len(parts) == 3:
                result[int(parts[0])] = (int(parts[1]), time.mktime(time.strptime(parts[2], "%a %b %d %H:%M:%S %Y")))
        return result

    def pin(self, pid, birth, cutoff):
        token = None
        if self.kernel:
            token = self.kernel.OpenProcess(0x100000 | 0x1000 | 1, False, pid)
            if not token:
                if self.ctypes.get_last_error() == 87:
                    raise ProcessLookupError()
                raise OSError()
            times = [self.types.FILETIME() for _ in range(4)]
            if not self.kernel.GetProcessTimes(token, *(self.ctypes.byref(item) for item in times)):
                self.kernel.CloseHandle(token)
                raise OSError()
            actual = ((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime) / 10_000_000 - 11644473600
        elif sys.platform.startswith("linux") and hasattr(os, "pidfd_open"):
            try:
                token = os.pidfd_open(pid)
            except OSError:
                token = None
            actual = self.snapshot().get(pid, (None, None))[1]
        else:
            actual = self.snapshot().get(pid, (None, None))[1]
        if actual is None or actual > cutoff or (birth is not None and actual != birth):
            if token is not None:
                self.close(token)
            raise ProcessLookupError()
        return _OwnedProcess(self, pid, actual, token)

    def close(self, token):
        if self.kernel:
            self.kernel.CloseHandle(token)
        else:
            os.close(token)

    def job(self, process):
        c, t, kernel = self.ctypes, self.types, self.kernel

        class Limits(c.Structure):
            _fields_ = [("process_time", c.c_int64), ("job_time", c.c_int64), ("flags", t.DWORD),
                        ("minimum", c.c_size_t), ("maximum", c.c_size_t), ("active", t.DWORD),
                        ("affinity", c.c_size_t), ("priority", t.DWORD), ("scheduling", t.DWORD)]

        class Extended(c.Structure):
            _fields_ = [("basic", Limits), ("io", c.c_uint64 * 6), ("process_memory", c.c_size_t),
                        ("job_memory", c.c_size_t), ("peak_process", c.c_size_t), ("peak_job", c.c_size_t)]

        kernel.CreateJobObjectW.argtypes, kernel.CreateJobObjectW.restype = [c.c_void_p, t.LPCWSTR], t.HANDLE
        kernel.SetInformationJobObject.argtypes = [t.HANDLE, c.c_int, c.c_void_p, t.DWORD]
        kernel.SetInformationJobObject.restype = t.BOOL
        kernel.AssignProcessToJobObject.argtypes, kernel.AssignProcessToJobObject.restype = [t.HANDLE, t.HANDLE], t.BOOL
        kernel.TerminateJobObject.argtypes, kernel.TerminateJobObject.restype = [t.HANDLE, t.UINT], t.BOOL
        kernel.QueryInformationJobObject.argtypes = [t.HANDLE, c.c_int, c.c_void_p, t.DWORD, c.c_void_p]
        kernel.QueryInformationJobObject.restype = t.BOOL
        job = kernel.CreateJobObjectW(None, None)
        if not job:
            raise OSError()
        limits = Extended()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; no breakaway.
        # SET_QUOTA is required only to assign this known driver to its job;
        # descendant handles request only inspection/signal permissions.
        assignment = kernel.OpenProcess(0x100 | 1 | 0x1000, False, process.pid)
        times = [t.FILETIME() for _ in range(4)]
        verified = assignment and kernel.GetProcessTimes(assignment, *(c.byref(item) for item in times))
        birth = ((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime) / 10_000_000 - 11644473600
        try:
            assigned = (verified and birth == process.birth and process.alive()
                        and kernel.SetInformationJobObject(job, 9, c.byref(limits), c.sizeof(limits))
                        and kernel.AssignProcessToJobObject(job, assignment))
        finally:
            if assignment:
                kernel.CloseHandle(assignment)
        if not assigned:
            kernel.CloseHandle(job)
            raise OSError()
        return job

    def job_exited(self, job):
        # JOBOBJECT_BASIC_ACCOUNTING_INFORMATION: four int64 times, then
        # PageFaultCount, TotalProcesses, ActiveProcesses, TerminatedProcesses.
        accounting = (self.ctypes.c_uint64 * 6)()
        if not self.kernel.QueryInformationJobObject(job, 1, accounting, self.ctypes.sizeof(accounting), None):
            raise OSError()
        raw = self.ctypes.string_at(accounting, self.ctypes.sizeof(accounting))
        return int.from_bytes(raw[40:44], "little") == 0


class _OwnedProcess:
    def __init__(self, inspector, pid, birth, token):
        self.inspector, self.pid, self.birth, self.token = inspector, pid, birth, token

    def alive(self):
        if self.token is not None:
            if self.inspector.kernel:
                result = self.inspector.kernel.WaitForSingleObject(self.token, 0)
                if result not in (0, 258):
                    raise OSError()
                return result == 258
            return not select.select([self.token], [], [], 0)[0]
        return self.inspector.snapshot().get(self.pid, (None, None))[1] == self.birth

    def terminate(self):
        if not self.alive():
            return
        if self.token is None:
            raise OSError()  # Retain ownership when atomic signaling is unavailable.
        if self.inspector.kernel:
            if not self.inspector.kernel.TerminateProcess(self.token, 1) and self.alive():
                raise OSError()
        elif hasattr(signal, "pidfd_send_signal"):
            with suppress(ProcessLookupError):
                signal.pidfd_send_signal(self.token, signal.SIGKILL)
        else:
            raise OSError()

    def freeze(self):
        if not self.alive():
            return True
        if self.inspector.kernel or self.token is None or not hasattr(signal, "pidfd_send_signal"):
            return False
        with suppress(ProcessLookupError):
            signal.pidfd_send_signal(self.token, signal.SIGSTOP)
        deadline = time.monotonic() + 0.25
        while self.alive():
            try:
                fields = Path(f"/proc/{self.pid}/stat").read_text().rsplit(")", 1)[1].split()
                if fields[0] in {"T", "t"} and int(fields[19]) / os.sysconf("SC_CLK_TCK") == self.birth:
                    return True
            except (OSError, ValueError, IndexError):
                pass
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)
        return True

    def resume(self):
        if not self.inspector.kernel and self.token is not None and hasattr(signal, "pidfd_send_signal") and self.alive():
            with suppress(ProcessLookupError):
                signal.pidfd_send_signal(self.token, signal.SIGCONT)

    def close(self):
        token, self.token = self.token, None
        if token is not None:
            self.inspector.close(token)


class _OwnedBrowserProcesses:
    """Pin a launched browser and verified descendants before authentication.

    Handles/pidfds survive driver death and PID reuse. Unsupported atomic kill
    platforms retain the account lock when natural cleanup cannot be proven.
    """

    def __init__(self, inspector, root):
        self.inspector = inspector
        self.processes = {root.pid: root}
        self.complete = True
        self.job = None
        self.pending = {}
        self.scan_failed = False

    @classmethod
    def capture(cls, inspector, pid, cutoff):
        snapshot = inspector.snapshot()
        root = inspector.pin(pid, snapshot.get(pid, (None, None))[1], cutoff)
        guard = cls(inspector, root)
        try:
            guard.refresh()
        except Exception:
            guard.close()
            raise
        return guard

    def refresh(self):
        cutoff = self.inspector.cutoff()
        snapshot = self.inspector.snapshot()
        for pid, (_, birth) in tuple(self.pending.items()):
            current = snapshot.get(pid)
            if current is None or (birth is not None and current[1] != birth):
                self.pending.pop(pid)
        parents = {pid: process for pid, process in self.processes.items() if process.alive()}
        changed = True
        while changed:
            changed = False
            for pid, (parent, birth) in snapshot.items():
                if pid in self.processes or parent not in parents:
                    continue
                try:
                    process = self.inspector.pin(pid, birth, cutoff)
                except ProcessLookupError:
                    # A child born during the snapshot is still live and must
                    # not silently turn incomplete capture into exit proof.
                    current = self.inspector.snapshot().get(pid)
                    if current is not None and current[0] == parent:
                        self.pending[pid] = (parent, birth)
                    continue  # Never signal an identity we could not pin.
                except OSError:
                    self.pending[pid] = (parent, birth)
                    continue
                if process.birth < parents[parent].birth or not parents[parent].alive():
                    process.close()
                    continue
                self.processes[pid] = parents[pid] = process
                self.pending.pop(pid, None)
                changed = True
        self.complete = not self.pending and not self.scan_failed

    def exited(self):
        if self.job is not None:
            return self.inspector.job_exited(self.job)
        return self.complete and all(not process.alive() for process in self.processes.values())

    def terminate(self):
        if self.job is not None:
            if not self.inspector.kernel.TerminateJobObject(self.job, 1) and not self.exited():
                raise OSError()
            return
        # Atomic SIGSTOP prevents the pinned Linux parents creating children
        # while their final descendant identities are captured. No killpg/PID
        # fallback can accidentally signal a reused or unrelated identity.
        try:
            stable = False
            for _ in range(3):
                count = len(self.processes)
                frozen = all(process.freeze() for process in self.processes.values())
                self.refresh()
                if frozen and len(self.processes) == count and self.complete:
                    stable = True
                    break
            if sys.platform.startswith("linux") and not stable:
                self.scan_failed = True
                self.complete = False
            for process in self.processes.values():
                try:
                    process.terminate()
                except OSError:
                    pass  # Unsupported signaling must still allow natural exit.
        finally:
            # A failed capture/signal must not leave surviving owned Linux
            # processes frozen, preventing their own natural exit/recovery.
            for process in self.processes.values():
                with suppress(OSError):
                    process.resume()

    def close(self):
        for process in self.processes.values():
            process.close()
        if self.job is not None:
            self.inspector.kernel.CloseHandle(self.job)
            self.job = None


class AccountOwnerLock:
    """OS-held lock; crashes release ownership without stale PID heuristics.

    The filename is a hash of the authentication cookie, so account aliases
    and separate config directories cannot concurrently use the same account.
    Neither credential values nor account labels are written into the file.
    """

    def __init__(self, key: str, directory: Optional[Path] = None):
        self.path = (directory or Path(tempfile.gettempdir()) / "xuse-browser-locks") / (key + ".lock")
        self._file = None

    def acquire(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            stream = self.path.open("a+b")
            try:
                stream.seek(0, os.SEEK_END)
                if stream.tell() == 0:
                    stream.write(b"\0")
                    stream.flush()
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                stream.close()
                raise SessionError("account_in_use") from None
            self._file = stream
        except SessionError:
            raise
        except OSError:
            raise SessionError("session_unavailable") from None

    def release(self) -> None:
        stream, self._file = self._file, None
        if stream is None:
            return
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()


@dataclass
class SessionEntry:
    browser_manager: Any
    context: Any
    owner_lock: AccountOwnerLock
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_used: float = field(default_factory=time.monotonic)
    closing: bool = False
    close_done: asyncio.Event = field(default_factory=asyncio.Event)
    force_close: asyncio.Event = field(default_factory=asyncio.Event)

    def touch(self):
        self.last_used = time.monotonic()


def _positive_seconds(value: float) -> float:
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("Browser timeouts must be finite and positive.")
    return value


def _proxy_config(value: Any) -> Optional[dict]:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise SessionError("invalid_proxy")
    try:
        if len(value) > 8192 or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError()
        value = value.strip()
        if not value or any(char.isspace() for char in value) or "${" in value:
            raise ValueError()
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https", "socks5") or not parsed.hostname or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError()
        port = parsed.port
        authority = parsed.netloc.rsplit("@", 1)[-1]
        if authority.endswith(":") or port == 0:
            raise ValueError()
        host = parsed.hostname
        if ":" in host:
            host = "[" + str(ipaddress.IPv6Address(host)) + "]"
        else:
            host = host.encode("idna").decode("ascii").lower()
            labels = host.rstrip(".").split(".")
            if len(host) > 254 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9_-]{0,61}[a-z0-9])?", label) for label in labels):
                raise ValueError()
        if parsed.scheme == "socks5" and parsed.username is not None:
            # Chromium supports SOCKS5 transport, but not SOCKS authentication.
            raise ValueError()
        result = {"server": parsed.scheme + "://" + host + (":" + str(port) if port else "")}
        for key, encoded in (("username", parsed.username), ("password", parsed.password)):
            if encoded is None:
                continue
            if re.search(r"%(?![0-9a-fA-F]{2})", encoded):
                raise ValueError()
            decoded = unquote(encoded, errors="strict")
            if any(ord(char) < 32 or ord(char) == 127 for char in decoded):
                raise ValueError()
            if key == "username" and not decoded:
                raise ValueError()
            result[key] = decoded
        return result
    except (ValueError, TypeError):
        raise SessionError("invalid_proxy") from None


def _expand_proxy_environment(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 8192:
        raise SessionError("invalid_proxy")

    def substitute(match):
        replacement = os.environ.get(match.group(1))
        if not replacement:
            raise SessionError("invalid_proxy")
        return replacement

    expanded = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", substitute, value)
    if len(expanded) > 8192 or "${" in expanded or any(ord(char) < 32 or ord(char) == 127 for char in expanded):
        raise SessionError("invalid_proxy")
    return expanded.strip()


def resolve_account_proxy(config_loader, account: dict) -> Optional[dict]:
    """Validate one account's route without network access or state changes.

    Explicit account values override the global fallback. Pools use a stable
    account-ID hash; rotating strategies and unresolved credentials fail closed.
    Returned authentication fields are private and must not enter diagnostics.
    """
    value = account.get("proxy")
    if value is None or value == "":
        value = config_loader.get_setting("browser_settings.proxy", None)
    if value is None or value == "":
        return None
    value = _expand_proxy_environment(value)
    if value.startswith("pool:"):
        pool_name = value[5:]
        pools = config_loader.get_setting("browser_settings.proxy_pools", {})
        strategy = config_loader.get_setting("browser_settings.proxy_pool_strategy", "hash")
        account_id = account.get("account_id")
        if not isinstance(strategy, str) or strategy.strip().lower() != "hash":
            raise SessionError("invalid_proxy")
        if not isinstance(pools, dict) or not pool_name or not isinstance(account_id, str) or not account_id:
            raise SessionError("invalid_proxy")
        members = pools.get(pool_name)
        if not isinstance(members, list) or not members or any(not isinstance(member, str) or not member for member in members):
            raise SessionError("invalid_proxy")
        index = int(hashlib.sha256(account_id.encode("utf-8")).hexdigest(), 16) % len(members)
        value = _expand_proxy_environment(members[index])
    # A configured route cannot silently become a direct connection.
    if not value:
        raise SessionError("invalid_proxy")
    return _proxy_config(value)


class PlaywrightSessionPool:
    """One Chromium process; lazily authenticated isolated contexts per account.

    There are no persistent profiles, cookie snapshots, fingerprint overrides,
    stealth scripts, CAPTCHA solvers, or automatic write retries. Test seams
    accept a Playwright-compatible factory and an isolated lock directory.
    """

    backend = "playwright"

    def __init__(self, config_loader, idle_timeout_seconds=600,
                 cold_start_timeout_seconds=180, reap_interval_seconds=60,
                 playwright_factory=None, max_sessions=None, lock_directory=None):
        self.config_loader = config_loader
        self.idle_timeout_seconds = _positive_seconds(idle_timeout_seconds)
        self.cold_start_timeout_seconds = _positive_seconds(cold_start_timeout_seconds)
        self.reap_interval_seconds = _positive_seconds(reap_interval_seconds)
        self.messaging_timeout_ms = int(_positive_seconds(
            config_loader.get_setting("mcp.messaging_timeout_seconds", 60)
        ) * 1000)
        configured_limit = config_loader.get_setting("mcp.max_browser_sessions", 4)
        self.max_sessions = max_sessions if max_sessions is not None else configured_limit
        if isinstance(self.max_sessions, bool) or not isinstance(self.max_sessions, int) or not 1 <= self.max_sessions <= 16:
            raise ValueError("max_browser_sessions must be an integer between 1 and 16.")
        self._factory = playwright_factory
        self._lock_directory = Path(lock_directory) if lock_directory else None
        self._entries: Dict[str, SessionEntry] = {}
        self._create_lock = asyncio.Lock()
        self._runtime = None
        self._browser = None
        self._reaper_task = None
        self._start_tasks = set()
        self._cleanup_tasks = set()
        self._stranded_owners = set()
        self._process_guard = None
        self._process_guard_lock = asyncio.Lock()
        self._capture_task = None
        self._shutdown_task = None
        self._closed = False

    @property
    def active_accounts(self) -> Iterator[str]:
        return iter(tuple(self._entries))

    def entry_for(self, account_id: str) -> Optional[SessionEntry]:
        return self._entries.get(account_id)

    def find_account_dict(self, account_id: str) -> dict:
        for raw in self.config_loader.get_accounts_config():
            if isinstance(raw, dict) and raw.get("account_id") == account_id:
                return normalize_account_dict(raw)
        raise SessionError("unknown_account")

    async def acquire(self, account_id: str) -> SessionEntry:
        while True:
            if self._closed:
                raise SessionError("pool_closed")
            entry = self._entries.get(account_id)
            if entry and entry.closing:
                await entry.close_done.wait()
                continue
            if entry:
                entry.touch()
                return entry
            async with self._create_lock:
                if self._closed:
                    raise SessionError("pool_closed")
                if account_id in self._entries:
                    continue
                if len(self._entries) >= self.max_sessions:
                    candidates = [(key, value) for key, value in self._entries.items()
                                  if not value.lock.locked() and not value.closing]
                    if not candidates:
                        raise SessionError("session_limit")
                    oldest = min(candidates, key=lambda item: item[1].last_used)[0]
                    await self.close(oldest)
                    if self._closed:
                        raise SessionError("pool_closed")
                start_task = asyncio.create_task(self._cold_start(account_id))
                self._start_tasks.add(start_task)
                try:
                    entry = await asyncio.wait_for(start_task, self.cold_start_timeout_seconds)
                except asyncio.CancelledError:
                    # Cancellation can arrive after cold start succeeded but
                    # before wait_for hands its entry back to this caller. The
                    # completed entry is not yet tracked in _entries, so close
                    # it explicitly rather than losing its context and owner.
                    if start_task.done() and not start_task.cancelled():
                        try:
                            abandoned = start_task.result()
                        except Exception:
                            pass
                        else:
                            await self._cleanup(abandoned.context, abandoned.owner_lock)
                    raise
                except asyncio.TimeoutError:
                    raise SessionError("startup_timeout") from None
                finally:
                    self._start_tasks.discard(start_task)
                if self._closed:
                    await self._cleanup(entry.context, entry.owner_lock)
                    raise SessionError("pool_closed")
                self._entries[account_id] = entry
                self._ensure_reaper()
                return entry

    @asynccontextmanager
    async def session(self, account_id: str):
        while True:
            entry = await self.acquire(account_id)
            await entry.lock.acquire()
            if self._closed or entry.closing or self._entries.get(account_id) is not entry:
                entry.lock.release()
                continue
            break
        try:
            self._check_session_transport(entry)
            yield entry.browser_manager
        finally:
            entry.touch()
            entry.lock.release()

    def _check_session_transport(self, entry):
        # Expiry is reported before handing a page to an action. Recovery stays
        # explicit: never silently replay a write or replace an active account.
        try:
            connected = getattr(self._browser, "is_connected", None)
            if callable(connected) and not connected():
                hint = "The browser disconnected. Restart the MCP server before resuming account actions."
            else:
                closed = getattr(entry.browser_manager.page, "is_closed", None)
                if not callable(closed) or not closed():
                    return
                hint = "The account tab is closed. Use close_session, refresh cookies if needed, then resume_account_actions."
        except Exception:
            hint = "Browser transport is unavailable. Restart the MCP server before resuming account actions."
        error = SessionError("session_expired")
        error.operator_hint = hint
        raise error from None

    async def close(self, account_id: str, *, wait: bool = True):
        entry = self._entries.get(account_id)
        if not entry:
            return
        if entry.closing:
            if not wait:
                # Shutdown must not wait behind an action merely because an
                # ordinary close was already waiting for that action's lock.
                entry.force_close.set()
            await entry.close_done.wait()
            if self._entries.get(account_id) is entry:
                await self.close(account_id, wait=wait)
            return
        entry.closing = True
        entry.close_done.clear()
        entry.force_close.clear()
        acquired = False
        lock_waiter = force_waiter = None
        try:
            if wait:
                lock_waiter = asyncio.create_task(entry.lock.acquire())
                force_waiter = asyncio.create_task(entry.force_close.wait())
                await asyncio.wait((lock_waiter, force_waiter), return_when=asyncio.FIRST_COMPLETED)
                if lock_waiter.done() and not lock_waiter.cancelled():
                    acquired = lock_waiter.result()
        except asyncio.CancelledError:
            # Cancellation may arrive just after the waiter acquired the
            # lock. Release only a lock proven to be owned by this close.
            if lock_waiter is not None and lock_waiter.done() and not lock_waiter.cancelled():
                entry.lock.release()
            entry.closing = False
            entry.close_done.set()
            raise
        finally:
            for waiter in (lock_waiter, force_waiter):
                if waiter is not None and not waiter.done():
                    waiter.cancel()
        async def finish_close():
            try:
                await self._cleanup(entry.context, entry.owner_lock)
            finally:
                # Caller cancellation must not make an authenticated context
                # look disposed while its shielded cleanup is still running.
                self._entries.pop(account_id, None)
                if acquired:
                    entry.lock.release()
                entry.close_done.set()

        task = asyncio.create_task(finish_close())
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)
        await asyncio.shield(task)

    async def close_all(self):
        # Keep process handles and cleanup alive when the shutdown caller is
        # cancelled; cancellation is still delivered to that caller.
        self._closed = True
        if self._shutdown_task is None or self._shutdown_task.done():
            self._shutdown_task = asyncio.create_task(self._close_all())
        await asyncio.shield(self._shutdown_task)

    async def _close_all(self):
        self._closed = True
        reaper, self._reaper_task = self._reaper_task, None
        if reaper and not reaper.done():
            reaper.cancel()
            with suppress(asyncio.CancelledError):
                await reaper
        starts = tuple(self._start_tasks)
        for task in starts:
            task.cancel()
        if starts:
            await asyncio.gather(*starts, return_exceptions=True)
        async with self._create_lock:
            await self._refresh_owned_processes()
            for account in list(self._entries):
                await self.close(account, wait=False)
            if self._cleanup_tasks:
                await asyncio.gather(*tuple(self._cleanup_tasks), return_exceptions=True)
            browser, runtime = self._browser, self._runtime
            terminated = browser is None and runtime is None
            if browser is not None:
                try:
                    await asyncio.wait_for(browser.close(), 10)
                    terminated = True
                except Exception:
                    pass
            if runtime is not None:
                try:
                    await asyncio.wait_for(runtime.stop(), 10)
                    terminated = True
                except Exception:
                    pass
            if self._process_guard is not None:
                terminated = await self._drain_owned_processes()
            if terminated:
                self._browser = self._runtime = None
                for owner in tuple(self._stranded_owners):
                    with suppress(OSError):
                        owner.release()
                    self._stranded_owners.discard(owner)
                if self._process_guard is not None:
                    self._process_guard.close()
                    self._process_guard = None

    async def _get_browser(self):
        if self._browser is not None:
            if hasattr(self._browser, "is_connected") and not self._browser.is_connected():
                error = SessionError("browser_unavailable")
                error.operator_hint = "The browser disconnected. Restart the MCP server before resuming account actions."
                raise error from None
            return self._browser
        runtime = None
        try:
            if self._factory:
                runtime = self._factory()
                if inspect.isawaitable(runtime):
                    runtime = await runtime
            else:
                if self.backend == "patchright":
                    from patchright.async_api import async_playwright
                else:
                    from playwright.async_api import async_playwright
                runtime = await async_playwright().start()
            await self._start_owned_processes(runtime)
            headless = self.config_loader.get_setting("mcp.browser_headless", True)
            if not isinstance(headless, bool):
                raise SessionError("startup_failed")
            channel = self.config_loader.get_setting("mcp.browser_channel",
                self.config_loader.get_setting("browser_settings.channel", None))
            # A warm process can later host proxied accounts. Restrict WebRTC
            # for its whole lifetime so UDP cannot bypass those HTTP routes.
            # X-use does not implement voice/video calling.
            launch_options = {"headless": headless, "args": [
                "--webrtc-ip-handling-policy=disable_non_proxied_udp",
                "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
            ]}
            if channel is not None:
                if channel not in ("chrome", "msedge", "chromium"):
                    raise SessionError("startup_failed")
                launch_options["channel"] = channel
            browser = await runtime.chromium.launch(**launch_options)
            self._runtime, self._browser = runtime, browser
            await self._capture_owned_processes(browser)
            return browser
        except asyncio.CancelledError:
            if runtime is not None:
                await self._stop_runtime(runtime)
            raise
        except SessionError:
            if runtime is not None:
                await self._stop_runtime(runtime)
            raise
        except Exception:
            if runtime is not None:
                await self._stop_runtime(runtime)
            raise SessionError("browser_unavailable") from None

    async def _cold_start(self, account_id):
        from .page import XBrowser
        context = None
        owner = None
        try:
            account = self.find_account_dict(account_id)
            if not account.get("is_active", True):
                raise SessionError("inactive_account")
            cookies = load_account_cookies(account, self.config_loader)
            identity = next(c["value"] for c in cookies if c["name"] == "auth_token")
            owner = AccountOwnerLock(hashlib.sha256(identity.encode("utf-8")).hexdigest(), self._lock_directory)
            owner.acquire()
            proxy = resolve_account_proxy(self.config_loader, account)
            browser = await self._get_browser()
            options = {"accept_downloads": False, "service_workers": "block"}
            if proxy:
                options["proxy"] = proxy
            context = await browser.new_context(**options)
            context.set_default_timeout(10_000)
            context.set_default_navigation_timeout(30_000)
            page = await context.new_page()
            await self._refresh_owned_processes()
            if self._process_guard is not None and not self._process_guard.complete and self._process_guard.job is None:
                raise SessionError("browser_unavailable")
            await context.add_cookies(cookies)
            manager = XBrowser(page, account=account)
            manager.backend = self.backend
            manager.messaging_timeout_ms = self.messaging_timeout_ms
            await manager.navigate("https://x.com/home")
            await manager.ensure_ready()
            await self._refresh_owned_processes()
            return SessionEntry(manager, context, owner)
        except asyncio.CancelledError:
            await self._cleanup(context, owner)
            raise
        except SessionError:
            await self._cleanup(context, owner)
            raise
        except Exception:
            await self._cleanup(context, owner)
            raise SessionError("startup_failed") from None

    async def _cleanup(self, context, owner):
        async def finish():
            terminated = context is None
            try:
                if context is not None:
                    await self._refresh_owned_processes()
                    try:
                        await asyncio.wait_for(context.close(), 10)
                        terminated = True
                    except Exception:
                        # A timed-out close could leave an authenticated page
                        # running. Stop the process before releasing ownership,
                        # and prevent further actions on the damaged runtime.
                        self._closed = True
                        if self._browser is not None:
                            try:
                                await asyncio.wait_for(self._browser.close(), 10)
                                terminated = True
                            except Exception:
                                pass
                        if not terminated and self._runtime is not None:
                            try:
                                await asyncio.wait_for(self._runtime.stop(), 10)
                                terminated = True
                            except Exception:
                                pass
                    # A disconnected driver can return success from close()
                    # while its authenticated Chromium process is still alive.
                    try:
                        disconnected = self._process_guard is not None and (
                            self._browser is None or not self._browser.is_connected())
                    except Exception:
                        disconnected = self._process_guard is not None
                    if self._process_guard is not None and (self._closed or disconnected):
                        self._closed = True
                        terminated = await self._drain_owned_processes()
                elif self._process_guard is not None and self._closed:
                    terminated = await self._drain_owned_processes()
            finally:
                if owner:
                    if terminated:
                        with suppress(OSError):
                            owner.release()
                    else:
                        # Keep the OS lock until close_all proves termination
                        # or process exit releases it. Never permit concurrent
                        # sessions because cleanup failed.
                        self._stranded_owners.add(owner)
        task = asyncio.create_task(finish())
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)
        await asyncio.shield(task)

    async def _start_owned_processes(self, runtime):
        impl = getattr(runtime, "_impl_obj", None)
        if impl is None and self._factory is not None:
            return  # Explicit fake-runtime seam.
        process = impl._connection._transport._proc
        inspector = _ProcessInspector()

        def capture():
            cutoff = inspector.cutoff()
            snapshot = inspector.snapshot()
            if process.returncode is not None or snapshot.get(process.pid, (None, None))[0] != os.getpid():
                raise SessionError("browser_unavailable")
            guard = _OwnedBrowserProcesses.capture(inspector, process.pid, cutoff)
            try:
                if inspector.kernel:
                    guard.job = inspector.job(guard.processes[process.pid])
                return guard
            except Exception:
                guard.close()
                raise

        async def finish():
            self._process_guard = await asyncio.to_thread(capture)
        task = self._capture_task = asyncio.create_task(finish())
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)
        await asyncio.shield(task)

    async def _capture_owned_processes(self, browser):
        cdp_factory = getattr(browser, "new_browser_cdp_session", None)
        if not callable(cdp_factory) and self._factory is not None:
            return  # Explicit fake-driver seam; native factories still capture.
        cdp = await cdp_factory()
        try:
            info = await cdp.send("SystemInfo.getProcessInfo")
            roots = [item["id"] for item in info["processInfo"] if item["type"] == "browser"]
            if len(roots) != 1 or isinstance(roots[0], bool) or not isinstance(roots[0], int) or roots[0] <= 0:
                raise SessionError("browser_unavailable")
            await self._refresh_owned_processes()
            if self._process_guard is None or roots[0] not in self._process_guard.processes:
                raise SessionError("browser_unavailable")
        finally:
            with suppress(Exception):
                await cdp.detach()

    async def _refresh_owned_processes(self):
        if self._process_guard is None:
            return
        async def finish():
            async with self._process_guard_lock:
                guard = self._process_guard
                if guard is not None:
                    try:
                        await asyncio.to_thread(guard.refresh)
                    except Exception:
                        guard.complete = False
                        guard.scan_failed = True
                        self._closed = True
        task = asyncio.create_task(finish())
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)
        await asyncio.shield(task)

    async def _drain_owned_processes(self):
        async with self._process_guard_lock:
            guard = self._process_guard
            if guard is None:
                return False
            try:
                await asyncio.to_thread(guard.refresh)
                if await asyncio.to_thread(guard.exited):
                    return True
                await asyncio.to_thread(guard.terminate)
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    if await asyncio.to_thread(guard.exited):
                        return True
                    await asyncio.sleep(0.02)
            except Exception:
                guard.complete = False
            return False

    async def _stop_runtime(self, runtime):
        async def finish():
            if self._capture_task is not None:
                with suppress(Exception):
                    await asyncio.shield(self._capture_task)
            try:
                await asyncio.wait_for(runtime.stop(), 10)
            except Exception:
                self._closed = True
                self._runtime = runtime
            if self._process_guard is not None:
                if await self._drain_owned_processes():
                    self._process_guard.close()
                    self._process_guard = None
                    self._runtime = self._browser = None
                else:
                    self._closed = True
        task = asyncio.create_task(finish())
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)
        await asyncio.shield(task)

    def _ensure_reaper(self):
        if self._reaper_task is None or self._reaper_task.done():
            self._reaper_task = asyncio.create_task(self._reaper_loop())

    async def _reaper_loop(self):
        try:
            while not self._closed:
                await asyncio.sleep(self.reap_interval_seconds)
                now = time.monotonic()
                for account, entry in list(self._entries.items()):
                    if not entry.lock.locked() and not entry.closing and now - entry.last_used > self.idle_timeout_seconds:
                        await self.close(account)
        except asyncio.CancelledError:
            pass


class PatchrightSessionPool(PlaywrightSessionPool):
    """Patchright driver with the same lifecycle, ownership and action limits."""

    backend = "patchright"


SessionPool = PatchrightSessionPool
