"""Shared helpers: sandbox management, junctions, fake recycle bin.

Every filesystem change made by the tests happens inside a sandbox created
under dm.config.TEMP. Nothing is ever sent to the real recycle bin.
"""

import ctypes
import os
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
from ctypes import wintypes
from pathlib import Path
from unittest import mock

from dm import config, winapi


def mkjunction(link: str, target: str) -> bool:
    r = subprocess.run(["cmd", "/c", "mklink", "/J", link, target], capture_output=True)
    return r.returncode == 0 and os.path.isdir(link)


def _onexc(func, path, exc):
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def remove_links(root: str) -> None:
    """rmdir every junction/symlink under root (never followed)."""
    for cur, dirs, files in os.walk(root, followlinks=False):
        for name in list(dirs) + list(files):
            p = os.path.join(cur, name)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            if winapi.is_link_stat(st):
                try:
                    if st.st_file_attributes & winapi.FILE_ATTRIBUTE_DIRECTORY:
                        os.rmdir(p)
                    else:
                        os.unlink(p)
                except OSError:
                    pass
                if name in dirs:
                    dirs.remove(name)


def destroy_sandbox(path: str) -> None:
    assert os.path.normcase(path).startswith(os.path.normcase(config.TEMP)), path
    assert os.path.basename(path).startswith("dm_test_"), path
    remove_links(path)
    shutil.rmtree(path, onexc=_onexc)


def write(path: str, size: int = 0, data: bytes | None = None) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data if data is not None else b"x" * size)
    return path


def set_age(path: str, age_s: float) -> None:
    """Make a file look `age_s` seconds old: modification AND creation times
    (dm.safety treats max(mtime, birthtime) as the file's age)."""
    t = time.time() - age_s
    ft = int((t + 11644473600) * 10_000_000)
    ftime = wintypes.FILETIME(ft & 0xFFFFFFFF, ft >> 32)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k32.SetFileTime.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    # FILE_WRITE_ATTRIBUTES, share all, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS
    h = k32.CreateFileW(path, 0x100, 7, None, 3, 0x02000000, None)
    if h in (None, wintypes.HANDLE(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        p = ctypes.byref(ftime)
        if not k32.SetFileTime(h, p, p, p):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        k32.CloseHandle(h)


class SandboxCase(unittest.TestCase):
    """Creates self.sb (a fresh sandbox) and redirects journal/scans/history into it."""

    def setUp(self):
        self.sb = os.path.realpath(tempfile.mkdtemp(dir=config.TEMP, prefix="dm_test_"))
        self.addCleanup(destroy_sandbox, self.sb)
        self.data = os.path.join(self.sb, "_data")
        os.makedirs(os.path.join(self.data, "scans"))
        self.journal_path = Path(self.data) / "journal.jsonl"
        scans = Path(self.data) / "scans"

        def ensure():
            scans.mkdir(parents=True, exist_ok=True)

        from dm import history
        patches = [
            mock.patch.object(config, "JOURNAL_PATH", self.journal_path),
            mock.patch.object(config, "SCANS_DIR", scans),
            mock.patch.object(config, "ensure_data_dirs", ensure),
            mock.patch.object(history, "SNAPSHOTS", scans / "snapshots.jsonl"),
            mock.patch.object(history, "USAGE", scans / "usage.jsonl"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def path(self, *parts: str) -> str:
        return os.path.join(self.sb, *parts)

    def journal_lines(self) -> list[str]:
        try:
            return self.journal_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []

    def junction(self, link: str, target: str) -> None:
        if not mkjunction(link, target):
            self.skipTest("cannot create junction")


class FakeRecycle:
    """Replacement for winapi.send_to_recycle_bin: deletes sandbox paths for real."""

    def __init__(self, sandbox: str):
        self.sandbox = os.path.normcase(sandbox)
        self.calls: list[list[str]] = []

    def __call__(self, paths):
        self.calls.append(list(paths))
        for p in paths:
            if not os.path.normcase(p).startswith(self.sandbox + "\\"):
                raise AssertionError(f"fake recycle bin refused non-sandbox path {p}")
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p, onexc=_onexc)
            elif os.path.lexists(p):
                os.remove(p)
        return 0, False
