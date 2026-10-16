"""Append-only journal of every destructive action (JSON Lines)."""

import json
import threading
import time

from . import config

_lock = threading.Lock()


def append(action: str, **fields) -> dict:
    """Record an action. `action` is one of: recycle, purge, empty_recycle_bin."""
    entry = {"ts": time.time(), "action": action, **fields}
    line = json.dumps(entry, ensure_ascii=False)
    with _lock:
        config.ensure_data_dirs()
        with open(config.JOURNAL_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    return entry


def read(limit: int = 500) -> list[dict]:
    """Most recent entries first."""
    try:
        with open(config.JOURNAL_PATH, encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return []
    out = []
    for line in reversed(lines[-limit:]):
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def total_freed() -> int:
    return sum(int(e.get("bytes", 0)) for e in read(limit=100_000))
