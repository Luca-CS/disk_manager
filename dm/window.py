"""Opens the UI in a chromeless Edge (or Chrome/Brave) app window.

The browser runs with its own throwaway profile inside data/ and with
background networking disabled, so the window itself does not phone home.

An elevated window must not read anything a non-elevated process could have
written: its profile lives in a folder only administrators can write
(admin_profile_root), and the browser is only taken from Program Files.
"""

import logging
import os
import subprocess
import webbrowser

from . import config, winapi

log = logging.getLogger("dm.window")

_EDGE = r"Microsoft\Edge\Application\msedge.exe"
_CHROME = r"Google\Chrome\Application\chrome.exe"
_BRAVE = r"BraveSoftware\Brave-Browser\Application\brave.exe"
_CANDIDATES = [
    (os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), _EDGE),
    (os.environ.get("ProgramFiles", r"C:\Program Files"), _EDGE),
    (os.environ.get("LOCALAPPDATA", ""), _EDGE),
    (os.environ.get("ProgramFiles", r"C:\Program Files"), _CHROME),
    (os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), _CHROME),
    (os.environ.get("LOCALAPPDATA", ""), _CHROME),
    (os.environ.get("ProgramFiles", r"C:\Program Files"), _BRAVE),
]

# Chromium switches that cut the browser's own background traffic
# (sync, component updates, field trials, metrics, safe-browsing pings...).
_PRIVACY_FLAGS = [
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-sync",
    "--disable-domain-reliability",
    "--disable-client-side-phishing-detection",
    "--disable-breakpad",
    "--disable-extensions",
    "--disable-default-apps",
    "--no-pings",
    "--metrics-recording-only",
    "--disable-features=Translate,OptimizationHints,MediaRouter,AutofillServerCommunication,"
    "msEdgeShopping,msUndersideButton,EdgeCollections",
    # Hard network lock: the flags above still leave Edge's own services
    # (config, SmartScreen...) talking to Microsoft. Every hostname now fails to
    # resolve and any other traffic goes to a dead local proxy. Only 127.0.0.1
    # (the app itself) is excluded from both, so it stays reachable.
    "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1",
    "--proxy-server=http://127.0.0.1:9",
    "--proxy-bypass-list=127.0.0.1",
    "--disable-quic",
]


def _admin_candidates() -> list[tuple[str | None, str]]:
    """Browsers an elevated process may start: Program Files only (per-user installs in
    %LOCALAPPDATA% are writable by the user), located without environment variables."""
    pf = winapi.known_folder(winapi.FOLDERID_PROGRAM_FILES)
    x86 = winapi.known_folder(winapi.FOLDERID_PROGRAM_FILES_X86)
    return [(x86, _EDGE), (pf, _EDGE), (pf, _CHROME), (x86, _CHROME), (pf, _BRAVE)]


def find_browser(elevated: bool = False) -> str | None:
    for base, rel in (_admin_candidates() if elevated else _CANDIDATES):
        if base:
            p = os.path.join(base, rel)
            if os.path.isfile(p):
                return p
    return None


def admin_profile_root() -> str:
    """Folder holding the elevated window's browser profile: the elevated data folder,
    %ProgramData%\\Disk Manager (read from HKLM, not from %ProgramData%)."""
    return config.admin_data_dir()


def admin_profile_dir() -> str | None:
    """Browser profile for an elevated window, or None if it cannot be trusted.

    data/edge-profile is writable by every process of the user, which could plant
    Preferences / Local State there for the elevated browser to load. The elevated
    profile sits in a folder created with an Administrators + SYSTEM only DACL and
    checked again on every launch: one with another owner, a looser DACL or a link
    in its place (planted before the first elevated launch) is refused, never fixed.
    """
    root = admin_profile_root()
    if not config.ensure_admin_dir(root):
        log.error("admin profile folder missing, not protected or in an untrusted parent: %s", root)
        return None
    profile = os.path.join(root, "edge-profile")
    try:
        st = os.lstat(profile)
    except FileNotFoundError:
        return profile  # created by the browser, inheriting the protected DACL
    except OSError:
        return None
    if st.st_file_attributes & (winapi.FILE_ATTRIBUTE_REPARSE_POINT | winapi.FILE_ATTRIBUTE_DIRECTORY) \
            != winapi.FILE_ATTRIBUTE_DIRECTORY:
        log.error("admin profile is not a plain folder: %s", profile)
        return None
    return profile


def open_window(url: str, elevated: bool = False, width: int = 1440,
                height: int = 900) -> subprocess.Popen | None:
    """Start an app-mode window. Returns the browser process, or None if the
    default browser had to be used (no process to track) or, when elevated,
    if no trusted browser/profile is available (nothing is opened then)."""
    exe = find_browser(elevated)
    if elevated:
        # Never fall back to the default browser: started from an elevated
        # process, it would run elevated on the user's own (writable) profile.
        profile = admin_profile_dir() if exe else None
        if profile is None:
            return None
    else:
        if exe is None:
            webbrowser.open(url)
            return None
        config.EDGE_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        profile = str(config.EDGE_PROFILE_DIR)
    args = [exe, f"--app={url}", f"--user-data-dir={profile}",
            f"--window-size={width},{height}", *_PRIVACY_FLAGS]
    return subprocess.Popen(args, close_fds=True, env=config.child_env(),
                            creationflags=subprocess.CREATE_NO_WINDOW)
