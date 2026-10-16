"""Paths and tunables shared by the whole app.

Elevated (administrator) mode trusts nothing a non-elevated process of the user
can change: system locations come from the kernel and HKLM instead of the
environment (inherited from the user, who can set any variable in
HKCU\\Environment), and every file the app writes lives in an admin-only folder
instead of data/ (see ensure_data_dirs).
"""

import os
import re
import winreg
from pathlib import Path

from . import winapi

APP_NAME = "Disk Manager"
ELEVATED = winapi.is_admin()

ROOT_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT_DIR / "web"

# Files at least this big are kept individually in the scan tree (largest files,
# duplicates, treemap). Smaller ones are aggregated per folder to save memory.
KEEP_FILE_MIN = 1024 * 1024

# Scanner worker threads. os.scandir releases the GIL during FindNextFileW.
SCAN_WORKERS = min(32, (os.cpu_count() or 4) * 4)

# HTTP
MAX_BODY_BYTES = 1024 * 1024
HEARTBEAT_TIMEOUT_S = 90  # shut down when the UI has been gone this long


# ------------------------------------------------------------------ locations

def _env(name: str, default: str = "") -> str:
    # realpath expands 8.3 short names (e.g. C:\Users\JOHNDO~1) so prefix comparisons stay reliable.
    value = os.environ.get(name, default)
    try:
        return os.path.realpath(value) if value and os.path.exists(value) else value
    except OSError:
        return value


def _env_locations() -> dict:
    # SystemDrive not through _env: realpath("C:") would return the current directory of drive C.
    home = _env("USERPROFILE", str(Path.home()))
    local = _env("LOCALAPPDATA", os.path.join(home, "AppData", "Local"))
    return {
        "SYSTEM_DRIVE": os.environ.get("SystemDrive", "C:")[:2] + "\\",
        "SYSTEM_ROOT": _env("SystemRoot", r"C:\Windows"),
        "USERPROFILE": home,
        "LOCALAPPDATA": local,
        "APPDATA": _env("APPDATA", os.path.join(home, "AppData", "Roaming")),
        "PROGRAMDATA": _env("ProgramData", r"C:\ProgramData"),
        "PROGRAM_FILES": _env("ProgramFiles", r"C:\Program Files"),
        "PROGRAM_FILES_X86": _env("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        "TEMP": _env("TEMP", os.path.join(local, "Temp")),
    }


_SYSTEM_VAR = re.compile(r"%(systemdrive|systemroot|windir)%", re.IGNORECASE)
_DRIVE_ABS = re.compile(r"^[A-Za-z]:\\")


def expand_system_vars(value, system_root: str | None = None) -> str | None:
    """Expand a registry REG_EXPAND_SZ path. Only %SystemDrive%, %SystemRoot% and %windir% are
    expanded, from `system_root` (default SYSTEM_ROOT), never from the environment; None for any
    other variable or for something that is not an absolute local path."""
    if not isinstance(value, str):
        return None
    root = system_root or SYSTEM_ROOT
    p = _SYSTEM_VAR.sub(lambda m: root[:2] if m.group(1).lower() == "systemdrive" else root, value)
    if "%" in p:
        return None
    p = os.path.normpath(p)
    return p if _DRIVE_ABS.match(p) else None


_PROFILE_LIST_KEY = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList"
_CURRENT_VERSION_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion"


def _hklm_path(subkey: str, name: str, system_root: str) -> str | None:
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, subkey, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
            value = winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None
    return expand_system_vars(value, system_root)


def _trusted_locations() -> tuple[dict, bool]:
    """Same keys as _env_locations, from the kernel (Windows folder), HKLM (ProgramData, Program Files)
    and the process token's ProfileList entry (profile). AppData and %TEMP% are their default places in
    that profile: their HKCU / environment values are the user's to change. Never resolves links: a
    junction the user puts there makes check_purgeable refuse everything below it.

    Returns (locations, ok); ok is False if the Windows folder or the profile could not be read
    (fixed fallbacks are used, and run.py refuses to start elevated)."""
    ok = True
    sysroot = winapi.system_windows_dir()
    if not sysroot or not _DRIVE_ABS.match(sysroot):
        ok, sysroot = False, r"C:\Windows"
    sysroot = os.path.normpath(winapi.long_path(sysroot))
    drive = sysroot[:2] + "\\"
    home = winapi.user_profile_dir()
    if not home or not _DRIVE_ABS.match(home):
        ok, home = False, os.path.join(drive, "Users", "Default")  # protected tree: nothing purgeable
    home = os.path.normpath(winapi.long_path(home))
    local = os.path.join(home, "AppData", "Local")

    def hklm(subkey, name, default):
        return winapi.long_path(_hklm_path(subkey, name, sysroot) or os.path.join(drive, default))

    return {
        "SYSTEM_DRIVE": drive,
        "SYSTEM_ROOT": sysroot,
        "USERPROFILE": home,
        "LOCALAPPDATA": local,
        "APPDATA": os.path.join(home, "AppData", "Roaming"),
        "PROGRAMDATA": hklm(_PROFILE_LIST_KEY, "ProgramData", "ProgramData"),
        "PROGRAM_FILES": hklm(_CURRENT_VERSION_KEY, "ProgramFilesDir", "Program Files"),
        "PROGRAM_FILES_X86": hklm(_CURRENT_VERSION_KEY, "ProgramFilesDir (x86)", "Program Files (x86)"),
        "TEMP": os.path.join(local, "Temp"),
    }, ok


if ELEVATED:
    _LOC, TRUSTED_LOCATIONS_OK = _trusted_locations()
else:
    _LOC, TRUSTED_LOCATIONS_OK = _env_locations(), True

SYSTEM_DRIVE = _LOC["SYSTEM_DRIVE"]
SYSTEM_ROOT = _LOC["SYSTEM_ROOT"]
USERPROFILE = _LOC["USERPROFILE"]
LOCALAPPDATA = _LOC["LOCALAPPDATA"]
APPDATA = _LOC["APPDATA"]
PROGRAMDATA = _LOC["PROGRAMDATA"]
PROGRAM_FILES = _LOC["PROGRAM_FILES"]
PROGRAM_FILES_X86 = _LOC["PROGRAM_FILES_X86"]
TEMP = _LOC["TEMP"]


def admin_data_dir() -> str:
    """%ProgramData%\\Disk Manager, from HKLM: the elevated instance's data folder (scan cache,
    journal, history, log, browser profile), writable by Administrators and SYSTEM only."""
    return os.path.join(_trusted_locations()[0]["PROGRAMDATA"], APP_NAME)


# ------------------------------------------------------------------ data files

DATA_DIR = Path(admin_data_dir()) if ELEVATED else ROOT_DIR / "data"
SCANS_DIR = DATA_DIR / "scans"          # cached scan trees (gzip JSON) + history snapshots
JOURNAL_PATH = DATA_DIR / "journal.jsonl"  # append-only log of every deletion
LOG_PATH = DATA_DIR / "app.log"
INSTANCE_PATH = DATA_DIR / "instance.json"
EDGE_PROFILE_DIR = DATA_DIR / "edge-profile"

# Owners accepted for the folder holding the admin data folder: a parent the
# user owns would let them rename the protected folder away and plant another.
SYSTEM_OWNERS = winapi.ADMIN_SIDS | {winapi.SID_TRUSTED_INSTALLER}


def ensure_admin_dir(root: str) -> bool:
    """Create `root` with an Administrators + SYSTEM only DACL, or accept an existing one that has
    exactly that protection, inside a parent owned by the system. Checked on every call: a folder
    with another owner, a looser DACL or a link in its place (planted before the first elevated
    launch) is refused, never fixed or emptied."""
    parent = winapi.security_info(os.path.dirname(root))
    if parent is None or parent["owner"] not in SYSTEM_OWNERS:
        return False
    return winapi.ensure_private_dir(root)


def ensure_data_dirs() -> None:
    """Create the data folders. Elevated, data/ is never used: it is writable by every process of
    the user, who could plant links there to make the administrator write or rename any file
    (log rotation, journal, scan cache). Raises PermissionError if the admin folder is not trusted."""
    if ELEVATED:
        if not ensure_admin_dir(str(DATA_DIR)):
            raise PermissionError(f"admin data folder missing or not protected: {DATA_DIR}")
    else:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    SCANS_DIR.mkdir(exist_ok=True)


# ------------------------------------------------------------------ child processes

def child_env() -> dict | None:
    """Environment for the subprocesses the app starts (PowerShell, browser): None = inherit,
    except when elevated, where only trusted values are passed. A user-set PSModulePath, for
    instance, would make the elevated PowerShell load the user's own "Storage" module."""
    if not ELEVATED:
        return None
    sys32 = os.path.join(SYSTEM_ROOT, "System32")
    tmp = os.path.join(SYSTEM_ROOT, "Temp")
    env = {
        "SystemRoot": SYSTEM_ROOT, "windir": SYSTEM_ROOT, "SystemDrive": SYSTEM_DRIVE[:2],
        "ComSpec": os.path.join(sys32, "cmd.exe"),
        "Path": ";".join([sys32, SYSTEM_ROOT, os.path.join(sys32, "Wbem"),
                          os.path.join(sys32, "WindowsPowerShell", "v1.0")]),
        "PATHEXT": ".COM;.EXE;.BAT;.CMD",
        "PSModulePath": os.path.join(sys32, "WindowsPowerShell", "v1.0", "Modules"),
        "ProgramData": PROGRAMDATA, "ALLUSERSPROFILE": PROGRAMDATA,
        "ProgramFiles": PROGRAM_FILES, "ProgramW6432": PROGRAM_FILES, "ProgramFiles(x86)": PROGRAM_FILES_X86,
        "USERPROFILE": USERPROFILE, "LOCALAPPDATA": LOCALAPPDATA, "APPDATA": APPDATA,
        "TEMP": tmp, "TMP": tmp,
    }
    for name in ("COMPUTERNAME", "USERNAME", "USERDOMAIN", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE"):
        if os.environ.get(name):  # informative only
            env[name] = os.environ[name]
    return env
