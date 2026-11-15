# WORK-000004: This repository runs workers: agent profiles in devsys.json, coordination in AGENTS.md, message hooks

**Status:** completed
**Type:** process-change
**Request:** requests/processing/2026-11-15-process-change-configure-this--req-chat-20261115-002.md
**Requirements:** WORKER-012, COORD-004, COORD-005, COORD-012
**Decision:** ADR-000203
**Evidence:** VER-000004

## Scope

Process change ordered by the operator (REQ-CHAT-20261115-002, ADR-000203):
configure this repository to run workers with two agent products, put agent
coordination into the agent constitution, push messages into running
sessions through hooks, and adjust WORKER-012 so that a project's own
configuration may name the agents it runs. Then run a real worker.

Protected artifacts changed: `AGENTS.md` (a new subsection "Coordination
between agents"; no lifecycle, completion, traceability or verification
rule changed) and `devsys.json` (project name, `tests.no_test_patterns`,
`workers`).

## Implementation

- `devsys.json`: `project.name` `auto_ci_cd`; `workers.agent` (first agent CLI, worker-01), `workers.profiles` / `workers.assign` (second agent CLI through the adapter, worker-02); `tests.no_test_patterns` extended by the agent settings directory.
- `AGENTS.md`: "Coordination between agents" under Concurrent Agents.
- The first agent CLI's project settings file: message hooks (`msg.py hook`) for session start, prompt, after tool use and stop.
- `scripts/tests/test_workers.py` `Identity`, `docs/requirements/worker_orchestration.md` (WORKER-012): project configuration (the project's `devsys.json`, the agent CLI's settings directory) is not scanned; defaults and templates still are.
- `docs/decisions/ADR-000203-this-repository-runs-workers.md`, `docs/decisions/decisions.txt` and its template, `docs/process/agent-profiles.md`.
- Registered and not changed: `docs/process/agent-coordination.md`, `docs/audits/current-state.md`.

## Tests

- **Test-Run:** 20261115T003929-3347557 (t2, PASS, with the new configuration)
- Live verification (2026-11-15, this machine), recorded in the evidence:
  - worker-01 started through `devsys.py workers start` on REQ-CHAT-20261115-003 with the configured default agent: the session launched, joined the roster as Nova, claimed and worked the request.
  - Coordination between two real agent sessions: a `status` message from this session (Cody) to Nova was read and answered; Nova's answers were injected into this session by the new `PostToolUse` hook (COORD-012 in a live session); on being told that a file it had registered is protected, Nova verified it, reverted its edit and asked the operator instead of working around the gate.
  - worker-02's exact configured command (the adapter around the second agent CLI, version 1.18) ran a trivial prompt in a scratch directory: exit 0, the transcript parsed with 0 bad lines, 1 tool use, a `success` result. A full request has not been run by worker-02.

## Regression impact and documentation

- Workers can now be started in this repository and spend the operator's agent subscriptions; they act with automatic permissions in this checkout.
- `test_devsys_tooling.Templates` now skips its instance-equals-template check here (an installed project): the mirrored manifests in `scripts/templates/` are kept current by hand.
- Every session of the first agent CLI in this repository runs the message hooks.
- Found by the live run, NOT fixed here: `claim.py` has no command to withdraw a path registered by mistake; a protected file registered and then left unchanged still fails the gate (REQ-CHAT-20261115-003 is waiting on that decision).

## Traceability

REQ-CHAT-20261115-002 -> WORKER-012, COORD-004, COORD-005, COORD-012 -> WORK-000004 -> evidence (pending)

## Transitions

- 2026-11-15T05:39:29.557783+00:00 created in active by agent-dev-20261114230032-8bee2e
- 2026-11-15T05:49:41.688086+00:00 active -> completed by agent-dev-20261114230032-8bee2e
