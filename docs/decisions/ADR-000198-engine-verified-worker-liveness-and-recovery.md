# ADR-000198: The ticket engine verifies worker liveness and recovers stale work after a restart

**Status:** ACCEPTED, carried into ADR-000200 (requested in chat 2026-09-24, REQ-CHAT-20260924-018: never trust a persisted RUNNING; use a run/attempt identity as well as a pid; an explicit recover command with a safe force option; interrupted work recovered without losing history. The identity fields, the counting rule and the pause-on-unverifiable rule are this work's design, recorded here for review.)
**Date:** 2026-09-24
**Work:** WORK-000324 (REQ-CHAT-20260924-018, bug)
**Requirements:** `autonomous_ticket_engine.md` TICKET-ENGINE-008 (amended: restart safety)
**Amends:** ADR-000196 (external ticket engine: "re-adoption after an engine crash")
**Implementation:** `<ENGINE_HOME>` `61a48e6` (engine 1.2.0), outside this repository

## Context

The external ticket engine (ADR-000196) keeps its state in JSON files. When
it starts a worker, `run.json` `current`, the attempt's `attempt.json`, the
run's ticket record and the ledger all say RUNNING. A crash, a `kill -9` or
a reboot leaves those records unchanged. Engine 1.1.1 decided whether that
worker was alive as follows (audit in WORK-000324):

| Where | Check | Defect |
|---|---|---|
| `runner.recover` (every `resume`) | `pid_alive(pid, start_time)`: `kill(pid, 0)` and the kernel start time; a missing start time passes | No boot identity. A worker gone after a reboot was concluded **CRASHED and counted** as a failed attempt. With one attempt left, the request went on hold (HUMAN_REQUIRED) and was stranded until a human cleared it. The reboot also counted toward the circuit breaker and triggered a supervisor review. |
| `status` | `Active: 1 if run.json has current` | A dead worker reported as active. |
| control panel | `os.kill(pid, 0)` | A **bare pid**: any process that later took the pid was "a worker from it still running; resume re-adopts it". |
| `stop` without a loop | `pid_alive` | Refused to stop "while the worker runs" when the pid was reused. |
| `resume` with a reused pid and start time | re-adopt, wait, at the deadline `kill_tree` | **Signalled an unrelated process** (TICKET-ENGINE-005). Reproduced: `sleep 300` was killed by SIGTERM (VER-000325). |
| selection | the ledger's RUNNING treated as resumable | Nothing checked whether a worker was still attached. An engine that died after launching a worker but before recording it as `current` would have started a second worker on the same request. |

The project's own claim heartbeat does not answer the question either. The
engine refreshes it only while a worker runs, so right after a `stop --now`
or a reboot the claim still shows ACTIVE with a heartbeat minutes old.

## Decision

1. **A persisted RUNNING is a record, never proof.** At launch, each session
   (worker and supervisor) records its identity:
   - `host`;
   - `boot_id` (`/proc/sys/kernel/random/boot_id`);
   - `pid` and the kernel start time;
   - `session_id`, which the process also carries in its environment as a
     session marker variable (today `DEVSYS_WORKER_SESSION`, ADR-000200).

   `attempt.json` gets the boot id and host before the launch, so an engine
   that dies mid-launch still leaves a verifiable record.
   The session verifier (today `devsys_platform.verify_session(identity, markers)`)
   returns one of three verdicts:
   - **ALIVE**: same host, same boot, same pid and start time, and the
     marker is not contradicted. Within one boot a (pid, start time) pair
     names one process.
   - **DEAD**: another boot; the process exited or is a zombie; the pid has
     another start time, or its environment lacks the marker.
   - **UNKNOWN**: another host; the kernel identity matches but the marker
     contradicts it; or nothing can confirm it.

   A bare pid is never ALIVE. Records written by 1.1.1 have no boot id.
   For them, the recorded start is compared with the kernel boot time
   (120 s margin), and the attempt and run ids stand in for the marker.
2. **Every start settles first.** `engine/recovery.py` finds every record
   that says running: `current`, `current_review`, an `attempt.json` not
   yet ENDED, and ledger or run tickets marked RUNNING. It verifies each
   one. `runner.settle` applies the result at every loop start and at
   `stop`:
   - **DEAD attempt:** concluded through the normal conclusion (transcript,
     report, commits, claim, validation). If the worker finished while
     nobody was attached, its report decides the result. Otherwise the
     attempt is **INTERRUPTED**, with the cause `reboot` or
     `unobserved-exit`.
   - **ALIVE attempt of the run being continued:** re-adopted.
   - **UNKNOWN attempt:** left alone. A loop for that run **pauses** rather
     than start a second worker.
   - **Stale review:** cleared; its attempts stay unreviewed.
   - **RUNNING ticket with no attempt in flight:** set INTERRUPTED.

   Only a verified process is ever signalled.
3. **INTERRUPTED is a ticket status.** It covers stop-now and the three
   recovery causes. It is not counted as a failed attempt or as a
   circuit-breaker failure, and it is not a significant failure for the
   supervisor. The next iteration or run resumes the ticket first, under its
   existing claim, with a fresh session whose prompt lists the interrupted
   attempt. Selection never resumes a ticket on a RUNNING record's word.
   `queue` shows the verified verdict for each one.
4. **History is kept.** The interrupted attempt keeps its prompt, launch
   record, transcript, stderr and final `attempt.json`, which records
   `interruption` and `recovery`, plus `recovery.json`. The ledger keeps its
   `history` entry, and a new `recoveries` list covers tickets that had no
   attempt. Every action is appended to `var/recovery.jsonl`, to the run's
   `recovery.*` events and to the audit's Problems section.
5. **Operator command:** a `recover [TICKET...] [--dry-run] [--force]`
   command (today `scripts/devsys.py workers recover [ID ...] [--force]`).
   - It shows every record that says running, with its verdict, the reason
     and the project claim's heartbeat age.
   - It settles the stale ones as in 2, and sets a RUNNING run with no loop
     to PAUSED.
   - It reports what changed, and exits 1 when an UNKNOWN record remains.
   - `--force` resets UNKNOWN records as well, never one verified running.
   - `recover` never signals a process, and never touches the checkout,
     commits, evidence, the claim or the history.
   - While a loop runs, it only reports.
6. **Operator view:** `status` counts only verified workers as active, shows
   the verdict and the reason, and lists the other unverified records.
   `status --json` carries `worker` and `recorded_running`. The control
   panel uses that verdict instead of `os.kill(pid, 0)`. It offers recover,
   and a force-reset for an unverifiable worker.

## Alternatives considered

- **Treat any record older than the boot as dead, and do nothing else.** It
  fixes the reboot case, but not a pid reused within one boot, a missing
  start time, or a worker launched but never recorded.
- **Count a reboot-interrupted attempt as CRASHED, as before.** A reboot is
  not the worker's failure. Counting it consumed attempts and could strand a
  request on hold, which is the defect reported.
- **Count a worker that died while no engine was attached as CRASHED.** The
  engine cannot tell a worker crash from a kill of the whole session (for
  example a logout or an OOM kill). Not counting it cannot loop: the next
  attempt runs attached, so a real crash is then counted.
- **Reset unverifiable records automatically.** On a shared state
  directory, a second host's live worker would get a twin. The operator
  decides, with `--force`.
- **Release the project claim on recovery.** The claim protects the
  interrupted attempt's uncommitted, registered work. It is kept, and the
  next attempt continues under it (TICKET-ENGINE-003/-005).
- **Make the claim heartbeat the liveness signal.** It is refreshed only
  while a worker runs. Changing `scripts/claim.py` would be a process change
  for a question the engine can answer exactly.

## Consequences

- A reboot, a kill or a crash cannot strand the backlog. The next engine
  start, or `recover`, verifies what was running, concludes what is gone as
  INTERRUPTED with its history, and resumes the request first.
- Nothing is adopted or signalled on the word of a pid.
- Ticket status gains INTERRUPTED. After `stop --now`, the ledger says
  INTERRUPTED where 1.1.1 said PARTIAL; both are resumed the same way.
  Records written by 1.1.1 are still verified, using the boot-time
  comparison.
- An UNKNOWN record pauses its run until an operator acts. That happens
  only with a state directory shared across hosts or an unreadable `/proc`.
- `status` scans every run's attempt records to find what is recorded as
  running. The cost grows with the number of attempts kept in `var/runs/`.
  Archive old runs as `recovery.md` describes.
