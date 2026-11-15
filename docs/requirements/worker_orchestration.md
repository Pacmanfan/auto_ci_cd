# Worker Orchestration Requirements

**Status:** ACTIVE
**Owner:** Development tooling (`scripts/workers/`, `tools/panel/`)
**Depends On:** `development_operations.md` (REQ-DEVOPS-014 intake, REQ-DEVOPS-016 claims, REQ-DEVOPS-019 completion gate, REQ-DEVOPS-024 escalation, REQ-DEVOPS-025 recovery, REQ-DEVOPS-042 commit attribution, REQ-DEVOPS-051 verify-commit): the lifecycle the workers drive, unchanged
**Decision:** ADR-000200 (supersedes the external-engine placement of ADR-000196; carries ADR-000197 and ADR-000198 forward)
**Supersedes:** TICKET-ENGINE-001..008 (`docs/history/requirements/autonomous_ticket_engine.md`)
**System documentation:** `docs/process/worker-orchestrator.md`, `docs/process/agent-profiles.md`

The Worker Orchestrator runs coding-agent workers that execute this
development system's requests unattended. The request lifecycle stays the
single source of truth; workers add execution, not a second ticket system.

## WORKER-001 — Requests are the unit of work

A worker SHALL execute framework requests (`requests/`), and SHALL
determine every outcome from the request's authoritative state: its
directory, its claim, its work and evidence records and its gate records.
A worker's own records SHALL NOT hold a status of a request.

## WORKER-002 — No second ticket system

The orchestrator SHALL keep no ticket list, ticket status or hold of its own.

- Its only per-request memory SHALL be an append-only attempts journal keyed
  by request id. The journal is used to count attempts.
- A request that needs a human SHALL be moved to `needs-review` through the
  framework, with the reason recorded (`transition_request`).
- A human's resolution SHALL go through the framework too.

## WORKER-003 — One active request per worker, never shared

Each worker SHALL have at most one active request.

- **Resume first:** it SHALL resume the request its own agent id holds in
  `processing` before taking a new one.
- **Queue:** it SHALL then take the framework's queue (WORKER-007), skipping
  excluded types and requests whose attempts are exhausted.
- **Atomic assignment:** it SHALL claim through the framework's atomic claim,
  with the worker process as the claim's owner. Assignment SHALL be
  serialised across workers, so no two workers ever hold the same request.

## WORKER-004 — Per-worker state

Every worker SHALL have its own runtime directory
(`.agent/state/runtime/workers/<id>/`, never committed) holding:

- its record;
- its process lock;
- its event log;
- its attempts: prompt, launch identity, transcript, report and validation.

Each worker SHALL have a fixed agent id. No worker SHALL write another
worker's state.

## WORKER-005 — Isolation

`workers.isolation` SHALL select how workers are separated:

- **`shared`:** every worker runs in the project checkout, kept apart by
  claims and owned-path commits.
- **`worktree`:** every request runs in its own git worktree and branch
  (`agent_process.create_worktree`), and the agent's tools SHALL be the
  worktree's own copy. Publishing a request branch is manual (roadmap COR-14)
  until a publish step exists.

## WORKER-006 — Configuration and control

- **Configuration:** `workers.max_workers` SHALL bound the number of worker
  slots (`worker-01` .. `worker-NN`) and of running workers.
- **Control:** a worker SHALL be enabled / disabled, started, stopped
  (after its current attempt) and stopped now (its session ended, the
  attempt INTERRUPTED, the claim kept).
- **Refusals:** start SHALL be refused for a disabled worker, when no agent
  is configured (exit 10), and when the maximum already runs.
- **Interfaces:** every control SHALL be available through
  `scripts/devsys.py workers …` and through the `workers.pool` API.

## WORKER-007 — Priority and queue

- **Priority values:** a request's priority SHALL be one of CRITICAL, HIGH,
  NORMAL, LOW.
- **Setting it:** it SHALL be set at intake (`create_request.py --priority`,
  including a single chat prompt, `--source chat`) and changed afterwards on
  an open request (`devsys.py priority`, `agent_process.set_priority`). Each
  change SHALL be recorded in the request's sidecar.
- **Queue order:** the queue SHALL be the unclaimed inbox ordered by
  priority, then arrival.
- **Views:** the queued, working and finished views SHALL be derived from
  the request states: inbox, processing, completed / rejected / needs-review.

## WORKER-008 — Gates are never weakened

A worker's request SHALL complete only through the framework: the completion
gate record, `--complete-request` and the owned-path commit.

- **Validation:** the worker's independent validation SHALL re-check commit
  binding (`verify-commit`).
- **Refused completion:** a completion the framework refused SHALL count as
  an attempt without completion.
- **Exhausted attempts:** after `workers.attempt.max_attempts_per_request`
  such attempts the request SHALL be escalated to `needs-review`.

## WORKER-009 — Recovery

Whether a worker process or an agent session runs SHALL be verified, never
inferred from a record: host, boot, pid with start time, and the session's
environment marker.

- **Settling:** `workers recover` SHALL settle a stopped worker's recorded
  attempt. A DEAD session is concluded (INTERRUPTED without a result, not
  counted), and the claim stays with the worker, which resumes it.
- **Running workers:** a running worker SHALL never be touched.
- **Orphaned sessions:** a still-running orphaned session SHALL be ended only
  with `--force`.
- **Re-adoption:** a worker starting again SHALL first settle or re-adopt
  its own recorded attempt.

## WORKER-010 — Status and inspection

`workers status` / `workers.pool.status` SHALL report:

- every worker's enabled state, status and verified process liveness;
- its active request (id, priority, title, elapsed time, session liveness);
- its counters;
- the queue, the working set (with the worker holding each request) and
  recently finished requests.

`workers peek` SHALL add the attempt, its validation, the transcript tail and
the worker's recent events.

## WORKER-011 — Control panel

A Qt control panel (`tools/panel/worker_panel.ui` designed in Qt Designer,
`tools/panel/worker_panel.py` as its controller) SHALL show:

- the maximum and running worker counts;
- worker cards with status and active request;
- a detail view of the selected worker;
- queued, working and finished requests with their priority.

It SHALL offer start, stop, stop now, recover, enable, disable and
set-priority. It SHALL be a client of the `workers.pool` API and
`agent_process.set_priority` only, with no lifecycle logic of its own.

## WORKER-012 — Vendor-neutral system

The coding agent SHALL be configuration (`workers.agent.command`, a command
speaking the JSON-lines protocol of `scripts/workers/agent.py`).

- **Neutral names:** code, configuration defaults, environment variables
  (`DEVSYS_WORKER_*`, `DEVSYS_SCRIPTS`), paths and documentation SHALL name
  no agent vendor or product.
- **The one exception:** the documented agent profiles
  (`docs/process/agent-profiles.md`), which hold the vendor-specific command
  lines and variables an external agent genuinely requires.

## WORKER-013 — Relative to the containing project

The system SHALL derive every path from its own location and
`paths.project_root` (the project it serves, relative to the system
directory: `.` today, `..` for an installation as
`<project>/<system-directory>/`).

- Workers SHALL run their agents in the project root, or the request's
  worktree.
- The prompt and `DEVSYS_SCRIPTS` SHALL name the tools relative to the
  agent's working directory.
- No machine-specific path SHALL appear in the system.

Verification: `scripts/tests/test_workers.py`, `scripts/tests/test_worker_panel.py`, `scripts/tests/test_devsys_tooling.py`.
