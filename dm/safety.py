"""The only module allowed to delete anything.

Two operations exist, both validated server-side whatever the UI sends:

* ``recycle(paths)`` — user-chosen files/folders go to the Windows recycle bin
  (reversible). Refused for system locations, profile roots, links...
* ``purge(target)`` — permanent deletion, reserved for regenerable caches
  declared by cleanup rules. Only allowed inside folders that are caches by
  construction (allow-list of name patterns), never inside personal folders,
  never through a junction or symlink.

Every completed operation is written to the journal. Set ``DM_DRY_RUN=1`` to
simulate deletions (nothing is touched, results are computed as if it was).
"""

import os
import re
import sys
import threading
import time
import winreg
from dataclasses import dataclass, field

from . import config, journal, winapi

DRY_RUN = os.environ.get("DM_DRY_RUN") == "1"


class Refused(Exception):
    """A path failed validation. The message is user-facing (French)."""


# --------------------------------------------------------------------------- paths

_DRIVE_ABS = re.compile(r"^[A-Za-z]:\\")
_FORBIDDEN_CHARS = set('*?"<>|')


def canon(path: str) -> str:
    """Normalize a user-supplied path or raise Refused.

    Accepts only absolute local drive-letter paths. Rejects device / UNC paths,
    wildcards, alternate data streams, and components ending with a dot or a
    space (Windows silently strips those, which would defeat prefix checks).
    """
    if not isinstance(path, str) or not path or len(path) > 32_000 or "\x00" in path:
        raise Refused("Chemin invalide.")
    p = path.replace("/", "\\")
    if not _DRIVE_ABS.match(p):
        raise Refused("Seuls les chemins absolus locaux (ex. C:\\...) sont acceptés.")
    rest = p[2:]
    if ":" in rest or any(c in _FORBIDDEN_CHARS for c in rest):
        raise Refused("Le chemin contient des caractères non autorisés.")
    p = os.path.normpath(p)
    if not _DRIVE_ABS.match(p):
        raise Refused("Chemin invalide.")
    for part in p[3:].split("\\"):
        if part and (part.endswith(".") or part.endswith(" ")):
            raise Refused("Composant de chemin ambigu (point ou espace final).")
    return p


def key(path: str) -> str:
    return os.path.normcase(path).rstrip("\\")


def is_within(child: str, parent: str) -> bool:
    c, p = key(child), key(parent)
    return c == p or c.startswith(p + "\\")


def _is_drive_root(p: str) -> bool:
    return len(key(p)) <= 2  # "c:"


# ------------------------------------------------------------------ known locations

def _user_shell_folders() -> list[str]:
    """Personal folders (Documents, Desktop, ...) including redirections."""
    names = ["Desktop", "Personal", "My Pictures", "My Music", "My Video", "Favorites",
             "{374DE290-123F-4565-9164-39C4925E467B}"]  # Downloads
    out = []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as k:
            for n in names:
                try:
                    val, _ = winreg.QueryValueEx(k, n)
                    out.append(os.path.normpath(os.path.expandvars(val)))
                except OSError:
                    pass
    except OSError:
        pass
    home = config.USERPROFILE
    for n in ("Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos", "Favorites", "OneDrive",
              "Contacts", "Links", "Saved Games", "Searches", "3D Objects"):
        out.append(os.path.join(home, n))
    for v in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        if os.environ.get(v):
            out.append(os.path.normpath(os.environ[v]))
    return sorted(set(out), key=str.lower)


PERSONAL_DIRS = _user_shell_folders()
SD = config.SYSTEM_DRIVE  # "C:\\"

# Never deletable, nor anything inside (except explicit system cache roots for purge).
PROTECTED_TREES = [
    config.SYSTEM_ROOT,
    config.PROGRAM_FILES,
    config.PROGRAM_FILES_X86,
    os.environ.get("ProgramW6432", config.PROGRAM_FILES),
    config.PROGRAMDATA,
    os.path.join(SD, "Recovery"),
    os.path.join(SD, "System Volume Information"),
    os.path.join(SD, "$Recycle.Bin"),
    os.path.join(SD, "Boot"),
    os.path.join(SD, "EFI"),
    os.path.join(SD, "$WinREAgent"),
    os.path.join(SD, "$SysReset"),
    os.path.join(SD, "$WINDOWS.~BT"),
    os.path.join(SD, "$Windows.~WS"),
    os.path.join(SD, "Windows.old"),
    os.path.join(SD, "Config.Msi"),
    os.path.join(SD, "Documents and Settings"),
    os.path.join(SD, "Users", "Default"),
    os.path.join(SD, "Users", "All Users"),
    os.path.join(config.LOCALAPPDATA, "Microsoft", "Windows"),
    os.path.join(config.LOCALAPPDATA, "Microsoft", "WindowsApps"),
    os.path.join(config.LOCALAPPDATA, "Packages"),
    os.path.join(config.APPDATA, "Microsoft", "Windows"),
    os.path.join(config.APPDATA, "Microsoft", "Protect"),
    os.path.join(config.APPDATA, "Microsoft", "Crypto"),
    os.path.join(config.APPDATA, "Microsoft", "SystemCertificates"),
    os.path.join(config.USERPROFILE, ".ssh"),
    os.path.join(config.USERPROFILE, ".gnupg"),
    str(config.ROOT_DIR),
    sys.base_prefix,
    sys.prefix,
]

# Deletable content, but the folder itself must stay. User profile roots are
# added at check time (see profile_roots / _protected_exact).
PROTECTED_EXACT = [
    os.path.join(SD, "Users"),
    os.path.join(SD, "Users", "Public"),
    config.USERPROFILE,
    os.path.join(config.USERPROFILE, "AppData"),
    config.LOCALAPPDATA,
    config.APPDATA,
    os.path.join(config.USERPROFILE, "AppData", "LocalLow"),
    config.TEMP,
    *PERSONAL_DIRS,
]

# Files at the root of the system drive that must never be touched.
PROTECTED_ROOT_FILES = {"pagefile.sys", "hiberfil.sys", "swapfile.sys", "bootmgr", "bootnxt", "dumpstack.log"}
_PROFILE_HIVES = re.compile(r"^(ntuser\.(dat|ini|pol)|usrclass\.dat)", re.IGNORECASE)

_PROFILE_LIST_KEY = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList"
_PROFILES_TTL_S = 2.0
_profiles_lock = threading.Lock()
_profiles_cache: tuple[float, tuple[str, ...]] = (float("-inf"), ())


def _expand_profile_path(value) -> str | None:
    p = config.expand_system_vars(value)  # not os.path.expandvars: the environment is the user's
    return p if p and not _is_drive_root(p) else None


def _read_profile_list() -> tuple[list[str], str | None]:
    """(ProfileImagePath of every account, ProfilesDirectory) from HKLM ProfileList."""
    paths, profiles_dir = [], None
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _PROFILE_LIST_KEY) as k:
            try:
                profiles_dir = _expand_profile_path(winreg.QueryValueEx(k, "ProfilesDirectory")[0])
            except OSError:
                pass
            i = 0
            while True:
                try:
                    sid = winreg.EnumKey(k, i)
                except OSError:
                    break
                i += 1
                try:
                    with winreg.OpenKey(k, sid) as sk:
                        p = _expand_profile_path(winreg.QueryValueEx(sk, "ProfileImagePath")[0])
                except OSError:
                    continue
                if p:
                    paths.append(p)
    except OSError:
        pass
    return paths, profiles_dir


def profile_roots() -> tuple[str, ...]:
    """Every user profile folder (other people's too): the children of C:\\Users and of the registry's
    ProfilesDirectory, plus each account's ProfileImagePath (profiles moved to another drive...).

    Re-read every few seconds, so an account created after start-up is protected as well.
    """
    global _profiles_cache
    with _profiles_lock:
        expires, roots = _profiles_cache
    if time.monotonic() < expires:
        return roots
    found, profiles_dir = _read_profile_list()
    parents = [os.path.join(SD, "Users")]
    if profiles_dir:
        found.append(profiles_dir)
        parents.append(profiles_dir)
    for parent in parents:
        try:
            found += [e.path for e in os.scandir(parent) if e.is_dir(follow_symlinks=False)]
        except OSError:
            pass
    roots = tuple({key(p): p for p in found}.values())
    with _profiles_lock:
        _profiles_cache = (time.monotonic() + _PROFILES_TTL_S, roots)
    return roots


def _protected_exact() -> list[str]:
    return [*PROTECTED_EXACT, *profile_roots()]


# Outside PURGE_SYSTEM_ROOTS and %TEMP%, permanent deletion is limited to
# folders that are caches by construction: (root, regex of the cache folder
# relative to root, optional extra check on that folder). A target is accepted
# when it is such a folder or lies inside one. Everything else in AppData and
# in the profile (installed applications, browser profiles with saved
# passwords, settings, ~\.cargo\bin...) is refused.
_CHROMIUM_CACHES = (r"(?:cache|code cache|gpucache|(?:graphite)?dawn\w*cache|grshadercache|shadercache"
                    r"|service worker\\(?:cachestorage|scriptcache))")
_ANY_DEPTH = r"(?:[^\\]+\\)*"


def _is_squirrel_app(folder: str) -> bool:
    """`folder` sits next to a regular Update.exe (Squirrel installer layout: app-<version> folders)."""
    try:
        st = os.lstat(os.path.join(os.path.dirname(folder), "Update.exe"))
    except OSError:
        return False
    return not st.st_file_attributes & (winapi.FILE_ATTRIBUTE_DIRECTORY | winapi.FILE_ATTRIBUTE_REPARSE_POINT)


_L, _R, _H = config.LOCALAPPDATA, config.APPDATA, config.USERPROFILE
PURGE_CACHE_PATTERNS = [
    (root, re.compile(rf"(?:{rx})(?=\\|$)", re.IGNORECASE), check) for root, rx, check in [
        # Chromium / Electron caches (browsers, Discord, Teams, VS Code...), wherever the app keeps its profile.
        (_L, _ANY_DEPTH + _CHROMIUM_CACHES, None),
        (_R, _ANY_DEPTH + _CHROMIUM_CACHES, None),
        (_L, r"mozilla\\firefox\\profiles\\[^\\]+\\(?:cache2|startupcache|thumbnails)", None),
        (_L, r"crashdumps|d3dscache|nvidia\\(?:dx|gl)cache|amd\\(?:dx|dxc|gl|vk)cache|intel\\shadercache", None),
        (_L, r"microsoft\\vscode-cpptools\\ipch", None),
        (_R, r"code\\(?:cachedextensionvsixs|cacheddata|logs)", None),
        (_L, r"spotify\\(?:data|storage)", None),
        (_L, r"[^\\]+-updater", None),  # electron-builder update downloads
        (_L, r"[^\\]+\\app-\d[^\\]*", _is_squirrel_app),  # old versions of Squirrel apps (Discord, Slack...)
        (_L, r"npm-cache\\(?:_cacache|_npx)|yarn\\cache|pnpm-cache|pnpm\\store|pip\\cache|uv\\cache"
             r"|nuget\\(?:v3|plugins)-cache|go-build|conda\\conda\\pkgs", None),
        (_H, r"\.cache\\(?:huggingface|torch)|\.gradle\\caches|\.m2\\repository|\.cargo\\registry"
             r"|\.nuget\\packages|\.keras\\models|go\\pkg\\mod|(?:ana|mini)conda3\\pkgs", None),
    ]
]
# Never purged, even when a folder inside matches a pattern: per-user installed applications.
PURGE_EXCLUDED = [os.path.join(config.LOCALAPPDATA, "Programs")]

# Explicit system cache locations (inside otherwise protected trees). Purging
# them usually needs administrator rights.
PURGE_SYSTEM_ROOTS = [
    os.path.join(config.SYSTEM_ROOT, "Temp"),
    os.path.join(config.SYSTEM_ROOT, "SoftwareDistribution", "Download"),
    os.path.join(config.SYSTEM_ROOT, "Minidump"),
    os.path.join(config.SYSTEM_ROOT, "LiveKernelReports"),
    os.path.join(config.SYSTEM_ROOT, "ServiceProfiles", "NetworkService", "AppData", "Local",
                 "Microsoft", "Windows", "DeliveryOptimization", "Cache"),
    os.path.join(config.PROGRAMDATA, "Microsoft", "Windows", "WER"),
    os.path.join(config.PROGRAMDATA, "McNeel", "McNeelUpdate", "DownloadCache"),
    os.path.join(config.PROGRAMDATA, "NVIDIA Corporation", "Downloader"),
    os.path.join(SD, "msys64", "var", "cache", "pacman", "pkg"),
    os.path.join(config.LOCALAPPDATA, "Microsoft", "Windows", "INetCache"),
    os.path.join(config.LOCALAPPDATA, "Microsoft", "Windows", "Explorer"),  # thumbcache_*.db only (rule uses a glob)
    os.path.join(config.LOCALAPPDATA, "Microsoft", "Windows", "WER"),
]


# Build artefacts that tools recreate on demand. Inside personal folders, only
# a folder with one of these exact names may be purged (e.g. an inactive
# project's node_modules on the Desktop).
REGENERABLE_DIR_NAMES = {"node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}


def _within_any(p: str, roots) -> bool:
    return any(is_within(p, r) for r in roots)


def _exact_any(p: str, roots) -> bool:
    kp = key(p)
    return any(kp == key(r) for r in roots)


def _contains_protected(p: str) -> bool:
    """True when a protected location lies strictly inside `p` (deleting `p` would take it along)."""
    kp = key(p)
    for r in (*PROTECTED_TREES, *_protected_exact()):
        if key(r) != kp and is_within(r, p):
            return True
    return False


def _check_no_link_in_chain(p: str) -> None:
    """Refuse when the path or any ancestor is a junction/symlink, or when the
    OS resolves it to something else (8.3 short names, subst drives...)."""
    cur = p
    while not _is_drive_root(cur):
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            raise Refused("Élément introuvable (déjà supprimé ?).")
        except OSError:
            raise Refused("Accès impossible à l'élément.")
        if winapi.is_link_stat(st):
            raise Refused("Le chemin passe par un lien symbolique ou une jonction : refusé par sécurité.")
        cur = os.path.dirname(cur)
    try:
        real = os.path.realpath(p, strict=True)
    except OSError:
        raise Refused("Impossible de résoudre le chemin.")
    if key(real) != key(p):
        raise Refused("Le chemin réel diffère du chemin demandé : refusé par sécurité.")


def check_recyclable(path: str, scope_root: str | None = None) -> str:
    """Validate a user-chosen path for the recycle bin. Returns the canonical path."""
    p = canon(path)
    if _is_drive_root(p):
        raise Refused("Impossible de supprimer la racine d'un disque.")
    if scope_root and not is_within(p, scope_root):
        raise Refused("L'élément est hors du dossier analysé.")
    if _within_any(p, PROTECTED_TREES):
        raise Refused("Emplacement système protégé.")
    if _exact_any(p, _protected_exact()):
        raise Refused("Dossier essentiel : son contenu peut être nettoyé, pas le dossier lui-même.")
    if _contains_protected(p):
        raise Refused("Ce dossier contient des éléments système ou essentiels : refusé.")
    name = os.path.basename(p)
    if key(os.path.dirname(p)) == key(SD) and name.lower() in PROTECTED_ROOT_FILES:
        raise Refused("Fichier système protégé.")
    if _PROFILE_HIVES.match(name):
        raise Refused("Fichier de profil Windows protégé.")
    _check_no_link_in_chain(p)
    st = os.lstat(p)
    if st.st_file_attributes & winapi.FILE_ATTRIBUTE_SYSTEM:
        raise Refused("Élément marqué « système » par Windows : protégé.")
    return p


def _is_regenerable_dir(p: str) -> bool:
    """A project build folder (node_modules...) in the profile, outside the places where tools keep their
    own copies: AppData (per-user installed applications) and the hidden tool folders at the profile root
    (~\\.vscode\\extensions...). %TEMP% stays allowed."""
    if os.path.basename(p).lower() not in REGENERABLE_DIR_NAMES or not is_within(p, config.USERPROFILE):
        return False
    if not is_within(p, config.TEMP):
        if is_within(p, os.path.join(config.USERPROFILE, "AppData")):
            return False
        if key(p)[len(key(config.USERPROFILE)) + 1:].startswith("."):
            return False
    try:
        return bool(os.lstat(p).st_file_attributes & winapi.FILE_ATTRIBUTE_DIRECTORY)
    except OSError:
        return False


def is_cache_location(p: str) -> bool:
    """True when `p` (canonical) is %TEMP%, a folder matching PURGE_CACHE_PATTERNS, or inside one.
    Location only: check_purgeable adds the protected-location and link checks."""
    if is_within(p, config.TEMP):
        return True  # purge() always keeps the %TEMP% folder itself
    if _within_any(p, PURGE_EXCLUDED):
        return False
    kp = key(p)
    for root, rx, check in PURGE_CACHE_PATTERNS:
        kr = key(root)
        if not kp.startswith(kr + "\\"):
            continue
        m = rx.match(kp[len(kr) + 1:])
        if m and (check is None or check(os.path.join(root, m.group(0)))):
            return True
    return False


def check_purgeable(path: str) -> str:
    """Validate a cleanup-rule target for permanent deletion. Returns the canonical path."""
    p = canon(path)
    if _is_drive_root(p):
        raise Refused("Racine de disque refusée.")
    if _within_any(p, PURGE_SYSTEM_ROOTS):
        pass  # explicit system cache folders (inside otherwise protected trees)
    elif _is_regenerable_dir(p):
        # e.g. an inactive project's node_modules, wherever it lives in the profile
        if _within_any(p, PROTECTED_TREES):
            raise Refused("Emplacement système protégé.")
    else:
        if not is_cache_location(p):
            raise Refused("Hors des emplacements de cache autorisés.")
        # %TEMP% is the one root whose *content* may be purged (purge() keeps the folder).
        if not _exact_any(p, [config.TEMP]) and _exact_any(p, _protected_exact()):
            raise Refused("Dossier racine refusé : seule une cible précise peut être nettoyée.")
        if _within_any(p, PROTECTED_TREES):
            raise Refused("Emplacement système protégé.")
        if _within_any(p, PERSONAL_DIRS):
            raise Refused("Les dossiers personnels ne sont jamais supprimés définitivement.")
    if _contains_protected(p):
        raise Refused("Ce dossier contient des éléments système ou essentiels : refusé.")
    if is_within(p, str(config.ROOT_DIR)):
        raise Refused("Dossier de l'application.")
    name = os.path.basename(p)
    if _PROFILE_HIVES.match(name) or (key(os.path.dirname(p)) == key(SD) and name.lower() in PROTECTED_ROOT_FILES):
        raise Refused("Fichier système protégé.")
    _check_no_link_in_chain(p)
    return p


# ------------------------------------------------------------------ operations

@dataclass
class OpResult:
    bytes: int = 0
    files: int = 0
    dirs: int = 0
    skipped_recent: int = 0
    skipped_in_use: int = 0
    errors: list = field(default_factory=list)  # [{"path", "reason"}], capped
    refused: list = field(default_factory=list)  # [{"path", "reason"}]
    done: list = field(default_factory=list)  # paths actually removed (recycle only)

    def add_error(self, path: str, reason: str) -> None:
        if len(self.errors) < 200:
            self.errors.append({"path": path, "reason": reason})

    def as_dict(self) -> dict:
        return {
            "bytes": self.bytes, "files": self.files, "dirs": self.dirs,
            "skipped_recent": self.skipped_recent, "skipped_in_use": self.skipped_in_use,
            "errors": self.errors, "refused": self.refused, "done": self.done, "dry_run": DRY_RUN,
        }


def _newest_time(st) -> float:
    return max(st.st_mtime, getattr(st, "st_birthtime", 0) or 0)


def _delete_via_handle(path: str, root: str, res: OpResult) -> bool:
    """Delete `path` through a handle opened without following reparse points,
    after checking ON THAT HANDLE that the object really lives inside `root`.

    A path checked earlier can be redirected later (an ancestor swapped for a
    junction): the final-path check on the open handle defeats that race, and
    the deletion is applied to the very object that was checked.
    """
    with winapi.Handle(path, winapi.DELETE | winapi.FILE_READ_ATTRIBUTES) as h:
        if h.error:
            if h.error in (winapi.ERROR_SHARING_VIOLATION, winapi.ERROR_ACCESS_DENIED):
                res.skipped_in_use += 1
            elif h.error not in (2, 3):  # vanished meanwhile: nothing to do
                res.add_error(path, f"Ouverture impossible (erreur {h.error}).")
            return False
        final = h.final_path()
        if final is None or not is_within(final, root):
            res.refused.append({"path": path, "reason": "L'élément a été redirigé hors de la cible : refusé."})
            return False
        err = h.delete()
    if err == 0:
        return True
    if err in (winapi.ERROR_SHARING_VIOLATION, winapi.ERROR_ACCESS_DENIED):
        res.skipped_in_use += 1
    elif err != winapi.ERROR_DIR_NOT_EMPTY:
        res.add_error(path, f"Suppression impossible (erreur {err}).")
    return False


def purge(target: str, *, min_age_s: float = 0, keep_root: bool = True, name_glob: re.Pattern | None = None,
          cancel: threading.Event | None = None, result: OpResult | None = None, on_progress=None) -> OpResult:
    """Permanently delete the content of `target` (a cache directory or a file).

    * every deletion goes through `_delete_via_handle` (no path re-resolution);
    * links met during the walk are removed as links, never followed;
    * cloud placeholders are left alone (deleting them would delete the cloud copy);
    * files newer than `min_age_s` are kept (protects files in use, e.g. %TEMP%);
    * `name_glob` restricts deletion to files whose name matches (thumbcache_*.db).
    """
    res = result or OpResult()
    p = check_purgeable(target)
    if _exact_any(p, _protected_exact()) or _exact_any(p, PURGE_SYSTEM_ROOTS):
        keep_root = True  # never remove a whitelisted root folder itself
    now = time.time()
    root_st = os.lstat(p)

    def delete_file(path: str, st) -> None:
        attrs = st.st_file_attributes
        name = os.path.basename(path)
        if attrs & winapi.CLOUD_ATTRS or _PROFILE_HIVES.match(name):
            return
        if name_glob is not None and not name_glob.match(name):
            return
        if min_age_s and _newest_time(st) > now - min_age_s:
            res.skipped_recent += 1
            return
        if DRY_RUN or _delete_via_handle(path, p, res):
            res.bytes += st.st_size
            res.files += 1

    if not root_st.st_file_attributes & winapi.FILE_ATTRIBUTE_DIRECTORY:
        delete_file(p, root_st)
        return res

    stack = [(p, False)]
    last_report = 0.0
    while stack:
        if cancel is not None and cancel.is_set():
            break
        path, visited = stack.pop()
        if visited:
            if (path != p or not keep_root) and name_glob is None and not DRY_RUN:
                if _delete_via_handle(path, p, OpResult()):  # only succeeds when empty
                    res.dirs += 1
            continue
        try:
            if winapi.is_link_stat(os.lstat(path)):
                continue
            entries = list(os.scandir(path))
        except OSError as e:
            res.add_error(path, e.strerror or str(e))
            continue
        stack.append((path, True))
        for entry in entries:
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if winapi.is_link_stat(st):
                # Remove the link itself (the handle is opened on the link, not its target).
                if name_glob is None and not DRY_RUN:
                    _delete_via_handle(entry.path, p, res)
                continue
            if st.st_file_attributes & winapi.FILE_ATTRIBUTE_DIRECTORY:
                if name_glob is None:
                    stack.append((entry.path, False))
            else:
                delete_file(entry.path, st)
        if on_progress is not None and time.monotonic() - last_report > 0.25:
            last_report = time.monotonic()
            on_progress(res)
    return res


def measure(target: str, *, min_age_s: float = 0, name_glob: re.Pattern | None = None,
            cancel: threading.Event | None = None) -> tuple[int, int]:
    """(bytes, files) that purge() would delete. Read-only; same filters, no link following."""
    try:
        p = check_purgeable(target)
    except Refused:
        return 0, 0
    now = time.time()
    total = count = 0
    try:
        root_st = os.lstat(p)
    except OSError:
        return 0, 0

    def accept(name: str, st) -> bool:
        if st.st_file_attributes & winapi.CLOUD_ATTRS:
            return False
        if name_glob is not None and not name_glob.match(name):
            return False
        return not (min_age_s and _newest_time(st) > now - min_age_s)

    if not root_st.st_file_attributes & winapi.FILE_ATTRIBUTE_DIRECTORY:
        return (root_st.st_size, 1) if accept(os.path.basename(p), root_st) else (0, 0)
    stack = [p]
    while stack:
        if cancel is not None and cancel.is_set():
            break
        cur = stack.pop()
        try:
            it = os.scandir(cur)
        except OSError:
            continue
        with it:
            for entry in it:
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if winapi.is_link_stat(st):
                    continue
                if st.st_file_attributes & winapi.FILE_ATTRIBUTE_DIRECTORY:
                    if name_glob is None:
                        stack.append(entry.path)
                elif accept(entry.name, st):
                    total += st.st_size
                    count += 1
    return total, count


def purge_many(targets: list[dict], *, rule_id: str, cancel=None, on_progress=None) -> OpResult:
    """Purge several targets of one cleanup rule and journal the outcome.

    Each target: {"path", "min_age_s"?, "keep_root"?, "name_glob"?}.
    """
    res = OpResult()
    for t in targets:
        if cancel is not None and cancel.is_set():
            break
        try:
            purge(t["path"], min_age_s=t.get("min_age_s", 0), keep_root=t.get("keep_root", True),
                  name_glob=t.get("name_glob"), cancel=cancel, result=res, on_progress=on_progress)
        except Refused as e:
            res.refused.append({"path": t.get("path"), "reason": str(e)})
        except FileNotFoundError:
            continue
    journal.append("purge", rule=rule_id, bytes=res.bytes, files=res.files,
                   targets=[t["path"] for t in targets][:50], errors=len(res.errors),
                   skipped_in_use=res.skipped_in_use, refused=len(res.refused), dry_run=DRY_RUN)
    return res


def _tree_size(p: str) -> int:
    """Size of a file or folder, without following links (used for journaling)."""
    st = os.lstat(p)
    if not st.st_file_attributes & winapi.FILE_ATTRIBUTE_DIRECTORY:
        return 0 if st.st_file_attributes & winapi.CLOUD_ATTRS else st.st_size
    total = 0
    stack = [p]
    while stack:
        try:
            it = os.scandir(stack.pop())
        except OSError:
            continue
        with it:
            for e in it:
                try:
                    s = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                if winapi.is_link_stat(s):
                    continue
                if s.st_file_attributes & winapi.FILE_ATTRIBUTE_DIRECTORY:
                    stack.append(e.path)
                elif not s.st_file_attributes & winapi.CLOUD_ATTRS:
                    total += s.st_size
    return total


def _lock_ancestors(paths: list[str], held: dict) -> set[str]:
    """Open every ancestor folder of `paths` WITHOUT FILE_SHARE_DELETE.

    While held, no process can rename, delete or replace those folders (e.g. by
    a junction), so the shell operation that follows acts on the folders that
    were validated. Each handle is checked: real folder, same final path.
    Returns the keys of paths whose ancestors could not be locked.
    """
    failed = set()
    for p in paths:
        chain = []
        cur = os.path.dirname(p)
        while not _is_drive_root(cur):
            chain.append(cur)
            cur = os.path.dirname(cur)
        for anc in reversed(chain):
            k = key(anc)
            if k in held:
                continue
            h = winapi.Handle(anc, winapi.FILE_LIST_DIRECTORY | winapi.FILE_READ_ATTRIBUTES,
                              share=winapi.FILE_SHARE_READ_WRITE)
            ok = not h.error
            if ok:
                attrs = h.attributes()
                final = h.final_path()
                ok = (attrs is not None and not (attrs[0] & winapi.FILE_ATTRIBUTE_REPARSE_POINT
                                                  and attrs[1] & winapi.REPARSE_NAME_SURROGATE)
                      and final is not None and key(final) == k)
            if not ok:
                h.close()
                failed.add(key(p))
                break
            held[k] = h
    return failed


def recycle(paths: list[str], *, scope_root: str | None = None, source: str = "",
            sizes: dict | None = None, cancel=None, on_progress=None) -> OpResult:
    """Send user-chosen items to the recycle bin after validating each of them."""
    res = OpResult()
    valid: list[tuple[str, int]] = []
    seen = set()
    for raw in paths:
        try:
            p = check_recyclable(raw, scope_root)
        except Refused as e:
            res.refused.append({"path": raw, "reason": str(e)})
            continue
        except OSError as e:
            res.refused.append({"path": raw, "reason": e.strerror or "Accès impossible."})
            continue
        if key(p) in seen:
            continue
        seen.add(key(p))
        size = (sizes or {}).get(raw)
        if size is None:
            try:
                size = _tree_size(p)
            except OSError:
                size = 0
        valid.append((p, int(size)))

    # Drop items nested inside another selected item (the parent covers them).
    valid.sort(key=lambda x: len(x[0]))
    kept: list[tuple[str, int]] = []
    for p, s in valid:
        if not any(is_within(p, q) for q, _ in kept):
            kept.append((p, s))

    batch_size = 25
    for i in range(0, len(kept), batch_size):
        if cancel is not None and cancel.is_set():
            break
        batch = kept[i:i + batch_size]
        held: dict = {}
        try:
            if DRY_RUN:
                rc, aborted = 0, False
            else:
                failed = _lock_ancestors([p for p, _ in batch], held)
                for p, _ in [b for b in batch if key(b[0]) in failed]:
                    res.refused.append({"path": p, "reason": "Un dossier parent est en cours d'utilisation ou "
                                                              "a été modifié : réessayez plus tard."})
                batch = [b for b in batch if key(b[0]) not in failed]
                rc, aborted = winapi.send_to_recycle_bin([p for p, _ in batch]) if batch else (0, False)
        finally:
            for h in held.values():
                h.close()
        for p, s in batch:
            if DRY_RUN or not os.path.lexists(p):
                res.done.append(p)
                res.bytes += s
                res.files += 1
            else:
                res.add_error(p, "Non supprimé (fichier ouvert, accès refusé ou opération annulée)."
                              if rc or aborted else "Non supprimé.")
        if on_progress is not None:
            on_progress(res)
    journal.append("recycle", source=source, bytes=res.bytes, files=res.files, items=res.done[:200],
                   errors=len(res.errors), refused=len(res.refused), dry_run=DRY_RUN)
    return res


def empty_recycle_bin() -> OpResult:
    res = OpResult()
    info = winapi.recycle_bin_info()
    if DRY_RUN or winapi.empty_recycle_bin():
        res.bytes, res.files = info["size"], info["count"]
    else:
        res.add_error("Corbeille", "La corbeille n'a pas pu être vidée.")
    journal.append("empty_recycle_bin", bytes=res.bytes, files=res.files, dry_run=DRY_RUN)
    return res
