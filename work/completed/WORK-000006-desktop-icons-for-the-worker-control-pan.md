# WORK-000006: Desktop icons for the worker control panel and the web dashboard

**Status:** completed
**Type:** feature
**Request:** requests/processing/2026-11-15-desktop-icons-put-an-icon-on-t-req-chat-20261115-004.md
**Requirements:** COORD-011
**Decision:** ADR-000202
**Evidence:** VER-000006

## Scope

Desktop icons for the worker control panel and the web dashboard

## Implementation

- `tools/panel/station.py`: `--open` (open the running station's web control panel in the browser; with no station, start one with the web panel only that stays), `--stop`, `--panels`.
- `tools/panel/desktop_launcher.py`: a second desktop entry, "Web Dashboard" (`station.py --open`), beside the worker control panel's; `uninstall` removes both; `show` prints both. Fixed: the temporary file written during `install` had no `.desktop` extension, so `desktop-file-validate` (when installed) rejected every install.
- Installed on the operator's machine with `python3 tools/panel/desktop_launcher.py install --desktop`: two entries in the application menu and two icons on the desktop, marked trusted.

## Tests

- `scripts/tests/test_agent_coordination.py` `Station`: the dashboard launcher opens the web panel through the browser, starts a station that stays with the web panel only, reuses a running one, `--stop` ends it; the installer writes and removes both entries (menu and desktop) inside a temporary home.
- Run 20261115T005154-3442568 (t2) FAILED and is superseded: the first version of the installer test ran `uninstall` against the real home and removed the two icons just installed on the operator's desktop. The test now uses a home of its own and asserts the real desktop is unchanged; the icons were installed again.
- **Test-Run:** 20261115T005553-3508555 (t2, PASS)
- Not verified: clicking the icons in the desktop session (the entries validate, are executable and marked trusted).

## Regression impact and documentation

- A station started by the dashboard icon stays until `station.py --stop` or logout, and starts no Qt panel; agents that join later use it.
- Documentation: `docs/process/agent-coordination.md` ("Desktop icons"), `docs/requirements/agent_coordination.md` (COORD-011), `docs/verification/tests.md`.
- Registered and not changed: `docs/requirements/requirements.txt`, `scripts/templates/requirements.txt`.

## Traceability

REQ-CHAT-20261115-004 -> COORD-011 -> WORK-000006 -> evidence (pending)

## Transitions

- 2026-11-15T05:51:54.402712+00:00 created in active by agent-dev-20261114230032-8bee2e
- 2026-11-15T05:59:26.737254+00:00 active -> completed by agent-dev-20261114230032-8bee2e
