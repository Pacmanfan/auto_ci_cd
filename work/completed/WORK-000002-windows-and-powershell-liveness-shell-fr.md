# WORK-000002: Windows and PowerShell: liveness, shell-free commands, UTF-8, curses-free dashboard

**Status:** completed
**Type:** feature
**Request:** requests/processing/2026-11-14-windows-support-make-sure-the--req-chat-20261114-002.md
**Requirements:** PORT-001, PORT-002, PORT-003, PORT-004, PORT-005, PORT-006
**Decision:** ADR-000199
**Evidence:** VER-000002

## Scope

Read the system for Windows / PowerShell problems, fix what can be fixed
safely without a Windows host, and record the rest (REQ-CHAT-20261114-002).
No Windows machine was available: the Windows code paths are written from
the API documentation and unit-tested on Linux with the Win32 calls
replaced. They are not verified on Windows.

Findings of the read-through (major first):

1. `pid_alive` returned "unknown" on Windows, so nothing was ever verified ALIVE: a started worker always showed as stopped, `stop` never waited, and claims fell back to heartbeat age. FIXED (Win32 process query).
2. `kill_tree` could not end a session's process tree on Windows (no `/proc` descendants, soft `taskkill` ignored by console programs, `SIGKILL` undefined). FIXED (`taskkill /T`, grace, `/T /F`).
3. The t0 structure suite, the structure audit check and the health check ran `bash scripts/verify_development_system.sh`; suite commands started `python3`. FIXED (`devsys.py verify-structure`; `python3` means the running interpreter).
4. Agent CLIs installed as `.cmd` shims would not start under their bare name. FIXED (resolved through the shell's lookup; `{python}` placeholder).
5. Output containing non-ASCII characters (the tools print dashes and arrows) raises on a legacy console code page, and git output was decoded with the locale. FIXED for the tools' own output and `agent_process.git`; other subprocess output and files opened without an encoding still need `PYTHONUTF8=1` (doctor warns).
6. The terminal dashboard needed `curses`, which Windows Python lacks. FIXED (plain live view).
7. The adapter referenced `SIGHUP` (undefined on Windows). FIXED.
8. Replacing a state file fails on Windows while a reader has it open. FIXED (short retry).
9. The web panel's `SO_REUSEADDR` lets a second server share the port on Windows. FIXED.
10. NOT FIXED, recorded in `docs/process/portability.md` "Known gaps on Windows": the unit suites themselves are POSIX-bound; no session marker and no boot id; string validation commands run through `cmd.exe`; locks, path handling and line endings are unexercised; the shell launchers are Linux conveniences.

## Implementation

- `scripts/devsys_platform.py`: Win32 process existence and creation time (`_win_process`, `pid_alive`, `process_start`), Windows `kill_tree` / `signal_group`, UTF-8 stdio.
- `scripts/agent_process.py`: `git` decoded as UTF-8; `atomic_write_text` retries the replace on Windows.
- `scripts/run_tests.py`: `python3` as a suite command's first word is the running interpreter.
- `scripts/test_manifest.json`, `.audit/manifest.json` and their templates, `scripts/health_check.py`: the structure check runs `devsys.py verify-structure` instead of the bash wrapper.
- `scripts/workers/agent.py`, `pool.py`: `{python}` placeholder, `resolve_executable`.
- `scripts/workers/profiles/event_stream_adapter.py`: no undefined signals, shim resolution, a byte-order mark tolerated.
- `scripts/workers/prompts/worker.md`, `prompt.py`: how to run the tools where scripts are not executable.
- `tools/panel/dash.py`: `--plain` view, automatic without curses. `tools/panel/web_panel.py`: no address reuse on Windows.
- `scripts/devsys.py`: doctor warnings on Windows (UTF-8 mode, `core.autocrlf`).
- `scripts/tests/test_workers.py` `Identity`, `docs/requirements/worker_orchestration.md` (WORKER-012): evidence records are not scanned for vendor names or machine paths. The first committed evidence of this repository (VER-000001: a run record with absolute paths, a gate record listing a registered path) made both identity tests fail in run 20261114T235423-3122862; evidence is a record of what ran on a machine and is not installed with the framework.
- `scripts/tests/test_process_concurrency.py` `RequestIdAllocation`: two tests compared against the date taken at import; run 20261114T235939-3154480 crossed local midnight and they failed. They now take the date when they run.
- Process note: `scripts/workers/pool.py` was edited before it was registered and then adopted (`--adopt-existing-changes`); no other session was active.

## Tests

- `scripts/tests/test_devsys_tooling.py`: `WindowsBranches` (liveness, pid reuse, API failure is unknown, tree termination order, shim resolution; Win32 calls replaced), `NoShellNeeded` (no registered command uses the bash wrapper; `python3` runs with no python3 on PATH).
- `scripts/tests/test_agent_coordination.py` `Dashboard.test_the_plain_view_needs_no_curses`.
- Runs 20261114T235423-3122862 and 20261114T235939-3154480 (t2) FAILED and are superseded (identity scan of committed evidence; two tests crossing local midnight; both fixed above).
- **Test-Run:** 20261115T000456-3184705 (t2, PASS; started with `AGENT_ID` / `AGENT_OWNER_PID` removed from the environment)
- Not verified: anything on a Windows host. `_win_process` (ctypes) has never executed.

## Regression impact and documentation

- Linux behaviour is unchanged except: the structure suite / audit check / health check call `devsys.py verify-structure` directly (the bash wrapper did the same with the unit tests skipped), and `git` output is decoded as UTF-8 with replacement instead of the locale.
- Documentation: `docs/requirements/portability.md` (new, PORT-001..006), `docs/requirements/requirements.txt` and its template, `docs/process/portability.md` ("Windows and PowerShell", known gaps, first-run checklist), `docs/process/agent-profiles.md`, `docs/process/agent-coordination.md`, `docs/verification/tests.md`.

## Traceability

REQ-CHAT-20261114-002 -> PORT-001, PORT-002, PORT-003, PORT-004, PORT-005, PORT-006 -> WORK-000002 -> evidence (pending)

## Transitions

- 2026-11-15T04:54:00.057194+00:00 created in active by agent-dev-20261114230032-8bee2e
- 2026-11-15T05:09:05.787087+00:00 active -> completed by agent-dev-20261114230032-8bee2e
