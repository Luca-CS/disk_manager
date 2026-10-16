"""Smart cleanup rules.

Rules discover concrete targets on this PC, measure them read-only, and later
delete them ONLY through dm.safety (purge / recycle / empty_recycle_bin).
The UI never sends paths for cleanup: it sends rule ids and item ids, and
``run`` maps them back to targets from the server-side analysis.

Private per-item data lives under the ``_targets`` key (list of
{"path", "min_age_s", "keep_root", "name_glob"}); ``public_view`` strips it.
"""

import copy
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime

from . import config, safety, winapi

log = logging.getLogger("dm.cleaners")

_HOUR = 3600
_DAY = 86400
_GB = 1024 ** 3
_PRIVATE_KEYS = {"min_age_s", "keep_root", "name_glob"}

LOCAL = config.LOCALAPPDATA
ROAMING = config.APPDATA
HOME = config.USERPROFILE
SYSROOT = config.SYSTEM_ROOT
PROGDATA = config.PROGRAMDATA
SYSDRIVE = config.SYSTEM_DRIVE

_RULES: list[dict] = []


# --------------------------------------------------------------------------- helpers

class _Ctx:
    def __init__(self, scan, is_admin: bool, running: set[str], cancel):
        self.scan = scan
        self.is_admin = is_admin
        self.running = {r.lower() for r in running}
        self.cancel = cancel

    def cancelled(self) -> bool:
        return self.cancel is not None and self.cancel.is_set()


def rule(id: str, title: str, category: str, risk: str, desc: str, *, procs=(), app: str = "", mode: str = "purge"):
    """Register a discovery function returning raw items (see ``_item``)."""
    def deco(fn):
        _RULES.append({"id": id, "title": title, "category": category, "risk": risk, "description": desc,
                       "procs": [p.lower() for p in procs], "app": app or title, "mode": mode, "fn": fn})
        return fn
    return deco


def _t(path: str, min_age_s: float = 0, keep_root: bool = True, name_glob=None) -> dict:
    return {"path": path, "min_age_s": min_age_s, "keep_root": keep_root, "name_glob": name_glob}


def _item(label: str, targets: list[dict], *, note=None, admin=None, proc=None, off=False, app=None) -> dict:
    return {"label": label, "targets": targets, "note": note, "admin": admin, "proc": proc, "off": off, "app": app}


def _is_dir(p: str) -> bool:
    """Real directory (not a junction / symlink)."""
    try:
        st = os.lstat(p)
    except OSError:
        return False
    return bool(st.st_file_attributes & winapi.FILE_ATTRIBUTE_DIRECTORY) and not winapi.is_link_stat(st)


def _subdirs(p: str) -> list[tuple[str, str]]:
    """(name, path) of the real subfolders of ``p``, sorted by name."""
    out = []
    try:
        with os.scandir(p) as it:
            for e in it:
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                if st.st_file_attributes & winapi.FILE_ATTRIBUTE_DIRECTORY and not winapi.is_link_stat(st):
                    out.append((e.name, e.path))
    except OSError:
        pass
    return sorted(out, key=lambda x: x[0].lower())


def _is_file(p: str) -> bool:
    try:
        st = os.lstat(p)
    except OSError:
        return False
    return not st.st_file_attributes & winapi.FILE_ATTRIBUTE_DIRECTORY


def _short(path: str) -> str:
    for base in (LOCAL, ROAMING, HOME, SYSROOT, PROGDATA, SYSDRIVE):
        if safety.is_within(path, base) and safety.key(path) != safety.key(base):
            return os.path.relpath(path, base)
    return path


def _dir_items(pairs, **opts) -> list[dict]:
    """One item per existing folder in ``pairs`` (path, or (label, path))."""
    items = []
    for p in pairs:
        label, path = p if isinstance(p, tuple) else (_short(p), p)
        if _is_dir(path):
            items.append(_item(label, [_t(path, **{k: v for k, v in opts.items() if k in _PRIVATE_KEYS})],
                               **{k: v for k, v in opts.items() if k not in _PRIVATE_KEYS}))
    return items


def _needs_admin(path: str) -> bool:
    return safety.is_within(path, SYSROOT) or safety.is_within(path, PROGDATA)


def _item_id(paths: list[str]) -> str:
    return hashlib.sha1("|".join(paths).lower().encode("utf-8", "replace")).hexdigest()[:12]


def _fmt_date(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%d/%m/%Y")


def _measure(raw: dict, ctx: _Ctx) -> dict | None:
    """Measure the targets of a raw item; unmeasurable/empty targets are dropped."""
    targets, size, files = [], 0, 0
    for t in raw["targets"]:
        if ctx.cancelled():
            break
        b, f = safety.measure(t["path"], min_age_s=t["min_age_s"], name_glob=t["name_glob"], cancel=ctx.cancel)
        if b > 0 or f > 0:
            targets.append(t)
            size += b
            files += f
    if not targets or size <= 0:
        return None
    return {"raw": raw, "targets": targets, "size": size, "files": files}


# --------------------------------------------------------------------------- system

@rule("user_temp", "Fichiers temporaires", "system", "safe",
      "Fichiers temporaires créés par vos applications. Seuls ceux de plus de 24 h sont supprimés ; "
      "les applications les recréent au besoin.")
def _user_temp(ctx):
    return _dir_items([config.TEMP], min_age_s=24 * _HOUR)


@rule("windows_temp", "Fichiers temporaires de Windows", "system", "safe",
      "Fichiers temporaires du système (de plus de 24 h). Windows les recrée si nécessaire. "
      "Nécessite les droits administrateur.")
def _windows_temp(ctx):
    return _dir_items([os.path.join(SYSROOT, "Temp")], min_age_s=24 * _HOUR, admin=True)


@rule("crash_dumps", "Rapports de plantage", "system", "safe",
      "Copies de la mémoire enregistrées lors de plantages. Elles ne servent qu'au diagnostic ; "
      "rien ne change pour vous une fois supprimées.")
def _crash_dumps(ctx):
    return _dir_items([os.path.join(LOCAL, "CrashDumps")]) + _dir_items(
        [os.path.join(SYSROOT, "Minidump"), os.path.join(SYSROOT, "LiveKernelReports")], admin=True)


@rule("wer_reports", "Rapports d'erreurs Windows", "system", "safe",
      "Rapports d'erreurs en attente d'envoi ou déjà archivés. Sans effet sur le fonctionnement de Windows.")
def _wer(ctx):
    subs = ("ReportArchive", "ReportQueue", "Temp")
    mine = [os.path.join(LOCAL, "Microsoft", "Windows", "WER", s) for s in subs]
    shared = [os.path.join(PROGDATA, "Microsoft", "Windows", "WER", s) for s in subs]
    return _dir_items(mine) + _dir_items(shared, admin=True)


@rule("thumbnail_cache", "Cache des miniatures", "system", "safe",
      "Aperçus des images et icônes de l'Explorateur. Ils sont recréés automatiquement ; "
      "les dossiers d'images s'afficheront un peu plus lentement au début. "
      "Les fichiers utilisés par l'Explorateur sont ignorés.")
def _thumbs(ctx):
    glob = re.compile(r"^(thumbcache|iconcache)_.*\.db$", re.IGNORECASE)
    return _dir_items([os.path.join(LOCAL, "Microsoft", "Windows", "Explorer")], name_glob=glob, keep_root=True)


@rule("inet_cache", "Cache Internet de Windows", "system", "safe",
      "Fichiers web temporaires utilisés par Windows et certaines applications. Ils sont retéléchargés au besoin.")
def _inet(ctx):
    return _dir_items([os.path.join(LOCAL, "Microsoft", "Windows", "INetCache")])


@rule("windows_update", "Téléchargements Windows Update", "system", "safe",
      "Fichiers de mise à jour déjà téléchargés. Les mises à jour déjà installées ne sont pas affectées ; "
      "Windows retélécharge ce qui manque. Nécessite les droits administrateur.")
def _wu(ctx):
    return _dir_items([os.path.join(SYSROOT, "SoftwareDistribution", "Download")], admin=True)


@rule("delivery_optimization", "Cache d'optimisation de distribution", "system", "safe",
      "Copies de mises à jour partagées avec d'autres PC. Windows les retélécharge si besoin. "
      "Nécessite les droits administrateur.")
def _do(ctx):
    p = os.path.join(SYSROOT, "ServiceProfiles", "NetworkService", "AppData", "Local",
                     "Microsoft", "Windows", "DeliveryOptimization", "Cache")
    return _dir_items([p], admin=True)


@rule("shader_caches", "Caches de shaders (DirectX / GPU)", "system", "safe",
      "Effets graphiques précompilés par votre carte graphique. Les jeux recompilent leurs shaders "
      "au premier lancement (un léger ralentissement est possible).")
def _shaders(ctx):
    rel = [r"D3DSCache", r"NVIDIA\DXCache", r"NVIDIA\GLCache", r"AMD\DxCache", r"AMD\DxcCache", r"AMD\GLCache",
           r"AMD\VkCache", r"Intel\ShaderCache"]
    return _dir_items([os.path.join(LOCAL, r) for r in rel])


@rule("recycle_bin", "Corbeille", "system", "moderate",
      "Suppression définitive du contenu de la corbeille. Cette action est irréversible.", mode="recycle_bin")
def _bin(ctx):
    info = winapi.recycle_bin_info()
    if not info.get("ok") or info["size"] <= 0:
        return []
    it = _item("Corbeille", [], note=f"{info['count']} élément(s)")
    it.update(size=info["size"], files=info["count"], path="Corbeille")
    return [it]


# --------------------------------------------------------------------------- browsers

_PROFILE_RE = re.compile(r"^(Default|Profile \d+|Guest Profile|System Profile)$", re.IGNORECASE)
_CHROMIUM_CACHE_DIRS = ("Cache", "Code Cache", "GPUCache", "DawnCache", "DawnGraphiteCache", "DawnWebGPUCache",
                        os.path.join("Service Worker", "ScriptCache"))
_BROWSER_LEVEL_DIRS = ("ShaderCache", "GrShaderCache", "GraphiteDawnCache")
_BROWSERS: list[tuple] = []  # (id, name, exe, user_data, cache_base)


def _profile_names(user_data: str) -> dict[str, str]:
    try:
        with open(os.path.join(user_data, "Local State"), "r", encoding="utf-8") as f:
            info = json.load(f).get("profile", {}).get("info_cache", {})
        return {k: str(v.get("name") or k) for k, v in info.items() if isinstance(v, dict)}
    except (OSError, ValueError, AttributeError):
        return {}


def _chromium_items(name: str, exe: str, user_data: str, base: str) -> list[dict]:
    if not _is_dir(base):
        return []
    names = _profile_names(user_data)
    profiles = [(n, p) for n, p in _subdirs(base) if _PROFILE_RE.match(n)]
    if _is_dir(os.path.join(base, "Cache")) or _is_dir(os.path.join(base, "Code Cache")):
        profiles.insert(0, ("", base))  # Opera keeps a profile directly in its folder
    items = []
    for dirname, path in profiles:
        pname = names.get(dirname) or dirname or "principal"
        caches = [_t(os.path.join(path, c)) for c in _CHROMIUM_CACHE_DIRS if _is_dir(os.path.join(path, c))]
        if caches:
            items.append(_item(f"Profil « {pname} » — cache", caches, proc=exe, app=name))
        cs = os.path.join(path, "Service Worker", "CacheStorage")
        if _is_dir(cs):
            items.append(_item(f"Profil « {pname} » — données hors-ligne des sites", [_t(cs)], proc=exe, app=name,
                               note="Données hors-ligne des sites (re-téléchargées au besoin)", off=True))
    shared = [_t(os.path.join(base, d)) for d in _BROWSER_LEVEL_DIRS if _is_dir(os.path.join(base, d))]
    if shared:
        items.append(_item("Caches partagés du navigateur", shared, proc=exe, app=name))
    return items


def _register_chromium(id_: str, name: str, exe: str, user_data: str, cache_base: str | None = None) -> None:
    base = cache_base or user_data
    _BROWSERS.append((user_data, base))
    desc = (f"Fichiers de {name} mis en cache pour accélérer l'ouverture des sites. Votre historique, vos mots de "
            "passe, vos cookies et vos sessions ne sont pas touchés. Les sites se rechargeront un peu plus "
            "lentement la première fois.")

    @rule(id_, f"Cache de {name}", "browsers", "safe", desc, procs=[exe], app=name)
    def _fn(ctx, _n=name, _e=exe, _u=user_data, _b=base):
        return _chromium_items(_n, _e, _u, _b)


_register_chromium("chrome_cache", "Google Chrome", "chrome.exe", os.path.join(LOCAL, "Google", "Chrome", "User Data"))
_register_chromium("edge_cache", "Microsoft Edge", "msedge.exe", os.path.join(LOCAL, "Microsoft", "Edge", "User Data"))
_register_chromium("brave_cache", "Brave", "brave.exe",
                   os.path.join(LOCAL, "BraveSoftware", "Brave-Browser", "User Data"))
_register_chromium("vivaldi_cache", "Vivaldi", "vivaldi.exe", os.path.join(LOCAL, "Vivaldi", "User Data"))
_register_chromium("opera_cache", "Opera", "opera.exe", os.path.join(ROAMING, "Opera Software", "Opera Stable"),
                   os.path.join(LOCAL, "Opera Software", "Opera Stable"))


@rule("firefox_cache", "Cache de Firefox", "browsers", "safe",
      "Fichiers de Firefox mis en cache pour accélérer les sites. Votre historique, vos mots de passe, vos "
      "cookies et vos sessions ne sont pas touchés. Les sites se rechargeront un peu plus lentement la première "
      "fois.", procs=["firefox.exe"], app="Firefox")
def _firefox(ctx):
    items = []
    for name, path in _subdirs(os.path.join(LOCAL, "Mozilla", "Firefox", "Profiles")):
        ts = [_t(os.path.join(path, d)) for d in ("cache2", "startupCache", "thumbnails")
              if _is_dir(os.path.join(path, d))]
        if ts:
            items.append(_item(f"Profil « {name.split('.', 1)[-1]} » — cache", ts, proc="firefox.exe", app="Firefox"))
    return items


# --------------------------------------------------------------------------- apps

_ELECTRON_CACHES = ("Cache", "Code Cache", "GPUCache", "DawnCache", "DawnGraphiteCache", "DawnWebGPUCache",
                    "GrShaderCache", "ShaderCache", os.path.join("Service Worker", "CacheStorage"),
                    os.path.join("Service Worker", "ScriptCache"))


def _looks_electron(p: str) -> bool:
    """Chromium user-data signature: "Local State", or Local Storage + (Preferences or Session Storage)."""
    if _is_file(os.path.join(p, "Local State")):
        return True
    return _is_dir(os.path.join(p, "Local Storage")) and (
        _is_file(os.path.join(p, "Preferences")) or _is_dir(os.path.join(p, "Session Storage")))


@rule("electron_caches", "Caches d'applications (Discord, Teams, VS Code…)", "apps", "safe",
      "Caches temporaires des applications basées sur Chromium (Discord, Teams, VS Code, Slack…). "
      "Vos comptes et réglages sont conservés ; l'application recrée son cache au prochain lancement.")
def _electron(ctx):
    skip = [os.path.join(LOCAL, "Packages"), os.path.join(LOCAL, "Microsoft", "Windows"), os.path.join(LOCAL, "Temp"),
            *(u for u, _ in _BROWSERS), *(b for _, b in _BROWSERS)]
    skipped = lambda p: any(safety.is_within(p, s) for s in skip)  # noqa: E731
    found = []
    for base in (ROAMING, LOCAL):
        for n1, p1 in _subdirs(base):
            if skipped(p1):
                continue
            if _looks_electron(p1):
                found.append((p1, base))
                continue
            for n2, p2 in _subdirs(p1):
                if not skipped(p2) and _looks_electron(p2):
                    found.append((p2, base))
    items = []
    for path, base in found:
        ts = [_t(os.path.join(path, c)) for c in _ELECTRON_CACHES if _is_dir(os.path.join(path, c))]
        if ts:
            last = os.path.basename(path)
            items.append(_item(os.path.relpath(path, base), ts, proc=last.lower() + ".exe", app=last))
    return items


@rule("vscode_cpptools", "Cache IntelliSense C/C++ (VS Code)", "apps", "safe",
      "Cache de l'analyse du code C/C++ de VS Code. Il est reconstruit automatiquement ; l'autocomplétion peut "
      "être un peu plus lente à la prochaine ouverture d'un projet.", procs=["code.exe"], app="VS Code")
def _cpptools(ctx):
    return _dir_items([os.path.join(LOCAL, "Microsoft", "vscode-cpptools", "ipch")], proc="code.exe")


@rule("vscode_extras", "Fichiers résiduels de VS Code", "apps", "safe",
      "Anciens paquets d'extensions, caches de code d'anciennes versions et journaux de plus de 7 jours. "
      "VS Code n'en a plus besoin et les recrée si nécessaire.", procs=["code.exe"], app="VS Code")
def _vscode(ctx):
    code = os.path.join(ROAMING, "Code")
    items = _dir_items([os.path.join(code, "CachedExtensionVSIXs")], proc="code.exe")
    subs = _subdirs(os.path.join(code, "CachedData"))
    if len(subs) > 1:
        def mtime(p):
            try:
                return os.lstat(p).st_mtime
            except OSError:
                return 0
        newest = max(subs, key=lambda s: mtime(s[1]))
        old = [_t(p, keep_root=False) for n, p in subs if p != newest[1]]
        items.append(_item("Anciens caches de code", old, proc="code.exe"))
    items += _dir_items([os.path.join(code, "logs")], min_age_s=7 * _DAY, proc="code.exe")
    return items


def _ver(name: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", name[4:]))


@rule("squirrel_old_versions", "Anciennes versions d'applications", "apps", "moderate",
      "Anciennes versions laissées par les mises à jour d'applications (Discord, Slack…). "
      "L'application tourne sur la version la plus récente.")
def _squirrel(ctx):
    items = []
    for appname, apath in _subdirs(LOCAL):
        if not _is_file(os.path.join(apath, "Update.exe")):
            continue
        vers = [(n, p) for n, p in _subdirs(apath) if re.match(r"^app-\d", n, re.IGNORECASE)]
        if len(vers) < 2:
            continue
        vers.sort(key=lambda x: _ver(x[0]))
        for n, p in vers[:-1]:
            items.append(_item(f"{appname} {n[4:]}", [_t(p, keep_root=False)], proc=appname.lower() + ".exe",
                               app=appname))
    return items


@rule("updater_caches", "Installeurs de mises à jour téléchargés", "apps", "moderate",
      "Installeurs de mises à jour déjà téléchargés par vos applications. Ils sont retéléchargés si une mise à "
      "jour est encore nécessaire.")
def _updaters(ctx):
    items = []
    for n, p in _subdirs(LOCAL):
        if n.lower().endswith("-updater"):
            items.append(_item(n, [_t(p)], proc=n.lower() + ".exe", app=n))
    # ProgramData files are usually owned by an updater service: admin needed.
    items += _dir_items([os.path.join(PROGDATA, "McNeel", "McNeelUpdate", "DownloadCache")], admin=True)
    items += _dir_items([os.path.join(PROGDATA, "NVIDIA Corporation", "Downloader")], admin=True)
    return items


@rule("spotify_cache", "Cache Spotify", "apps", "safe",
      "Musique mise en cache par Spotify. Elle sera retéléchargée à l'écoute ; votre bibliothèque et vos "
      "téléchargements hors-ligne en ligne ne sont pas concernés.", procs=["spotify.exe"], app="Spotify")
def _spotify(ctx):
    return _dir_items([os.path.join(LOCAL, "Spotify", "Data"), os.path.join(LOCAL, "Spotify", "Storage")],
                      proc="spotify.exe")


# --------------------------------------------------------------------------- dev

def _local(*parts): return os.path.join(LOCAL, *parts)
def _home(*parts): return os.path.join(HOME, *parts)


@rule("npm_cache", "Cache npm", "dev", "safe",
      "Paquets JavaScript téléchargés par npm. npm les retélécharge au besoin ; la prochaine installation "
      "sera un peu plus longue.")
def _npm(ctx):
    return _dir_items([_local("npm-cache", "_cacache"), _local("npm-cache", "_npx")])


@rule("yarn_pnpm_cache", "Caches Yarn / pnpm", "dev", "moderate",
      "Paquets JavaScript mis en cache par Yarn et pnpm. Ils seront retéléchargés lors des prochaines "
      "installations (consomme de la bande passante).")
def _yarn(ctx):
    return _dir_items([_local("Yarn", "Cache"), _local("pnpm-cache"), _local("pnpm", "store")])


@rule("python_caches", "Caches pip / uv", "dev", "safe",
      "Paquets Python téléchargés par pip et uv. Ils seront retéléchargés au besoin.")
def _python(ctx):
    return _dir_items([_local("pip", "cache"), _local("uv", "cache")])


@rule("nuget_cache", "Caches NuGet (.NET)", "dev", "moderate",
      "Caches de paquets .NET. Les paquets globaux (~\\.nuget\\packages) sont restaurés au prochain build, "
      "ce qui demande une connexion.")
def _nuget(ctx):
    items = _dir_items([_local("NuGet", "v3-cache"), _local("NuGet", "plugins-cache")])
    items += _dir_items([_home(".nuget", "packages")], off=True,
                        note="Paquets restaurés au prochain build (connexion nécessaire).")
    return items


@rule("jvm_caches", "Caches Gradle / Maven", "dev", "moderate",
      "Dépendances Java téléchargées par Gradle et Maven. Elles seront retéléchargées au prochain build.")
def _jvm(ctx):
    return _dir_items([_home(".gradle", "caches"), _home(".m2", "repository")])


@rule("rust_go_caches", "Caches Rust / Go", "dev", "moderate",
      "Caches de compilation et dépendances Rust (Cargo) et Go. Ils seront retéléchargés / recompilés au besoin.")
def _rustgo(ctx):
    return _dir_items([_home(".cargo", "registry", "cache"), _home(".cargo", "registry", "src"),
                       _local("go-build"), _home("go", "pkg", "mod")])


@rule("ml_caches", "Modèles d'IA téléchargés", "dev", "review",
      "Modèles téléchargés par Hugging Face, PyTorch et Keras. Plusieurs Go à re-télécharger si vous réutilisez "
      "ces modèles : vérifiez avant de supprimer.")
def _ml(ctx):
    return _dir_items([_home(".cache", "huggingface"), _home(".cache", "torch"), _home(".keras", "models")])


@rule("conda_pkgs", "Paquets Conda en cache", "dev", "review",
      "Archives de paquets conservées par Conda. Pour un nettoyage propre, vous pouvez aussi lancer "
      "« conda clean --all » dans un terminal.")
def _conda(ctx):
    return _dir_items([_home("anaconda3", "pkgs"), _home("miniconda3", "pkgs"), _local("conda", "conda", "pkgs")])


@rule("msys2_cache", "Paquets MSYS2 en cache", "dev", "safe",
      "Archives de paquets téléchargées par pacman (MSYS2). Elles sont retéléchargées lors d'une réinstallation.")
def _msys2(ctx):
    return _dir_items([os.path.join(SYSDRIVE, "msys64", "var", "cache", "pacman", "pkg")])


@rule("inactive_node_modules", "Dossiers node_modules inactifs", "dev", "review",
      "Dépendances de projets JavaScript non modifiés depuis 90 jours. « npm install » les recrée si vous "
      "rouvrez le projet.")
def _node_modules(ctx):
    scan = ctx.scan
    if scan is None:
        return []
    cutoff = time.time() - 90 * _DAY
    items = []
    for node in scan.iter_nodes():
        if node.name.lower() != "node_modules" or node.parent is None:
            continue
        anc, nested = node.parent, False
        while anc is not None:
            if anc.name.lower() == "node_modules":
                nested = True
                break
            anc = anc.parent
        if nested or node.size <= 0:
            continue
        path = node.path()
        project = os.path.dirname(path)
        if safety.is_within(path, ROAMING) or safety.is_within(path, LOCAL):
            continue
        try:
            if os.stat(os.path.join(project, "package.json")).st_mtime > cutoff:
                continue
            safety.check_purgeable(path)
        except (OSError, safety.Refused):
            continue
        label = os.path.relpath(project, HOME) if safety.is_within(project, HOME) else project
        it = _item(label, [_t(path, keep_root=False)], off=True)
        it.update(size=node.size, files=node.file_count, path=path)
        items.append(it)
    return items


# --------------------------------------------------------------------------- downloads

_ARCHIVE_EXT = {".exe", ".msi", ".msix", ".appx", ".zip", ".rar", ".7z", ".iso", ".img", ".dmg", ".tar", ".gz", ".tgz"}


def _downloads_dirs() -> list[str]:
    dirs = [d for d in safety.PERSONAL_DIRS if os.path.basename(d).lower() == "downloads" and _is_dir(d)]
    if not dirs and _is_dir(_home("Downloads")):
        dirs = [_home("Downloads")]
    return list(dict.fromkeys(dirs))


@rule("old_downloads", "Installeurs et archives dans Téléchargements", "downloads", "review",
      "Installeurs et archives de plus de 30 jours dans votre dossier Téléchargements. Ils sont envoyés à la "
      "corbeille : vous pouvez les restaurer.", mode="recycle")
def _downloads(ctx):
    cutoff = time.time() - 30 * _DAY
    items, seen = [], set()
    for d in _downloads_dirs():
        level = [d] + [p for _, p in _subdirs(d)]
        for folder in level:
            try:
                it = os.scandir(folder)
            except OSError:
                continue
            with it:
                for e in it:
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if (st.st_file_attributes & winapi.FILE_ATTRIBUTE_DIRECTORY or winapi.is_link_stat(st)
                            or os.path.splitext(e.name)[1].lower() not in _ARCHIVE_EXT or st.st_mtime > cutoff
                            or safety.key(e.path) in seen):
                        continue
                    seen.add(safety.key(e.path))
                    x = _item(e.name, [_t(e.path)], note=f"Téléchargé le {_fmt_date(st.st_mtime)}", off=True)
                    x.update(size=st.st_size, files=1, path=e.path)
                    items.append(x)
    items.sort(key=lambda x: x["size"], reverse=True)
    return items[:500]


# --------------------------------------------------------------------------- analyze

def _build_rule(r: dict, ctx: _Ctx) -> dict | None:
    raws = r["fn"](ctx)
    items, ids = [], set()
    for raw in raws:
        if ctx.cancelled():
            break
        if "size" in raw:  # pre-measured (scan, recycle, bin)
            m = {"raw": raw, "targets": raw["targets"], "size": raw["size"], "files": raw.get("files", 0)}
        else:
            m = _measure(raw, ctx)
        if m is None or m["size"] <= 0:
            continue
        targets = m["targets"]
        paths = [t["path"] for t in targets] or [raw["label"]]
        iid = _item_id(paths)
        if iid in ids:
            continue
        ids.add(iid)
        admin = raw["admin"] if raw["admin"] is not None else any(_needs_admin(t["path"]) for t in targets)
        blocked = admin and not ctx.is_admin
        procs = [raw["proc"]] if raw["proc"] else r["procs"]
        run_procs = sorted(p for p in procs if p in ctx.running)
        note = raw["note"]
        if run_procs:
            warn = f"{raw['app'] or r['app']} est ouvert : fermez-le pour un nettoyage complet."
            note = f"{warn} {note}" if note else warn
        items.append({
            "id": iid, "label": raw["label"], "path": raw.get("path") or paths[0], "size": m["size"],
            "files": m["files"], "note": note, "requires_admin": admin, "blocked": blocked,
            "default_selected": r["risk"] == "safe" and not run_procs and not blocked and not raw["off"],
            "_targets": targets, "_running": run_procs,
        })
    if not items:
        return None
    items.sort(key=lambda i: i["size"], reverse=True)
    all_admin = all(i["requires_admin"] for i in items)
    ok = [i for i in items if not i["blocked"]]
    running = sorted({p for i in items for p in i.pop("_running")})
    return {
        "id": r["id"], "title": r["title"], "category": r["category"], "description": r["description"],
        "risk": r["risk"], "mode": r["mode"], "requires_admin": all_admin,
        "blocked": all_admin and not ctx.is_admin, "running": running,
        "size": sum(i["size"] for i in ok), "files": sum(i["files"] for i in ok), "items": items,
    }


def analyze(scan=None, *, is_admin: bool, running: set[str], cancel=None, progress=None) -> dict:
    """Discover and measure every rule. Read-only.

    ``scan``: current dm.scanner.Scan or None (scan-based rules are skipped when None).
    ``running``: lower-cased process image names currently running (app window excluded).
    ``progress``: dict updated in place: {"done": int, "total": int, "current": "rule title"}.

    Returns {"rules": [...], "total": int, "analyzed_at": float, "is_admin": bool, "dry_run": bool}; see
    docs/CLEANERS_SPEC.md for the rule / item shapes. Rules without items or with no data are omitted.
    """
    ctx = _Ctx(scan, is_admin, running, cancel)
    out = []
    n = len(_RULES)
    for i, r in enumerate(_RULES):
        if ctx.cancelled():
            break
        if progress is not None:
            progress.update(done=i, total=n, current=r["title"])
        try:
            built = _build_rule(r, ctx)
        except Exception:
            log.exception("Rule %s failed during analysis", r["id"])
            continue
        if built is not None:
            out.append(built)
    else:
        if progress is not None:
            progress.update(done=n, total=n, current="")
    out.sort(key=lambda x: x["size"], reverse=True)
    return {"rules": out, "total": sum(x["size"] for x in out), "analyzed_at": time.time(),
            "is_admin": is_admin, "dry_run": safety.DRY_RUN}


# --------------------------------------------------------------------------- run

def run(analysis: dict, selection: dict, *, cancel=None, progress=None) -> dict:
    """Execute a selection against a previous ``analyze`` result.

    ``selection``: {rule_id: "all" | [item_id, ...]}. Unknown ids are ignored; blocked items are skipped.
    Targets come only from the server-side ``analysis``, never from the selection.

    Returns {"freed", "files", "dry_run", "rules": [{"id", "title", "bytes", "files", "skipped_in_use",
    "skipped_recent", "errors", "refused"}], "touched_paths": [str]}.
    """
    selection = selection if isinstance(selection, dict) else {}
    chosen = []
    for r in analysis.get("rules", []):
        sel = selection.get(r["id"])
        if sel == "all":
            items = [i for i in r["items"] if not i.get("blocked")]
        elif isinstance(sel, (list, tuple, set)):
            wanted = {s for s in sel if isinstance(s, str)}
            items = [i for i in r["items"] if i["id"] in wanted and not i.get("blocked")]
        else:
            continue
        if items:
            chosen.append((r, items))
    result = {"freed": 0, "files": 0, "dry_run": safety.DRY_RUN, "rules": [], "touched_paths": []}
    n = len(chosen)
    for i, (r, items) in enumerate(chosen):
        if cancel is not None and cancel.is_set():
            break
        if progress is not None:
            progress.update(done=i, total=n, current=r["title"])
        targets = [t for it in items for t in it.get("_targets", [])]
        on_prog = None
        if progress is not None:
            def on_prog(res, _i=i, _t=r["title"]):
                progress.update(done=_i, total=n, current=_t, bytes=res.bytes)
        try:
            if r["mode"] == "recycle_bin":
                res = safety.empty_recycle_bin()
            elif r["mode"] == "recycle":
                sizes = {t["path"]: it["size"] for it in items for t in it.get("_targets", [])}
                res = safety.recycle([t["path"] for t in targets], source=f"clean:{r['id']}", sizes=sizes,
                                     cancel=cancel, on_progress=on_prog)
            else:
                res = safety.purge_many(targets, rule_id=r["id"], cancel=cancel, on_progress=on_prog)
        except Exception as e:
            log.exception("Rule %s failed during cleanup", r["id"])
            result["rules"].append({"id": r["id"], "title": r["title"], "bytes": 0, "files": 0,
                                    "skipped_in_use": 0, "skipped_recent": 0,
                                    "errors": [{"path": "", "reason": str(e)}], "refused": []})
            continue
        result["rules"].append({"id": r["id"], "title": r["title"], "bytes": res.bytes, "files": res.files,
                                "skipped_in_use": res.skipped_in_use, "skipped_recent": res.skipped_recent,
                                "errors": res.errors, "refused": res.refused})
        result["freed"] += res.bytes
        result["files"] += res.files
        result["touched_paths"] += [t["path"] for t in targets]
    if progress is not None:
        progress.update(done=n, total=n, current="")
    return result


def _strip(obj):
    if isinstance(obj, dict):
        return {k: _strip(v) for k, v in obj.items() if not k.startswith("_") and k not in _PRIVATE_KEYS}
    if isinstance(obj, list):
        return [_strip(v) for v in obj]
    return copy.deepcopy(obj)


def public_view(analysis: dict) -> dict:
    """Strip private keys (min_age_s, keep_root, name_glob, _targets...) before sending to the UI."""
    return _strip(analysis)


# --------------------------------------------------------------------------- advice

def _card(id_, title, body, severity="info", size=None, action=None) -> dict:
    return {"id": id_, "title": title, "body": body, "size": size, "severity": severity, "action": action}


def _find_vhdx() -> list[str]:
    roots = []
    for _, p in _subdirs(os.path.join(LOCAL, "wsl")):
        roots.append(p)
    for _, p in _subdirs(os.path.join(LOCAL, "Packages")):
        roots.append(os.path.join(p, "LocalState"))
    for _, p in _subdirs(os.path.join(LOCAL, "Docker", "wsl")):
        roots.append(p)
    out = []
    for r in roots:
        f = os.path.join(r, "ext4.vhdx")
        if _is_file(f):
            out.append(f)
    return out


def advice(scan=None, system: dict | None = None) -> list[dict]:
    """Guidance cards for big items the app must not delete itself.

    Each: {"id", "title", "body" (French), "size": int | None, "severity": "info"|"tip"|"warning",
           "action": None | {"type": "open", "target": <SHELL_TARGETS key>, "label"}
                          | {"type": "command", "command", "label", "admin"}}
    """
    cards = []
    special = {}
    try:
        with os.scandir(SYSDRIVE) as it:
            for e in it:
                if e.name.lower() in ("hiberfil.sys", "pagefile.sys"):
                    try:
                        special[e.name.lower()] = e.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
    except OSError:
        pass

    if "hiberfil.sys" in special:
        cards.append(_card(
            "hibernation", "Fichier d'hibernation", "Windows enregistre ici l'état de la mémoire pour l'hibernation "
            "et le démarrage rapide. Vous pouvez le réduire (le démarrage rapide reste actif). "
            "« powercfg /h off » le supprime complètement mais désactive le démarrage rapide.",
            "tip", special["hiberfil.sys"],
            {"type": "command", "command": "powercfg /h /type reduced", "label": "Réduire le fichier", "admin": True}))

    try:
        ram = winapi.memory_status()["total"]
    except Exception:
        ram = 0
    if ram and special.get("pagefile.sys", 0) > 1.5 * ram:
        cards.append(_card(
            "pagefile", "Fichier d'échange volumineux", "Le fichier d'échange (mémoire virtuelle) est géré par "
            "Windows et dépasse nettement votre mémoire vive. Vous pouvez en réduire la taille dans "
            "« Mémoire virtuelle », sans le supprimer.", "tip", special["pagefile.sys"],
            {"type": "open", "target": "performance_options", "label": "Ouvrir les options de performance"}))

    vhdx = []
    for p in _find_vhdx():
        try:
            vhdx.append((p, os.lstat(p).st_size))
        except OSError:
            pass
    if vhdx:
        listing = " ; ".join(f"{p} ({s / _GB:.1f} Go)" for p, s in vhdx)
        cards.append(_card(
            "wsl_disks", "Disques virtuels WSL / Docker", "Ces disques virtuels grandissent mais ne rétrécissent "
            f"jamais seuls : {listing}. Arrêtez WSL (« wsl --shutdown »), puis activez le mode sparse : "
            "« wsl --manage <distribution> --set-sparse true ». Sinon, compactez avec diskpart : "
            "select vdisk file=\"<chemin>\", attach vdisk readonly, compact vdisk, detach vdisk.",
            "tip", sum(s for _, s in vhdx),
            {"type": "command", "command": "wsl --shutdown", "label": "Arrêter WSL", "admin": False}))

    cards.append(_card(
        "component_store", "Magasin de composants (WinSxS)", "Le dossier WinSxS ne doit jamais être vidé à la main : "
        "seul Windows peut le nettoyer sans risque, via « Nettoyer les fichiers système ». En ligne de commande "
        "(administrateur) : Dism /Online /Cleanup-Image /StartComponentCleanup.", "info", None,
        {"type": "open", "target": "cleanmgr", "label": "Nettoyer les fichiers système"}))

    old = []
    for name in ("Windows.old", "$WINDOWS.~BT"):
        p = os.path.join(SYSDRIVE, name)
        if os.path.exists(p):
            size = None
            try:
                node = scan.find(p) if scan is not None else None
                size = node.size if node is not None else None
            except Exception:
                pass
            old.append(size)
    if old:
        known = [s for s in old if s]
        cards.append(_card(
            "windows_old", "Anciennes installations de Windows", "Windows conserve l'installation précédente "
            "(Windows.old) après une mise à niveau. Utilisez le nettoyage de disque, option « Installations "
            "précédentes de Windows », pour la supprimer proprement.", "tip", sum(known) if known else None,
            {"type": "open", "target": "cleanmgr", "label": "Ouvrir le nettoyage de disque"}))

    cards.append(_card(
        "restore_points", "Points de restauration", "Les points de restauration occupent un espace caché "
        "(« System Volume Information »). Vous pouvez limiter l'espace réservé ou supprimer les anciens points "
        "dans la protection du système.", "info", None,
        {"type": "open", "target": "system_protection", "label": "Protection du système"}))
    cards.append(_card(
        "storage_sense", "Assistant de stockage", "Activez l'Assistant de stockage pour que Windows libère "
        "automatiquement les fichiers temporaires et vide la corbeille régulièrement.", "tip", None,
        {"type": "open", "target": "storagesense", "label": "Ouvrir l'Assistant de stockage"}))

    try:
        recov = [p for d in (system or {}).get("disks", []) for p in d.get("partitions", [])
                 if p.get("kind") == "recovery" and (p.get("size") or 0) > 5 * _GB]
    except Exception:
        recov = []
    if recov:
        cards.append(_card(
            "oem_recovery", "Partition de récupération du fabricant", "Cette partition contient l'image d'usine "
            "de votre PC. Ne la supprimez pas sans disposer d'une clé USB de récupération.", "info",
            sum(p["size"] for p in recov)))

    try:
        if scan is not None and len(scan.root_path) == 3:
            un = ((scan.summary().get("volume") or {}).get("unscanned") or 0)
            if un > 2 * _GB:
                cards.append(_card(
                    "unscanned", "Espace non analysé", "Une partie du disque n'est pas visible dans l'analyse : "
                    "points de restauration, table des fichiers (MFT), fichiers système cachés ou fichiers "
                    "d'autres utilisateurs.", "info", un))
    except Exception:
        log.exception("unscanned advice failed")
    return cards
