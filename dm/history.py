"""Scan snapshots (folder sizes over time) and volume usage samples."""

import json
import os
import threading
import time

from . import config, winapi

_lock = threading.Lock()
SNAPSHOTS = config.SCANS_DIR / "snapshots.jsonl"
USAGE = config.SCANS_DIR / "usage.jsonl"
MAX_SNAPSHOTS = 60


def _append(path, entry: dict) -> None:
    with _lock:
        config.ensure_data_dirs()
        with open(path, "a", encoding="utf-8", errors="surrogatepass") as f:
            f.write(json.dumps(entry, ensure_ascii=True) + "\n")


def _read(path) -> list[dict]:
    try:
        with open(path, encoding="utf-8", errors="surrogatepass") as f:
            out = []
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
            return out
    except FileNotFoundError:
        return []


def record_snapshot(scan) -> dict:
    """Store the big-folder sizes of a finished scan."""
    summary = scan.summary()
    entry = {
        "id": f"{int(scan.finished)}",
        "ts": scan.finished,
        "root": scan.root_path,
        "total": summary["size"],
        "files": summary["files"],
        "volume": summary.get("volume"),
        "folders": scan.snapshot(),
    }
    _append(SNAPSHOTS, entry)
    snaps = _read(SNAPSHOTS)
    if len(snaps) > MAX_SNAPSHOTS:  # compact: keep the most recent ones
        with _lock:
            tmp = str(SNAPSHOTS) + ".tmp"
            with open(tmp, "w", encoding="utf-8", errors="surrogatepass") as f:
                for s in snaps[-MAX_SNAPSHOTS:]:
                    f.write(json.dumps(s, ensure_ascii=True) + "\n")
            os.replace(tmp, SNAPSHOTS)
    return entry


def list_snapshots() -> list[dict]:
    """Snapshots without the folder maps, most recent first."""
    return [{k: v for k, v in s.items() if k != "folders"} for s in reversed(_read(SNAPSHOTS))]


def compare(a_id: str, b_id: str, limit: int = 100) -> dict | None:
    """Folder size changes from snapshot a (older) to snapshot b (newer)."""
    snaps = {s["id"]: s for s in _read(SNAPSHOTS)}
    a, b = snaps.get(a_id), snaps.get(b_id)
    if not a or not b:
        return None
    fa, fb = a["folders"], b["folders"]
    changes = []
    for path in set(fa) | set(fb):
        before, after = fa.get(path), fb.get(path)
        delta = (after or 0) - (before or 0)
        if delta:
            changes.append({"path": path, "before": before, "after": after, "delta": delta})
    # Report the deepest changed folders first-class: a parent's growth is the
    # sum of its children, so sort by absolute delta and let the UI indent.
    changes.sort(key=lambda c: abs(c["delta"]), reverse=True)
    return {"a": {k: a[k] for k in ("id", "ts", "root", "total")},
            "b": {k: b[k] for k in ("id", "ts", "root", "total")},
            "delta": b["total"] - a["total"], "changes": changes[:limit]}


def sample_usage() -> None:
    """Record used/free space of every fixed volume (called at start-up and after cleanups)."""
    now = time.time()
    for v in winapi.list_volumes():
        if v["type"] == "fixed":
            _append(USAGE, {"ts": now, "root": v["root"], "total": v["total"], "used": v["used"]})


def usage_series(root: str | None = None, limit: int = 500) -> list[dict]:
    rows = _read(USAGE)
    if root:
        rows = [r for r in rows if r["root"].lower() == root.lower()]
    return rows[-limit:]
