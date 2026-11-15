# WORK-000003: Push hub, autostarted panels, random names with Cody first, test isolation

**Status:** completed
**Type:** feature
**Request:** requests/processing/2026-11-15-agent-coordination-second-step-req-chat-20261115-001.md
**Requirements:** COORD-001, COORD-004, COORD-010, COORD-011, COORD-012
**Decision:** ADR-000202
**Evidence:** VER-000003

## Scope

Second step of agent coordination (REQ-CHAT-20261115-001): names drawn at
random with the first agent named Cody; push-based messaging over a
lightweight IPC mechanism; the web dashboard and the control panels started
by the first agent and ended with the last, their addresses printed at
start; all test failures fixed, including tests that fail when a session's
agent variables are inherited.

## Implementation

- `scripts/agent_messages.py`: `FIRST_NAME` / random draw, names unique ignoring case; hub client (`publish`, `subscribe`, `next_event`, `follow`), `wait` woken by the hub with a polling fallback; `station_info`, `ensure_station` (autostart), `panel_lines`, `present`; `send` and `register` announce.
- `tools/panel/station.py` (new): the station - single instance, hub (loopback TCP, token, line-delimited JSON), web control panel in process, Qt control panel as a child, `station.json`, ends after the last present agent, prunes dead entries.
- `tools/panel/web_panel.py`: the event stream waits on announced changes (`notify_change` / `wait_change`); a standalone panel follows the hub. `tools/panel/dash.py`: refresh on announcement.
- `scripts/msg.py`: `listen`, `panels`, `hook` (hook events answered with injected context / a blocked stop; agent found by `$AGENT_ID` or its session process); `join` shows the addresses.
- `scripts/claim.py`: `--claim-request` under `$AGENT_ID` joins the roster and, when it started the station, prints the addresses on stderr.
- `scripts/workers/overview.py`: the station (without its token) in the snapshot.
- `scripts/devsys_config.py`: `coordination.*` defaults.
- Test isolation: `scripts/run_tests.py` removes `AGENT_ID` / `AGENT_OWNER_PID` from every test job and switches the station autostart off; `scripts/tests/devsys_testlib.py` and the three suites with their own harness do the same at import.
- `scripts/workers/prompts/worker.md`: `wait` is pushed.

## Tests

- `scripts/tests/test_agent_coordination.py`: `Roster` (Cody first, random draw, unique ignoring case, Cody again on an empty roster), `Station` (addresses shown to the first agent, one station per clone, a message pushed to a waiting agent well inside a 120 s poll interval, roster announcements, a wrong hub token refused, the station gone after the last agent with dead entries pruned, polling fallback, the off switch), `Hooks` (context after a tool call once, a stop blocked once, agent found by its session process, silence on bad input), `WebPanel` (the stream answers at once on an announced change with a 60 s interval), `Overview` (claiming joins the roster).
- **Test-Run:** 20261115T001651-3293952 (t2, PASS; run under `claim.py --run` with the session's `AGENT_ID` / `AGENT_OWNER_PID` exported, which made nine tests fail before the isolation fix)
- Not verified by a test: the Qt control panel being opened by the station (needs a display; tried by hand, see evidence), hook injection in a live agent session (the output forms are tested against the documented contract), anything on Windows.

## Regression impact and documentation

- A background process (the station) now starts when an agent claims a request or joins, unless switched off; with `"qt"` in `coordination.panels` a window opens on the operator's desktop.
- `claim.py --claim-request` writes `AGENT <name>` (first time) and, when it started the station, the panel lines to stderr; stdout and exit codes are unchanged.
- Names are no longer allocated in pool order and are capitalised.
- Documentation: ADR-000202 (amends ADR-000201), `docs/requirements/agent_coordination.md` (COORD-001, -004 amended; COORD-010..012), manifests and templates, `docs/process/agent-coordination.md`, `docs/process/agent-profiles.md` (hook configuration), `docs/process/configuration.md`, `docs/README.md`, `docs/verification/tests.md`.

## Traceability

REQ-CHAT-20261115-001 -> COORD-001, COORD-004, COORD-010, COORD-011, COORD-012 -> WORK-000003 -> evidence (pending)

## Transitions

- 2026-11-15T05:16:51.035760+00:00 created in active by agent-dev-20261114230032-8bee2e
- 2026-11-15T05:20:42.527676+00:00 active -> completed by agent-dev-20261114230032-8bee2e
