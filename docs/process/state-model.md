# Machine-Operable State Model

**Normative rule:** `docs/requirements/development_operations.md` REQ-DEVOPS-015..019 (Phase 2)
**Enforcement:** `scripts/claim.py` (request transitions), `scripts/work.py` (work transitions), `scripts/evidence.py` (verification records), `scripts/completion_gate.py` (gate records), `scripts/drift_detect.py`, `scripts/preflight.py`, `scripts/health_check.py`
**Code:** the state sets and transition tables are `REQUEST_TRANSITIONS`, `WORK_TRANSITIONS` and `VERIFICATION_STATES` in `scripts/devsys_config.py`; this document and that table must agree (ADR-000199).
**Advisory mirrors:** `.agent/state/*.md` (human-readable mirrors of canonical records)

This file is the single authoritative definition of valid states and transitions.
Status strings outside this set are drift (`drift_detect` reports them).

## 1. Request State

Canonical location: `requests/inbox|processing|completed|rejected|needs-review` plus `.agent/state/requests.md`

| State | Meaning | Directory |
|---|---|---|
| `inbox` | Submitted, not yet claimed | `requests/inbox/` |
| `processing` | Claimed by one agent | `requests/processing/` |
| `completed` | Delivered with evidence | `requests/completed/` |
| `rejected` | Closed without delivery, reason recorded | `requests/rejected/` |
| `needs-review` | Requires human decision | `requests/needs-review/` |

Valid transitions:

```
inbox -> processing          claim.py --claim-request (atomic; devsys.py next --claim)
processing -> completed      claim.py --complete-request   (a PASS gate record of every linked work item
                                                            on the current code, or --waive-gate <reason>)
processing -> rejected       claim.py --reject-request --reason <text>
processing -> needs-review   claim.py --needs-review-request --reason <text>  (the claim is parked)
processing -> inbox          claim.py --release-request --requeue  (nothing owned uncommitted, or --abandon <reason>)
needs-review -> processing   claim.py --claim-request   (the owner resumes; another agent takes a parked claim over)
needs-review -> rejected
needs-review -> completed    (only if a human explicitly resolves it as completed; same gate)
```

Every transition writes a `resolution` (state, reason, waiver, agent, time) into the request's JSON sidecar and an entry into the claim history; a request in `completed/` without a resolution was moved by hand and is reported as UNVERIFIED_COMPLETION by `drift_detect.py`.

Forbidden: `completed -> processing`, `rejected -> processing` without explicit reopening (new request file referencing predecessor).

## 2. Work State

Canonical location: `work/queued|active|blocked|needs-review|verification|completed|cancelled` plus `work/work.txt` column 2

| State | Meaning | Directory |
|---|---|---|
| `queued` | Eligible, not yet claimed | `work/queued/` |
| `active` | Claimed, implementation in progress | `work/active/` |
| `blocked` | Waiting on dependency | `work/blocked/` |
| `needs-review` | Requires human decision | `work/needs-review/` |
| `verification` | Implementation done, verification evidence being collected | `work/verification/` |
| `completed` | Passed completion gate, evidence recorded | `work/completed/` |
| `cancelled` | Intentionally abandoned | `work/cancelled/` |

Valid transitions:

```
queued -> active | cancelled
active -> blocked | needs-review | verification | cancelled
active -> completed           (the gate record is still required)
blocked -> active | needs-review | cancelled
verification -> completed     (a PASS gate record on the request's current code)
verification -> active        (verification failed, rework)
verification -> blocked
needs-review -> active        (human resolved)
needs-review -> blocked | cancelled
```

All work transitions go through `python3 scripts/work.py transition WORK-NNNNNN --to <state> [--reason <text>]`
(a reason is required for `blocked`, `needs-review` and `cancelled`). The tool moves the record, rewrites its
`**Status:**` header, updates `work/work.txt`, appends a line to the record's `## Transitions` section and to
`.agent/state/runtime/transitions.jsonl`; repeating a transition that was interrupted finishes it.

Forbidden without reopening mechanism: `completed -> active`, `cancelled -> active`. Reopening requires new WORK referencing predecessor and human approval.

`work/work.txt` allowed status tokens are exactly the lowercase names above (case-insensitive check normalizes). Legacy values such as `ACTIVE/REVIEW`, `HISTORICAL`, `PENDING` are drift — migrate them.

## 3. Verification State

Canonical location: `evidence/evidence.txt` column 2 + `evidence/VER-NNNNNN-*.md` header `Status:`

| State | Meaning |
|---|---|
| `pending` | Evidence expected but not yet recorded |
| `pass` | Verification executed, all required checks passed |
| `fail` | Verification executed, regression or requirement violation |
| `blocked` | Verification blocked by environment (classified) |
| `pre-existing` | Failure matches documented pre-existing/environmental baseline (classified) |

Valid transitions: `pending -> pass|fail|blocked|pre-existing`. Once `pass/fail`, new VER record required for re-verification; do not mutate classification silently. Records are created with `python3 scripts/evidence.py create --work WORK-NNNNNN --status <state> ...`, which also copies each linked test-run record into `evidence/VER-NNNNNN/`. Only the statuses in `completion.passing_evidence_statuses` (default `pass`, `pre-existing`) satisfy the completion gate.

## 3a. Workers do not add states

The Worker Orchestrator (`docs/process/worker-orchestrator.md`, ADR-000200)
executes requests with the states above. It has no request or ticket state
of its own:

- **Views:** queued = `inbox` (unclaimed), working = `processing`,
  finished = `completed` / `rejected` / `needs-review`.
- **Escalation:** a worker that cannot finish a request escalates it with
  `processing -> needs-review` and the reason.
- **Worker records:** the worker states (`stopped`, `idle`, `working`,
  `stopping`) describe worker processes, never requests.

## 4. Transition Enforcement

- `scripts/drift_detect.py` reports any status token outside allowed sets, completions without a recorded resolution or PASS gate record, and a claim whose recorded state differs from the request's directory.
- `scripts/completion_gate.py --record` writes `evidence/gates/<WORK-ID>.json`; `work.py` (`-> completed`), `claim.py --complete-request` and `commit_request.py` (for a completed request) refuse without a PASS record on the request's current fingerprint (exit 9, GATE_REQUIRED). `completion.enforce_on_transition: false` in `devsys.json` turns this off for an instance that has not adopted the gate.
- `scripts/preflight.py` fails when the request is claimed by another agent or is processing without a claim.
- Request transitions are performed only by `scripts/claim.py` inside an inter-process critical section, owner-checked and no-clobber: a request is in exactly one state directory, and of concurrent attempts to claim/complete/reject the same request exactly one succeeds (ADR-000090).
- State files `.agent/state/requests.md` and `.agent/state/work.md` are mirrors; disagreement between mirror and canonical directories/locations is reported as drift (canonical location wins).

## 5. Reopening

If completed/cancelled work must be revisited, create a new WORK item (`WORK-NNNNNN`) with a `Supersedes:` link to predecessor rather than moving the old file back to `active`. This preserves history and prevents silent rewriting of completion records.

## 6. Machine Tags

Agent tooling may append structured tags to request/work headers:

```
Status: PROCESSING
Claimed-By: agent-5501
Claim-Time: 2026-09-08T04:12:03Z
PID: 12345
```

Tags are advisory; canonical location is the source of truth. Ownership itself (agent, heartbeat, owned paths, commits, recovery history) is recorded in the claim, `.agent/state/claims/request-<file>.lock`, never in the tracked request file (ADR-000090).
