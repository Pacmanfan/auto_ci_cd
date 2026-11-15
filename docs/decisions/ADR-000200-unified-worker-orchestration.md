# ADR-000200: One development system, with a Worker Orchestrator inside it

**Status:** ACCEPTED
**Date:** 2026-09-24
**Requirements:** `docs/requirements/worker_orchestration.md` WORKER-001..013 (new); supersedes TICKET-ENGINE-001..008
**Supersedes:** ADR-000196: the engine's placement outside the repository, its one-worker rule and its ticket ledger
**Carries forward:** ADR-000197 (selection rules, git sanity against the attempt baseline), ADR-000198 (verified liveness, recovery without losing history)

## Context

The unattended request processor started as a separate installation beside
this system. It had its own:

- CLI, configuration, state directory and prompts;
- per-ticket **ledger**, with statuses (RUNNING, PARTIAL, HUMAN_REQUIRED, …)
  and holds that lived only there.

Merge phase 1 imported its source. The operator's direction for phase 2 has
two parts.

The consolidated system must:

- be one development operating system, installable later as a
  subdirectory of a project;
- have exactly one authoritative ticket / request system;
- support multiple concurrent, isolated workers with one active request
  each, and a human-inspectable status;
- carry no vendor identity.

The completion gates must not be weakened to suit the worker.

Imported as it was, the engine violated four of these:

- its ledger was a second ticket system;
- it ran one worker;
- it completed requests with a lifecycle that predated the gate records;
- it was named after, and hard-wired to, one agent product.

## Decision

1. **Requests are the only unit of work.** A worker executes one request
   and reads every outcome from the request's authoritative state:
   - its directory, claim, work and evidence records;
   - its gate records.

   Worker records never hold a request status.
2. **No ledger.**
   - **Attempt memory:** an append-only attempts journal
     (`.agent/state/runtime/workers/attempts.jsonl`) is orchestration memory
     keyed by request id. It is used only to count attempts.
   - **Holds:** what the engine called a hold is an escalation through the
     framework: `transition_request → needs-review` with the reason. A human
     resolves it through the framework as for any request.
3. **Workers.**
   - **Slots and ids:** `workers.max_workers` slots (`worker-01` ..), each
     with a fixed agent id, one process and one lock.
   - **Claiming:** a worker claims through `agent_process.claim_request` with
     its own process as the claim's owner (ADR-000199 liveness), resumes its
     own claim first, and otherwise takes the framework's queue (priority,
     then arrival).
   - **No sharing:** assignment is serialised (`workers-assign` lock) on top
     of the atomic claim, so two workers never share a request.
   - **Outcomes:** PARTIAL and failed attempts are retried under the same
     claim up to `max_attempts_per_request`, then escalated. Interruptions
     and usage limits are not counted.
4. **Isolation.**
   - **State:** per-worker runtime directories, always.
   - **Checkout:** `workers.isolation: shared` keeps all workers in the
     project checkout under owned-path commits. `worktree` gives each request
     its own git worktree and branch (`agent_process.create_worktree`, moved
     out of the CLI), and the agent runs that worktree's copy of the tools.
   - **Publishing:** request branches are published manually for now
     (roadmap COR-14).
5. **Gates kept.**
   - **Prompt:** the worker prompt teaches the gated lifecycle
     (`work.py`, `evidence.py`, `completion_gate.py --record`).
   - **Validation:** independent validation re-checks commit binding with
     `verify-commit`.
   - **Refusals:** a completion the framework refuses is an attempt without
     completion, never a success.
6. **Vendor-neutral agent adapter.**
   - **Protocol:** the agent is `workers.agent.command`, any command that
     reads the prompt on stdin and writes JSON-lines events with a final
     `result` event.
   - **Configuration only:** vendor command lines and variables are
     configuration, documented as the one exception in
     `docs/process/agent-profiles.md`.
   - **Neutral names:** the system's own variables are `DEVSYS_WORKER_*` and
     `DEVSYS_SCRIPTS`. Process identity, zombie detection, session markers and
     process-tree termination moved into `scripts/devsys_platform.py`,
     replacing the duplicate.
7. **One interface, two faces.**
   - **API:** `scripts/workers/pool.py` (status, start, stop, enable,
     disable, recover, peek) is the API.
   - **Clients:** `scripts/devsys.py workers | queue | priority` and the Qt
     control panel (`tools/panel/worker_panel.ui` + `worker_panel.py`) are
     its clients.
8. **Project-relative.** `paths.project_root` names the project the system
   serves, relative to the system directory (`.` today, `..` for
   `<project>/<system-directory>/`). Workers run agents there; the prompt
   and `DEVSYS_SCRIPTS` name the tools relative to the agent's working
   directory.

## Deferred (not ported in this phase)

These capabilities of the imported engine were not ported:

- **Supervisor review:** a periodic read-only reviewer session.
- **Run records and the run audit** (`final-audit.md`).
- **Operator-named ticket lists per run.**
- **Circuit breaker:** only a per-worker consecutive-failure stop was kept.
- **Usage-limit waiting:** a bounded wait is kept, not the engine's pause /
  resume.
- **The build-tree provenance check.**

The imported source is preserved outside this system: the original
repository, unchanged, plus the phase-1 record in
`docs/history/worker-orchestrator-merge.md`. Each capability can return as a
client of the same API.

## Rejected alternatives

- **Adapt the engine's runner and ledger.** Its single `current` attempt, its
  ledger and its statuses are the competing ticket system this decision
  removes. Converting them in place would keep two state models alive.
- **Keep the engine external and only rename it.** That leaves two
  installations, two configurations and the ledger.
- **Workers as threads of one scheduler.** Separate processes give each
  worker its own crash domain, verified liveness and lock, and a simple stop
  / recover model.
- **A worker database or service.** File-based state, as everywhere else.

## Consequences

- **Removed:** `worker_orchestrator/` and the separate installation's CLI,
  configuration, ledger and panel script.
- **Replaced:** TICKET-ENGINE-001..008 by WORKER-001..013. The old contract
  and the phase-1 mapping are historical.
- **Agent configuration:** workers need an agent (`workers.agent.command`).
  Without one, `start` is refused (NOT CONFIGURED), which `doctor` reports.
