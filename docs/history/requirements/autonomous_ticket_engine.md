# External Autonomous Ticket Engine Contract

> **HISTORICAL — superseded (2026-09-24) by `docs/requirements/worker_orchestration.md`
> (WORKER-001..013, ADR-000200).** The external engine this contract governed was merged into
> the development system as the Worker Orchestrator (`scripts/workers/`). Kept as the record of
> TICKET-ENGINE-001..008; not current authority.


**Status:** ACTIVE (V1)
**Owner:** Development tooling outside this repository: `<ENGINE_HOME>` (its own git repository)
**Depends On:** `development_operations.md` (REQ-DEVOPS-006 lifecycle, REQ-DEVOPS-016 claims, REQ-DEVOPS-024 escalation, REQ-DEVOPS-025 recovery, REQ-DEVOPS-027 process-next-request order, REQ-DEVOPS-037 priorities, REQ-DEVOPS-038 commits in a shared worktree, REQ-DEVOPS-043 submit without processing: the mechanisms the engine drives, unchanged)
**Decision:** ADR-000196; amended by ADR-000197, ADR-000198
**Work:** WORK-000316 (REQ-CHAT-20260924-004); WORK-000319 (REQ-CHAT-20260924-013: TICKET-ENGINE-002, -006 amended); WORK-000321 (REQ-CHAT-20260924-015: TICKET-ENGINE-001 amended, operator entry point); WORK-000324 (REQ-CHAT-20260924-018: TICKET-ENGINE-008 amended, restart safety)
**System documentation (at the time):** a project-side process page, since replaced by `docs/process/worker-orchestrator.md`; `<ENGINE_HOME>/docs/` (the engine)
**Extraction note:** the engine is a separate repository and was not extracted; this document
is kept because it states the engine's contract with the repository-side tooling.
**Merge phase 1 note (2026-09-24, not normative):** the engine's source has since been imported
into this repository as the **Worker Orchestrator** (phase 1 import), at the operator's
direction, without redesign. That conflicts with TICKET-ENGINE-001 ("SHALL live outside this
repository") and with the copy-anywhere launcher / panel behaviour. Neither is amended here: the
conflict was recorded in the phase-1 mapping (`docs/history/worker-orchestrator-merge.md`) and is resolved by an ADR
in the merge phase, which also renames the contract.

An external engine may process requests from `requests/inbox/` unattended.
It runs one fresh coding-agent worker session per attempt in this checkout.
The worker follows the same lifecycle as any other agent (AGENTS.md). This
contract states what such an engine must and must not do in this repository.
It adds no lifecycle, completion, traceability or verification rule. The
engine is one more agent under REQ-DEVOPS-006..046, and the requirements
above stay the authority for the lifecycle.

## TICKET-ENGINE-001 — Outside the repository, working inside it

The engine, its configuration, prompts, state, logs, transcripts and audits
SHALL live outside this repository and SHALL NOT be committed to it. A
worker session SHALL be started with the repository root as its working
directory, so that AGENTS.md, the project settings and the per-project
agent memory apply to it exactly as to an interactive session.

The operator's entry point is the engine's control panel. Opening the
engine's launcher with no command, or double-clicking it, SHALL open the
control panel. A copy of the launcher or of the control panel placed
outside the engine directory, for example on the desktop, SHALL find the
engine and run its current control panel. If it cannot find the engine,
it SHALL say so. It SHALL NOT fail silently or run with a default
configuration.

## TICKET-ENGINE-002 — Selection

The engine SHALL take first the requests an operator names, in the
operator's order. It SHALL then resume the requests it started and has not
finished, and then take the rest of the inbox in the order of
`scripts/process_next_request.py` (priority CRITICAL > HIGH > NORMAL > LOW,
then file name; REQ-DEVOPS-037/043). Naming requests SHALL NOT end the run
when they are done: the run continues until one of its bounds
(TICKET-ENGINE-008). It SHALL NOT start a `PROCESS-CHANGE` request, which
needs human review under process protection, unless an operator names it.

A phrase such as "do not perform (it) right now / at this time" in a request
is an instruction to the session that filed it (submit without starting,
REQ-DEVOPS-043). It is not a property of the request, and the engine SHALL
NOT withhold a request for it. An operator naming a `requests/needs-review/`
request is the human re-queue that the state model allows
(needs-review -> processing).

When a run stops, its recorded reason SHALL name the bound it reached and the
number of runnable requests left. When it found nothing to start, the reason
SHALL say why the remaining inbox requests were not runnable, and an empty
inbox SHALL be reported as such. (Amended 2026-09-24, REQ-CHAT-20260924-013,
ADR-000197: a run limited to one named request reported "no eligible tickets
left" while 21 requests were in the inbox.)

## TICKET-ENGINE-003 — Claims

The engine SHALL claim through `scripts/claim.py --claim-request` under an
agent id from `scripts/claim.py --new-agent-id`. It SHALL use one agent id per
request for every attempt at that request, and SHALL pass it to the worker as
`AGENT_ID`. While a worker runs, the engine SHALL refresh the claim's
heartbeat (`--heartbeat`), so that a long session is never taken for an
abandoned one. It SHALL NOT recover, or take back, a claim held by another
session, whatever the heartbeat age. A worker that is blocked by another
session's claim reports BLOCKED.

## TICKET-ENGINE-004 — Workers own the work

Each attempt SHALL be a new agent session that receives no earlier
conversation. It gets the request, the project instructions, and a summary of
earlier attempts at that request. The worker SHALL follow AGENTS.md "Request
Processing" from intent onward. It SHALL register paths before editing them
and commit only through `scripts/commit_request.py`. It SHALL move the request
with `scripts/claim.py`: `--complete-request` and `--release-request` when it
is done; `--needs-review-request` when a human decision is needed; the claim
kept, with progress committed, when it runs out of time. It SHALL end with a
structured completion report whose status is one of `COMPLETED`, `PARTIAL`,
`BLOCKED`, `FAILED` or `ESCALATE`.

## TICKET-ENGINE-005 — The engine does not change the checkout

The engine SHALL NOT stage, commit, reset, stash, check out or delete
anything in the checkout. The only request transitions it performs itself
are:

* the claim;
* returning an untouched claim to the inbox (`--release-request --requeue`)
  when an attempt needs a human and left neither commits nor uncommitted
  owned work.

It SHALL NOT signal a process it did not start. Uncommitted work a worker
leaves behind SHALL be kept and reported, never discarded.

## TICKET-ENGINE-006 — Independent verification

After each attempt the engine SHALL record, independently of the worker's
report:

* HEAD before and after;
* the commits attributed to the request, and other sessions' commits in the
  same window;
* uncommitted work still registered to the claim;
* where the request file is now.

It SHALL measure the shared index and working tree against the attempt's own
baseline, recorded before the worker started. The baseline includes each
staged path's index content, so the engine can tell:

* staged before and unchanged;
* staged again with other content, or no longer staged;
* newly staged, as a new file or a change to a tracked file;
* modified before;
* created or modified during the attempt.

It SHALL check the build tree's Makefiles for a forbidden generator
(PROJECT-SPECIFIC; the source project: Qt 5 qmake, Qt 6 only) before and after the
attempt. State present before the attempt SHALL be reported as pre-existing
and SHALL NOT be counted against the attempt. The checkout and build tree are
shared, so a change in the attempt's window SHALL be reported as the
window's, not asserted to be the worker's. The engine SHALL NOT stage,
unstage, clean or regenerate anything to fix what it finds (amended
2026-09-24, ADR-000197).

It SHALL then run this project's own checks: `scripts/completion_gate.py`
for the request's WORK records, the canonical incremental shadow build when
C++ or qmake inputs changed, and `scripts/run_tests.py --tier t1 --paths
<committed files>`. Each check SHALL be recorded as PASS, WARN, FAIL or
UNKNOWN. Inability to establish a result SHALL be UNKNOWN, never PASS. A
worker's COMPLETED SHALL NOT be counted as completed unless the request is
in `requests/completed/`.

## TICKET-ENGINE-007 — Supervision and follow-ups

A separate, read-only agent session SHALL periodically review the
workers' results against the requests, requirements, commits and validation
records. Follow-up work it identifies SHALL enter the backlog only through
`scripts/create_request.py --source automation`. It SHALL NOT be processed in
the run that filed it.

## TICKET-ENGINE-008 — Bounded, recoverable, audited

The engine SHALL bound:

* each worker's runtime;
* the attempts per request;
* the requests per run;
* the run's duration;
* consecutive failed attempts.

Reaching a bound SHALL pause or end the run with the reason recorded. Its
state SHALL survive a worker crash, an engine crash or kill, and a reboot,
under the restart-safety rules below. Every run SHALL end with an audit
that lists what was completed and committed, what validation found, what
the supervisor found, what failed, and which requests need a human.

### Restart safety

(Amended 2026-09-24, REQ-CHAT-20260924-018, ADR-000198. A worker killed by
a reboot was concluded CRASHED and counted as a failed attempt, which could
put its request on hold. A bare or reused process id was reported as a
running worker, and could be re-adopted and later signalled.)

A persisted RUNNING state records that the engine started something. The
engine SHALL NOT treat it, or a process id alone, as evidence that a worker
or supervisor session is running. At launch it SHALL record the session's
identity: host, boot id, process id, the process's kernel start time, and a
session identifier that the process carries in its environment. Before the
engine relies on such a record, it SHALL verify the record against the live
system. That includes showing status, selecting a request, re-adopting a
session and signalling one. The verdict SHALL be one of:

* running (verified);
* not running (verified: the process exited, its process id now belongs to
  another process, or the machine has restarted since);
* unverifiable.

At every start (`start`, `resume`, `stop`), before it selects a request, the
engine SHALL find every record that says a session is running and verify
it:

* A worker verified running for the run being continued SHALL be re-adopted.
* A worker verified not running SHALL have its attempt concluded. If the
  worker finished, its own report decides the result; otherwise the attempt
  is INTERRUPTED. The attempt's records, the verdict and its reason SHALL be
  kept, and the request SHALL become resumable. It is resumed first, under
  its existing claim, by a fresh worker that is told about the interrupted
  attempt. An INTERRUPTED attempt SHALL NOT count as a failed attempt or
  toward the consecutive-failure bound.
* The engine SHALL NOT start a second worker for a request whose worker is
  running or cannot be verified.
* The engine SHALL NOT signal a process it has not verified to be its own
  session.

The engine's status SHALL distinguish:

* running (verified);
* stale (recorded as running, verified not running);
* interrupted;
* queued (available for processing);
* needing a human (blocked or escalated).

The engine SHALL provide an operator recovery command. The command SHALL
show every record that says a session is running, with its verdict, the
reason and the claim's heartbeat age. It SHALL settle the records verified
not running, as above, and report exactly what it changed. With a dry-run
option it SHALL change nothing. A force option SHALL also reset records that
cannot be verified, but never a session verified running. Neither the
command nor its force option SHALL signal a process, or change the checkout,
commits, evidence, the claim or the attempt history. Every recovery SHALL be
recorded in the attempt's records, in the run's events and in a cross-run
recovery log.

## Verification

* The engine's own tests: `cd <ENGINE_HOME> && python3 -m unittest discover -s tests`.
  They run this repository's real lifecycle scripts (`claim.py`,
  `commit_request.py`, `create_request.py`) in a sandbox project with a
  stand-in agent executable.
* Live acceptance: one real request processed end to end by a real worker
  and supervisor in this checkout (VER-000317).
* Amendments of 2026-09-24 (ADR-000197): the engine tests `Selection`,
  `GitSanityBaseline`, `GitSanityEndToEnd`, `BuildTreeProvenance` and
  `ControlPanel`, plus a replay of RUN-20260924-113742's recorded attempt
  through V1's git-sanity and the amended check (VER-000320).
* Operator entry point (TICKET-ENGINE-001, 2026-09-24): the engine tests
  `ControlPanel` (launcher without a command, stale panel copy) and
  `Launcher` (copy outside the engine directory, copy without an engine),
  plus the desktop double-click path through a stand-in terminal
  (VER-000322).
* Restart safety (TICKET-ENGINE-008, 2026-09-24): the engine tests
  `RestartRecovery` (reboot mid-attempt, then resume with a fresh worker; a
  pid reused after a reboot, never signalled; a live worker never reset;
  `recover` with a dry run, applied and repeated; an unverifiable worker
  that blocks until `--force`; an orphaned launch re-adopted rather than
  duplicated; a worker that finished unobserved; a stale review; a RUNNING
  ledger entry with no attempt) and `LivenessVerification`. A black-box
  reproduction runs against engine 1.1.1 and 1.2.0 (VER-000325).
