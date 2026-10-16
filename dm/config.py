"""Paths and tunables shared by the whole app."""

import os
from pathlib import Path

APP_NAME = "Disk Manager"

ROOT_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT_DIR / "web"
DATA_DIR = ROOT_DIR / "data"
SCANS_DIR = DATA_DIR / "scans"          # cached scan trees (gzip JSON) + history snapshots
JOURNAL_PATH = DATA_DIR / "journal.jsonl"  # append-only log of every deletion
LOG_PATH = DATA_DIR / "app.log"
INSTANCE_PATH = DATA_DIR / "instance.json"
EDGE_PROFILE_DIR = DATA_DIR / "edge-profile"

# Files at least this big are kept individually in the scan tree (largest files,
# duplicates, treemap). Smaller ones are aggregated per folder to save memory.
KEEP_FILE_MIN = 1024 * 1024

# Scanner worker threads. os.scandir releases the GIL during FindNextFileW.
SCAN_WORKERS = min(32, (os.cpu_count() or 4) * 4)

# HTTP
MAX_BODY_BYTES = 1024 * 1024
HEARTBEAT_TIMEOUT_S = 90  # shut down when the UI has been gone this long

# Environment-derived locations (resolved once). realpath expands 8.3 short
# names (e.g. C:\Users\JOHNDO~1) so prefix comparisons stay reliable.
def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name, default)
    try:
        return os.path.realpath(value) if value and os.path.exists(value) else value
    except OSError:
        return value

# Not through _env: realpath("C:") would return the current directory of drive C.
SYSTEM_DRIVE = os.environ.get("SystemDrive", "C:")[:2] + "\\"
SYSTEM_ROOT = _env("SystemRoot", r"C:\Windows")
USERPROFILE = _env("USERPROFILE", str(Path.home()))
LOCALAPPDATA = _env("LOCALAPPDATA", os.path.join(USERPROFILE, "AppData", "Local"))
APPDATA = _env("APPDATA", os.path.join(USERPROFILE, "AppData", "Roaming"))
PROGRAMDATA = _env("ProgramData", r"C:\ProgramData")
PROGRAM_FILES = _env("ProgramFiles", r"C:\Program Files")
PROGRAM_FILES_X86 = _env("ProgramFiles(x86)", r"C:\Program Files (x86)")
TEMP = _env("TEMP", os.path.join(LOCALAPPDATA, "Temp"))


def ensure_data_dirs() -> None:
    for d in (DATA_DIR, SCANS_DIR):
        d.mkdir(parents=True, exist_ok=True)
