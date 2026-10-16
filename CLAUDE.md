# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Disk Manager: a 100 % local storage analyzer and cleaner for **this Windows 10 PC** (French UI). Python
stdlib backend (`dm/`) serving a vanilla-JS UI (`web/`, ES modules, no build step, no dependencies) on
`127.0.0.1`, shown in an Edge `--app` window with an isolated profile and a hard network lock
(`dm/window.py`). User guide: `README.md`. API reference: `docs/API.md`. Cleanup rules: `docs/CLEANERS_SPEC.md`.

## Commands

Use the official interpreter, not the MSYS2 one first on PATH (`C:\msys64\mingw64\bin\python.exe` uses POSIX
path rules and gives wrong results):
`PY="$LOCALAPPDATA/Python/pythoncore-3.14-64/python.exe"`

```
"$PY" run.py --no-window --dry-run       # dev server only; prints http://127.0.0.1:<port>/#t=<token>
"$PY" run.py --dry-run --debug           # full app (window); every deletion simulated; requests logged to data/app.log
"$PY" -m unittest discover -s tests -t .                          # all tests (~40 s, 150 tests)
"$PY" -m unittest tests.test_safety_races                         # one module
"$PY" -m unittest tests.test_safety.PurgeTests.test_min_age_keeps_recent_files   # one test
"/c/Program Files/nodejs/node.exe" --check web/js/views/clean.js  # JS syntax check (no linter configured)
"$PY" run.py --install-shortcut          # Desktop + Start menu shortcuts (icon generated in data/)
```

Users launch `Disk Manager.cmd` (finds `pythonw.exe`, no console). Stop a dev server with
`POST /api/app/quit` (header `X-DM-Token`). `web/dev/charts.html` is a demo page for the chart components.

**This runs on the user's real machine.** Never exercise cleanup / recycle endpoints without `--dry-run`
(or `DM_DRY_RUN=1`, read once when `dm.safety` is imported). Tests only touch `dm_test_*` sandboxes under
`%TEMP%` and patch `winapi.send_to_recycle_bin` / `empty_recycle_bin` — keep it that way.

## Architecture

**Request path.** `run.py` builds `AppState` + router (`dm/api.py`), starts `AppServer` (`dm/server.py`),
opens the window and lives until the Edge process exits or heartbeats (`POST /api/ping`, every 15 s) stop.
`server.py` enforces: Host header = `127.0.0.1:<port>`/`localhost:<port>`, per-launch token in `X-DM-Token`,
same-origin `Origin`/`Sec-Fetch-Site`, JSON-only POST ≤ 1 MB, static files only from `web/` with an extension
whitelist, strict CSP (`script-src 'self'`, no inline JS). The token reaches the page via the URL fragment
(`#t=`), is kept in sessionStorage by `web/js/api.js`. `data/instance.json` (pid/port/token) implements
single-instance; it is never written in `--no-window` mode or when elevated.

**Long operations are tasks** (`dm/tasks.py`): a POST starts a thread and returns the task dict; the UI polls
`GET /api/tasks/<id>`. One running task per group: `scan` (scan, dupes), `clean` (clean_analyze), `delete`
(clean_run, recycle, empty_bin) → 409 `busy` otherwise.

**Scanner** (`dm/scanner.py`): work-stealing `os.scandir` over a thread pool (~35 s for C:, ~880k files).
Never follows name-surrogate reparse points; cloud placeholders count 0 bytes. Files ≥ `KEEP_FILE_MIN`
(1 MiB) are kept individually, smaller ones only aggregated per folder (count/size) — hence the
"petits fichiers" pseudo-entries (`t: 's'`) and "autres éléments" (`t: 'o'`) in tree responses. Scans are
cached as gzip JSON in `data/scans/` (never pickle) and the system drive's cache is preloaded at start-up.
After deletions the tree is patched (`remove_path`, `refresh`) instead of rescanned. Cached trees store own
sizes; aggregates are recomputed on load.

**Deletion — `dm/safety.py` is the only module allowed to delete.** Two operations:
- `recycle(paths)` (user-chosen items → recycle bin via `SHFileOperationW` + `FOF_ALLOWUNDO|FOF_WANTNUKEWARNING`):
  each path through `check_recyclable`, ancestors held open without `FILE_SHARE_DELETE` (`_lock_ancestors`)
  during the shell call.
- `purge(target)` (permanent, regenerable caches only): `check_purgeable` whitelist (`PURGE_USER_ROOTS`,
  `PURGE_SYSTEM_ROOTS`, regenerable dir names), then every file/dir/link is deleted through `winapi.Handle`
  (opened with `FILE_FLAG_OPEN_REPARSE_POINT`, final path re-checked on the handle, disposition set on that
  handle). Do not reintroduce `os.unlink`/`os.rmdir`/`os.chmod` on path strings after validation: that
  re-opens the junction-swap TOCTOU found in the security review (`tests/test_safety_races.py`).
- Both refuse anything inside or containing `PROTECTED_TREES`/`PROTECTED_EXACT`, links in the chain,
  paths whose `realpath` differs (8.3 names), trailing dots/spaces, ADS, UNC/device paths. Every operation is
  appended to `data/journal.jsonl`.

**Cleanup rules** (`dm/cleaners.py`): registered with the `@rule` decorator; discovery returns raw items whose
private `_targets` (path, min_age_s, keep_root, name_glob) stay server-side — `public_view()` strips them.
`run()` maps rule/item ids from the UI back to the stored analysis; the UI never sends cleanup paths. Items of
running apps (process list excludes the app's own Edge tree) and non-`safe` rules are never preselected.
Things the app must not delete (hiberfil, pagefile, WSL vhdx, WinSxS, restore points) are `advice()` cards.

**Other backend modules:** `winapi.py` (ctypes only: volumes, recycle bin, processes, Explorer reveal,
elevation, handle-based deletion — no shell), `system_info.py` (one fixed PowerShell script, 5-min cache),
`apps.py` (registry, sizes from the scan when an install folder is unique), `duplicates.py` (size → partial
blake2b → full hash; restricted by default to the user profile minus AppData and non-binary files),
`history.py` (snapshots + usage samples in `data/scans/*.jsonl`).

**Frontend.** `web/js/app.js` is the shell (hash router `#/<view>?params`, sidebar, theme, heartbeat).
A view is `web/js/views/<id>.js` exporting `styles` (loads `css/views/<styles>.css`) and
`mount(root, ctx)` (+ optional `unmount`); `ctx.on(event, fn)` subscriptions are disposed automatically.
Shared state/events in `store.js` (`scan`, `volumes`, `task:<kind>`; `store.track(kind, promise)` polls a task).
Shared flows in `components.js` (`recycleItems` = the only UI path to delete user files, `scanPrompt`,
`scanProgress`, `adviceGrid`, `isProtectedHint` — display only, the server decides). `ui.js` has the DOM
builder `el()`, French formatters (binary units shown as Ko/Mo/Go), icons, modals, toasts, tooltip.
Charts in `web/js/charts/` (canvas treemap, SVG sunburst, bars, disk map, area, ring);
`bars.js` `colorOf` receives the whole item, not its key.

## Conventions

- Backend: Python stdlib only — no third-party packages, no outbound network, no `shell=True`; subprocesses
  only with absolute executable paths and fixed scripts (values through env vars).
- UI text in French. File names/paths are untrusted: build DOM with `el()`/`textContent`, never
  `innerHTML`; no inline `<script>`/handlers (CSP). Inline style attributes are allowed by CSP but prefer
  `el(..., {style: {...}})` or classes.
- Colors only from CSS tokens in `web/css/app.css` (light + dark themes, validated categorical palette:
  8 file categories in a fixed order + neutral "other"; status colors reserved for risk/health). Charts
  re-render on the `themechange` document event.
- Repo files use LF (`.gitattributes`; `.cmd` files CRLF). Beware shell heredocs eating backslashes in
  Windows paths — prefer the Edit/Write tools.
- `data/` is runtime state (scan cache, journal, history, Edge profile, logs) and is git-ignored.

<!-- subagent-budget:start -->
## Delegation and agent budget

The user is on a limited plan. Usage is the constraint, not model access. Spend it deliberately.

- Default to doing the work in the main session. Do not use a workflow or subagents for small or already-understood changes: the handoff and the review cost as much as the work.
- Maximum 3 concurrent agents, and keep workflows under 5 agents in total, unless the user explicitly asks for more. When unsure whether a task warrants fan-out, ask.
- Route by task, not by habit. The user's rule: always pick the cheapest model you are very confident will complete the task you assign it.
  - haiku: exploration, grep/reading, collecting facts, mechanical edits, git operations.
  - sonnet: implementation, tests, routine review.
  - opus: architecture, hard debugging, security-sensitive or concurrency-heavy changes. At most 1 to 2 Opus agents at a time.
- Always name the model explicitly when spawning a subagent or writing a workflow stage. Never rely on inheriting the session model for fan-out stages.
- Use the custom agents: `scoper` (unfamiliar task, before planning), `implementer` (well-defined code or test changes), `shipper` (commit/push/PR when asked), `reviewer` (only when asked or for risky changes).
- A subagent starts with an empty context: give it the goal, acceptance criteria, relevant files, decisions already taken, and the commands to validate.
- Keep architecture, scope and ambiguity in the main session. Review every returned diff yourself before reporting done. Send corrections back to the same agent with SendMessage to keep its context.
- ultracode is opt-in per task (the `ultracode:` keyword). Do not recommend turning it on for the whole session.
- Before a large fan-out, say how many agents and which models you plan to use, in one line.
<!-- subagent-budget:end -->
