"""Physical disks, partitions and volumes.

Implementation rules
* Data comes from ONE fixed PowerShell script (no user input ever interpolated),
  run with the absolute path %SystemRoot%\\System32\\WindowsPowerShell\\v1.0\\powershell.exe,
  args ["-NoProfile", "-NonInteractive", "-NoLogo", "-Command", SCRIPT],
  creationflags=subprocess.CREATE_NO_WINDOW, timeout 30 s, output = ConvertTo-Json -Compress.
* Cmdlets: Get-PhysicalDisk, Get-Disk, Get-Partition, Get-Volume and, wrapped in
  try/catch because it needs admin, Get-StorageReliabilityCounter.
* Cache the result for 5 minutes (thread-safe); ``refresh=True`` forces a new run.
* Never raise: on failure return the same shape with ``disks=[]`` and ``error`` set.
"""

import json
import os
import subprocess
import threading
import time

from . import config, winapi

_CACHE_TTL_S = 300
_lock = threading.Lock()
_cache: tuple[float, list] | None = None

_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
function L($c) { if ($c -and [int][char]$c -ne 0) { "$c" } else { '' } }
$disks = @(Get-Disk | ForEach-Object { [pscustomobject]@{
    Number = [int]$_.Number; Style = "$($_.PartitionStyle)"; IsBoot = [bool]$_.IsBoot; IsSystem = [bool]$_.IsSystem } })
$phys = @(Get-PhysicalDisk | ForEach-Object {
    $rel = $null
    try {
        $r = $_ | Get-StorageReliabilityCounter -ErrorAction Stop
        if ($r) { $rel = [pscustomobject]@{ Temperature = $r.Temperature; TemperatureMax = $r.TemperatureMax;
            Wear = $r.Wear; PowerOnHours = $r.PowerOnHours; ReadErrors = $r.ReadErrorsTotal; WriteErrors = $r.WriteErrorsTotal } }
    } catch { $rel = $null }
    [pscustomobject]@{ DeviceId = "$($_.DeviceId)"; Name = "$($_.FriendlyName)"; Media = "$($_.MediaType)"; Bus = "$($_.BusType)";
        Health = "$($_.HealthStatus)"; Status = "$(@($_.OperationalStatus)[0])"; Size = [int64]$_.Size; Rel = $rel } })
$parts = @(Get-Partition | ForEach-Object { [pscustomobject]@{
    Disk = [int]$_.DiskNumber; Number = [int]$_.PartitionNumber; Letter = (L $_.DriveLetter); Type = "$($_.Type)";
    Offset = [int64]$_.Offset; Size = [int64]$_.Size; Paths = @($_.AccessPaths | ForEach-Object { "$_" });
    IsBoot = [bool]$_.IsBoot; IsSystem = [bool]$_.IsSystem; Gpt = "$($_.GptType)" } })
$vols = @(Get-Volume | ForEach-Object { [pscustomobject]@{
    Path = "$($_.Path)"; Id = "$($_.UniqueId)"; Letter = (L $_.DriveLetter); Label = "$($_.FileSystemLabel)";
    Fs = "$($_.FileSystem)"; Size = [int64]$_.Size; Remaining = [int64]$_.SizeRemaining; Health = "$($_.HealthStatus)" } })
[pscustomobject]@{ Disks = $disks; Phys = $phys; Parts = $parts; Vols = $vols } | ConvertTo-Json -Depth 5 -Compress
"""

_MEDIA = {"0": "Unspecified", "3": "HDD", "4": "SSD", "5": "SCM"}
_BUS = {"1": "SCSI", "2": "ATAPI", "3": "ATA", "4": "1394", "5": "SSA", "6": "Fibre Channel", "7": "USB", "8": "RAID",
        "9": "iSCSI", "10": "SAS", "11": "SATA", "12": "SD", "13": "MMC", "14": "Virtual", "15": "File-Backed Virtual",
        "16": "Storage Spaces", "17": "NVMe"}
_HEALTH = {"0": "Healthy", "1": "Warning", "2": "Unhealthy", "5": "Unknown"}
_EFI_GUID = "{c12a7328-f81f-11d2-ba4b-00a0c93ec93b}"


def _as_list(v) -> list:
    """ConvertTo-Json collapses 1-element arrays to objects and empty ones to null."""
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _int(v) -> int | None:
    try:
        return None if v is None else int(v)
    except (TypeError, ValueError):
        return None


def _health(v) -> str:
    v = str(v or "").strip()
    v = _HEALTH.get(v, v)
    return v if v in ("Healthy", "Warning", "Unhealthy") else "Unknown"


def _letter(v) -> str | None:
    v = (v or "").strip()
    return v[0].upper() if v and v[0].isalpha() else None


def _kind(p: dict, letter: str | None, system_letter: str) -> str:
    low = (p.get("Type") or "").strip().lower()
    if (p.get("Gpt") or "").strip().lower() == _EFI_GUID or low == "system":
        return "efi"
    if low == "reserved":
        return "msr"
    if low == "recovery":
        return "recovery"
    if letter and low in ("basic", "ifs", "logical", "primary"):
        return "os" if letter == system_letter else "data"
    return "data" if letter else "other"


def _build(raw: dict) -> list[dict]:
    system_letter = config.SYSTEM_DRIVE[:1].upper()
    vols = {}
    for v in _as_list(raw.get("Vols")):
        for k in (v.get("Path"), v.get("Id")):
            if k:
                vols[k.lower()] = v
    phys = {str(p.get("DeviceId")): p for p in _as_list(raw.get("Phys"))}
    parts = _as_list(raw.get("Parts"))
    out = []
    for d in sorted(_as_list(raw.get("Disks")), key=lambda x: x.get("Number", 0)):
        num = d.get("Number")
        ph = phys.get(str(num)) or {}
        rel = ph.get("Rel")
        if isinstance(rel, dict):
            rel = {"temperature": _int(rel.get("Temperature")), "temperature_max": _int(rel.get("TemperatureMax")),
                   "wear": _int(rel.get("Wear")), "power_on_hours": _int(rel.get("PowerOnHours")),
                   "read_errors": _int(rel.get("ReadErrors")), "write_errors": _int(rel.get("WriteErrors"))}
        else:
            rel = None
        style = (d.get("Style") or "").upper()
        plist = []
        for p in sorted((x for x in parts if x.get("Disk") == num), key=lambda x: x.get("Offset", 0)):
            vol = None
            for ap in _as_list(p.get("Paths")):
                vol = vols.get(str(ap).lower())
                if vol:
                    break
            letter = _letter(p.get("Letter")) or (_letter(vol.get("Letter")) if vol else None)
            vsize, rem = (vol.get("Size"), vol.get("Remaining")) if vol else (None, None)
            have = bool(vol) and bool(vsize)
            plist.append({
                "number": p.get("Number"), "letter": letter,
                "type": p.get("Type") or "Unknown", "kind": _kind(p, letter, system_letter),
                "offset": p.get("Offset", 0), "size": p.get("Size", 0),
                "label": (vol.get("Label") or None) if vol else None,
                "fs": (vol.get("Fs") or None) if vol else None,
                "free": rem if have else None, "used": (vsize - rem) if have else None,
                "health": _health(vol.get("Health")) if vol else None,
                "is_boot": bool(p.get("IsBoot")), "is_system": bool(p.get("IsSystem")),
            })
        out.append({
            "number": num,
            "model": (ph.get("Name") or "").strip() or "Unknown disk",
            "media": _MEDIA.get(str(ph.get("Media")), ph.get("Media") or "Unspecified"),
            "bus": _BUS.get(str(ph.get("Bus")), ph.get("Bus") or "Unknown"),
            "health": _health(ph.get("Health")),
            "status": ph.get("Status") or "Unknown",
            "size": ph.get("Size") or sum(x["size"] for x in plist),
            "partition_style": style if style in ("GPT", "MBR", "RAW") else None,
            "reliability": rel,
            "partitions": plist,
        })
    return out


def _collect() -> list[dict]:
    exe = os.path.join(config.SYSTEM_ROOT, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    proc = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-NoLogo", "-Command", _SCRIPT],
                          capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    out = proc.stdout.decode("utf-8", "replace").strip().lstrip("﻿")
    if proc.returncode != 0 or not out:
        err = proc.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(err[:300] or f"PowerShell exited with code {proc.returncode}")
    return _build(json.loads(out))


def _volumes() -> list:
    try:
        return winapi.list_volumes()
    except Exception:
        return []


def get_system_info(refresh: bool = False) -> dict:
    """Return:

    {
      "disks": [{
        "number": 0,
        "model": "NVMe SSD 256 GB",
        "media": "SSD" | "HDD" | "Unspecified",
        "bus": "NVMe" | "SATA" | "USB" | ...,
        "health": "Healthy" | "Warning" | "Unhealthy" | "Unknown",
        "status": "OK" | ...,                       # OperationalStatus
        "size": 256060514304,                       # bytes
        "partition_style": "GPT" | "MBR" | "RAW" | None,
        "reliability": None | {"temperature": int|None, "temperature_max": int|None,
                               "wear": int|None, "power_on_hours": int|None,
                               "read_errors": int|None, "write_errors": int|None},
        "partitions": [{                            # sorted by offset
          "number": 3, "letter": "C" | None,
          "type": "Basic" | "System" | "Reserved" | "Recovery" | ...,   # raw Get-Partition Type
          "kind": "efi" | "msr" | "os" | "data" | "recovery" | "other",
          "offset": int, "size": int,
          "label": str | None, "fs": "NTFS" | "FAT32" | None,
          "free": int | None, "used": int | None,    # from Get-Volume when a volume exists
          "health": str | None,
          "is_boot": bool, "is_system": bool,
        }],
      }],
      "volumes": winapi.list_volumes(),   # fresh on every call (cheap)
      "error": None | "message",
      "collected_at": epoch seconds,
    }
    """
    global _cache
    with _lock:
        if not refresh and _cache and time.time() - _cache[0] < _CACHE_TTL_S:
            return {"disks": _cache[1], "volumes": _volumes(), "error": None, "collected_at": _cache[0]}
        try:
            disks, err = _collect(), None
        except Exception as e:
            disks, err = [], f"{type(e).__name__}: {e}"[:300]
        ts = time.time()
        if err is None:
            _cache = (ts, disks)
    return {"disks": disks, "volumes": _volumes(), "error": err, "collected_at": ts}
