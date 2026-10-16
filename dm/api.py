"""JSON API routes. Every input is validated here or in dm.safety; the UI is never trusted."""

import logging
import os
import sys
import threading
import time

from . import (__version__, apps, categories, cleaners, config, duplicates, history, journal, safety,
               system_info, winapi)
from .scanner import Scan, cache_path
from .server import ApiError, Router
from .tasks import Busy, TaskManager

log = logging.getLogger("dm.api")

# Windows tools the UI may open. Keys are the only accepted values. Executables
# are absolute System32 paths (never resolved through PATH or the current folder).
_SYS32 = os.path.join(config.SYSTEM_ROOT, "System32")
SHELL_TARGETS = {
    "storagesense": ("ms-settings:storagesense", None),
    "apps": ("ms-settings:appsfeatures", None),
    "appwiz": (os.path.join(_SYS32, "control.exe"), "appwiz.cpl"),
    "cleanmgr": (os.path.join(_SYS32, "cleanmgr.exe"), None),
    "system_protection": (os.path.join(_SYS32, "SystemPropertiesProtection.exe"), None),
    "performance_options": (os.path.join(_SYS32, "SystemPropertiesPerformance.exe"), None),
    "recyclebin": (os.path.join(config.SYSTEM_ROOT, "explorer.exe"), "shell:RecycleBinFolder"),
    "diskmgmt": (os.path.join(_SYS32, "mmc.exe"), os.path.join(_SYS32, "diskmgmt.msc")),
}


class AppState:
    def __init__(self):
        self.tasks = TaskManager()
        self.scan: Scan | None = None
        self.clean_analysis: dict | None = None
        self.dupes: dict | None = None
        self.window_pids: set[int] = set()
        self.on_quit = None       # set by run.py
        self.on_elevate = None    # set by run.py
        self._apps_cache: tuple[float, list] | None = None
        self.lock = threading.Lock()

    def running_names(self) -> set[str]:
        return winapi.running_process_names(exclude_tree_of=self.window_pids)


def _require_scan(state: AppState) -> Scan:
    if state.scan is None:
        raise ApiError(409, "Aucune analyse disponible. Lancez d'abord une analyse.", "no_scan")
    return state.scan


def _start(state: AppState, kind: str, label: str, fn, group: str) -> dict:
    try:
        return state.tasks.start(kind, label, fn, group=group).as_dict()
    except Busy as e:
        raise ApiError(409, str(e), "busy")


def _str_list(value, name: str, max_items: int = 5000) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > max_items or \
            not all(isinstance(v, str) for v in value):
        raise ApiError(400, f"« {name} » doit être une liste de chemins.")
    return value


def build_router(state: AppState) -> Router:
    r = Router()

    # ------------------------------------------------------------------ app
    @r.route("GET", "/api/meta")
    def meta(req):
        return {
            "version": __version__,
            "admin": winapi.is_admin(),
            "dry_run": safety.DRY_RUN,
            "python": sys.version.split()[0],
            "keep_file_min": config.KEEP_FILE_MIN,
            "categories": [{"key": k, "label": lbl} for k, lbl in categories.CATEGORIES],
            "user": os.environ.get("USERNAME", ""),
            "computer": os.environ.get("COMPUTERNAME", ""),
            "paths": {"home": config.USERPROFILE, "downloads": os.path.join(config.USERPROFILE, "Downloads"),
                      "system_drive": config.SYSTEM_DRIVE},
        }

    @r.route("POST", "/api/ping")
    def ping(req):
        req.handler.server.last_ping = time.monotonic()
        return {"ok": True, "ts": time.time()}

    @r.route("POST", "/api/app/quit")
    def quit_app(req):
        if state.on_quit:
            threading.Timer(0.3, state.on_quit).start()
        return {"ok": True}

    @r.route("POST", "/api/app/elevate")
    def elevate(req):
        if winapi.is_admin():
            raise ApiError(409, "L'application est déjà lancée en administrateur.")
        if state.tasks.active():
            raise ApiError(409, "Attendez la fin des tâches en cours.", "busy")
        if not state.on_elevate or not state.on_elevate():
            raise ApiError(500, "L'élévation a été refusée ou a échoué.")
        return {"ok": True}

    # ------------------------------------------------------------------ system
    @r.route("GET", "/api/system")
    def system(req):
        info = system_info.get_system_info(refresh=req.q("refresh") == "1")
        sd = config.SYSTEM_DRIVE
        special = {}
        try:
            with os.scandir(sd) as it:  # FindFirstFile works even on locked system files
                for e in it:
                    if e.name.lower() in ("pagefile.sys", "hiberfil.sys", "swapfile.sys"):
                        special[e.name.lower().split(".")[0]] = e.stat(follow_symlinks=False).st_size
        except OSError:
            pass
        return {**info, "memory": winapi.memory_status(), "special_files": special,
                "recycle_bin": winapi.recycle_bin_info()}

    @r.route("GET", "/api/volumes")
    def volumes(req):
        return {"volumes": winapi.list_volumes(), "recycle_bin": winapi.recycle_bin_info()}

    # ------------------------------------------------------------------ tasks
    @r.route("GET", "/api/tasks")
    def tasks_list(req):
        return {"tasks": [t.as_dict(include_result=False) for t in state.tasks.active()]}

    @r.route("GET", "/api/tasks/{task_id}")
    def task_get(req):
        t = state.tasks.get(req.params["task_id"])
        if t is None:
            raise ApiError(404, "Tâche inconnue.", "not_found")
        return t.as_dict()

    @r.route("POST", "/api/tasks/{task_id}/cancel")
    def task_cancel(req):
        t = state.tasks.get(req.params["task_id"])
        if t is None:
            raise ApiError(404, "Tâche inconnue.", "not_found")
        t.cancel.set()
        return t.as_dict(include_result=False)

    # ------------------------------------------------------------------ scan
    def _scan_root(raw) -> str:
        if not isinstance(raw, str):
            raise ApiError(400, "Chemin à analyser manquant.")
        raw = raw.strip()
        if len(raw) == 2 and raw[1] == ":":
            raw += "\\"
        try:
            p = safety.canon(raw)
        except safety.Refused as e:
            raise ApiError(400, str(e))
        if len(p) == 2:
            p += "\\"
        if not os.path.isdir(p):
            raise ApiError(400, "Ce dossier n'existe pas.")
        try:
            if winapi.is_link_stat(os.lstat(p)):
                raise ApiError(400, "Ce dossier est un lien (jonction) : analysez plutôt sa cible.")
        except OSError:
            raise ApiError(400, "Dossier inaccessible.")
        return p

    @r.route("GET", "/api/scan")
    def scan_current(req):
        cached = []
        for v in winapi.list_volumes():
            p = cache_path(v["root"])
            if os.path.exists(p):
                cached.append({"root": v["root"], "saved": os.path.getmtime(p)})
        return {"scan": state.scan.summary() if state.scan else None, "cached": cached}

    @r.route("POST", "/api/scan")
    def scan_start(req):
        root = _scan_root(req.body.get("path"))

        def work(task):
            scan = Scan(root)
            stop = threading.Event()

            def report():
                while not stop.wait(0.3):
                    if scan._workers:
                        task.progress = scan.progress()
            reporter = threading.Thread(target=report, daemon=True)
            reporter.start()
            try:
                scan.run(cancel=task.cancel)
            finally:
                stop.set()
            if scan.cancelled:
                return {"cancelled": True}
            state.scan = scan
            state.dupes = None
            task.progress = {**scan.progress(), "phase": "saving"} if scan._workers else {"phase": "saving"}
            threading.Thread(target=_persist_scan, args=(scan,), daemon=True).start()
            return scan.summary()

        return _start(state, "scan", f"Analyse de {root}", work, "scan")

    @r.route("POST", "/api/scan/load")
    def scan_load(req):
        root = _scan_root(req.body.get("path"))

        def work(task):
            task.progress = {"phase": "loading"}
            scan = Scan.load(root)
            if scan is None:
                raise RuntimeError("Aucune analyse enregistrée pour ce dossier.")
            state.scan = scan
            state.dupes = None
            return scan.summary()

        return _start(state, "scan", f"Chargement de l'analyse de {root}", work, "scan")

    @r.route("GET", "/api/scan/tree")
    def scan_tree(req):
        scan = _require_scan(state)
        path = req.q("path") or scan.root_path
        depth = req.q_int("depth", 4, 1, 8)
        min_ratio = min(0.05, max(0.0001, req.q_float("min_ratio", 0.001)))
        max_nodes = req.q_int("max_nodes", 4000, 50, 12000)
        tree = scan.tree(path, depth=depth, min_ratio=min_ratio, max_nodes=max_nodes)
        if tree is None:
            raise ApiError(404, "Dossier absent de l'analyse.", "not_found")
        return tree

    @r.route("GET", "/api/scan/children")
    def scan_children(req):
        scan = _require_scan(state)
        out = scan.children(req.q("path") or scan.root_path)
        if out is None:
            raise ApiError(404, "Dossier absent de l'analyse.", "not_found")
        return out

    @r.route("GET", "/api/scan/largest")
    def scan_largest(req):
        scan = _require_scan(state)
        cat = req.q("category")
        if cat and cat not in categories.CATEGORY_KEYS:
            raise ApiError(400, "Catégorie inconnue.")
        return {"files": scan.largest(limit=req.q_int("limit", 200, 1, 2000), category=cat,
                                      min_age_days=req.q_int("min_age_days", 0, 0, 36500),
                                      under=req.q("under"))}

    @r.route("GET", "/api/scan/types")
    def scan_types(req):
        return _require_scan(state).types()

    @r.route("GET", "/api/scan/age")
    def scan_age(req):
        return {"buckets": _require_scan(state).age()}

    # ------------------------------------------------------------------ duplicates
    @r.route("GET", "/api/dupes")
    def dupes_get(req):
        return {"result": state.dupes}

    @r.route("POST", "/api/dupes")
    def dupes_start(req):
        scan = _require_scan(state)
        try:
            min_size = int(req.body.get("min_size", config.KEEP_FILE_MIN))
        except (TypeError, ValueError):
            raise ApiError(400, "Taille minimale invalide.")
        min_size = max(config.KEEP_FILE_MIN, min_size)
        include_system = bool(req.body.get("include_system", False))

        def work(task):
            res = duplicates.find_duplicates(scan, min_size=min_size, include_system=include_system,
                                             cancel=task.cancel, progress=task.progress)
            if not res.get("cancelled"):
                state.dupes = res
            return {k: v for k, v in res.items() if k != "groups"} | {"groups_count": len(res.get("groups", []))}

        return _start(state, "dupes", "Recherche de doublons", work, "scan")

    # ------------------------------------------------------------------ cleanup
    @r.route("GET", "/api/clean")
    def clean_get(req):
        return {"analysis": cleaners.public_view(state.clean_analysis) if state.clean_analysis else None}

    @r.route("POST", "/api/clean/analyze")
    def clean_analyze(req):
        def work(task):
            res = cleaners.analyze(state.scan, is_admin=winapi.is_admin(), running=state.running_names(),
                                   cancel=task.cancel, progress=task.progress)
            state.clean_analysis = res
            return {"total": res.get("total", 0), "rules": len(res.get("rules", []))}

        return _start(state, "clean_analyze", "Recherche de fichiers à nettoyer", work, "clean")

    @r.route("GET", "/api/clean/advice")
    def clean_advice(req):
        try:
            sysinfo = system_info.get_system_info()
        except Exception:
            sysinfo = None
        return {"advice": cleaners.advice(state.scan, sysinfo)}

    @r.route("POST", "/api/clean/run")
    def clean_run(req):
        if state.clean_analysis is None:
            raise ApiError(409, "Lancez d'abord la recherche de fichiers à nettoyer.", "no_analysis")
        selection = req.body.get("selection")
        if not isinstance(selection, dict) or not selection or len(selection) > 200:
            raise ApiError(400, "Sélection invalide.")
        for k, v in selection.items():
            if not isinstance(k, str) or not (v == "all" or (isinstance(v, list) and len(v) <= 10000 and
                                                             all(isinstance(i, str) for i in v))):
                raise ApiError(400, "Sélection invalide.")
        analysis = state.clean_analysis

        def work(task):
            res = cleaners.run(analysis, selection, cancel=task.cancel, progress=task.progress)
            state.clean_analysis = None  # sizes are stale now; the UI re-analyzes
            scan = state.scan
            if scan is not None:
                task.progress = {**task.progress, "phase": "refresh"}
                for p in res.get("touched_paths", [])[:500]:
                    if scan.contains(p):
                        try:
                            scan.refresh(p if os.path.isdir(p) else os.path.dirname(p))
                        except Exception:
                            log.exception("refresh failed for %s", p)
            history.sample_usage()
            return res

        return _start(state, "clean_run", "Nettoyage", work, "delete")

    # ------------------------------------------------------------------ files
    @r.route("POST", "/api/files/recycle")
    def files_recycle(req):
        scan = _require_scan(state)
        paths = _str_list(req.body.get("paths"), "paths")
        source = req.body.get("source", "")
        if source not in ("explorer", "largest", "dupes", "types", "old"):
            raise ApiError(400, "Source inconnue.")
        sizes = {}
        for p in paths:
            node = scan.find(p)
            if node is not None:
                sizes[p] = node.size

        def work(task):
            def on_progress(res):
                task.progress = {"done": res.files, "total": len(paths), "bytes": res.bytes}
            res = safety.recycle(paths, scope_root=scan.root_path, source=source, sizes=sizes or None,
                                 cancel=task.cancel, on_progress=on_progress)
            for p in res.done:
                scan.remove_path(p)
            if state.dupes and res.done:
                _drop_from_dupes(state, res.done)
            history.sample_usage()
            return res.as_dict()

        return _start(state, "recycle", "Mise à la corbeille", work, "delete")

    @r.route("POST", "/api/recycle-bin/empty")
    def recycle_bin_empty(req):
        def work(task):
            res = safety.empty_recycle_bin()
            history.sample_usage()
            return res.as_dict()

        return _start(state, "empty_bin", "Vidage de la corbeille", work, "delete")

    # ------------------------------------------------------------------ shell
    @r.route("POST", "/api/shell/reveal")
    def shell_reveal(req):
        raw = req.body.get("path")
        try:
            p = safety.canon(raw if isinstance(raw, str) else "")
        except safety.Refused as e:
            raise ApiError(400, str(e))
        if not os.path.lexists(p):
            raise ApiError(404, "Élément introuvable (déjà supprimé ?).", "not_found")
        ok = winapi.open_folder(p) if os.path.isdir(p) and not req.body.get("select") else winapi.reveal_in_explorer(p)
        if not ok:
            raise ApiError(500, "Impossible d'ouvrir l'Explorateur.")
        return {"ok": True}

    @r.route("POST", "/api/shell/open")
    def shell_open(req):
        target = SHELL_TARGETS.get(req.body.get("target"))
        if target is None:
            raise ApiError(400, "Cible inconnue.")
        if not winapi.open_system_target(*target):
            raise ApiError(500, "Impossible d'ouvrir cet outil Windows.")
        return {"ok": True}

    # ------------------------------------------------------------------ apps & history
    @r.route("GET", "/api/apps")
    def apps_list(req):
        now = time.monotonic()
        cached = state._apps_cache
        if cached is None or now - cached[0] > 60 or req.q("refresh") == "1":
            cached = (now, apps.list_installed_apps(state.scan))
            state._apps_cache = cached
        return {"apps": cached[1]}

    @r.route("GET", "/api/history")
    def history_get(req):
        entries = journal.read(limit=req.q_int("limit", 300, 1, 5000))
        return {
            "journal": entries,
            "total_freed": journal.total_freed(),
            "snapshots": history.list_snapshots(),
            "usage": history.usage_series(req.q("root") or config.SYSTEM_DRIVE),
        }

    @r.route("GET", "/api/history/compare")
    def history_compare(req):
        res = history.compare(req.q("a", ""), req.q("b", ""))
        if res is None:
            raise ApiError(404, "Instantané introuvable.", "not_found")
        return res

    return r


def preload_cached_scan(state: AppState) -> None:
    """At start-up, reopen the saved scan of the system drive (as a normal 'scan' task)."""
    root = config.SYSTEM_DRIVE
    if state.scan is not None or not os.path.exists(cache_path(root)):
        return

    def work(task):
        task.progress = {"phase": "loading"}
        scan = Scan.load(root)
        if scan is None:
            return {"cancelled": True}
        if state.scan is None:
            state.scan = scan
        return scan.summary()

    try:
        state.tasks.start("scan", f"Chargement de l'analyse de {root}", work, group="scan")
    except Busy:
        pass


def _persist_scan(scan: Scan) -> None:
    try:
        scan.save()
        if not scan.cancelled:
            history.record_snapshot(scan)
    except Exception:
        log.exception("could not persist scan")


def _drop_from_dupes(state: AppState, removed: list[str]) -> None:
    gone = {safety.key(p) for p in removed}
    groups = []
    for g in state.dupes.get("groups", []):
        files = [f for f in g["files"] if safety.key(f["path"]) not in gone]
        if len(files) >= 2:
            groups.append({**g, "files": files, "count": len(files), "wasted": g["size"] * (len(files) - 1)})
    state.dupes = {**state.dupes, "groups": groups, "total_wasted": sum(g["wasted"] for g in groups)}
