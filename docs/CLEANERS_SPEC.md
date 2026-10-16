# Cleanup rules — implementation spec for `dm/cleaners.py`

Contract (signatures, return shapes): see the docstrings in `dm/cleaners.py`. Keep them exactly.

## Hard rules

- **Never delete directly.** Deletion only through `dm.safety`:
  - mode `purge`  → `safety.purge_many(targets, rule_id=..., cancel=..., on_progress=...)` where each target is
    `{"path", "min_age_s", "keep_root", "name_glob"}` (`name_glob` = compiled `re.Pattern` or `None`);
  - mode `recycle` → `safety.recycle(paths, source="clean:<rule_id>", cancel=...)`;
  - mode `recycle_bin` → `safety.empty_recycle_bin()`.
- **Measuring is read-only**: `safety.measure(path, min_age_s=..., name_glob=...)` → `(bytes, files)`.
  It already refuses paths that `purge` would refuse (returns `(0, 0)`), so a target outside the
  safety whitelist simply measures 0 and must be dropped (do not "fix" safety.py — report instead).
- **`run()` never trusts the UI**: it receives rule/item ids, looks them up in the server-side
  `analysis` dict (which holds the private target data), skips `blocked` items, and re-validation
  happens in `dm.safety` at deletion time.
- Discovery must never follow junctions/symlinks: use `os.scandir` + `entry.stat(follow_symlinks=False)`
  and `dm.winapi.is_link_stat(st)` to skip links.
- Pure standard library. No subprocess (process info is provided as `running`).
- Item ids: short stable strings unique within a rule (e.g. `sha1(path.lower())[:12]`).
- `default_selected`: True only when the rule risk is `safe` **and** none of the rule's processes is running
  **and** the item is not blocked. Always False for `moderate` / `review` rules.
- Item `requires_admin`: True for targets under `%SystemRoot%` or `%ProgramData%` (and the DO cache).
  `blocked = requires_admin and not is_admin`. Blocked items are listed (so the user sees the potential)
  but are excluded from the rule `size`, and `run()` skips them.
- A rule is omitted from the analysis when it has no item with size > 0 (except `recycle_bin`, omitted when empty).
- Rules sorted by size desc. `total` = sum of rule sizes.
- `progress` dict updated in place: `{"done": i, "total": n_rules, "current": rule title}`; check `cancel`
  between rules (return what has been analyzed so far when cancelled).
- `public_view(analysis)`: deep copy without private keys (`min_age_s`, `keep_root`, `name_glob`, `_targets`...).
  The UI receives item `path` (display only).
- All user-facing strings in **French**, short and clear. Descriptions explain what it is, why it is
  safe/regenerated, and what the user will notice (e.g. "Les sites se rechargeront un peu plus lentement la
  première fois.").
- `running` matching: compare lower-case exe names (e.g. `"chrome.exe"`). A rule exposes `"running": [names]`
  of its related processes that are running. Items of a running app are `default_selected = False` and get
  `note = "<App> est ouvert : fermez-le pour un nettoyage complet."`

Env/paths: use `dm.config` (`LOCALAPPDATA`, `APPDATA`, `USERPROFILE`, `TEMP`, `SYSTEM_ROOT`, `PROGRAMDATA`,
`SYSTEM_DRIVE`). `~` below = `config.USERPROFILE`. Only add a target if it exists.

## Rules

Risk legend: **safe** = regenerated automatically, no user data. **moderate** = regenerable but costs
time/bandwidth or a small inconvenience. **review** = the user must look at the items.

### Category `system`
| id | title | targets | options | risk |
|---|---|---|---|---|
| `user_temp` | Fichiers temporaires | `TEMP` | min_age 24 h, keep_root | safe |
| `windows_temp` | Fichiers temporaires de Windows | `SYSTEM_ROOT\Temp` | min_age 24 h, keep_root, admin | safe |
| `crash_dumps` | Rapports de plantage | `LOCALAPPDATA\CrashDumps`, `SYSTEM_ROOT\Minidump` (admin), `SYSTEM_ROOT\LiveKernelReports` (admin) | keep_root | safe |
| `wer_reports` | Rapports d'erreurs Windows | `LOCALAPPDATA\Microsoft\Windows\WER\{ReportArchive,ReportQueue,Temp}`, `PROGRAMDATA\Microsoft\Windows\WER\{ReportArchive,ReportQueue,Temp}` (admin) | keep_root | safe |
| `thumbnail_cache` | Cache des miniatures | `LOCALAPPDATA\Microsoft\Windows\Explorer` | name_glob `^(thumbcache|iconcache)_.*\.db$` (case-insensitive) | safe — note: files locked by Explorer are skipped |
| `inet_cache` | Cache Internet de Windows | `LOCALAPPDATA\Microsoft\Windows\INetCache` | keep_root | safe |
| `windows_update` | Téléchargements Windows Update | `SYSTEM_ROOT\SoftwareDistribution\Download` | keep_root, admin | safe — "les mises à jour déjà installées ne sont pas affectées" |
| `delivery_optimization` | Cache d'optimisation de distribution | `SYSTEM_ROOT\ServiceProfiles\NetworkService\AppData\Local\Microsoft\Windows\DeliveryOptimization\Cache` | keep_root, admin | safe |
| `shader_caches` | Caches de shaders (DirectX / GPU) | `LOCALAPPDATA\D3DSCache`, `LOCALAPPDATA\NVIDIA\DXCache`, `LOCALAPPDATA\NVIDIA\GLCache`, `LOCALAPPDATA\AMD\DxCache`, `LOCALAPPDATA\AMD\DxcCache`, `LOCALAPPDATA\AMD\GLCache`, `LOCALAPPDATA\AMD\VkCache`, `LOCALAPPDATA\Intel\ShaderCache` | keep_root | safe — "les jeux recompilent leurs shaders au premier lancement" |
| `recycle_bin` | Corbeille | (whole bin) — size/count from `winapi.recycle_bin_info()`, a single item | mode `recycle_bin` | moderate — "suppression définitive du contenu de la corbeille" |

### Category `browsers`
One rule per installed browser: `chrome_cache` (Google Chrome, `chrome.exe`, `LOCALAPPDATA\Google\Chrome\User Data`),
`edge_cache` (Microsoft Edge, `msedge.exe`, `LOCALAPPDATA\Microsoft\Edge\User Data`),
`brave_cache` (Brave, `brave.exe`, `LOCALAPPDATA\BraveSoftware\Brave-Browser\User Data`),
`vivaldi_cache` (Vivaldi, `vivaldi.exe`, `LOCALAPPDATA\Vivaldi\User Data`),
`opera_cache` (Opera, `opera.exe`, user data `APPDATA\Opera Software\Opera Stable`, caches in `LOCALAPPDATA\Opera Software\Opera Stable`),
`firefox_cache` (Firefox, `firefox.exe`, `LOCALAPPDATA\Mozilla\Firefox\Profiles\*\{cache2,startupCache,thumbnails}`).

Chromium layout: profiles = subfolders of `User Data` named `Default`, `Profile <n>`, `Guest Profile`, `System Profile`.
Display names: read `User Data\Local State` (JSON) → `profile.info_cache[<dir>].name` (fallback: folder name);
item label e.g. `Profil « Perso » — cache`.
Per profile, targets (keep_root): `Cache`, `Code Cache`, `GPUCache`, `DawnCache`, `DawnGraphiteCache`,
`DawnWebGPUCache`, `Service Worker\ScriptCache` → one item per profile grouping these (the item holds
several targets privately). A second item per profile for `Service Worker\CacheStorage` with
note "Données hors-ligne des sites (re-téléchargées au besoin)" and `default_selected = False`.
Browser-level item (keep_root): `ShaderCache`, `GrShaderCache`, `GraphiteDawnCache` directly under `User Data`.
Risk: safe. Description mentions that history, passwords, cookies and sessions are **not** touched.

### Category `apps`
| id | title | discovery | risk |
|---|---|---|---|
| `electron_caches` | Caches d'applications (Discord, Teams, VS Code…) | Folders at depth 1 and 2 under `APPDATA` and `LOCALAPPDATA` that look like a Chromium/Electron user-data dir (contain a `Local State` or `Preferences` file, or a `Local Storage` folder) and are **not** a browser `User Data` dir handled above, not under `LOCALAPPDATA\Packages`, not under `LOCALAPPDATA\Microsoft\Windows`. One item per app: targets = existing subfolders among `Cache`, `Code Cache`, `GPUCache`, `DawnCache`, `DawnGraphiteCache`, `DawnWebGPUCache`, `GrShaderCache`, `ShaderCache`, `Service Worker\CacheStorage`, `Service Worker\ScriptCache` (keep_root). Label = relative path (`discord`, `Microsoft\Teams`, `Code`, `Slack`…). Process guess = `<last folder name>.exe` lower-case (e.g. `discord.exe`, `teams.exe`, `code.exe`, `slack.exe`, `claude.exe`). | safe |
| `vscode_cpptools` | Cache IntelliSense C/C++ (VS Code) | `LOCALAPPDATA\Microsoft\vscode-cpptools\ipch` (keep_root); process `code.exe` | safe |
| `vscode_extras` | Fichiers résiduels de VS Code | `APPDATA\Code\CachedExtensionVSIXs` (keep_root); in `APPDATA\Code\CachedData` every subfolder except the most recently modified one (keep_root False — the item target is the subfolder); `APPDATA\Code\logs` min_age 7 days. Process `code.exe` | safe |
| `squirrel_old_versions` | Anciennes versions d'applications | Folders `LOCALAPPDATA\<App>` containing `Update.exe` and ≥ 2 subfolders `app-<version>`: every `app-*` except the highest version (compare as int tuples). keep_root False (the target is the old `app-x.y.z` folder). One item per old folder, label `Discord 1.0.9041`. Process `<app>.exe` | moderate — "l'application tourne sur la version la plus récente" |
| `updater_caches` | Installeurs de mises à jour téléchargés | `LOCALAPPDATA\*-updater` folders (electron-updater caches, keep_root), `PROGRAMDATA\McNeel\McNeelUpdate\DownloadCache` (keep_root, admin only if not writable — try `os.access(path, os.W_OK)`), `PROGRAMDATA\NVIDIA Corporation\Downloader` (admin) | moderate |
| `spotify_cache` | Cache Spotify | `LOCALAPPDATA\Spotify\Data`, `LOCALAPPDATA\Spotify\Storage` (keep_root); process `spotify.exe` | safe |

### Category `dev`
| id | title | targets (keep_root unless noted) | risk |
|---|---|---|---|
| `npm_cache` | Cache npm | `LOCALAPPDATA\npm-cache\_cacache`, `LOCALAPPDATA\npm-cache\_npx` | safe |
| `yarn_pnpm_cache` | Caches Yarn / pnpm | `LOCALAPPDATA\Yarn\Cache`, `LOCALAPPDATA\pnpm-cache`, `LOCALAPPDATA\pnpm\store` | moderate |
| `python_caches` | Caches pip / uv | `LOCALAPPDATA\pip\cache`, `LOCALAPPDATA\uv\cache` | safe |
| `nuget_cache` | Caches NuGet (.NET) | `LOCALAPPDATA\NuGet\v3-cache`, `LOCALAPPDATA\NuGet\plugins-cache` (safe) ; `~\.nuget\packages` in a separate item, default off | moderate |
| `jvm_caches` | Caches Gradle / Maven | `~\.gradle\caches`, `~\.m2\repository` | moderate |
| `rust_go_caches` | Caches Rust / Go | `~\.cargo\registry\cache`, `~\.cargo\registry\src`, `LOCALAPPDATA\go-build`, `~\go\pkg\mod` | moderate |
| `ml_caches` | Modèles d'IA téléchargés | `~\.cache\huggingface`, `~\.cache\torch`, `~\.keras\models` | review — "plusieurs Go à re-télécharger si vous réutilisez ces modèles" |
| `conda_pkgs` | Paquets Conda en cache | `~\anaconda3\pkgs`, `~\miniconda3\pkgs`, `LOCALAPPDATA\conda\conda\pkgs` | review — suggest `conda clean --all` in description |
| `msys2_cache` | Paquets MSYS2 en cache | `SYSTEM_DRIVE\msys64\var\cache\pacman\pkg` | safe |
| `inactive_node_modules` | Dossiers node_modules inactifs | **Scan-based** (skip when `scan is None`): scan nodes named `node_modules` (case-insensitive) whose parent folder contains a `package.json` and has not been modified for 90 days (`os.stat(parent/package.json).st_mtime` and the scan node `mtime` of the parent excluding node_modules can be approximated by package.json mtime); skip nested ones (an ancestor is already a node_modules), skip anything under `APPDATA` or `LOCALAPPDATA` (global installs). Size = scan node size (do not re-measure). keep_root False. Label = project folder path relative to `~`. | review |

### Category `downloads`
| id | title | discovery | mode / risk |
|---|---|---|---|
| `old_downloads` | Installeurs et archives dans Téléchargements | Files directly in the Downloads folder (resolve via `dm.safety.PERSONAL_DIRS` entry ending in `Downloads`, fallback `~\Downloads`) and one level below, with extensions `exe msi msix appx zip rar 7z iso img dmg tar gz tgz` and mtime older than 30 days. One item per file, label = file name, note = "Téléchargé le JJ/MM/AAAA". | **recycle** (to the recycle bin, reversible) / review |

## `advice(scan, system)`
Guidance only, nothing deleted. Use `system` (output of `system_info.get_system_info()`, may be None)
and `os.scandir(SYSTEM_DRIVE)` for the special files. Cards (only when relevant):

1. `hibernation` — if `hiberfil.sys` exists: size; explain hibernation / fast startup; action
   `{"type": "command", "command": "powercfg /h /type reduced", "label": "Réduire le fichier", "admin": true}`;
   mention `powercfg /h off` to remove it completely (disables fast startup).
2. `pagefile` — if `pagefile.sys` > 1.5 × RAM (`winapi.memory_status()["total"]`): explain it is managed by
   Windows, can be reduced in "Mémoire virtuelle"; action open `performance_options`. severity tip.
3. `wsl_disks` — every `ext4.vhdx` under `LOCALAPPDATA\wsl\*`, `LOCALAPPDATA\Packages\*\LocalState`,
   `LOCALAPPDATA\Docker\wsl\*` (depth-limited search, no link following): total size; explain that WSL/Docker
   virtual disks grow but never shrink; command `wsl --shutdown` then
   `wsl --manage <distribution> --set-sparse true`, or compaction with diskpart (`select vdisk file="<path>"`,
   `attach vdisk readonly`, `compact vdisk`, `detach vdisk`). Action type command `wsl --shutdown`, admin false.
4. `component_store` — explain WinSxS can be cleaned by Windows only; action open `cleanmgr`
   ("Nettoyer les fichiers système"), mention `Dism /Online /Cleanup-Image /StartComponentCleanup` (admin).
5. `windows_old` — if `SYSTEM_DRIVE\Windows.old` or `$WINDOWS.~BT` exists (size via scan if available):
   action open `cleanmgr` ("Installations précédentes de Windows").
6. `restore_points` — action open `system_protection`; explain restore points use hidden space
   ("System Volume Information").
7. `storage_sense` — tip to enable Storage Sense; action open `storagesense`.
8. `oem_recovery` — if `system` has a recovery partition > 5 GB: info card; explain it is the manufacturer
   factory image, do not delete it without a recovery USB key. No action.
9. `unscanned` — if `scan` is a drive-root scan and `scan.summary()["volume"]["unscanned"]` > 2 GB: info card
   explaining hidden/system space (restore points, MFT, files of other users).

## Validation commands
```
python -m py_compile dm/cleaners.py
python -c "import sys; sys.path.insert(0,'.'); import os; os.environ['DM_DRY_RUN']='1'; from dm import cleaners, winapi; a=cleaners.analyze(None, is_admin=winapi.is_admin(), running=winapi.running_process_names()); import json; print(json.dumps(cleaners.public_view(a), ensure_ascii=False, indent=1)[:6000])"
```
Run `run()` **only with `DM_DRY_RUN=1`** (set before importing dm) — never delete real data during development.
