"""Installed applications from the registry.

Read-only. Uses ``winreg`` only (no subprocess, no uninstaller is ever run:
the UI sends the user to Windows' own "Apps & features" to uninstall).
"""

import hashlib
import os
import re
import winreg

_UNINSTALL = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
_UNINSTALL_32 = r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"

# (hive, hive label, subkey, view flag, scope, arch)
_SOURCES = [
    (winreg.HKEY_LOCAL_MACHINE, "HKLM", _UNINSTALL, winreg.KEY_WOW64_64KEY, "machine", "x64"),
    (winreg.HKEY_LOCAL_MACHINE, "HKLM32", _UNINSTALL_32, winreg.KEY_WOW64_64KEY, "machine", "x86"),
    (winreg.HKEY_CURRENT_USER, "HKCU", _UNINSTALL, 0, "user", None),
]

_UPDATE_RELEASES = {"update", "hotfix", "security update", "update rollup", "service pack"}


def _val(key, name: str):
    try:
        return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def _text(v) -> str | None:
    if isinstance(v, str):
        v = v.strip()
        return v or None
    return None


def _date(v) -> str | None:
    s = str(v).strip() if v is not None else ""
    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", s)
    if m and 1 <= int(m[2]) <= 12 and 1 <= int(m[3]) <= 31 and int(m[1]) >= 1980:
        return f"{m[1]}-{m[2]}-{m[3]}"
    return None


def _location(v) -> str | None:
    s = _text(v)
    if not s:
        return None
    s = s.strip('"').strip()
    try:
        return s if s and os.path.isabs(s) and os.path.isdir(s) else None
    except (OSError, ValueError):
        return None


def _size_kb(v) -> int | None:
    if isinstance(v, int) and not isinstance(v, bool) and v > 0:
        return v * 1024
    return None


def _read_entry(hive, label, sub, flag, name, scope, arch) -> dict | None:
    try:
        with winreg.OpenKey(hive, sub + "\\" + name, 0, winreg.KEY_READ | flag) as k:
            dn = _text(_val(k, "DisplayName"))
            if not dn or _val(k, "SystemComponent") == 1 or _text(_val(k, "ParentKeyName")):
                return None
            rel = _text(_val(k, "ReleaseType"))
            if rel and rel.lower() in _UPDATE_RELEASES:
                return None
            return {
                "id": hashlib.sha1(f"{label}\\{name}".lower().encode("utf-8", "replace")).hexdigest()[:12],
                "name": dn, "publisher": _text(_val(k, "Publisher")), "version": _text(_val(k, "DisplayVersion")),
                "install_date": _date(_val(k, "InstallDate")),
                "size": _size_kb(_val(k, "EstimatedSize")),
                "size_source": None,
                "location": _location(_val(k, "InstallLocation")),
                "scope": scope, "arch": arch,
            }
    except OSError:
        return None


def list_installed_apps(scan=None) -> list[dict]:
    """Installed programs, largest first.

    Sources: HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall (64-bit view),
    HKLM\\SOFTWARE\\WOW6432Node\\...\\Uninstall, HKCU\\...\\Uninstall.
    Skip entries without DisplayName, with SystemComponent=1, with a
    ParentKeyName, or whose ReleaseType is an update/hotfix. Deduplicate on
    (name, version).

    If ``scan`` (a dm.scanner.Scan) is given and contains InstallLocation,
    use the scanned folder size (more accurate than EstimatedSize).

    Each item:
    {
      "id": "stable short id (hash of hive+key)",
      "name": str, "publisher": str | None, "version": str | None,
      "install_date": "YYYY-MM-DD" | None,
      "size": int | None,                      # bytes
      "size_source": "scan" | "registry" | None,
      "location": str | None,                  # InstallLocation if it exists on disk
      "scope": "machine" | "user",
      "arch": "x64" | "x86" | None,
    }
    """
    seen: set[tuple[str, str]] = set()
    apps: list[dict] = []
    for hive, label, sub, flag, scope, arch in _SOURCES:
        try:
            root = winreg.OpenKey(hive, sub, 0, winreg.KEY_READ | flag)
        except OSError:
            continue
        with root:
            names = []
            i = 0
            while True:
                try:
                    names.append(winreg.EnumKey(root, i))
                except OSError:
                    break
                i += 1
        for name in names:
            app = _read_entry(hive, label, sub, flag, name, scope, arch)
            if app is None:
                continue
            dk = (app["name"].lower(), (app["version"] or "").lower())
            if dk in seen:
                continue
            seen.add(dk)
            apps.append(app)

    for app in apps:
        if app["size"] is not None:
            app["size_source"] = "registry"

    if scan is not None:
        # Several entries often share one folder (Office language packs, suites):
        # the measured size goes to a single entry so totals are not inflated.
        by_loc: dict[str, list[dict]] = {}
        for app in apps:
            loc = app["location"]
            # Skip broad folders (C:\, C:\Program Files) that would swallow other apps.
            if loc and len(os.path.normpath(loc).rstrip("\\").split("\\")) >= 3:
                by_loc.setdefault(os.path.normcase(os.path.normpath(loc)), []).append(app)
        for group in by_loc.values():
            try:
                node = scan.find(group[0]["location"]) if scan.contains(group[0]["location"]) else None
            except Exception:
                node = None
            if node is None:
                continue
            owner = max(group, key=lambda a: (a["size"] or 0, -len(a["name"])))
            owner["size"], owner["size_source"] = node.size, "scan"
            for other in group:
                if other is not owner:
                    other["shared_with"] = owner["name"]
    apps.sort(key=lambda a: (a["size"] is None, -(a["size"] or 0), a["name"].lower()))
    return apps
