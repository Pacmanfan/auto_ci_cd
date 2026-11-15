# ADR-000196: An external, single-worker engine processes requests unattended through the existing lifecycle

**Status:** ACCEPTED; superseded in part by ADR-000200 (the engine is merged into this system as the Worker Orchestrator; its placement outside the repository, the one-worker rule and its ticket ledger no longer apply)
**Date:** 2026-09-24
**Work:** WORK-000316 (REQ-CHAT-20260924-004)
**Requirements:** `docs/requirements/autonomous_ticket_engine.md` TICKET-ENGINE-001..008 (new)

## Context

The user wants to leave the backlog running for an extended period (a
weekend) and have a coding agent work through `requests/inbox/` one request at a
time, with no one watching. When they come back, a record should show what
was done, what was committed, what passed, what failed and what needs them.

The repository already has everything that defines *how* a request is
processed:

* the request states and their transitions (`docs/process/state-model.md`);
* atomic, owner-checked claims with heartbeats, stale detection and explicit
  recovery (`scripts/claim.py`, ADR-000090);
* commits of a request's own delta in a shared checkout (`scripts/commit_request.py`);
* the completion gate, the tiered test orchestrator and escalation to
  `needs-review`;
* intake (`scripts/create_request.py`) and the selection order
  (`scripts/process_next_request.py`).

What it lacks is something that keeps starting agents. Today, every request is
processed by a person opening an agent session. Nothing yet records,
across sessions, what an unattended agent did and how it ended.

## Decision

1. **The engine lives outside the repository** (`<ENGINE_HOME>`, its own git
   repository). Its state, prompts, logs, transcripts and audits are under its
   own `var/` directory. This repository gets only the contract
   (TICKET-ENGINE-*), a short operator page
   (`docs/process/autonomous-ticket-engine.md`) and the usual work records.
2. **Each worker is a fresh headless agent session started in the repository
   root.** AGENTS.md, the project settings and the per-project memory
   therefore apply to it as to an interactive session. No conversation carries
   over between requests. A retry is a new session, given the previous
   attempt's report.
3. **The engine drives the existing lifecycle and does not duplicate it.** It
   claims with `scripts/claim.py` under its own agent id, one id per request,
   and passes the id to the worker as `AGENT_ID`. It refreshes the heartbeat
   while the worker runs. The worker does everything else as AGENTS.md
   prescribes, including committing through `scripts/commit_request.py` and
   completing or escalating through `scripts/claim.py`. The engine reads the
   outcome from where the request file ends up and from git. It never
   commits, resets or discards anything.
4. **One worker, in the normal checkout, no worktrees (V1).** The checkout is
   already shared with other sessions, and the claim system is what keeps
   them apart. A second engine worker would add failure modes without adding
   anything the claim system lacks.
5. **Independent, cheap verification with the project's own tools** after
   every attempt:
   * the completion gate;
   * the incremental shadow build when C++ or qmake inputs changed;
   * `run_tests.py --tier t1 --paths <committed files>`.

   A periodic, **separate read-only supervisor session** reviews the
   accumulated results. Its follow-ups enter through
   `scripts/create_request.py --source automation`, and are not processed in
   the run that filed them.
6. **Conservative selection.** The project's order is used, unless the
   operator names requests. `PROCESS-CHANGE` requests are not started unless
   named, because process changes need human review. Neither are requests
   that record a "do not perform right now" intake constraint; those are
   reported as needing a human decision, not silently skipped.
7. **Bounded and durable.** The engine bounds worker runtime, attempts per
   request, requests per run, run duration and consecutive failures. It
   waits out usage limits. State is kept in atomically written JSON and
   append-only JSON-lines files. A still-running worker is re-adopted after
   an engine crash. HUMAN_REQUIRED holds persist across runs. Every run ends
   with a final audit.

## Alternatives considered

* **Put the engine in this repository (`scripts/` or `tools/`).** Rejected.
  The engine's runtime state and transcripts must not be committed here. Its
  process also changes independently of the product. The contract is what
  belongs in the repository.
* **Extend `scripts/process_next_request.py` into the loop.** Rejected. That
  script is the per-agent claim-and-plan step, and it is protected by the
  process rules. The engine calls the same primitives instead.
* **One long agent session working through the whole backlog.** Rejected:
  its context would grow without limit, and one crash would lose all state.
  Disposable sessions plus durable records are the point.
* **Parallel workers in git worktrees.** Deferred. The extension points are
  documented in `<ENGINE_HOME>/docs/README.md`.
* **A new REQ-DEVOPS requirement.** Rejected. The engine adds no lifecycle,
  completion or verification rule: it is one more agent under the existing
  ones. It gets its own small requirement domain, as the architecture audit
  did (ADR-000195), rather than a protected-artifact change.

## Consequences

* Unattended processing becomes available. How much the engine may do is
  the operator's choice: the selection filters and naming requests
  explicitly. The Weekend Run procedure in the engine documentation
  recommends curating the list.
* The engine's claims appear in `scripts/claim.py --status` under ordinary
  `agent-*` ids. A claim it keeps for partial work can be recovered by a
  human in the usual way. The engine then records the request as taken over
  and never takes it back.
* Workers run under the agent's own permission mode (default `auto`). Actions
  its classifier denies are not attempted another way. They are reported,
  and a request that needed them ends BLOCKED or ESCALATE for a human.
* Verification of the engine lives in its own repository. The acceptance
  evidence for this repository is a real request processed end to end
  (VER-000317).
