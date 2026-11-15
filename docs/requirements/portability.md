# Portability Requirements

**Status:** ACTIVE (Windows: implemented from documentation, NOT yet verified on a Windows host)
**Owner:** Development tooling (`scripts/devsys_platform.py`, and every entry point)
**Decision:** ADR-000199 decision 8 (platform primitives in one module); ADR-000201 (coordination, panels, agent profiles)
**System documentation:** `docs/process/portability.md`

Linux is the supported and tested platform. These requirements state what
the system does so that it can also run on Windows, driven from PowerShell.

## PORT-001 — Process liveness and termination on Windows

- `pid_alive` and `process_start` SHALL answer from the operating system
  (process existence and creation time), so a recorded owner process is
  verified ALIVE or DEAD and a reused pid is recognised. A query that fails
  SHALL be "unknown", never an exception and never ALIVE.
- Ending a session (`kill_tree`) SHALL ask the process tree to stop, wait
  for the grace period, then force the whole tree to end.

## PORT-002 — No POSIX shell, no `python3` command

- Nothing the system runs by itself (registered test suites, audits, the
  health check) SHALL need `bash`. The structural check runs as
  `devsys.py verify-structure`; `scripts/verify_development_system.sh`
  remains a convenience wrapper for POSIX shells.
- `python3` as the first word of a registered command SHALL mean the
  interpreter that runs the tool. Agent commands MAY use the `{python}`
  placeholder for the same purpose.

## PORT-003 — Agent commands installed as shims

A worker's agent command, and a command wrapped by an adapter, SHALL be
started through the path the shell would find, so a CLI installed as a
`.cmd` / `.bat` shim starts under its bare name.

## PORT-004 — Text is UTF-8

- The tools' own output SHALL be UTF-8 on Windows, with unencodable
  characters replaced rather than raising.
- git output read through `agent_process.git` SHALL be decoded as UTF-8.
- `devsys.py doctor` SHALL warn on Windows when Python's UTF-8 mode is off
  and when `core.autocrlf` is not `false`.

## PORT-005 — Views without curses

The terminal dashboard SHALL run where `curses` is missing: a plain live
view redrawn with ANSI sequences, with the same keys read from the Windows
console. `--once` and the web control panel need neither.

## PORT-006 — Concurrent file access

Replacing a state file SHALL be retried briefly on Windows when another
process has it open, instead of failing the writer. The web panel SHALL NOT
share its port with another server.

## Verification

`python3 -m unittest scripts.tests.test_devsys_tooling` (`WindowsBranches`:
the Windows code paths with the Win32 calls replaced; `NoShellNeeded`),
`scripts.tests.test_agent_coordination` (`Dashboard`: the view without
curses). **No test has run on Windows.** `docs/process/portability.md` lists
what must be checked there first.
