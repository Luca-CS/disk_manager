"""Disk Manager entry point.

    pythonw run.py              normal launch (opens the app window)
    python  run.py --no-window  server only; prints the URL (development)
    python  run.py --dry-run    simulate every deletion (nothing is touched)
    python  run.py --install-shortcut   create Desktop + Start menu shortcuts
"""

import argparse
import json
import logging
import logging.handlers
import os
import sys
import threading
import time
import urllib.request


def _parse_args():
    p = argparse.ArgumentParser(description="Disk Manager")
    p.add_argument("--no-window", action="store_true", help="do not open the app window")
    p.add_argument("--dry-run", action="store_true", help="simulate deletions")
    p.add_argument("--debug", action="store_true", help="log every HTTP request to data/app.log")
    p.add_argument("--install-shortcut", action="store_true", help="create Desktop and Start menu shortcuts")
    p.add_argument("--after-elevate", action="store_true", help=argparse.SUPPRESS)
    return p.parse_args()


ARGS = _parse_args()
if ARGS.dry_run:
    os.environ["DM_DRY_RUN"] = "1"  # read by dm.safety at import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dm import config, history, winapi, window  # noqa: E402
from dm.api import AppState, build_router, preload_cached_scan  # noqa: E402
from dm.server import AppServer, serve_forever_in_thread  # noqa: E402

log = logging.getLogger("dm")


def _setup_logging() -> None:
    config.ensure_data_dirs()
    handler = logging.handlers.RotatingFileHandler(config.LOG_PATH, maxBytes=1_000_000, backupCount=2,
                                                   encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if ARGS.debug else logging.INFO)
    root.addHandler(handler)
    if sys.stdout is not None and ARGS.no_window:
        root.addHandler(logging.StreamHandler(sys.stdout))


def _existing_instance() -> dict | None:
    """Return the running instance's info if one answers on its port."""
    try:
        info = json.loads(config.INSTANCE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(info, dict) or not winapi.pid_alive(int(info.get("pid", 0))):
        return None
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{int(info['port'])}/api/ping", data=b"{}", method="POST",
                                     headers={"X-DM-Token": info["token"], "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=2) as r:  # loopback only
            return info if r.status == 200 else None
    except Exception:
        return None


def _hand_off_to_running_instance(elevated: bool) -> bool:
    """Open a window on the instance already running, if any. Returns True if one was opened.

    Never when elevated: data/instance.json is writable by every process of the user, so its
    port and token could point an elevated window at a server of their choosing."""
    if elevated:
        return False
    running = _existing_instance()
    if not running:
        return False
    window.open_window(f"http://127.0.0.1:{running['port']}/#t={running['token']}")
    return True


_ADMIN_WINDOW_ERROR = (
    "Disk Manager ne peut pas ouvrir sa fenêtre en mode administrateur.\n\n"
    "Ce mode a besoin de Microsoft Edge (ou Chrome, Brave) installé dans Program Files et d'un dossier de "
    "profil modifiable uniquement par les administrateurs :\n{root}\n\n"
    "Si ce dossier existe sans être protégé (créé par un autre programme), supprimez-le puis relancez "
    "l'application. Le mode normal n'est pas concerné.")


_ADMIN_DATA_ERROR = (
    "Disk Manager ne peut pas démarrer en mode administrateur.\n\n"
    "En mode administrateur, l'application n'écrit que dans un dossier modifiable uniquement par les "
    "administrateurs :\n{root}\n\n"
    "Ce dossier existe sans être protégé (créé par un autre programme), ou l'emplacement du dossier Windows "
    "ou de votre profil n'a pas pu être lu. Supprimez ce dossier s'il existe, puis relancez l'application. "
    "Le mode normal n'est pas concerné.")


def _start_logging() -> bool:
    """Set up logging. False if the elevated instance has no trusted place to write to: it then
    writes nothing at all (data/ is the user's, see config.ensure_data_dirs)."""
    if config.ELEVATED and not config.TRUSTED_LOCATIONS_OK:
        return False
    try:
        _setup_logging()
    except PermissionError:
        if not config.ELEVATED:
            raise
        return False
    return True


def _elevate_params(script: str) -> str:
    """Command line of the elevated instance. -E -s: ignore PYTHON* variables and the user's
    site-packages (both writable by the user, both able to run code at start-up); -B: no .pyc
    written by the administrator into the app folder."""
    return f'-E -s -B "{script}" --after-elevate' + (" --dry-run" if os.environ.get("DM_DRY_RUN") == "1" else "")


def _window_url(server: AppServer, elevated: bool) -> str:
    """URL opened in the app window. Any process of the user can read the browser's command line, so an
    elevated window gets a one-time code (traded for the token by web/js/api.js) instead of the token."""
    if elevated:
        return f"{server.url}#c={server.issue_login_code()}"
    return f"{server.url}#t={server.token}"


def main() -> int:
    if not _start_logging():
        winapi.show_error(config.APP_NAME, _ADMIN_DATA_ERROR.format(root=config.DATA_DIR))
        return 1
    if ARGS.install_shortcut:
        from dm import shortcut
        made = shortcut.install_shortcuts()
        print("\n".join(f"Raccourci créé : {p}" for p in made) or "Aucun raccourci créé.")
        return 0 if made else 1
    elevated = config.ELEVATED
    if ARGS.after_elevate:
        time.sleep(1.5)  # let the previous (non-elevated) instance close its window
    elif not ARGS.no_window and _hand_off_to_running_instance(elevated):
        return 0

    state = AppState()
    server = AppServer(build_router(state))
    url = f"{server.url}#t={server.token}"
    # Dev/test servers never register as the main instance, and an elevated
    # instance never writes its token to the (user-writable) data folder.
    if not ARGS.no_window and not elevated:
        config.INSTANCE_PATH.write_text(json.dumps({"pid": os.getpid(), "port": server.port, "token": server.token}),
                                        encoding="utf-8")
    serve_forever_in_thread(server)
    threading.Thread(target=history.sample_usage, daemon=True).start()
    preload_cached_scan(state)
    log.info("Disk Manager listening on %s (admin=%s, dry_run=%s)", server.url, elevated,
             os.environ.get("DM_DRY_RUN") == "1")

    proc = None if ARGS.no_window else window.open_window(_window_url(server, elevated), elevated=elevated)
    if proc is None and elevated and not ARGS.no_window:
        log.error("elevated window not opened: no trusted browser or profile folder")
        server.shutdown()
        winapi.show_error(config.APP_NAME, _ADMIN_WINDOW_ERROR.format(root=window.admin_profile_root() or "?"))
        return 1
    if proc is not None:
        state.window_pids = {proc.pid}
    if ARGS.no_window:
        print(url, flush=True)

    stop = threading.Event()

    def quit_app():
        stop.set()

    def elevate() -> bool:
        exe = sys.executable
        if exe.lower().endswith("python.exe"):
            exe = exe[:-10] + "pythonw.exe"
        script = os.path.abspath(__file__)
        if not winapi.relaunch_as_admin(exe, _elevate_params(script), os.path.dirname(script)):
            return False
        threading.Timer(0.5, stop.set).start()
        return True

    state.on_quit = quit_app
    state.on_elevate = elevate

    # Lifetime: stop when the window process is gone, or when the UI stopped
    # sending heartbeats (default browser fallback / crash).
    started = time.monotonic()
    while not stop.wait(2.0):
        idle = time.monotonic() - server.last_ping
        if proc is not None and proc.poll() is not None and idle > 10:
            break
        if not ARGS.no_window and idle > config.HEARTBEAT_TIMEOUT_S and time.monotonic() - started > 60:
            break

    log.info("shutting down")
    if proc is not None and proc.poll() is None:
        proc.terminate()
    server.shutdown()
    if not elevated:  # an elevated instance never wrote it, and never touches the user-writable file
        try:
            info = json.loads(config.INSTANCE_PATH.read_text(encoding="utf-8"))
            if info.get("pid") == os.getpid():
                config.INSTANCE_PATH.unlink()
        except (OSError, ValueError):
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
