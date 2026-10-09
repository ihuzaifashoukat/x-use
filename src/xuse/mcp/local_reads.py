"""Bounded local diagnostics reads, called from workers by async tools."""
import json
from pathlib import Path

MAX_SUMMARY_BYTES = 256 * 1024
MAX_EVENT_TAIL_BYTES = 256 * 1024
MAX_COOKIE_BYTES = 1024 * 1024


def read_json(path: Path, *, max_bytes=MAX_SUMMARY_BYTES):
    with path.open("rb") as stream:
        data = stream.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("Local JSON file exceeds the diagnostics size limit.")
    return json.loads(data.decode("utf-8"))


def read_event_tail(path: Path, *, limit=20, max_bytes=MAX_EVENT_TAIL_BYTES):
    """Read at most a bounded tail; discard any partial first record."""
    try:
        with path.open("rb") as stream:
            size = stream.seek(0, 2)
            start = max(0, size - max_bytes)
            # Include the preceding byte to detect an exact newline boundary.
            stream.seek(max(0, start - 1))
            data = stream.read(max_bytes + 1)
    except FileNotFoundError:
        return [], False
    if start:
        if data[:1] == b"\n":
            data = data[1:]
        else:
            _, _, data = data.partition(b"\n")
    lines = [line for line in data.splitlines() if line.strip()]
    events = []
    for line in lines[-limit:]:
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if isinstance(event, dict):
            events.append(event)
    return events, size > max_bytes
