# Portability

**Decision:** ADR-000199 (decision 8)
**Code:** `scripts/devsys_platform.py`

## Supported environment

| Requirement | Minimum | Checked by |
|---|---|---|
| Python | 3.9, standard library only | `devsys.py doctor` |
| git | 2.20 | `devsys.py doctor` |
| Shell | POSIX `bash` (`verify_development_system.sh`, `tools/audit`'s callers) | — |
| OS | Linux (supported, tested); other POSIX systems expected to work with the fallbacks below | `devsys.py doctor` "platform" |

The framework needs no database, service, daemon or network access. Its state
consists of files in the repository, plus `.agent/state/` in the main
worktree of each clone.

## What is platform-specific, and where

Every primitive that differs between operating systems lives in
`scripts/devsys_platform.py`. The rest of the tooling calls only that module.

| Primitive | Linux | other POSIX | Windows (best-effort, untested) |
|---|---|---|---|
| inter-process lock (`lock` / `unlock`) | `fcntl.flock` | `fcntl.flock` | `msvcrt.locking` on the first byte |
| create without overwrite (`link_no_clobber`) | hard link | hard link | check + rename (callers hold the claims lock) |
| boot identity (`boot_id`) | `/proc/sys/kernel/random/boot_id` | none: reboot not detected | none |
| process start time (`process_start`) | `/proc/<pid>/stat` field 22 | none: pid reuse not detected | none |
| process exists (`pid_alive`) | `kill(pid, 0)` | `kill(pid, 0)` | unknown |
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

## Porting checklist

For Windows, or any platform without `/proc`:

1. Implement `boot_id` and `process_start` (on Windows:
   `GetTickCount64` / the process creation time), then `pid_alive`
   (`OpenProcess`).
2. Run `python3 -m unittest discover -s scripts/tests`. The liveness tests
   (`test_lifecycle_hardening.Liveness`) and the sweep tests show whether
   the verdicts hold.
3. Replace the shell entry points: `verify_development_system.sh` is a thin
   wrapper around `python3 scripts/devsys.py verify-structure` and the unit
   tests.
