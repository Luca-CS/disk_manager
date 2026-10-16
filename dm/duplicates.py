"""Duplicate file finder.

Read-only. Works from a finished dm.scanner.Scan (only files kept
individually, i.e. >= config.KEEP_FILE_MIN, are candidates).
"""

import hashlib
import os
import time

from . import config, safety, winapi

_CHUNK = 1024 * 1024
_PARTIAL = 64 * 1024
_MAX_GROUPS = 1000
_SKIP_DIRS = {"node_modules", ".git", "site-packages", ".venv", "venv", "__pycache__"}
# Program binaries are often byte-identical copies on purpose (one exe under
# several names, shared DLLs): deleting one breaks the program.
_SKIP_EXT = {"dll", "sys", "ocx", "mui", "pak", "asar", "node", "pyd", "so", "lib", "drv", "efi", "cpl",
             "cat", "manifest", "jar", "bin", "dat"}


def _excluded(path: str, roots: list[str]) -> bool:
    if any(safety.is_within(path, r) for r in roots):
        return True
    # On the system drive, only the user's own folders are candidates: anything
    # else at the drive root (C:\intelFPGA, C:\msys64...) is usually an installed program.
    if safety.is_within(path, config.SYSTEM_DRIVE) and not safety.is_within(path, config.USERPROFILE):
        return True
    name = os.path.basename(path)
    dot = name.rfind(".")
    if dot > 0 and name[dot + 1:].lower() in _SKIP_EXT:
        return True
    parts = os.path.normcase(path).split("\\")[:-1]
    return any(p in _SKIP_DIRS for p in parts)


def _partial_hash(path: str, size: int) -> bytes:
    h = hashlib.blake2b(digest_size=16)
    h.update(str(size).encode())
    with open(path, "rb", buffering=0) as f:
        h.update(f.read(_PARTIAL))
        if size > _PARTIAL:
            f.seek(max(_PARTIAL, size - _PARTIAL))
            h.update(f.read(_PARTIAL))
    return h.digest()


def find_duplicates(scan, *, min_size: int, include_system: bool = False, cancel=None, progress=None) -> dict:
    """Find identical files.

    Pipeline: group by size -> drop singletons -> lstat each candidate (skip
    links, skip cloud placeholders: winapi.CLOUD_ATTRS, skip vanished files)
    -> collapse hard links (same st_dev+st_ino keep one) -> partial hash
    (blake2b of first 64 KiB + last 64 KiB) -> full hash (1 MiB chunks,
    check ``cancel`` between chunks) -> groups of >= 2 files.

    Excluded unless include_system: anything inside dm.safety.PROTECTED_TREES,
    config.LOCALAPPDATA, config.APPDATA, anything on the system drive outside the
    user profile (installed programs), program binaries (dll, sys, pak...), and any
    path containing a folder named node_modules, .git, site-packages, .venv, venv,
    __pycache__ (duplication is normal there and deleting would break things).

    ``progress`` is a dict updated in place:
    {"phase": "group"|"partial"|"full", "done_files": int, "total_files": int,
     "done_bytes": int, "total_bytes": int, "groups": int}

    Returns:
    {
      "groups": [{"id": "hash prefix", "size": int, "count": int, "wasted": size*(count-1),
                  "files": [{"path": str, "name": str, "mtime": int}]}],   # wasted desc, max 1000
      "total_wasted": int, "files_hashed": int, "bytes_hashed": int,
      "min_size": int, "include_system": bool, "elapsed": float, "cancelled": bool,
    }
    """
    t0 = time.monotonic()
    prog = progress if progress is not None else {}
    prog.update(phase="group", done_files=0, total_files=0, done_bytes=0, total_bytes=0, groups=0)
    state = {"cancelled": False, "files": 0, "bytes": 0}
    floor = max(int(min_size), config.KEEP_FILE_MIN)

    def cancelled() -> bool:
        if cancel is not None and cancel.is_set():
            state["cancelled"] = True
        return state["cancelled"]

    def result(groups: list[dict]) -> dict:
        groups.sort(key=lambda g: (-g["wasted"], g["id"]))
        groups = groups[:_MAX_GROUPS]
        return {
            "groups": groups, "total_wasted": sum(g["wasted"] for g in groups),
            "files_hashed": state["files"], "bytes_hashed": state["bytes"],
            "min_size": min_size, "include_system": include_system,
            "elapsed": round(time.monotonic() - t0, 3), "cancelled": state["cancelled"],
        }

    roots = [] if include_system else list(safety.PROTECTED_TREES) + [config.LOCALAPPDATA, config.APPDATA]

    # 1. group by size
    by_size: dict[int, list[tuple[str, int]]] = {}
    for n, name, size, mtime in scan.iter_files():
        if size < floor:
            continue
        path = os.path.join(n.path(), name)
        if roots and _excluded(path, roots):
            continue
        by_size.setdefault(size, []).append((path, int(mtime)))
    cands = {s: v for s, v in by_size.items() if len(v) > 1}
    del by_size
    prog.update(total_files=sum(len(v) for v in cands.values()), total_bytes=sum(s * len(v) for s, v in cands.items()))

    # 2. lstat: drop links, cloud placeholders, vanished files; collapse hard links
    prog["phase"] = "group"
    sized: dict[int, list[tuple[str, int]]] = {}
    for size, items in cands.items():
        seen_ids = set()
        kept = []
        for path, mtime in items:
            if cancelled():
                return result([])
            prog["done_files"] += 1
            prog["done_bytes"] += size
            try:
                st = os.lstat(path)
            except OSError:
                continue
            attrs = getattr(st, "st_file_attributes", 0)
            if winapi.is_link_stat(st) or attrs & winapi.CLOUD_ATTRS or attrs & winapi.FILE_ATTRIBUTE_DIRECTORY:
                continue
            if (st.st_mode & 0o170000) != 0o100000 or st.st_size != size:
                continue
            fid = (st.st_dev, st.st_ino)
            if st.st_ino and fid in seen_ids:
                continue
            seen_ids.add(fid)
            kept.append((path, int(st.st_mtime)))
        if len(kept) > 1:
            sized[size] = kept
    total_files = sum(len(v) for v in sized.values())
    total_bytes = sum(s * len(v) for s, v in sized.items())

    # 3. partial hash
    prog.update(phase="partial", done_files=0, total_files=total_files, done_bytes=0, total_bytes=total_bytes)
    partial: dict[tuple[int, bytes], list[tuple[str, int]]] = {}
    for size, items in sized.items():
        for path, mtime in items:
            if cancelled():
                return result([])
            prog["done_files"] += 1
            prog["done_bytes"] += size
            try:
                key = (size, _partial_hash(path, size))
            except OSError:
                continue
            partial.setdefault(key, []).append((path, mtime))
    del sized
    partial = {k: v for k, v in partial.items() if len(v) > 1}

    # 4. full hash
    total_files = sum(len(v) for v in partial.values())
    total_bytes = sum(k[0] * len(v) for k, v in partial.items())
    prog.update(phase="full", done_files=0, total_files=total_files, done_bytes=0, total_bytes=total_bytes, groups=0)
    full: dict[tuple[int, bytes], list[tuple[str, int]]] = {}
    for (size, _), items in partial.items():
        for path, mtime in items:
            if cancelled():
                break
            h = hashlib.blake2b(digest_size=20)
            ok = True
            try:
                with open(path, "rb", buffering=0) as f:
                    while True:
                        if cancelled():
                            ok = False
                            break
                        chunk = f.read(_CHUNK)
                        if not chunk:
                            break
                        h.update(chunk)
                        prog["done_bytes"] += len(chunk)
                        state["bytes"] += len(chunk)
            except OSError:
                ok = False
            if not ok:
                continue
            prog["done_files"] += 1
            state["files"] += 1
            full.setdefault((size, h.digest()), []).append((path, mtime))
            if len(full[(size, h.digest())]) == 2:
                prog["groups"] += 1
        if state["cancelled"]:
            break

    groups = []
    for (size, digest), items in full.items():
        if len(items) < 2:
            continue
        items.sort(key=lambda it: (it[1], it[0].lower()))
        groups.append({
            "id": digest.hex()[:12], "size": size, "count": len(items), "wasted": size * (len(items) - 1),
            "files": [{"path": p, "name": os.path.basename(p), "mtime": m} for p, m in items],
        })
    prog["groups"] = len(groups)
    return result(groups)
