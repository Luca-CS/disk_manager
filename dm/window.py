"""Opens the UI in a chromeless Edge (or Chrome/Brave) app window.

The browser runs with its own throwaway profile inside data/ and with
background networking disabled, so the window itself does not phone home.
"""

import os
import subprocess
import webbrowser

from . import config

_CANDIDATES = [
    (os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), r"Microsoft\Edge\Application\msedge.exe"),
    (os.environ.get("ProgramFiles", r"C:\Program Files"), r"Microsoft\Edge\Application\msedge.exe"),
    (os.environ.get("LOCALAPPDATA", ""), r"Microsoft\Edge\Application\msedge.exe"),
    (os.environ.get("ProgramFiles", r"C:\Program Files"), r"Google\Chrome\Application\chrome.exe"),
    (os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), r"Google\Chrome\Application\chrome.exe"),
    (os.environ.get("LOCALAPPDATA", ""), r"Google\Chrome\Application\chrome.exe"),
    (os.environ.get("ProgramFiles", r"C:\Program Files"), r"BraveSoftware\Brave-Browser\Application\brave.exe"),
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


def find_browser() -> str | None:
    for base, rel in _CANDIDATES:
        if base:
            p = os.path.join(base, rel)
            if os.path.isfile(p):
                return p
    return None


def open_window(url: str, width: int = 1440, height: int = 900) -> subprocess.Popen | None:
    """Start an app-mode window. Returns the browser process, or None if the
    default browser had to be used (no process to track)."""
    exe = find_browser()
    if exe is None:
        webbrowser.open(url)
        return None
    config.EDGE_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    args = [exe, f"--app={url}", f"--user-data-dir={config.EDGE_PROFILE_DIR}",
            f"--window-size={width},{height}", *_PRIVACY_FLAGS]
    return subprocess.Popen(args, close_fds=True, creationflags=subprocess.CREATE_NO_WINDOW)
