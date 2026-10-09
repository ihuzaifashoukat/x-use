"""Prepare private state files; refuse links instead of changing their targets.

This protects against accidental linked paths and other-user file access. It
does not promise protection against malicious path swaps by the current user.
"""
import os
import stat
from pathlib import Path


class PrivateStateError(OSError):
    """A state file could not be established as private and ordinary."""


def _ordinary(path, *, directory=False, missing=False):
    try:
        entry = path.lstat()
    except FileNotFoundError:
        if missing:
            return None
        raise
    if stat.S_ISLNK(entry.st_mode) or getattr(entry, "st_file_attributes", 0) & 0x400:
        raise PrivateStateError("Private state paths cannot contain links or reparse points.")
    if directory:
        if not stat.S_ISDIR(entry.st_mode):
            raise PrivateStateError("A private state parent is not a directory.")
    elif not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1:
        raise PrivateStateError("Private state requires a regular file with one link.")
    return entry


def _check_parents(path):
    for parent in reversed(path.parents):
        _ordinary(parent, directory=True, missing=True)


def private_state_file(path):
    path = Path(path)
    # Keep '..' components while checking parents: normalizing them first
    # could hide a linked ancestor that the caller's later open would follow.
    target = path if path.is_absolute() else Path.cwd() / path
    try:
        _check_parents(target)
        _ordinary(target, missing=True)
        if os.name == "nt":
            from .windows_permissions import create_private_directory
            for parent in reversed(target.parents):
                if _ordinary(parent, directory=True, missing=True) is None:
                    create_private_directory(parent)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
        _check_parents(target)
        if os.name == "nt":
            from .windows_permissions import ensure_private_file
            ensure_private_file(target)
        else:
            flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0) | os.O_NONBLOCK
            descriptor = os.open(target, flags, 0o600)
            try:
                entry = os.fstat(descriptor)
                if not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1 or entry.st_uid != os.geteuid():
                    raise PrivateStateError("Private state requires an owner-held regular file with one link.")
                os.fchmod(descriptor, 0o600)
                current = _ordinary(target)
                if (entry.st_dev, entry.st_ino) != (current.st_dev, current.st_ino):
                    raise PrivateStateError("Private state path changed during preparation.")
                if stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o600:
                    raise PrivateStateError("Private state permissions could not be verified.")
            finally:
                os.close(descriptor)
        _check_parents(target)
        _ordinary(target)
    except PrivateStateError:
        raise
    except (OSError, ValueError) as exc:
        raise PrivateStateError("Private state file preparation failed.") from exc
    return path


def private_sqlite_file(path):
    """Prepare a database only where its journals and WAL can remain private.

    Windows SQLite sidecars inherit the containing directory's DACL instead of
    the database file's DACL. Existing directories are inspected, never silently
    hardened. POSIX SQLite copies the private main file's mode to its sidecars.
    """
    path = Path(path)
    if os.name != "nt":
        return private_state_file(path)
    target = path if path.is_absolute() else Path.cwd() / path
    try:
        from .windows_permissions import create_private_directory, verify_private_sqlite_path
        _check_parents(target)
        _ordinary(target, missing=True)
        for parent in reversed(target.parents):
            if _ordinary(parent, directory=True, missing=True) is None:
                create_private_directory(parent)
        verify_private_sqlite_path(target.parent, directory=True)
        for suffix in ("-journal", "-wal", "-shm"):
            sidecar = target.with_name(target.name + suffix)
            if _ordinary(sidecar, missing=True) is not None:
                try:
                    verify_private_sqlite_path(sidecar, directory=False)
                except FileNotFoundError:
                    pass  # Another SQLite connection removed a private sidecar.
        return private_state_file(path)
    except (OSError, ValueError) as exc:
        raise PrivateStateError(
            "SQLite state requires a private directory and sidecars. Use a directory "
            "accessible only to your Windows user, SYSTEM and Administrators; "
            "existing directory permissions were not changed."
        ) from exc
