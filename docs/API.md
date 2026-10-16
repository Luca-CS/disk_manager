# Disk Manager — local JSON API

Served on `http://127.0.0.1:<random port>` only. Every `/api/*` call needs the header `X-DM-Token`
(handled by `web/js/api.js`). POST bodies are JSON objects. Errors: HTTP 4xx/5xx with
`{"error": {"code", "message"}}` (French message, show it to the user). Sizes are bytes, times are epoch
seconds, paths are absolute Windows paths (backslashes).

Long operations start a **task** and return its dict immediately: track it with `store.track(kind, promise)`
(see `web/js/store.js`) or `waitTask()` (`web/js/api.js`).

## Tasks
| | |
|---|---|
| `GET /api/tasks` | `{tasks: [task]}` running tasks |
| `GET /api/tasks/<id>` | `task` = `{id, kind, label, status: running\|done\|error\|cancelled, progress: {}, error, started, finished, result?}` |
| `POST /api/tasks/<id>/cancel` | request cancellation |

Task groups: only one running task per group — `scan` (scan, dupes), `clean` (clean_analyze),
`delete` (clean_run, recycle, empty_bin). Starting a second one → 409 `busy`.

## App & system
- `GET /api/meta` → `{version, admin, dry_run, python, keep_file_min, categories: [{key, label}], user, computer, paths: {home, downloads, system_drive}}`
- `POST /api/ping` (heartbeat, done by the shell) · `POST /api/app/quit` · `POST /api/app/elevate`
- `GET /api/volumes` → `{volumes: [{letter, root, label, fs, type: fixed|removable|network|ramdisk, total, free, used, is_system}], recycle_bin: {size, count}}`
- `GET /api/system[?refresh=1]` → `{disks: [{number, model, media, bus, health, status, size, partition_style, reliability|null, partitions: [{number, letter, type, kind, offset, size, label, fs, free, used, health, is_boot, is_system}]}], volumes, error, collected_at, memory: {total, available}, special_files: {pagefile?, hiberfil?, swapfile?}, recycle_bin}` (first call ≈ 5 s)

## Scan
- `GET /api/scan` → `{scan: summary|null, cached: [{root, saved}]}`
  summary = `{root, size, files, dirs, denied, links, cloud_files, cloud_bytes, started, finished, duration, cancelled, from_cache, volume: null|{total, free, used, unscanned}}`
- `POST /api/scan {path}` → task kind `scan`; progress `{files, dirs, bytes, denied, current, elapsed}`; result = summary
- `POST /api/scan/load {path}` → task kind `scan` (reopen the saved scan of that root)
- `GET /api/scan/tree?path=&depth=4&min_ratio=0.001&max_nodes=4000` → nested node `{n, s, t: d|f|s|o, fc?, cat?, m?, k?, ch?, more?, x?}`, root has `path`
- `GET /api/scan/children?path=` → `{node: {name, path, parent, size, type: 'dir', files, dirs, mtime, denied, cloud}, items: [...]}`
  items: dirs `{name, path, size, type: 'dir', files, dirs, mtime, denied, cloud, empty}`, big files `{name, path, size, type: 'file', mtime, category, ext}`, small-files aggregate `{name, size, type: 'small', count}` (no path); sorted by size desc
- `GET /api/scan/largest?limit=200&category=&min_age_days=0&under=` → `{files: [{path, name, size, mtime, category, ext}]}` (files ≥ 1 Mo only)
- `GET /api/scan/types` → `{categories: [{key, label, size, count}], extensions: [{ext, category, size, count}] (top 300, "" = no extension), total}`
- `GET /api/scan/age` → `{buckets: [{label, max_days|null, count, size}]}` (by last modification)

## Duplicates
- `POST /api/dupes {min_size?, include_system?}` → task kind `dupes`; progress `{phase: group|partial|full, done_files, total_files, done_bytes, total_bytes, groups}`
- `GET /api/dupes` → `{result: null | {groups: [{id, size, count, wasted, files: [{path, name, mtime}]}], total_wasted, files_hashed, bytes_hashed, min_size, include_system, elapsed, cancelled}}` (files oldest first)

## Cleanup
- `POST /api/clean/analyze` → task kind `clean_analyze`; progress `{done, total, current}`
- `GET /api/clean` → `{analysis: null | {rules: [rule], total, analyzed_at, is_admin, dry_run}}`
  rule = `{id, title, category: system|browsers|apps|dev|downloads|leftovers, description, risk: safe|moderate|review, mode: purge|recycle|recycle_bin, requires_admin, blocked, running: [exe], size, files, items: [{id, label, path, size, files, note, requires_admin, blocked, default_selected}]}`
- `POST /api/clean/run {selection: {rule_id: "all" | [item_id]}}` → task kind `clean_run`; result `{freed, files, dry_run, rules: [{id, title, bytes, files, skipped_in_use, skipped_recent, errors: [{path, reason}], refused: [{path, reason}]}], touched_paths}`. The analysis is consumed: re-analyze afterwards.
- `GET /api/clean/advice` → `{advice: [{id, title, body, size|null, severity: info|tip|warning, action: null | {type: 'open', target, label} | {type: 'command', command, label, admin}}]}`

## Files
- `POST /api/files/recycle {paths: [..], source: explorer|largest|dupes|types|old}` → task kind `recycle`; result `{bytes, files, done: [paths], errors, refused, dry_run, ...}`. Paths must be inside the current scan; the server re-validates each one (system locations refused). Use `recycleItems()` from `web/js/components.js`, which confirms with the user first.
- `POST /api/recycle-bin/empty` → task kind `empty_bin` (permanent)
- `POST /api/shell/reveal {path, select?}` → opens Explorer (never executes the file). Use `reveal()` / `revealButton()`.
- `POST /api/shell/open {target}` → target ∈ `storagesense, apps, appwiz, cleanmgr, system_protection, performance_options, recyclebin, diskmgmt`. Use `openTool()`.

## Apps & history
- `GET /api/apps[?refresh=1]` → `{apps: [{id, name, publisher, version, install_date, size, size_source: scan|registry|null, location, scope: machine|user, arch, shared_with?: name of the app that owns the measured folder}]}`
- `GET /api/history[?root=C:\]` → `{journal: [entry], total_freed, snapshots: [{id, ts, root, total, files, volume}], usage: [{ts, root, total, used}]}`
  journal entry = `{ts, action: recycle|purge|empty_recycle_bin, bytes, files, dry_run, rule?, source?, items?, targets?, errors, refused?}` (most recent first)
- `GET /api/history/compare?a=<older id>&b=<newer id>` → `{a, b, delta, changes: [{path, before|null, after|null, delta}]}`
