"""Thin ctypes wrappers around the Win32 APIs the app needs.

No subprocesses here: everything goes through kernel32/shell32/ole32 so that
nothing is ever interpreted by a shell.
"""

import ctypes
import os
import string
import sys
from ctypes import wintypes

if sys.platform != "win32":  # pragma: no cover - the app is Windows-only
    raise ImportError("Disk Manager only runs on Windows")

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
ole32 = ctypes.WinDLL("ole32", use_last_error=True)

# --- error mode: never show "insert a disk" dialogs when probing drives -----
SEM_FAILCRITICALERRORS = 0x0001
SEM_NOOPENFILEERRORBOX = 0x8000
kernel32.SetErrorMode.argtypes = [wintypes.UINT]
kernel32.SetErrorMode.restype = wintypes.UINT
kernel32.SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOOPENFILEERRORBOX)

# --- file attribute / reparse constants (shared with scanner & safety) -------
FILE_ATTRIBUTE_READONLY = 0x1
FILE_ATTRIBUTE_HIDDEN = 0x2
FILE_ATTRIBUTE_SYSTEM = 0x4
FILE_ATTRIBUTE_DIRECTORY = 0x10
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
FILE_ATTRIBUTE_OFFLINE = 0x1000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
# Cloud placeholders (OneDrive "online-only"...): not stored locally, and
# reading them would trigger a download — never hash or count them.
CLOUD_ATTRS = FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
# Reparse tags with the name-surrogate bit are links (junctions, symlinks,
# mount points): they point elsewhere and must never be followed.
REPARSE_NAME_SURROGATE = 0x20000000


def is_link_stat(st) -> bool:
    """True if an lstat() result describes a junction / symlink / mount point."""
    attrs = getattr(st, "st_file_attributes", 0)
    if not attrs & FILE_ATTRIBUTE_REPARSE_POINT:
        return False
    return bool(getattr(st, "st_reparse_tag", 0) & REPARSE_NAME_SURROGATE)


# --- privileges --------------------------------------------------------------
shell32.IsUserAnAdmin.restype = wintypes.BOOL


def is_admin() -> bool:
    try:
        return bool(shell32.IsUserAnAdmin())
    except OSError:
        return False


# --- memory ------------------------------------------------------------------
class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MEMORYSTATUSEX)]
kernel32.GlobalMemoryStatusEx.restype = wintypes.BOOL


def memory_status() -> dict:
    m = MEMORYSTATUSEX()
    m.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
        return {"total": 0, "available": 0}
    return {"total": m.ullTotalPhys, "available": m.ullAvailPhys}


# --- volumes -----------------------------------------------------------------
kernel32.GetLogicalDrives.restype = wintypes.DWORD
kernel32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
kernel32.GetDriveTypeW.restype = wintypes.UINT
kernel32.GetDiskFreeSpaceExW.argtypes = [
    wintypes.LPCWSTR,
    ctypes.POINTER(ctypes.c_ulonglong),
    ctypes.POINTER(ctypes.c_ulonglong),
    ctypes.POINTER(ctypes.c_ulonglong),
]
kernel32.GetDiskFreeSpaceExW.restype = wintypes.BOOL
kernel32.GetVolumeInformationW.argtypes = [
    wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
    wintypes.LPWSTR, wintypes.DWORD,
]
kernel32.GetVolumeInformationW.restype = wintypes.BOOL

DRIVE_TYPES = {2: "removable", 3: "fixed", 4: "network", 5: "cdrom", 6: "ramdisk"}


def disk_usage(root: str) -> tuple[int, int] | None:
    """(total_bytes, free_bytes) for a volume root such as 'C:\\'."""
    free_caller = ctypes.c_ulonglong()
    total = ctypes.c_ulonglong()
    free_total = ctypes.c_ulonglong()
    if not kernel32.GetDiskFreeSpaceExW(root, ctypes.byref(free_caller), ctypes.byref(total), ctypes.byref(free_total)):
        return None
    return total.value, free_total.value


def list_volumes() -> list[dict]:
    """Mounted drive letters with label, file system, size and free space."""
    mask = kernel32.GetLogicalDrives()
    out = []
    for i, letter in enumerate(string.ascii_uppercase):
        if not mask & (1 << i):
            continue
        root = f"{letter}:\\"
        dtype = DRIVE_TYPES.get(kernel32.GetDriveTypeW(root))
        if dtype is None or dtype == "cdrom":
            continue
        label = ctypes.create_unicode_buffer(261)
        fs = ctypes.create_unicode_buffer(261)
        serial = wintypes.DWORD()
        maxlen = wintypes.DWORD()
        flags = wintypes.DWORD()
        if not kernel32.GetVolumeInformationW(root, label, 261, ctypes.byref(serial), ctypes.byref(maxlen),
                                              ctypes.byref(flags), fs, 261):
            continue  # no media / not ready
        usage = disk_usage(root)
        if usage is None:
            continue
        total, free = usage
        out.append({
            "letter": letter,
            "root": root,
            "label": label.value,
            "fs": fs.value,
            "type": dtype,
            "total": total,
            "free": free,
            "used": total - free,
            "is_system": root.lower() == (os.environ.get("SystemDrive", "C:") + "\\").lower(),
        })
    return out


# --- recycle bin -------------------------------------------------------------
class SHQUERYRBINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("i64Size", ctypes.c_longlong), ("i64NumItems", ctypes.c_longlong)]


shell32.SHQueryRecycleBinW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(SHQUERYRBINFO)]
shell32.SHQueryRecycleBinW.restype = ctypes.c_long
shell32.SHEmptyRecycleBinW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.DWORD]
shell32.SHEmptyRecycleBinW.restype = ctypes.c_long
SHERB_NOCONFIRMATION, SHERB_NOPROGRESSUI, SHERB_NOSOUND = 0x1, 0x2, 0x4


def recycle_bin_info(root: str | None = None) -> dict:
    info = SHQUERYRBINFO()
    info.cbSize = ctypes.sizeof(SHQUERYRBINFO)
    hr = shell32.SHQueryRecycleBinW(root, ctypes.byref(info))
    if hr != 0:
        return {"size": 0, "count": 0, "ok": False}
    return {"size": int(info.i64Size), "count": int(info.i64NumItems), "ok": True}


def empty_recycle_bin(root: str | None = None) -> bool:
    hr = shell32.SHEmptyRecycleBinW(None, root, SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND)
    # S_OK, or E_UNEXPECTED (0x8000FFFF) when the bin was already empty.
    return hr == 0 or (hr & 0xFFFFFFFF) == 0x8000FFFF


# --- send to recycle bin (SHFileOperationW, FOF_ALLOWUNDO) ----------------------
class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", ctypes.c_void_p),
        ("pTo", ctypes.c_void_p),
        ("fFlags", ctypes.c_ushort),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


shell32.SHFileOperationW.argtypes = [ctypes.POINTER(SHFILEOPSTRUCTW)]
shell32.SHFileOperationW.restype = ctypes.c_int
FO_DELETE = 3
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040
FOF_NOCONFIRMMKDIR = 0x0200
FOF_NOERRORUI = 0x0400
# If an item is too big for the recycle bin Windows would silently destroy it
# under FOF_NOCONFIRMATION; this flag makes it ask the user instead.
FOF_WANTNUKEWARNING = 0x4000


def send_to_recycle_bin(paths: list[str]) -> tuple[int, bool]:
    """Move absolute paths to the recycle bin. Returns (error_code, aborted)."""
    if not paths:
        return 0, False
    buf = ctypes.create_unicode_buffer("\0".join(paths) + "\0\0")
    op = SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = FO_DELETE
    op.pFrom = ctypes.cast(buf, ctypes.c_void_p)
    op.pTo = None
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI | FOF_NOCONFIRMMKDIR | FOF_WANTNUKEWARNING
    hr_init = ole32.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED
    try:
        rc = shell32.SHFileOperationW(ctypes.byref(op))
    finally:
        if hr_init in (0, 1):
            ole32.CoUninitialize()
    return rc, bool(op.fAnyOperationsAborted)


# --- handle-based deletion (no path re-resolution between check and delete) ----
DELETE = 0x00010000
FILE_LIST_DIRECTORY = 0x0001  # data access: unlike attribute-only opens, takes part in share checks
FILE_READ_ATTRIBUTES = 0x0080
SYNCHRONIZE = 0x00100000
FILE_SHARE_ALL = 0x1 | 0x2 | 0x4
FILE_SHARE_READ_WRITE = 0x1 | 0x2  # no FILE_SHARE_DELETE: nobody can rename/delete it while held
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FileAttributeTagInfo = 9
_FileDispositionInfo = 4
_FileDispositionInfoEx = 21
_DISPOSITION_DELETE = 0x1
_DISPOSITION_POSIX = 0x2
_DISPOSITION_IGNORE_READONLY = 0x10
ERROR_ACCESS_DENIED = 5
ERROR_SHARING_VIOLATION = 32
ERROR_DIR_NOT_EMPTY = 145


class _FILE_ATTRIBUTE_TAG_INFO(ctypes.Structure):
    _fields_ = [("FileAttributes", wintypes.DWORD), ("ReparseTag", wintypes.DWORD)]


class _FILE_DISPOSITION_INFO_EX(ctypes.Structure):
    _fields_ = [("Flags", wintypes.DWORD)]


class _FILE_DISPOSITION_INFO(ctypes.Structure):
    _fields_ = [("DeleteFile", wintypes.BOOLEAN)]


kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                 wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
kernel32.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
kernel32.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
kernel32.SetFileInformationByHandle.restype = wintypes.BOOL


class Handle:
    """A file or directory opened WITHOUT following reparse points.

    Everything is then checked and done through this handle, so swapping a
    path component for a junction after the check cannot redirect the action.
    """

    def __init__(self, path: str, access: int, share: int = FILE_SHARE_ALL):
        self.h = kernel32.CreateFileW(path, access | SYNCHRONIZE, share, None, OPEN_EXISTING,
                                      FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, None)
        self.error = ctypes.get_last_error() if (not self.h or self.h == INVALID_HANDLE_VALUE) else 0
        if self.error:
            self.h = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self) -> None:
        if self.h:
            kernel32.CloseHandle(self.h)
            self.h = None

    def final_path(self) -> str | None:
        """Real path of the opened object, as the OS resolves it (long names, no links)."""
        buf = ctypes.create_unicode_buffer(32768)
        n = kernel32.GetFinalPathNameByHandleW(self.h, buf, 32768, 0)
        if not n or n >= 32768:
            return None
        p = buf.value
        if p.startswith("\\\\?\\UNC\\"):
            return None  # network path: never acted upon
        return p[4:] if p.startswith("\\\\?\\") else p

    def attributes(self) -> tuple[int, int] | None:
        """(file attributes, reparse tag) read from the handle."""
        info = _FILE_ATTRIBUTE_TAG_INFO()
        if not kernel32.GetFileInformationByHandleEx(self.h, _FileAttributeTagInfo, ctypes.byref(info),
                                                     ctypes.sizeof(info)):
            return None
        return info.FileAttributes, info.ReparseTag

    def delete(self) -> int:
        """Mark for deletion (applied on close). Returns 0 or a Win32 error code."""
        info = _FILE_DISPOSITION_INFO_EX(_DISPOSITION_DELETE | _DISPOSITION_POSIX | _DISPOSITION_IGNORE_READONLY)
        if kernel32.SetFileInformationByHandle(self.h, _FileDispositionInfoEx, ctypes.byref(info),
                                               ctypes.sizeof(info)):
            return 0
        err = ctypes.get_last_error()
        if err not in (1, 50, 87):  # not supported by this file system: classic disposition
            return err
        legacy = _FILE_DISPOSITION_INFO(True)
        if kernel32.SetFileInformationByHandle(self.h, _FileDispositionInfo, ctypes.byref(legacy),
                                               ctypes.sizeof(legacy)):
            return 0
        return ctypes.get_last_error()


# --- processes -----------------------------------------------------------------
class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32FirstW.restype = wintypes.BOOL
kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32NextW.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value


def running_processes() -> list[tuple[int, int, str]]:
    """(pid, parent_pid, lower-cased image name) of every running process."""
    snap = kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snap or snap == INVALID_HANDLE_VALUE:
        return []
    out = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            out.append((entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile.lower()))
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    return out


def running_process_names(exclude_tree_of: set[int] | None = None) -> set[str]:
    """Lower-cased image names of running processes (e.g. 'chrome.exe').

    Processes descending from a pid in `exclude_tree_of` are ignored, so the
    app's own Edge window does not count as "Edge is open".
    """
    procs = running_processes()
    excluded = set(exclude_tree_of or ())
    if excluded:
        children: dict[int, list[int]] = {}
        for pid, ppid, _ in procs:
            children.setdefault(ppid, []).append(pid)
        stack = list(excluded)
        while stack:
            for c in children.get(stack.pop(), ()):
                if c not in excluded:
                    excluded.add(c)
                    stack.append(c)
    return {name for pid, _, name in procs if pid not in excluded}


def pid_alive(pid: int) -> bool:
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    try:
        code = wintypes.DWORD()
        return bool(kernel32.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(h)


# --- shell integration -----------------------------------------------------------
shell32.ShellExecuteW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                  wintypes.LPCWSTR, ctypes.c_int]
shell32.ShellExecuteW.restype = ctypes.c_void_p
shell32.ILCreateFromPathW.argtypes = [wintypes.LPCWSTR]
shell32.ILCreateFromPathW.restype = ctypes.c_void_p
shell32.ILFree.argtypes = [ctypes.c_void_p]
shell32.ILFree.restype = None
shell32.SHOpenFolderAndSelectItems.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p, wintypes.DWORD]
shell32.SHOpenFolderAndSelectItems.restype = ctypes.c_long
ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
ole32.CoInitializeEx.restype = ctypes.c_long
ole32.CoUninitialize.argtypes = []
ole32.CoUninitialize.restype = None


def _shell_execute(verb: str | None, target: str, params: str | None = None) -> bool:
    rc = shell32.ShellExecuteW(None, verb, target, params, None, 1)  # SW_SHOWNORMAL
    return (rc or 0) > 32


def reveal_in_explorer(path: str) -> bool:
    """Open an Explorer window on the parent folder with `path` selected.

    Never executes anything: files are only selected, not opened.
    """
    hr_init = ole32.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED
    try:
        pidl = shell32.ILCreateFromPathW(path)
        if not pidl:
            return False
        try:
            return shell32.SHOpenFolderAndSelectItems(pidl, 0, None, 0) == 0
        finally:
            shell32.ILFree(pidl)
    finally:
        if hr_init in (0, 1):  # S_OK / S_FALSE
            ole32.CoUninitialize()


def open_folder(path: str) -> bool:
    """Open a directory in Explorer ('explore' only works on folders)."""
    if not os.path.isdir(path):
        return False
    return _shell_execute("explore", path)


def open_system_target(target: str, params: str | None = None) -> bool:
    """Open a fixed, whitelisted Windows tool. Never called with user-supplied text."""
    return _shell_execute("open", target, params)


def relaunch_as_admin(executable: str, params: str, cwd: str) -> bool:
    rc = shell32.ShellExecuteW(None, "runas", executable, params, cwd, 1)
    return (rc or 0) > 32
