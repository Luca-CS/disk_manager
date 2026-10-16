"""Desktop / Start-menu shortcut with a generated icon (pure Python, no PIL)."""

import math
import os
import struct
import subprocess
import sys
import winreg
import zlib

from . import config

ICON_PATH = config.DATA_DIR / "disk_manager.ico"

# Same drawing as web/img/logo.svg: rounded square + three ring segments.
_BG = (0x1C, 0x5C, 0xAB)
_SEGMENTS = [  # (start deg, end deg, color) clockwise from 12 o'clock
    (0, 68, (0x86, 0xB6, 0xEF)),
    (92, 160, (0x1B, 0xAF, 0x7A)),
    (188, 268, (0xED, 0xA1, 0x00)),
]


def _pixel(x: float, y: float) -> tuple[int, int, int, int]:
    """Color at (x, y) in a 64×64 design space."""
    r = 16.0
    dx = max(abs(x - 32) - (32 - r), 0)
    dy = max(abs(y - 32) - (32 - r), 0)
    if dx * dx + dy * dy > r * r:
        return (0, 0, 0, 0)
    d = math.hypot(x - 32, y - 32)
    if d <= 4.5:
        return (255, 255, 255, 255)
    if 13 <= d <= 21:
        ang = (math.degrees(math.atan2(x - 32, 32 - y)) + 360) % 360
        for a0, a1, col in _SEGMENTS:
            if a0 <= ang <= a1:
                return (*col, 255)
        return (0x49, 0x77, 0xBC, 255)  # faint track (white 18% over the background)
    return (*_BG, 255)


def _render(size: int, ss: int = 4) -> bytes:
    rows = []
    scale = 64.0 / size
    for py in range(size):
        row = bytearray([0])  # PNG filter: none
        for px in range(size):
            acc = [0, 0, 0, 0]
            for sy in range(ss):
                for sx in range(ss):
                    c = _pixel((px + (sx + 0.5) / ss) * scale, (py + (sy + 0.5) / ss) * scale)
                    a = c[3]
                    acc[0] += c[0] * a
                    acc[1] += c[1] * a
                    acc[2] += c[2] * a
                    acc[3] += a
            n = ss * ss
            a = acc[3]
            row += bytes([acc[0] // a if a else 0, acc[1] // a if a else 0, acc[2] // a if a else 0, a // n])
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def build_icon() -> str:
    """Write data/disk_manager.ico (PNG-compressed entries) and return its path."""
    sizes = [16, 24, 32, 48, 64, 128, 256]
    images = [_render(s, ss=4 if s <= 64 else 2) for s in sizes]
    header = struct.pack("<HHH", 0, 1, len(sizes))
    offset = 6 + 16 * len(sizes)
    entries = b""
    for s, img in zip(sizes, images):
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(img), offset)
        offset += len(img)
    config.ensure_data_dirs()
    with open(ICON_PATH, "wb") as f:
        f.write(header + entries + b"".join(images))
    return str(ICON_PATH)


def _desktop_dir() -> str:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as k:
            return os.path.expandvars(winreg.QueryValueEx(k, "Desktop")[0])
    except OSError:
        return os.path.join(config.USERPROFILE, "Desktop")


def _pythonw() -> str:
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        candidate = exe[:-10] + "pythonw.exe"
        if os.path.exists(candidate):
            return candidate
    return exe


def install_shortcuts() -> list[str]:
    """Create 'Disk Manager.lnk' on the Desktop and in the Start menu."""
    icon = build_icon()
    run_py = str(config.ROOT_DIR / "run.py")
    targets = [
        os.path.join(_desktop_dir(), "Disk Manager.lnk"),
        os.path.join(config.APPDATA, "Microsoft", "Windows", "Start Menu", "Programs", "Disk Manager.lnk"),
    ]
    # Fixed script; every value is passed through environment variables, never interpolated.
    script = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:DM_LNK);"
              "$s.TargetPath=$env:DM_TARGET;$s.Arguments=$env:DM_ARGS;$s.WorkingDirectory=$env:DM_CWD;"
              "$s.IconLocation=$env:DM_ICON;$s.Description='Disk Manager - analyse et nettoyage du stockage';$s.Save()")
    ps = os.path.join(config.SYSTEM_ROOT, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    made = []
    for lnk in targets:
        env = {**os.environ, "DM_LNK": lnk, "DM_TARGET": _pythonw(), "DM_ARGS": f'"{run_py}"',
               "DM_CWD": str(config.ROOT_DIR), "DM_ICON": icon + ",0"}
        r = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command", script], env=env,
                           capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
        if r.returncode == 0 and os.path.exists(lnk):
            made.append(lnk)
    return made
