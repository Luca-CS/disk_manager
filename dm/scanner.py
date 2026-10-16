"""Parallel, read-only directory scanner and the in-memory tree it builds.

Design notes
* Work-stealing over a shared queue: each worker lists one directory, then
  enqueues its subdirectories. Load stays balanced even when one folder holds
  most of the files (C:\\Users\\<you>).
* Junctions / symlinks / mount points are never followed (no double counting,
  no escaping the scanned tree). Cloud placeholders count as 0 bytes on disk.
* Files >= config.KEEP_FILE_MIN are kept individually; smaller ones are only
  aggregated per folder (count + size) to keep memory low on full-disk scans.
"""

import contextlib
import gzip
import hashlib
import heapq
import json
import os
import queue
import sys
import threading
import time

from . import categories, config, winapi

F_DENIED = 1     # folder could not be listed
F_CLOUD = 4      # contains online-only cloud files

AGE_BUCKETS = [  # (label, max age in days)
    ("< 1 mois", 30),
    ("1 – 6 mois", 182),
    ("6 – 12 mois", 365),
    ("1 – 2 ans", 730),
    ("2 – 5 ans", 1826),
    ("> 5 ans", None),
]
_DAY = 86400.0


class Node:
    __slots__ = ("name", "parent", "dirs", "files", "size", "small_size", "small_count",
                 "file_count", "dir_count", "mtime", "flags")

    def __init__(self, name: str, parent: "Node | None"):
        self.name = name
        self.parent = parent
        self.dirs: list[Node] | None = None
        self.files: list[tuple[str, int, int]] | None = None  # (name, size, mtime) for big files
        self.size = 0          # whole subtree (after finalize); own files before
        self.small_size = 0    # files < KEEP_FILE_MIN directly in this folder
        self.small_count = 0
        self.file_count = 0    # whole subtree (after finalize); own before
        self.dir_count = 0
        self.mtime = 0         # newest file mtime in subtree
        self.flags = 0

    def path(self) -> str:
        parts = []
        n = self
        while n.parent is not None:
            parts.append(n.name)
            n = n.parent
        root = n.name
        if not parts:
            return root
        parts.reverse()
        return (root if root.endswith("\\") else root + "\\") + "\\".join(parts)


def _join(base: str, name: str) -> str:
    return (base if base.endswith("\\") else base + "\\") + name


class _WorkerStats:
    __slots__ = ("files", "dirs", "bytes", "denied", "links", "cloud_files", "cloud_bytes", "ext", "age", "current")

    def __init__(self):
        self.files = self.dirs = self.bytes = 0
        self.denied = self.links = self.cloud_files = self.cloud_bytes = 0
        self.ext: dict[str, list[int]] = {}
        self.age = [[0, 0] for _ in AGE_BUCKETS]  # [count, size]
        self.current = ""


class Scan:
    """A scan of one root folder. Thread-safe for reads once `finished`."""

    def __init__(self, root: str):
        root = os.path.normpath(root)
        if len(root) == 2 and root[1] == ":":
            root += "\\"
        self.root_path = root
        self.root = Node(root, None)
        self.started = 0.0
        self.finished = 0.0
        self.cancelled = False
        self.from_cache = False
        self._workers: list[_WorkerStats] = []
        # merged results
        self.totals: dict = {}
        self.ext_stats: dict[str, list[int]] = {}
        self.age_stats: list[list[int]] = [[0, 0] for _ in AGE_BUCKETS]
        self.lock = threading.RLock()  # guards tree mutations after the scan (deletions)

    # ------------------------------------------------------------------ scanning
    def run(self, cancel: threading.Event | None = None, workers: int = config.SCAN_WORKERS) -> None:
        self.started = time.time()
        now = self.started
        q: queue.SimpleQueue = queue.SimpleQueue()
        state = {"outstanding": 1}
        state_lock = threading.Lock()
        self._workers = [_WorkerStats() for _ in range(workers)]
        keep_min = config.KEEP_FILE_MIN
        DIR = winapi.FILE_ATTRIBUTE_DIRECTORY
        REPARSE = winapi.FILE_ATTRIBUTE_REPARSE_POINT
        SURR = winapi.REPARSE_NAME_SURROGATE
        CLOUD = winapi.CLOUD_ATTRS
        bucket_limits = [(d * _DAY if d else None) for _, d in AGE_BUCKETS]

        def process(ws: _WorkerStats, node: Node, path: str) -> int:
            ws.current = path
            try:
                it = os.scandir(path)
            except OSError:
                node.flags |= F_DENIED
                ws.denied += 1
                return 0
            subdirs = []
            files = None
            own_size = small_size = small_count = count = 0
            newest = 0
            ext_stats = ws.ext
            age = ws.age
            with it:
                for e in it:
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    attrs = st.st_file_attributes
                    if attrs & REPARSE and st.st_reparse_tag & SURR:
                        ws.links += 1
                        continue
                    if attrs & DIR:
                        subdirs.append(Node(e.name, node))
                        continue
                    size = st.st_size
                    if attrs & CLOUD:
                        ws.cloud_files += 1
                        ws.cloud_bytes += size
                        node.flags |= F_CLOUD
                        size = 0
                    count += 1
                    own_size += size
                    mt = int(st.st_mtime)
                    if mt > newest:
                        newest = mt
                    name = e.name
                    dot = name.rfind(".")
                    ext = name[dot + 1:].lower() if 0 < dot < len(name) - 1 and len(name) - dot <= 13 else ""
                    es = ext_stats.get(ext)
                    if es is None:
                        ext_stats[ext] = [1, size]
                    else:
                        es[0] += 1
                        es[1] += size
                    a = now - mt
                    for i, lim in enumerate(bucket_limits):
                        if lim is None or a < lim:
                            age[i][0] += 1
                            age[i][1] += size
                            break
                    if size >= keep_min:
                        if files is None:
                            files = []
                        files.append((name, size, mt))
                    else:
                        small_size += size
                        small_count += 1
            node.size = own_size
            node.file_count = count
            node.small_size = small_size
            node.small_count = small_count
            node.mtime = newest
            node.files = files
            ws.files += count
            ws.bytes += own_size
            ws.dirs += 1
            if subdirs:
                node.dirs = subdirs
                with state_lock:
                    state["outstanding"] += len(subdirs)
                for child in subdirs:
                    q.put((child, _join(path, child.name)))
            return len(subdirs)

        def worker(ws: _WorkerStats) -> None:
            while True:
                item = q.get()
                if item is None:
                    return
                node, path = item
                try:
                    if cancel is None or not cancel.is_set():
                        process(ws, node, path)
                except Exception:  # never let one odd folder kill the scan
                    node.flags |= F_DENIED
                finally:
                    with state_lock:
                        state["outstanding"] -= 1
                        done = state["outstanding"] == 0
                    if done:
                        for _ in range(workers):
                            q.put(None)

        q.put((self.root, self.root_path))
        threads = [threading.Thread(target=worker, args=(ws,), daemon=True, name=f"scan-{i}")
                   for i, ws in enumerate(self._workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.cancelled = bool(cancel is not None and cancel.is_set())
        self._merge()
        self._finalize()
        self.finished = time.time()

    def progress(self) -> dict:
        ws = self._workers
        current = ""
        for w in ws:
            if w.current:
                current = w.current
                break
        return {
            "files": sum(w.files for w in ws),
            "dirs": sum(w.dirs for w in ws),
            "bytes": sum(w.bytes for w in ws),
            "denied": sum(w.denied for w in ws),
            "current": current,
            "elapsed": time.time() - self.started if self.started else 0,
        }

    def _merge(self) -> None:
        ext: dict[str, list[int]] = {}
        age = [[0, 0] for _ in AGE_BUCKETS]
        t = {"files": 0, "dirs": 0, "bytes": 0, "denied": 0, "links": 0, "cloud_files": 0, "cloud_bytes": 0}
        for w in self._workers:
            for k in t:
                t[k] += getattr(w, k)
            for e, (c, s) in w.ext.items():
                cur = ext.get(e)
                if cur is None:
                    ext[e] = [c, s]
                else:
                    cur[0] += c
                    cur[1] += s
            for i, (c, s) in enumerate(w.age):
                age[i][0] += c
                age[i][1] += s
        self.totals = t
        self.ext_stats = ext
        self.age_stats = age
        self._workers = []

    def _finalize(self) -> None:
        """Aggregate sizes bottom-up and sort children by size (largest first)."""
        order = []
        stack = [self.root]
        while stack:
            n = stack.pop()
            order.append(n)
            if n.dirs:
                stack.extend(n.dirs)
        for n in reversed(order):
            if n.dirs:
                size, fc, dc, mt = n.size, n.file_count, len(n.dirs), n.mtime
                for c in n.dirs:
                    size += c.size
                    fc += c.file_count
                    dc += c.dir_count
                    if c.mtime > mt:
                        mt = c.mtime
                    n.flags |= c.flags & F_CLOUD
                n.size, n.file_count, n.dir_count, n.mtime = size, fc, dc, mt
                n.dirs.sort(key=lambda c: c.size, reverse=True)
            if n.files:
                n.files.sort(key=lambda f: f[1], reverse=True)

    # ------------------------------------------------------------------ lookups
    def find(self, path: str) -> Node | None:
        """Directory node for an absolute path inside this scan."""
        p = os.path.normpath(path)
        root = self.root_path
        rk, pk = os.path.normcase(root).rstrip("\\"), os.path.normcase(p).rstrip("\\")
        if pk == rk:
            return self.root
        if not pk.startswith(rk + "\\"):
            return None
        node = self.root
        for part in p[len(root.rstrip("\\")) + 1:].split("\\"):
            if not part:
                continue
            lp = part.lower()
            nxt = None
            for c in node.dirs or ():
                if c.name.lower() == lp:
                    nxt = c
                    break
            if nxt is None:
                return None
            node = nxt
        return node

    def contains(self, path: str) -> bool:
        rk = os.path.normcase(self.root_path).rstrip("\\")
        pk = os.path.normcase(os.path.normpath(path)).rstrip("\\")
        return pk == rk or pk.startswith(rk + "\\")

    def iter_nodes(self):
        stack = [self.root]
        while stack:
            n = stack.pop()
            yield n
            if n.dirs:
                stack.extend(n.dirs)

    def iter_files(self):
        """(dir_node, name, size, mtime) for every kept (>= KEEP_FILE_MIN) file."""
        for n in self.iter_nodes():
            if n.files:
                for name, size, mt in n.files:
                    yield n, name, size, mt

    # ------------------------------------------------------------------ mutations
    def remove_path(self, path: str, size_hint: int = 0) -> None:
        """Reflect a deletion in the tree so the UI updates without rescanning."""
        with self.lock:
            p = os.path.normpath(path)
            node = self.find(p)
            parent = None
            removed_size = removed_files = 0
            if node is not None and node is not self.root:
                parent = node.parent
                parent.dirs = [c for c in parent.dirs if c is not node] or None
                removed_size, removed_files = node.size, node.file_count
                rm_dirs = node.dir_count + 1
            else:
                parent = self.find(os.path.dirname(p))
                if parent is None:
                    return
                name = os.path.basename(p).lower()
                rm_dirs = 0
                hit = None
                for f in parent.files or ():
                    if f[0].lower() == name:
                        hit = f
                        break
                if hit is not None:
                    parent.files = [f for f in parent.files if f is not hit] or None
                    removed_size, removed_files = hit[1], 1
                elif size_hint and parent.small_count:
                    take = min(size_hint, parent.small_size)
                    parent.small_size -= take
                    parent.small_count -= 1
                    removed_size, removed_files = take, 1
                else:
                    return
            n = parent
            while n is not None:
                n.size -= removed_size
                n.file_count -= removed_files
                n.dir_count -= rm_dirs
                n = n.parent

    def refresh(self, path: str, cancel: threading.Event | None = None) -> bool:
        """Re-scan one folder in place (after a cleanup) and fix ancestor totals."""
        node = self.find(path)
        if node is None:
            return False
        node_path = node.path()
        if not os.path.isdir(node_path):
            if node is not self.root:
                self.remove_path(node_path)
            return True
        sub = Scan(node_path)
        sub.run(cancel=cancel, workers=4)
        if sub.cancelled:
            return False
        with self.lock:
            fresh = sub.root
            d_size = fresh.size - node.size
            d_files = fresh.file_count - node.file_count
            d_dirs = fresh.dir_count - node.dir_count
            node.dirs = fresh.dirs
            for c in node.dirs or ():
                c.parent = node
            node.files, node.small_size, node.small_count = fresh.files, fresh.small_size, fresh.small_count
            node.size, node.file_count, node.dir_count = fresh.size, fresh.file_count, fresh.dir_count
            node.mtime, node.flags = fresh.mtime, fresh.flags
            n = node.parent
            while n is not None:
                n.size += d_size
                n.file_count += d_files
                n.dir_count += d_dirs
                n = n.parent
        return True

    # ------------------------------------------------------------------ queries
    def summary(self) -> dict:
        r = self.root
        out = {
            "root": self.root_path,
            "size": r.size,
            "files": r.file_count,
            "dirs": r.dir_count,
            "denied": self.totals.get("denied", 0),
            "links": self.totals.get("links", 0),
            "cloud_files": self.totals.get("cloud_files", 0),
            "cloud_bytes": self.totals.get("cloud_bytes", 0),
            "started": self.started,
            "finished": self.finished,
            "duration": max(0.0, self.finished - self.started),
            "cancelled": self.cancelled,
            "from_cache": self.from_cache,
            "volume": None,
        }
        if len(self.root_path) == 3:  # drive root: compare with what the volume reports
            usage = winapi.disk_usage(self.root_path)
            if usage:
                total, free = usage
                used = total - free
                out["volume"] = {"total": total, "free": free, "used": used,
                                 "unscanned": max(0, used - r.size)}
        return out

    def _node_json(self, n: Node, path: str | None = None) -> dict:
        d = {"name": n.name, "size": n.size, "type": "dir",
             "files": n.file_count, "dirs": n.dir_count, "mtime": n.mtime,
             "denied": bool(n.flags & F_DENIED), "cloud": bool(n.flags & F_CLOUD)}
        if path is not None:
            d["path"] = path
        return d

    def tree(self, path: str, depth: int = 3, min_ratio: float = 0.002, max_nodes: int = 4000) -> dict | None:
        """Nested structure for the treemap / sunburst, pruned to what is visible."""
        with self.lock:
            node = self.find(path)
            if node is None:
                return None
            total = max(1, node.size)
            threshold = total * min_ratio
            budget = [max_nodes]

            def build(n: Node, level: int) -> dict:
                d = {"n": n.name, "s": n.size, "t": "d", "fc": n.file_count}
                if n.flags & F_DENIED:
                    d["x"] = 1
                has_content = bool(n.dirs or n.files or n.small_count)
                if level >= depth or not has_content:
                    if has_content:
                        d["more"] = 1
                    return d
                kids = []
                other_s = other_k = 0
                items = []
                for c in n.dirs or ():
                    items.append((c.size, 0, c))
                for f in n.files or ():
                    items.append((f[1], 1, f))
                if n.small_count:
                    items.append((n.small_size, 2, None))
                items.sort(key=lambda x: x[0], reverse=True)
                for size, kind, obj in items:
                    if size < threshold or budget[0] <= 0:
                        other_s += size
                        other_k += 1 if kind else (obj.file_count or 1)
                        continue
                    budget[0] -= 1
                    if kind == 0:
                        kids.append(build(obj, level + 1))
                    elif kind == 1:
                        kids.append({"n": obj[0], "s": size, "t": "f", "cat": categories.category_of(obj[0]),
                                     "m": obj[2]})
                    else:
                        kids.append({"n": small_label(n.small_count), "s": size, "t": "s", "k": n.small_count})
                if other_s > 0:
                    kids.append({"n": f"{other_k} autre{'s' if other_k > 1 else ''} élément{'s' if other_k > 1 else ''}",
                                 "s": other_s, "t": "o", "k": other_k})
                d["ch"] = kids
                return d

            out = build(node, 0)
            out["path"] = node.path()
            return out

    def children(self, path: str) -> dict | None:
        """Direct content of a folder (folders + big files + small-file aggregate)."""
        with self.lock:
            node = self.find(path)
            if node is None:
                return None
            base = node.path()
            items = []
            for c in node.dirs or ():
                d = self._node_json(c, _join(base, c.name))
                d["empty"] = not (c.dirs or c.files or c.small_count)
                items.append(d)
            for name, size, mt in node.files or ():
                items.append({"name": name, "path": _join(base, name), "size": size, "type": "file",
                              "mtime": mt, "category": categories.category_of(name), "ext": categories.ext_of(name)})
            if node.small_count:
                items.append({"name": small_label(node.small_count), "size": node.small_size, "type": "small",
                              "count": node.small_count})
            items.sort(key=lambda x: x["size"], reverse=True)
            info = self._node_json(node, base)
            info["parent"] = node.parent.path() if node.parent is not None else None
            return {"node": info, "items": items}

    def largest(self, limit: int = 200, category: str | None = None, min_age_days: int = 0,
                under: str | None = None) -> list[dict]:
        with self.lock:
            start = self.find(under) if under else self.root
            if start is None:
                return []
            cutoff = time.time() - min_age_days * _DAY if min_age_days else None
            heap: list = []
            stack = [start]
            seq = 0
            while stack:
                n = stack.pop()
                if n.dirs:
                    stack.extend(n.dirs)
                for name, size, mt in n.files or ():
                    if cutoff is not None and mt > cutoff:
                        continue
                    if category and categories.category_of(name) != category:
                        continue
                    seq += 1
                    item = (size, seq, n, name, mt)
                    if len(heap) < limit:
                        heapq.heappush(heap, item)
                    elif size > heap[0][0]:
                        heapq.heapreplace(heap, item)
            heap.sort(reverse=True)
            return [{"path": _join(n.path(), name), "name": name, "size": size, "mtime": mt,
                     "category": categories.category_of(name), "ext": categories.ext_of(name)}
                    for size, _, n, name, mt in heap]

    def types(self) -> dict:
        cats = {k: {"key": k, "label": lbl, "size": 0, "count": 0} for k, lbl in categories.CATEGORIES}
        exts = []
        for ext, (count, size) in self.ext_stats.items():
            cat = categories.EXT_TO_CAT.get(ext, "other")
            cats[cat]["size"] += size
            cats[cat]["count"] += count
            exts.append({"ext": ext, "category": cat, "size": size, "count": count})
        exts.sort(key=lambda e: e["size"], reverse=True)
        return {"categories": sorted(cats.values(), key=lambda c: c["size"], reverse=True),
                "extensions": exts[:300], "total": self.root.size}

    def age(self) -> list[dict]:
        return [{"label": lbl, "max_days": d, "count": c, "size": s}
                for (lbl, d), (c, s) in zip(AGE_BUCKETS, self.age_stats)]

    def snapshot(self, depth: int = 3, min_size: int = 50 * 1024 * 1024) -> dict:
        """Compact {path: size} of big folders, used to show growth over time."""
        out = {}
        stack = [(self.root, 0)]
        while stack:
            n, lvl = stack.pop()
            if n.size < min_size:
                continue
            out[n.path()] = n.size
            if lvl < depth:
                for c in n.dirs or ():
                    stack.append((c, lvl + 1))
        return out

    # ------------------------------------------------------------------ cache (gzip JSON)
    def save(self) -> str:
        """Persist the tree so the next launch can reopen it instantly (no pickle)."""
        config.ensure_data_dirs()

        def enc(n: Node) -> list:
            # Own-folder values; subtree aggregates are recomputed on load.
            own_size = n.size - sum(c.size for c in n.dirs or ())
            own_count = n.file_count - sum(c.file_count for c in n.dirs or ())
            return [n.name, own_size, n.small_size, n.small_count, n.flags,
                    [list(f) for f in n.files] if n.files else 0,
                    [enc(c) for c in n.dirs] if n.dirs else 0, own_count, n.mtime]

        with self.lock, _deep_recursion():
            payload = {"v": 1, "root": self.root_path, "started": self.started, "finished": self.finished,
                       "cancelled": self.cancelled, "totals": self.totals, "ext": self.ext_stats,
                       "age": self.age_stats, "tree": enc(self.root)}
            path = cache_path(self.root_path)
            tmp = path + ".tmp"
            with gzip.open(tmp, "wt", encoding="utf-8", errors="surrogatepass", compresslevel=5) as f:
                json.dump(payload, f, separators=(",", ":"), ensure_ascii=False)
        os.replace(tmp, path)
        return path

    @classmethod
    def load(cls, root: str) -> "Scan | None":
        path = cache_path(root)
        try:
            with gzip.open(path, "rt", encoding="utf-8", errors="surrogatepass") as f:
                payload = json.load(f)
        except (OSError, ValueError):
            return None
        if payload.get("v") != 1:
            return None
        scan = cls(payload["root"])
        scan.started = payload["started"]
        scan.finished = payload["finished"]
        scan.cancelled = payload.get("cancelled", False)
        scan.totals = payload.get("totals", {})
        scan.ext_stats = payload.get("ext", {})
        scan.age_stats = payload.get("age", scan.age_stats)
        scan.from_cache = True

        def dec(arr: list, parent: Node | None, node: Node | None = None) -> Node:
            name, own_size, small_size, small_count, flags, files, dirs, own_count, mtime = arr[:9]
            n = node or Node(name, parent)
            n.size, n.small_size, n.small_count, n.flags = own_size, small_size, small_count, flags
            n.file_count, n.mtime = own_count, mtime
            n.files = [tuple(f) for f in files] if files else None
            n.dirs = [dec(c, n) for c in dirs] if dirs else None
            return n

        with _deep_recursion():
            dec(payload["tree"], None, scan.root)
        # mtime stored is already the subtree max; _finalize keeps the max.
        scan._finalize()
        return scan


def small_label(count: int) -> str:
    plural = "s" if count > 1 else ""
    return f"{count} petit{plural} fichier{plural} (< {config.KEEP_FILE_MIN // (1024 * 1024)} Mo)"


@contextlib.contextmanager
def _deep_recursion(limit: int = 10_000):
    """Directory trees can be deeper than Python's default recursion limit."""
    old = sys.getrecursionlimit()
    sys.setrecursionlimit(max(old, limit))
    try:
        yield
    finally:
        sys.setrecursionlimit(old)


def cache_path(root: str) -> str:
    k = os.path.normcase(os.path.normpath(root)).rstrip("\\")
    h = hashlib.sha1(k.encode("utf-8")).hexdigest()[:16]
    return str(config.SCANS_DIR / f"tree-{h}.json.gz")
