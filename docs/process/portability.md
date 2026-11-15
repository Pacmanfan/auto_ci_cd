# Portability

**Decision:** ADR-000199 (decision 8)
**Code:** `scripts/devsys_platform.py`

## Supported environment

| Requirement | Minimum | Checked by |
|---|---|---|
| Python | 3.9, standard library only | `devsys.py doctor` |
| git | 2.20 | `devsys.py doctor` |
| Shell | none required. POSIX `bash` only for the convenience wrappers (`scripts/verify_development_system.sh`, `tools/worker-panel`) and a project's own package scripts; PowerShell works (see below) | — |
| OS | Linux (supported, tested); other POSIX systems expected to work with the fallbacks below; Windows implemented but **not yet verified on a Windows host** (PORT-001..006, `docs/requirements/portability.md`) | `devsys.py doctor` "platform" |

The framework needs no database, service, daemon or network access. Its state
consists of files in the repository, plus `.agent/state/` in the main
worktree of each clone.

## What is platform-specific, and where

Every primitive that differs between operating systems lives in
`scripts/devsys_platform.py`. The rest of the tooling calls only that module.

| Primitive | Linux | other POSIX | Windows (untested) |
|---|---|---|---|
| inter-process lock (`lock` / `unlock`) | `fcntl.flock` | `fcntl.flock` | `msvcrt.locking` on the first byte |
| create without overwrite (`link_no_clobber`) | hard link | hard link | check + rename (callers hold the claims lock) |
| boot identity (`boot_id`) | `/proc/sys/kernel/random/boot_id` | none: reboot not detected | none |
| process start time (`process_start`) | `/proc/<pid>/stat` field 22 | none: pid reuse not detected | `GetProcessTimes` creation time |
| process exists (`pid_alive`) | `kill(pid, 0)` | `kill(pid, 0)` | `OpenProcess` + `GetExitCodeProcess` |
| end a session (`kill_tree`) | TERM, grace, KILL to the group and descendants | same (no descendants walk) | `taskkill /T`, grace, `taskkill /T /F` |
| session marker / environment of another process (`proc_environ`, `find_process`) | `/proc/<pid>/environ` | none | none: a session is verified by pid and creation time only |
| process groups (`group_popen_kwargs`, `signal_group`) | new session + `killpg` | same | `CREATE_NEW_PROCESS_GROUP` + `taskkill /T` |

## How missing primitives degrade

Liveness degrades in the safe direction:

- **Without a boot id or a start time:** a recorded owner process is checked
  by existence only.
  - A reboot is not recognised as such. The pid is simply gone (DEAD), or
    reused by another process (ALIVE, falsely).
  - A reused pid can keep a dead session's claim ALIVE. That blocks
    recovery; it never enables a wrong one. Use `--recover-request` after
    the pid is gone, or run the session without `--owner-pid` (heartbeat
    verdicts only).
- **Without process liveness (`pid_alive` returns None, as on Windows):**
  every claim is judged by its heartbeat alone (ACTIVE / STALE), as before
  ADR-000199. Nothing is ever DEAD, so the sweep never requeues anything
  automatically.

Other limits outside Linux:

- **Locks:** `fcntl.flock` on network filesystems (NFS) may not exclude
  processes on other hosts. Keep a checkout's `.agent/state/` on a local
  disk; the framework is designed for many agents on one host, or separate
  clones that exchange work through git.
- **Paths:** paths are handled with `pathlib`. Recorded paths are
  repository-relative with `/` separators. On Windows, git must be configured
  with `core.autocrlf=false` for owned-path commits: the baseline blobs are
  compared byte for byte.

## Windows and PowerShell

Status: the code paths below exist and their logic is unit-tested on Linux
with the Win32 calls replaced (`test_devsys_tooling.WindowsBranches`).
**Nothing has been run on Windows yet**; treat the first session there as
the verification, following "First run on Windows".

### Setting up a session

```powershell
$env:PYTHONUTF8 = '1'                                   # records and git output are UTF-8
git config core.autocrlf false                          # owned-path commits compare blobs byte for byte
python scripts/devsys.py doctor                         # warns about both when they are missing

$env:AGENT_ID = python scripts/claim.py --new-agent-id
$env:AGENT_OWNER_PID = $PID                             # this PowerShell process: the claim is ALIVE while it runs
python scripts/claim.py --claim-request <file>
python scripts/msg.py who
python tools/panel/web_panel.py --open                  # web control panel
python tools/panel/dash.py                              # terminal dashboard (plain view: no curses on Windows)
python tools/panel/worker_panel.py                      # Qt panel, when PySide6 is installed
```

- Use `python` (or `py -3`) wherever the documentation writes `python3`.
  Scripts are not executable by themselves; the worker prompt tells agents
  to run them through the interpreter.
- In `devsys.json`, write agent commands with `{python}` instead of
  `python3`, for example the adapter profile in
  `docs/process/agent-profiles.md`.
- Use Windows Terminal or PowerShell 7 for the dashboard (ANSI redraw).

### What was changed for Windows

| Area | Behaviour |
|---|---|
| Liveness | `OpenProcess` / `GetProcessTimes`: workers, claims and roster entries are verified ALIVE / DEAD; before this, nothing was ever ALIVE on Windows, so a started worker always showed as stopped |
| Ending sessions | `taskkill /T`, then `/T /F` after the grace period (console programs ignore the first) |
| Registered commands | no `bash`; `python3` means the running interpreter |
| Agent CLIs | `.cmd` / `.bat` shims found under their bare name |
| Text | tools write UTF-8; git output decoded as UTF-8 |
| State files | replace retried while a reader has the file open |
| Dashboard | plain live view without `curses` |
| Web panel | no shared listening port |

### Known gaps on Windows

- **The unit suites are POSIX-bound.** `scripts/tests` uses `os.killpg`,
  `SIGKILL`, symlinks and `sleep` helpers; expect failures that are the
  tests', not the system's, until they are ported.
- **No session marker.** Another process's environment cannot be read, so a
  worker's agent session is verified by pid and creation time only.
- **No boot id.** A reboot is recognised only because the pids are gone or
  their creation times differ.
- **Validation commands given as strings** (`workers.validation[].command`)
  run through `cmd.exe`, not a POSIX shell; write them as argument lists.
- **Locks** use `msvcrt.locking` on the first byte of the lock file
  (mandatory, per handle). Not exercised under contention on Windows.
- **Path handling** relies on `pathlib` and git printing `/`; recorded paths
  are repository-relative with `/`. Not exercised with Windows drive
  letters, long paths or case-insensitive collisions.
- **`tools/worker-panel`** and the desktop launcher are Linux conveniences;
  start the panels with `python` as above.
- **Line endings:** files the tools write use `\n`; with `core.autocrlf`
  other than `false` git rewrites them and the byte-for-byte commit checks
  refuse.

### First run on Windows

1. `python scripts/devsys.py doctor`: no FAIL; fix the two warnings.
2. `python -m unittest scripts.tests.test_agent_coordination scripts.tests.test_devsys_tooling`:
   note which failures are POSIX assumptions of the tests.
3. Two PowerShell windows, two agent ids: claim in one, try to claim in the
   other (CONFLICT), `msg.py send` / `inbox` between them, close the first
   window and check `claim.py --status` reports its claim DEAD.
4. Configure one worker profile, `python scripts/devsys.py workers start`,
   confirm the panel shows it running, then **Stop now** and confirm the
   agent's whole process tree is gone (Task Manager).

## Porting checklist

For Windows, or any platform without `/proc`:

1. Implement `boot_id` and `process_start`, then `pid_alive`. (Windows:
   `process_start` and `pid_alive` are implemented; `boot_id` is not.)
2. Run `python3 -m unittest discover -s scripts/tests`. The liveness tests
   (`test_lifecycle_hardening.Liveness`) and the sweep tests show whether
   the verdicts hold.
3. The shell entry points are optional: `verify_development_system.sh` is a
   thin wrapper around `python3 scripts/devsys.py verify-structure` and the
   unit tests, and nothing registered calls it.
