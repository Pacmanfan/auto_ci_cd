# WORK-000001: Agent coordination: roster, message bus, web control panel, terminal dashboard, agent profiles

**Status:** completed
**Type:** feature
**Request:** requests/processing/2026-11-14-agent-coordination-agents-work-req-chat-20261114-001.md
**Requirements:** COORD-001, COORD-002, COORD-003, COORD-004, COORD-005, COORD-006, COORD-007, COORD-008, COORD-009, WORKER-006, WORKER-012
**Decision:** ADR-000201
**Evidence:** VER-000001

## Scope

Agents coordinate instead of only excluding each other, and the operator can
watch and steer them (REQ-CHAT-20261114-001, operator decisions recorded in
the request's context): friendly names and a roster; a peer-to-peer message
bus with roles (a lead agent can be added later as a participant); a web
control panel with full control and a terminal dashboard, both showing the
messages between agents; agent profiles per worker slot for mixed fleets
(two agent products, one in two major versions); shared state across git
worktrees.

Out of scope, by decision (ADR-000201): a lead agent with behaviour of its
own, message injection into running sessions, log rotation. `AGENTS.md` and
`devsys.json` (protected artifacts) are not changed; mentioning `msg.py` in
`AGENTS.md` and configuring real profiles in `devsys.json` need a
PROCESS-CHANGE request.

## Implementation

- `scripts/agent_messages.py` (new): roster (names, roles, profile, owner-process liveness, worktree / branch) and the append-only message log under `.agent/state/runtime/coordination/` in the main worktree; send / unread / inbox / wait / notice.
- `scripts/msg.py` (new): the agents' and operator's CLI (join, who, send, reply, inbox, wait, log, leave, prune).
- `scripts/claim.py`: after any command, a `MESSAGES:` line on stderr while the calling agent has unread messages (stdout and exit code unchanged).
- `scripts/workers/agent.py`, `pool.py`, `runner.py`, `scripts/devsys_config.py`: agent profiles (`workers.profiles`, `workers.assign`, `{scripts}` placeholder), profile shown in status, undefined profile refused, validation; a worker registers its agent on the roster when an attempt starts.
- `scripts/workers/overview.py` (new): one snapshot (pool status, agents with claims, worktrees, messages) and the operator's actions through the existing APIs.
- `tools/panel/web_panel.py`, `web_panel.html` (new): the web control panel (standard library, server-sent events, loopback + token + Host check).
- `tools/panel/dash.py` (new): the curses terminal dashboard, `--once` for pipes.
- `scripts/workers/profiles/event_stream_adapter.py` (new): wraps an agent CLI whose JSON event stream is not the worker protocol (both stream shapes; the second major version's shape is unverified, as documented).
- `scripts/workers/prompts/worker.md`: how a worker's agent works with the other agents; messages grant nothing.
- `scripts/drift_detect.py`: the work-record check looked for the linked request only in the checkout it ran in. In a request's linked git worktree the request file is in the main worktree (claims move it there), so the gate failed with WORK_REQUEST_MISSING and no worktree-mode request could complete. It now looks in both roots, as `agent_process.locate_request` does. The defect was present in the baseline commit (both worktree tests failed there); fixed here on the operator's instruction, because worktree support is part of this request.
- `scripts/templates/requirements.txt`, `scripts/templates/decisions.txt`: the distribution's manifests equal their templates (`test_devsys_tooling.Templates`), so the new rows are mirrored.
- Process note: `scripts/agent_messages.py`, `scripts/msg.py`, `scripts/workers/agent.py`, `runner.py`, `pool.py`, `scripts/devsys_config.py`, `scripts/claim.py` and the adapter were edited before they were registered and then adopted (`claim.py --own --adopt-existing-changes`); no other session was active (one claim in the repository). Two paths were registered and never created or changed (`scripts/workers/profiles/README.md`, `docs/process/worker-orchestration.md`, a misspelling of `worker-orchestrator.md`), and `scripts/templates/devsys.json`, `scripts/devsys.py` were registered but needed no change.

## Tests

- `scripts/tests/test_agent_coordination.py` (new, 36 cases): Roster, Messages, Cli, Worktrees, Profiles, Adapter, Overview, WebPanel, Dashboard.
- `scripts/tests/test_workers.py` `Identity`: scans for both documented agent products; exceptions are the profiles page and the adapters directory; request records and the untracked claims directory are not scanned; the adapters directory holds only `*_adapter.py`.
- Regression cover for the drift fix: `test_workers.WorktreeIsolation.test_a_worktree_request_completes_on_its_branch_with_a_verified_binding` and `test_project_subdirectory.Workers.test_worktree_requests_are_isolated_and_their_commits_verified` (both failed on the baseline commit, pass now).
- Run 20261114T231847-3029357 (t2) FAILED and is superseded: 9 failures came from this session's `AGENT_ID` / `AGENT_OWNER_PID` being inherited by tests that create their own agents (a test-isolation weakness, see Known issues), 1 from the manifests not yet mirrored into the templates, 2 from the worktree defect above.
- **Test-Run:** 20261114T234259-3083906 (t2, PASS; started with `AGENT_ID` / `AGENT_OWNER_PID` removed from the environment)
- Not verified by a test: a live session of either real agent CLI through a worker (the second product's configured model endpoint was not reachable on this machine; its error event and stdin prompt handling were observed directly), and the second major version's stream (not released).

## Regression impact and documentation

- Existing configurations are unchanged: without `workers.profiles` / `workers.assign` every worker runs `workers.agent`. `pool.status` gains `profiles` and per-worker `profile` / `configured`; `agent_configured` now means every enabled worker's profile has a command.
- `claim.py` writes one more stderr line only when the caller has unread messages.
- Documentation: `docs/requirements/agent_coordination.md` (new), `docs/requirements/worker_orchestration.md` (WORKER-006, -012), `docs/requirements/requirements.txt`, `docs/decisions/ADR-000201-...md`, `docs/decisions/decisions.txt`, `docs/process/agent-coordination.md` (new), `docs/process/agent-profiles.md`, `docs/process/worker-orchestrator.md`, `docs/process/configuration.md`, `docs/README.md`, `docs/verification/tests.md`, `CI_CD.md`.

## Known issues

- Several suites (`test_completion_gate_test_run`, `test_process_protection`, `test_process_concurrency`) fail when the test run inherits a session's `AGENT_ID` / `AGENT_OWNER_PID`, which AGENTS.md tells a session to export. Not changed here; run `scripts/run_tests.py` with both removed from the environment until the harness scrubs them.
- `scripts/workers/profiles/` and `scripts/msg.py` usage are not mentioned in `AGENTS.md` (protected).

## Traceability

REQ-CHAT-20261114-001 -> COORD-001, COORD-002, COORD-003, COORD-004, COORD-005, COORD-006, COORD-007, COORD-008, COORD-009, WORKER-006, WORKER-012 -> WORK-000001 -> evidence (pending)

## Transitions

- 2026-11-15T04:18:17.540670+00:00 created in active by agent-dev-20261114230032-8bee2e
- 2026-11-15T04:47:38.965456+00:00 active -> completed by agent-dev-20261114230032-8bee2e
