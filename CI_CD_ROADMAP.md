# CI_CD_ROADMAP — Audit and Improvement Roadmap

**Scope:** the extracted development-control system in this directory, as
described in `CI_CD.md`.
**Nature:** audit and plan only. No implementation file was changed by this
work (see §13). `CI_CD.md` was corrected in two places where the audit found
it factually wrong (§13.4).
**Method:** each claim was checked against the code and, where possible, by
experiment on disposable scratch copies (`git init` + one commit). Experiment
ids `X01…X28` are listed with commands and observed results in §13.3.
Findings cite them.

**Implementation status (2026-09-24):** the audit was followed by the
combined hardening, generalization and automation implementation
(ADR-000199). §15 gives the status of every finding; the observations below
describe the system *before* that work and are kept as the audit record.

Terminology: **observed** = what the current code does (measured or read);
**desired** = the proposed target behaviour. Desired behaviour is a proposal,
not a requirement, until it goes through the system's own process (see
§11, R1).

---

## 1. Executive summary

The extracted system is a well-engineered **mechanics layer**:
- an atomic id allocator;
- no-clobber state moves under `flock`;
- per-path ownership with baseline blobs and three-way-merged, compare-and-swap commits;
- deterministic intake classification;
- a tiered, cached, isolated test orchestrator;
- a read-only audit.

Its 216 tests run green (212 pass, 4 skip). Those mechanics are worth preserving as they are.

It is weaker as a **control system**. The lifecycle rules in `AGENTS.md` are
mostly instructions to agents, not enforced transitions, and three
verification paths can report success when nothing was verified:

* **A request can be completed and released with no work record, no gate, no
  evidence and no commit** (X20). The completion gate is advisory; no
  transition consults it.
* **The completion gate passes a work item whose only evidence says
  `Status: FAIL`** and whose manifest row says `fail`. Its "implementation"
  check is satisfied by any uncommitted change in the tree, including
  another agent's (X05).
* **A test run that executed nothing is classified PASS.** Linked as
  `**Test-Run:**`, the gate reports that it "exercised the request's current
  code" (X21). This contradicts the system's own hard invariant: *never claim
  a test passed unless it ran*.

Recovery also has holes:
- an escalated request stays claimed and cannot be re-claimed (X19);
- a crash in the middle of a claim leaves a request stuck and invisible to recovery (X23);
- releasing a claim silently orphans uncommitted work (X20);
- concurrent follow-ups lose updates and can land in the wrong request (X13);
- `health_check` fails spuriously under load (X03, X27);
- worktree mode is blind in several checks and splits a request across two branches (X11, X12).

**Numbers:**
- **79 findings** in total.
- **24 existing** limitations were re-verified: 21 confirmed, 2 partially
  confirmed, 1 no longer applicable. They map to 24 roadmap findings.
- **55 findings are new.**
- By priority: **Critical 3**, **High 14**, Medium 32, Low 21, Future 9.

**Recommended evolution:**

| Phase | Name | Goal |
|---|---|---|
| 0 | Baseline | Today's system, frozen as the reference. |
| 1 | Correctness | Enforced gates, no false PASS, crash-consistent transitions, no silent orphaning. |
| 2 | Generalization | Central configuration, pluggable build/test adapters, no residual project assumptions. |
| 3 | Automation | Transitions, heartbeat, liveness, recovery, traceability and machine-readable interfaces. |
| 4 | Build/CI integration | A defined contract for an external CI system. |
| 5 | Advanced operations | Distributed and multi-machine operation, analytics, integrations. |

The core stays small: records, state machine, ownership, gate, evidence
schema and audit. Build systems, CI providers, ticket trackers and schedulers
belong in an integration layer.

---

## 2. Current baseline (Phase 0)

### 2.1 What exists

See `CI_CD.md` for the full map. In short:
- **Records:** requests, requirements, ADRs, work and evidence, as Markdown
  plus pipe manifests plus JSON sidecars.
- **`scripts/agent_process.py`**, behind `claim.py`, `commit_request.py` and
  `allocate_id.py`: identity, claims, ownership and owned commits.
- **Intake:** `create_request.py` + `request_classification.py`.
- **Checks:** `preflight.py`, `completion_gate.py`, `drift_detect.py`,
  `doc_integrity.py`, `invariant_check.py`, `verify_development_system.sh`,
  `health_check.py`.
- **Tests:** `run_tests.py` + `test_selection.py` + registry.
- **Audit:** `tools/audit`.
- **State:** `.agent/state/` (untracked claims, counters, locks).

Dependency trace, from the code rather than the documentation:

```text
claim.py ─┐
commit_request.py ─┼─► agent_process.py ─► git (rev-parse, hash-object, merge-file, commit-tree, update-ref)
allocate_id.py ─┤                         └► fcntl.flock (.agent/state/runtime/locks)
create_request.py ─┘──► request_classification.py
process_next_request.py ─► claim.py, preflight.py, health_check.py (subprocess)
preflight.py ─► request_classification.py
completion_gate.py ─► process_protection.py ─► agent_process.py
                   └► run_tests.py ─► test_selection.py (fingerprint, run records)
invariant_check.py ─► process_protection.py
health_check.py ─► verify_development_system.sh (─► unittest scripts/tests), drift_detect, doc_integrity, invariant_check
run_tests.py ─► test_manifest.json, test_quarantine.json, agent_process (claims for --request), make, readelf
tools/audit ─► audit_system/{audit,checks,report}.py ─► .audit/manifest.json, scripts/* (as commands), test_selection
```

Tools with **no direct test**: `health_check.py`, `build_health.py`,
`status_report.py`, `requirement_impact.py`, `crash_evidence.py`, and the
processing (non-dry-run) path of `process_next_request.py`. Several defects
below live exactly there.

### 2.2 Baseline validation (recorded before any analysis; §13.1)

| Validation | Result |
|---|---|
| `python3 -m unittest discover -s scripts/tests` | 195 tests: 191 ok, **4 skipped** (source-project real-record fixtures), 40.7 s |
| `python3 -m unittest discover -s tools/audit_system/tests` | 21 ok |
| `drift_detect` / `doc_integrity` / `invariant_check --strict` | PASS / PASS / PASS |
| `verify_development_system.sh` · `health_check.py` | pass (40.9 s) · PASS (41.3 s) |
| `build_health.py` | WARN (no build tree; expected) |
| `process_next_request.py --dry-run` (empty inbox) | exit 0 after **30.1 s**, printing "timed out after 30 seconds" |
| `create_request.py --review-all` | 2 bootstrap records INVALID-VOCABULARY |
| `run_tests.py --tier t2 --all` | PASS, 40.8 s |
| `run_tests.py --tier perf / stress / soak / live` | **PASS with 0 jobs**, exit 0 |
| `run_tests.py --tier package` | BLOCKED (placeholder requirement), exit 3 |
| `./tools/audit quick / standard / baseline` | 7/0/2/6 · 10/0/2/3 · **11/0/2/2** (PASS/DRIFT/UNKNOWN/SKIPPED); the baseline profile executed t1 |

### 2.3 What is correct and should be preserved

* **Id allocation:** `max(persistent counter, full-store scan) + 1` under an
  inter-process lock. It never reuses ids within a clone, and a collision is
  refused rather than renumbered.
* **Intake:** O_EXCL creation and submit-only semantics (tested). Request
  metadata is system-generated (local date, allocator).
* **Classification:** a closed type vocabulary, a deterministic validator
  with recorded evidence, and a reviewable `--reclassify` history.
* **State moves:** no-clobber moves inside a critical section, so of
  concurrent claimers exactly one wins (tested).
* **Ownership model:** exclusive, shared and concurrent modes, baseline blobs,
  snapshot/rebaseline, three-way-merge attribution, overlap refusal, a
  private index and CAS on HEAD, and file-mode fidelity. This is the most
  valuable part of the system and is extensively tested (47 tests).
* **Protection and collisions:** protected-artifact enforcement from claims,
  not from dirty files. Accepted id collisions are declared against their
  records and are not suppressible by id.
* **Test orchestrator:**
  * affected selection, where an unmapped path selects everything;
  * build-id cache, staleness detection, per-job isolation (XDG, TMP);
  * crash/timeout attribution with shard continuation;
  * FLAKY re-run alone, where a crash is never FLAKY;
  * quarantine with owner and expiry;
  * request fingerprints.
* **Audit:** read-only and manifest-driven. UNKNOWN is never PASS. It has
  per-check evidence, comparison, retention and a self-check.

---

## 3. Verified existing findings

Each limitation listed in `CI_CD.md` §11 and in the audit brief was checked
independently.

| # | Existing limitation | Verdict | Class | Evidence | Maps to |
|---|---|---|---|---|---|
| V01 | Manual movement of work items between state directories | **Confirmed**. `--claim-work` also accepts a nonexistent WORK id and nothing else acts on the lock | Missing capability | X01 | PRO-01 |
| V02 | Claims/enforcement discrepancies in docs | **Confirmed**. Preflight has no claim logic (0 references); mirrors are not read | Documentation-only issue | X03, X04 | PRO-04 |
| V03 | `.agent/state/*.md` mirrors not read | **Confirmed**. Nonsense values change nothing; no script reads them (2 only mention the path) | Design limitation | X03 | PRO-05 |
| V04 | Stale claims, no PID/dead-process validation | **Confirmed**. The lock PID is dead at once; a claim with PID 1 (alive) and a 3 h heartbeat is STALE | Design limitation (+ doc issue) | X02 | CON-02, PRO-04 |
| V05 | Completion gate relies on keyword checks | **Confirmed and worse**. It PASSes failing evidence and needs no executed test | Implementation defect | X05, X06 | COR-02, COR-15 |
| V06 | `doc_integrity.py` warning-only | **Confirmed**. A broken link gives WARN and exit 0; the code never emits FAIL | Design limitation | X07 | PRO-06 |
| V07 | `process_next_request.py` timeout | **Confirmed**. 30.1 s, "timed out after 30 seconds", exit 0 | Implementation defect | baseline, X08 | COR-13 |
| V08 | `crash_evidence.py` missing `import sys` | **Confirmed**. `NameError` traceback, exit 1 | Implementation defect | X09 | COR-11 |
| V09 | Local claim/id counters | **Confirmed**. Two clones both allocate `WORK-000002` | Design limitation | X10 | CON-03 |
| V10 | Manual heartbeat | **Confirmed**. Only `_touch()` in owner operations; lane budgets (soak/live 7200 s) exceed the 2 h threshold | Design limitation | X16, X25 | CON-01 |
| V11 | Worktree/state inspection blind spots | **Confirmed and broader**. Drift is blind in a worktree; `verify_development_system` and `health_check` **FAIL** in any worktree | Implementation defect | X11 | COR-14 |
| V12 | Concurrency safety of `--follow-up` | **Confirmed and broader**. Lost updates, `.md`/`.json` divergence, mis-targeting, writes to closed requests | Implementation defect | X13 | COR-08 |
| V13 | Recursive markdown scan cost/noise | **Partially confirmed**. Noise from ignored trees confirmed; cost modest (0.6 s for 20,000 extra files) | Implementation defect (minor) | X14 | PRO-10 |
| V14 | Structural check / bootstrap assumptions | **Confirmed**. A fresh clone fails until any `claim.py` call creates `.agent/state/claims` | Design limitation | X15 | PRO-11 |
| V15 | Old bootstrap request vocabulary | **Confirmed**. 2 records INVALID-VOCABULARY | Documentation/data issue | baseline | PRO-08 |
| V16 | Duplicated constants | **Confirmed**. State sets in 3 scripts plus `state-model.md`; stale threshold in 2 | Design limitation | X16 | GEN-01 |
| V17 | qmake-specific test selection | **Confirmed**. CMake/C paths are "unmapped" and select everything; builds use `make` | Design limitation | X17 | GEN-02, GEN-03 |
| V18 | English-only classifier heuristics | **Confirmed**. French/German/Spanish crash and performance reports → INVESTIGATION/low | Design limitation | X18 | GEN-09 |
| V19 | POSIX / `fcntl` dependency | **Confirmed by inspection** (`fcntl.flock`, also `os.link` for moves). Not executed on a non-POSIX host | Design limitation | X25 | POR-01 |
| V20 | External ticket-engine dependency | **Confirmed**. No code dependency; 11 documents describe the absent engine | Documentation-only issue | X16 | EXT-01 |
| V21 | Historical references to absent records | **Confirmed**. 104 distinct WORK/VER/ADR ids cited, 88 unresolved | Documentation-only issue | X16 | AUD-05 |
| V22 | No validation of binary/live/perf/stress/soak/package lanes | **Confirmed and worse**. No suites registered, empty lanes report PASS, `package` is BLOCKED | Missing capability + implementation defect | baseline, X21 | BLD-04, COR-03 |
| V23 | Baseline audit profile not validated | **No longer applicable**. Validated here: 11 PASS / 0 DRIFT / 2 UNKNOWN / 2 SKIPPED; `test.unit-tier` executed t1 | — | X28 | — |
| V24 | Limited validation of worktrees / non-Linux | **Partially confirmed**. Worktree mode now exercised end to end, with defects found (COR-14, COR-21); non-Linux still unvalidated | Missing capability | X11, X12, X26 | POR-02, COR-14 |

---

## 4. Newly discovered findings (overview)

These were not listed in `CI_CD.md` §11. Full records are in §5.

* **Lifecycle enforcement:** COR-01 (completion needs nothing), COR-02
  (extended: FAIL evidence passes), COR-03 (zero-job PASS), COR-09 (commit
  not bound to the gated state), COR-16 (drift cannot see ungated
  completion), AUD-01 (gate result never recorded).
- **Recovery / crash consistency:**
  - COR-04: release orphans owned work.
  - COR-05: an escalated request stays claimed.
  - COR-06: a crash between the lock and the move.
  - COR-07: non-atomic paired writes.
  - COR-21: old git silently splits worktree state.
  - CON-05: no repair tool.
  - CON-04: no automatic handling of stale claims.
* **Reliability:** COR-12 (`health_check` 60 s timeout vs a ~41–45 s check → spurious FAIL under load), COR-17, COR-18.
* **Identity:** COR-10 (the agent id is a bare string; `claim_token` unused).
* **Process:** PRO-02 (traceability links manual), PRO-03 (state-model transitions unsupported; the documented human resolution creates orphans), PRO-07 (duplicate requests), PRO-09.
* **Residual project assumptions missed by the extraction:** GEN-04 (`retry_isolated` special-cases suite id `"app"`), GEN-10, GEN-11.
* **Generalization:** GEN-03, GEN-05, GEN-06, GEN-07 (unconfigured placeholders read as PASS), GEN-08 (root override honoured by 9 of 18 scripts), GEN-12, POR-03.
* **Build/CI:** BLD-01 (no CI entry point), BLD-02 (run records untracked), BLD-03, BLD-05, BLD-06, BLD-07, BLD-08 (failure taxonomy documented, not implemented).
* **Audit/evidence:** AUD-02, AUD-03, AUD-04 (audit maps a tool's WARN to PASS), AUD-06 (31 of 64 process requirement ids without identifiable verification), AUD-07 (untested tools).
* **Automation / external / future:** AUTO-01..03, EXT-02, EXT-03, FUT-01..05.

---

## 5. Findings by category

Fields: **Evidence** · **Current (observed)** · **Desired (proposal)** · **Risk
if unchanged** · **Depends on** · **Phase** · **Priority** · **Mandatory /
Optional**. *Origin* is `existing (Vnn)` or `new`.

### 5.1 Correctness / Integrity

**COR-01 — Lifecycle completion is not enforced by the tools** · new
- Evidence: X20. The sequence was: claim; own a file and edit it; then
  `--complete-request` (exit 0) and `--release-request` (exit 0). The request
  is in `completed/`, the edit is uncommitted and "unowned", and
  `drift_detect` reports PASS. `transition_request()` and `release_request()`
  contain no gate, work, evidence or commit check.
- Current: completion is a file move any owner can perform at any time; the gate is advisory.
- Desired: `--complete-request` requires a linked work record whose latest
  persisted gate result (AUD-01) is PASS for the current request
  fingerprint. `--reject-request` requires a recorded reason. Release after
  completion requires the owned changes to be committed or explicitly
  abandoned.
- Risk: requests marked delivered without proof; traceability silently broken; the core promise of the system not kept.
- Depends on: COR-02, COR-03, AUD-01. · Phase 1 · **Critical** · Mandatory

**COR-02 — The completion gate passes failing or absent verification** · existing (V05), extended
- Evidence: X05. A work record containing only the words "test document
  regression trace" and an evidence file saying `**Status:** FAIL`, "12 tests
  failed", "none was run", with an `evidence.txt` row `fail` → `GATE: PASS`,
  exit 0. "Implementation: git diff shows changes" was satisfied by an
  unrelated staged file. There is no request link, so the protected-artifact
  check is skipped as "no claim".
- Current: "has classification" is true if the text contains PASS, FAIL or
  BLOCKED. Missing test runs are WARN. The diff check is whole-tree. The
  requirement, test, documentation, regression and traceability checks are
  keyword presence.
- Desired: the evidence status (header and manifest row) must be a passing
  class. A request-scoped, non-empty test run is required for code changes;
  docs-only work needs an explicit waiver. The implementation check uses the
  request's owned paths. The work record must link its request.
- Risk: false completion that looks rigorous; the gate is currently the only control.
- Depends on: GEN-01 (evidence schema), AUD-02. · Phase 1 · **Critical** · Mandatory

**COR-03 — A test run that executed nothing is PASS, and the gate accepts it** · new
- Evidence: baseline and X21. `--tier perf|stress|soak|live` → "classification:
  PASS", 0 groups, 0 jobs, exit 0. `--tier t1 --suite does-not-exist
  --request …` → "nothing to run", PASS. Linked as `**Test-Run:**`, the gate
  says "PASS (tier live…)" and "exercised the request's current code
  (fingerprint match)". The gate does not check the tier.
- Current: `classify()` returns PASS when no job failed or was blocked, including when there were no jobs.
- Desired: zero executed jobs is its own classification (e.g. EMPTY/NOT_RUN),
  with a non-zero exit. The gate requires a run of at least the policy tier
  (t2 per `AGENTS.md`) containing at least one executed or cached job.
- Risk: violates "NEVER CLAIM A TEST PASSED UNLESS IT RAN"; fabricated-looking proof.
- Depends on: — · Phase 1 · **Critical** · Mandatory

**COR-04 — Releasing a claim silently orphans owned uncommitted work** · new
- Evidence: X20 (release after complete) and X20b (`--release-request
  --requeue` with an edited owned file): both exit 0, and `commit_request
  --report` then shows the file as `unowned`.
- Current: release archives the claim and deletes the lock without looking at the owned paths.
- Desired: release refuses while owned paths differ from their baseline or
  from HEAD, unless given `--abandon` (recorded in history) or the work is
  committed. The history keeps the list of abandoned paths.
- Risk: work lost or attributed to nobody; a later agent may commit it under the wrong request.
- Depends on: — · Phase 1 · **High** · Mandatory

**COR-05 — Escalation keeps the claim; the needs-review request is blocked** · new
- Evidence: X19. `process_next_request` escalates on preflight FAIL, exit 2
  and the request is in `needs-review`, but the claim remains. Another agent
  gets "ALREADY CLAIMED". The owner's `--claim-request` prints "ALREADY OWNED
  (heartbeat refreshed)" and the request **stays** in `needs-review`.
- Current: transitions never release. `claim_request()` short-circuits for an existing owner without moving the request.
- Desired: moving to `needs-review` releases (or parks) the claim with the
  escalation recorded. A claim of a `needs-review` request moves it to
  `processing` whoever holds it (owner or new claimant).
- Risk: escalated work cannot be resumed by the documented path; humans resort to manual moves (PRO-03).
- Depends on: PRO-03 · Phase 1 · **High** · Mandatory

**COR-06 — A crash inside a claim leaves a stuck, invisible request** · new
- Evidence: X23 simulated a kill after `create_exclusive_text(lock)` and before
  `move_request()`. The request stays in `inbox` with an active claim.
  `--recover` reports 0 stale and 0 orphans, and drift reports PASS.
  `process_next_request --dry-run` keeps offering the request, another agent
  gets CONFLICT, and the same agent's retry says "ALREADY OWNED" and does not
  move it.
- Current: a two-step operation with rollback only on a Python exception, not on process death.
- Desired: the claim holds its intended state and is idempotently
  re-applied. A retry by the owner completes the move. A check reports a
  claim whose state disagrees with the request's directory.
- Risk: a request silently unprocessable until the heartbeat ages 2 h and someone recovers by hand.
- Depends on: CON-05 · Phase 1 · **High** · Mandatory

**COR-07 — Paired writes are not atomic as a unit** · new
- Evidence (code): `move_request()` links and unlinks the `.md`, then the
  `.json`. `_set_request_status()` writes the `.md`, then the `.json`.
  `transition_request()` moves before saving the claim. The source project's
  own state mirror recorded exactly this failure ("their JSON sidecars had
  stayed in inbox with stale status lines", from the extraction-time
  `.agent/state/requests.md`).
- Current: a crash between steps leaves the `.md` and `.json` in different directories or states.
- Desired: a single transition journal (intent → apply → commit) with an idempotent replay, or the sidecar treated as derived and regenerated.
- Risk: split-brain request state; drift does not detect a sidecar left behind.
- Depends on: CON-05 · Phase 1 · Medium · Mandatory

**COR-08 — `create_request.py --follow-up` loses updates and can hit the wrong record** · existing (V12), extended
- Evidence: X13.
  - 20 concurrent follow-ups → 17 present in the `.md` and 19 in the `.json`:
    lost updates, and the two files disagree.
  - A follow-up for request A was appended to request B, because B's text
    mentions A's id and `processing` is preferred.
  - A follow-up to a completed request was accepted.
- Current: substring matching on file name or content, plain read-modify-write with no lock, closed states included.
- Desired: resolve by the exact request id (sidecar or header), under a lock, with atomic writes. Refuse closed states and point to "file a new request referencing …".
- Risk: user input silently lost or attached to the wrong work item.
- Depends on: — · Phase 1 · **High** · Mandatory

**COR-09 — A commit is not bound to the verified state** · new
- Evidence (code): `commit_request.py` and `agent_process.commit_request()`
  have no reference to the gate, `run_tests` or the fingerprint (0 matches).
  The commit trailer carries only `Request-Id`, `Request-File` and
  `Agent-Id` (X25).
- Current: files can change between a passing gate and the commit, and nothing detects it.
- Desired: at commit time, recompute the request fingerprint and compare it
  with the latest persisted gate record; record the gate and run ids in
  trailers (AUD-03).
- Risk: committed code differs from tested code.
- Depends on: AUD-01, COR-03 · Phase 1 · Medium · Mandatory

**COR-10 — Identity is a bare string; `claim_token` is never checked** · new
- Evidence: X22. `--agent-id bob` is accepted, and a second session using
  `bob` is told "ALREADY OWNED (heartbeat refreshed)" and can register paths.
  `claim_token` is written but never read for authorization (X25 grep).
- Current: ownership = knowledge of the agent-id string.
- Desired: operations prove the session (e.g. a token or a session marker,
  in the spirit of ADR-000198's verified launch identity). Re-claims by the
  "same" id from a different session are detected.
- Risk: two sessions silently co-own and co-commit; recovery history misattributes work.
- Depends on: CON-02 · Phase 3 · Medium · Mandatory (before automatic recovery)

**COR-11 — `crash_evidence.py` crashes on an unknown request id** · existing (V08)
- Evidence: X09, `NameError: name 'sys' is not defined`, exit 1. The attach path also writes the request file non-atomically without an ownership check.
- Desired: import fixed; attach goes through the same locked, owned write path as other request edits.
- Risk: the crash workflow fails at its first step. · Depends on: — · Phase 1 · Medium · Mandatory

**COR-12 — `health_check` fails spuriously under load** · new
- Evidence: X03 and X27. `health_check` runs `verify_development_system.sh`
  with a hard 60 s timeout, and that script runs the whole unit suite. It
  took 40.9 s at baseline and 41.7–44.5 s when re-measured under a load
  average of 6–10 (other sessions on the host). One health run in this audit
  returned **HEALTH: FAIL** with "timed out after 60 seconds", with nothing
  wrong in the repository.
- Current: the go/no-go signal for autonomous work depends on host load and on suite growth.
- Desired: health runs only structural checks (unit tests belong to the
  orchestrator's `scripts` suite, as `DEVSYS_SKIP_UNITTESTS` already allows).
  A timeout is reported as UNKNOWN/ENVIRONMENTAL, not FAIL.
- Risk: automation stops or learns to ignore FAIL. · Depends on: — · Phase 1 · **High** · Mandatory

**COR-13 — `process_next_request.py` times out calling `health_check`** · existing (V07)
- Evidence: baseline and X08. 30.1 s, then the timeout message, then exit 0
  (the failure is swallowed). It is the same root cause as COR-12 with a
  30 s budget.
- Desired: resolved by COR-12; the empty-inbox path reports health without blocking. · Phase 1 · Medium · Mandatory

**COR-14 — Worktree mode is partly blind and splits a request across branches** · existing (V11), extended
- Evidence: X11 and X12.
  - From a linked worktree, `drift_detect` reports PASS while the claim is
    3 h stale (the main checkout reports STALE_CLAIM).
    `verify_development_system` fails with "MISSING DIRECTORY
    .agent/state/claims", and `health_check` fails.
  - The code commit lands on `request/<stem>`, while the request's state
    move happens in the main checkout and must be committed on `main`. The
    worktree still shows the request in `inbox`.
  - No tool integrates the branch.
- Current: `claim.py` resolves shared state through the main root; drift, health and structure use their own root.
- Desired: every state-reading tool uses `agent_process.state_dir()`. The
  request state and the code of one request land on one branch. Integrating
  a request branch is a defined, tool-supported step (policy: rebase or
  merge, and who may publish).
- Risk: the documented "strongest isolation" mode produces false alarms, missed alarms and split history.
- Depends on: GEN-08, COR-21 · Phase 1 (checks) / Phase 3 (integration tool) · **High** · Mandatory

**COR-15 — The gate's "claims COMPLETED" check never fires for the standard header** · existing (CI_CD.md §11.4)
- Evidence: X06. With `**Status:** completed` the message is absent; with a plain `Status: completed` it appears.
- Desired: parse the same header form drift uses. · Phase 1 · Low · Mandatory

**COR-16 — Drift cannot see ungated completion** · new
- Evidence: X05 (a work item in `completed/` whose evidence says FAIL, drift PASS) and X20 (a request in `completed/` with no work or evidence, drift PASS).
- Current: drift checks that completed work has some evidence file or row, not that its evidence passes, nor that completed requests have completed work.
- Desired: a completed request has a completed work record with a passing
  gate record. A completed work item's evidence status is passing. Either
  violation is a FAIL.
- Risk: after-the-fact detection of COR-01/02 is also absent. · Depends on: AUD-01 · Phase 1 · **High** · Mandatory

**COR-17 — A lock timeout is reported as "conflict"** · new
- Evidence (code): `exclusive()` raises `ProcessError(CONFLICT, "timed out …")`, the same exit code (1) as "owned by another active agent".
- Desired: a distinct exit code, e.g. BUSY. · Phase 1 · Low · Mandatory

**COR-18 — The shared-index refresh after a commit can silently give up** · new
- Evidence (code): 50 retries of `git reset -q <commit> -- <path>` with no report when all fail (X25).
- Desired: report as `index_left_untouched`. · Phase 1 · Low · Optional

**COR-19 — Work locks are described as "request ?"** · new
- Evidence: X01, `claim.py --status` prints "work request ? (WORK-999999) … host ?". This is cosmetic.
- Phase 2 · Low · Optional

**COR-20 — The request fingerprint ignores Markdown inputs** · new
- Evidence (code): `request_fingerprint()` skips `is_no_test_path()` paths
  (`*.md`, `docs/*`), but the `scripts` suite declares `AGENTS.md` and
  `docs/process/**` as inputs.
- Current: a change to such a document after the test run does not fail the gate.
- Desired: exclude only paths that no registered suite declares as input. · Phase 2 · Low · Optional

**COR-21 — Old git silently splits shared state** · new
- Evidence: X26. With `--path-format=absolute` unavailable (git < 2.31,
  simulated), `main_root()` falls back to the worktree root, and
  `state_dir()` from a linked worktree becomes the worktree's own
  `.agent/state`. Claims, counters and locks are no longer shared, with no
  error. The host has git 2.43, so this was simulated, not observed.
- Desired: check the git version at startup and fail closed; resolve the common dir without `--path-format` when possible.
- Risk: concurrency protection silently disabled. · Depends on: POR-03 · Phase 1 · Medium · Mandatory

### 5.2 Core development process

**PRO-01 — Work-item transitions are manual** · existing (V01)
- Evidence: X01. `--claim-work WORK-999999` succeeds for a nonexistent item
  and moves nothing. No script reads work locks except `claim.py --status`.
- Current: agents move `work/<state>/` files, edit `work.txt` and the header by hand, and drift detects mismatches afterwards.
- Desired: a transition command for work (queued→active→verification→completed…) that validates the state model, updates directory, header and manifest in one step, and consults the gate for →completed.
- Risk: state drift; human error; no enforcement of the state model.
- Depends on: GEN-01, COR-01 · Phase 3 · **High** · Mandatory

**PRO-02 — Request↔work↔evidence links are manual** · new
- Evidence (code): the request's `## Traceability` section is written once
  as "pending" by `create_request.py` and never updated by any tool (X25).
  Drift only WARNs `ORPHANED_REQUEST` for processing requests without a
  `WORK-` mention.
- Desired: linking a work item to a request records the link in both records (or a single link index); drift FAILs a completed request without links.
- Phase 3 · Medium · Mandatory

**PRO-03 — State-model transitions are unsupported and the documented human path creates orphans** · new
- Evidence: X19.
  - `--reject-request` on a `needs-review` request fails ("expected exactly
    'processing'"), although the state model allows needs-review → rejected
    and needs-review → completed.
  - `escalation.md` tells a human to move the file back to `processing`. Doing
    so makes an orphan that `--claim-request` refuses ("processing with no
    claim (orphaned) … use --recover-request").
- Desired: tool-supported human resolution (needs-review → processing, rejected or completed) with the reason recorded; documentation updated to the tool.
- Phase 1 · Medium · Mandatory

**PRO-04 — Documents claim enforcement that does not exist** · existing (V02, V04)
- Evidence:
  - `state-model.md` says preflight "refuses to advance a request already in
    processing under active claim", but `preflight.py` has 0 claim references
    (X04).
  - It says mirror/canonical disagreement "is reported as drift", but no tool
    reads the mirrors (X03).
  - `engineering-rules.md` says stale means "2 hours or dead PID", but the PID
    is never checked (X02).
- Desired: documents describe the implemented behaviour (or the behaviour is implemented). These are protected artifacts, so the change is a PROCESS-CHANGE.
- Phase 1 · Medium · Mandatory

**PRO-05 — `.agent/state/*.md` mirrors are hand-maintained and unread** · existing (V03)
- Evidence: X03, nonsense values → drift PASS. `verify_development_system.sh` only requires the files to exist.
- Desired: generate them from canonical state (`status_report --write`), or remove them from the model.
- Phase 3 · Low · Optional

**PRO-06 — `doc_integrity.py` can never fail** · existing (V06)
- Evidence: X07 (broken link → WARN, exit 0). The code has a FAIL filter but no FAIL producer.
- Desired: a policy decision on which classes FAIL (e.g. broken links in current, non-historical docs) and which stay WARN.
- Phase 1 · Medium · Mandatory (the decision) / Optional (strictness level)

**PRO-07 — No duplicate-request detection** · new
- Evidence: X24. The same text submitted twice creates `…-001` and `…-002`, with no warning at intake or in preflight.
- Desired: a similarity/duplicate warning at intake and preflight (never an automatic merge). · Phase 3 · Low · Optional

**PRO-08 — Bootstrap records predate the type vocabulary** · existing (V15)
- Evidence: baseline, `--review-all` reports 2 records INVALID-VOCABULARY.
- Desired: reclassify them explicitly (`--reclassify`, recorded) or mark them historical-exempt. · Phase 2 · Low · Optional

**PRO-09 — Preflight is almost entirely advisory, and placeholders make its warnings permanent** · new
- Evidence: preflight FAILs only on a missing or tiny request or a missing
  `docs/requirements` or `docs/decisions` directory (X04: a claimed request
  PASSes). `known_subsystems = []` gives every request the "no subsystem
  keyword" warning.
- Desired: meaningful checks (the request is claimed by the caller, no duplicate WORK, requirement owner identified) and configured keywords. · Phase 2 · Low · Optional

**PRO-10 — `drift_detect` scans ignored trees** · existing (V13), partially confirmed
- Evidence: X14. Ignored `node_modules/pkg/README.md` produced CONTRADICTORY_AUTHORITY and STALE_PATH_REF warnings; 20,000 extra `.md` files cost 0.6 s.
- Desired: reuse `doc_integrity.documentation_files()` (git scope). · Phase 2 · Low · Optional

**PRO-11 — The structural check depends on untracked state and fixed file names** · existing (V14)
- Evidence: X15. A fresh clone fails with "MISSING DIRECTORY
  .agent/state/claims" until any `claim.py` call creates it. Seven bootstrap
  records are required by exact file name.
- Desired: create the untracked directories on demand (or check the tracked scaffold only); make bootstrap records configurable. · Phase 2 · Medium · Mandatory

### 5.3 Concurrency / Recovery

**CON-01 — The heartbeat is manual, and long lanes outlive the stale threshold** · existing (V10), extended
- Evidence: only `_touch()` inside owner operations refreshes it (X25). The
  registry budgets `soak` and `live` at 7200 s, beyond the 2 h
  (`STALE_HOURS_DEFAULT`) threshold (X16).
- Current: a live session running one long lane looks STALE and can be recovered by another agent.
- Desired: a heartbeat wrapper for long commands (or `run_tests.py --request`
  refreshing it), and a threshold relative to the declared budget of the
  running step.
- Risk: live work recovered out from under its owner.
- Depends on: — · Phase 3 (interim: document calling `--heartbeat`) · **High** · Mandatory

**CON-02 — No process-liveness verification** · existing (V04)
- Evidence: X02. The recorded PID belongs to the short-lived `claim.py` process. Staleness is purely heartbeat age.
- Desired: record a verifiable session identity (host, boot id, pid + start
  time, session marker), with ALIVE / DEAD / UNKNOWN verdicts. The external
  engine's ADR-000198 describes this pattern for its own workers.
- Depends on: COR-10 · Phase 3 · Medium · Mandatory before CON-04

**CON-03 — Claims and id counters are clone-local** · existing (V09)
- Evidence: X10. Clone A and clone B both allocate `WORK-000002`.
- Desired (core): document the single-clone scope explicitly and make drift
  detect cross-branch duplicates after merge. Desired (optional, Phase 5): a
  shared allocator/claim service for multi-machine setups.
- Phase 2 (documentation, detection) / Phase 5 (service) · Medium · Mandatory (documentation) / Optional (service)

**CON-04 — Stale claims are only reported** · new
- Evidence: `claim.py --recover` is report-only by design; there is no automatic escalation, notification or recovery policy.
- Desired: a policy-driven sweep (report → escalate to needs-review → recover
  under a verified-dead verdict only). Never recover a live session.
- Depends on: CON-02, CON-05 · Phase 3 · Medium · Optional

**CON-05 — No consistency-repair tool** · new
- Evidence: X23 and COR-07. Inconsistent combinations (a claim whose state
  differs from the request directory, a lone sidecar, a claim without a
  request) are neither detected nor repairable except by hand.
- Desired: `claim.py --fsck [--repair]`: report every inconsistency and apply only idempotent, recorded repairs.
- Phase 3 (detection in Phase 1 via COR-06) · Medium · Mandatory

**CON-06 — Runtime logs grow without bound** · new
- Evidence (code): `claims-history.jsonl` and `id-allocations.log` are append-only with no rotation.
- Phase 3 · Low · Optional

### 5.4 Project generalization

**GEN-01 — Layout, id formats, header conventions and state sets are hard-coded and duplicated** · existing (V16), extended
- Evidence: X16. The state sets are defined in `agent_process.py`,
  `drift_detect.py`, `completion_gate.py` and `state-model.md`. Directory
  literals `requests/…`, `work/…` and `evidence/…` are repeated across 18
  scripts, as are the `**Status:**` / `**Request:**` / `**Test-Run:**`
  regexes, the `WORK-\d{6}` / `REQ-<SRC>-YYYYMMDD-NNN` formats and
  `STALE_HOURS` in two places.
- Desired: one configuration and schema module (paths, state machine, id formats, header fields, thresholds) that every tool imports, with defaults equal to today's behaviour.
- Risk: every adaptation touches many files; divergence between copies.
- Depends on: — · Phase 2 · **High** · Mandatory

**GEN-02 — Test selection and staleness understand only qmake** · existing (V17)
- Evidence: X17. `src/main.c` and `CMakeLists.txt` are "unmapped: … selected
  everything". `test_selection.py` parses only `.pro`/`.pri`/Makefiles. The
  audit's dependency builtins assume a qmake root.
- Desired: a build-graph adapter interface (qmake adapter = current code, plus
  "none" with explicit globs, CMake, others later). Unmapped → everything
  stays the safe default.
- Depends on: GEN-01 · Phase 2 · **High** · Mandatory (interface) / Optional (additional adapters)

**GEN-03 — Builds are hard-coded to `make`** · new
- Evidence: X17. `run_tests.py --build` calls `["make", "-C", …]`; `build_health.py` has `REQUIRED_TOOLS = ["make", "qmake", "g++"]` and a probe `make -j2`.
- Desired: the build command comes from configuration or the build adapter. · Phase 2 · Medium · Mandatory

**GEN-04 — A source-project assumption remains in the orchestrator** · new
- Evidence: X25. `retry_isolated()` does `name.split("/", 1)[1] if
  job.suite["id"] == "app" else name`, the filter semantics of the source
  project's GUI runner.
- Current: any project that names a suite `app` gets a different retry filter.
- Desired: a per-suite `filter_format` in the registry (default: the case name). · Phase 1 · Medium · Mandatory

**GEN-05 — Linux ELF assumptions in test identity and capability detection** · new
- Evidence: X25. Build ids come from `readelf -n` (sha256 fallback), and
  case-list support is detected by scanning the binary for the literal
  `PROJECT_TEST_CASES`.
- Desired: an explicit capability declaration in the registry (`supports_case_list: true`), with the build-id reader kept as an optimisation. · Phase 2 · Low · Optional

**GEN-06 — The test-runner contract is bespoke** · new
- Evidence: runners must speak `PROJECT_TEST_{LIST,CASES,SHARD,RESULTS}` and a custom JSONL format, or `PASS|FAIL|SKIP|TIMEOUT <name>` lines (X25). There is no JUnit XML or TAP input.
- Desired: a documented, versioned runner contract plus result adapters (JUnit, TAP, exit-code), so common frameworks plug in without modification. · Phase 4 · Medium · Mandatory for CI integration

**GEN-07 — Unconfigured placeholders are indistinguishable from configured results** · new
- Evidence:
  - baseline: `invariant_check --strict` prints "INVARIANTS: PASS" with zero invariants configured;
  - `static.warning-policy` is UNKNOWN;
  - the `package` lane is BLOCKED on `<PACKAGE_STAGING_DIR>/<APP_BINARY>`;
  - `known_subsystems = []`.
- Desired: a "NOT CONFIGURED" status, and an `init`/doctor step that lists unfilled placeholders. · Phase 2 · Medium · Mandatory

**GEN-08 — The root override is honoured by only half the tools** · new
- Evidence: X16. `AGENT_PROCESS_ROOT` is honoured by 9 of 18 scripts. The
  others use the script directory: `preflight`, `status_report`,
  `health_check`, `build_health`, `crash_evidence`, `requirement_impact`,
  `doc_integrity`, `run_tests` (partly). `process_next_request` honours it
  but calls `preflight`, which does not.
- Desired: one root and state resolver (from GEN-01) used everywhere. · Depends on: GEN-01 · Phase 2 · Medium · Mandatory

**GEN-09 — The classifier is English-only regex** · existing (V18)
- Evidence: X18. French, German and Spanish crash and performance statements → INVESTIGATION/low. English → CRASH/high.
- Desired: keep the deterministic validator; allow additional language packs or a model-assisted proposal validated by the same rules. · Phase 5 · Low · Optional

**GEN-10 — Audit inventory and builtins are Qt/qmake-specific** · new
- Evidence: X25. `checks.py` runs `qmake6 -query QT_VERSION`; `report.py` prints a "Qt:" line; `build_tree_qt`; qmake dependency builtins.
- Desired: build-adapter-provided inventory. · Depends on: GEN-02 · Phase 2 · Low · Optional

**GEN-11 — Crash diagnostics assume ELF cores, gdb and `log/dump/`** · new
- Evidence: `crash_evidence.py` constants; `AGENTS.md` / `crash-investigation.md`.
- Desired: a crash-artifact provider (location pattern, analyser command) in configuration. · Phase 2 · Low · Optional

**GEN-12 — Fixed vocabularies are hard-coded** · new
- Evidence (code): request sources (`chat|inbox|crash|automation`), the
  priorities, the `REQ-DEVOPS` namespace, the six-file
  `PROTECTED_ARTIFACTS`, and the type vocabulary.
- Desired: configuration with today's values as defaults (the protected list and type vocabulary stay protected artifacts). · Depends on: GEN-01 · Phase 2 · Low · Optional

### 5.5 Build / Test / CI integration

**BLD-01 — No CI entry point** · new
- Evidence: `CI_CD.md` §8. Every run is an agent command. `commit_request`
  uses `git commit-tree`, so no hooks run (ADR-000186). There is no trigger,
  scheduler or status endpoint.
- Desired: a documented CI contract (§9) and a thin CLI entry point
  (`ci verify --request … / --commit …`) that a CI job can call, with stable
  exit codes and JSON output.
- Depends on: AUTO-01, BLD-02, BLD-03 · Phase 4 · **High** · Mandatory for Phase 4

**BLD-02 — Test proof lives in untracked build trees** · new
- Evidence (code): run records are written to `build/<tree>/test-results/runs/`.
  `find_run_record()` falls back to `evidence/*/run-<id>.json`, but copying
  is manual (`--evidence` prints Markdown only). The gate downgrades a
  missing record to a WARN ("cannot verify it").
- Current: another agent or machine cannot independently verify a linked run.
- Desired: the run record (or its digest plus an artifact-store reference) is attached to evidence automatically when a run is linked.
- Depends on: BLD-03 · Phase 3 · **High** · Mandatory

**BLD-03 — Run records lack reproducibility provenance** · new
- Evidence: X25. The record has `head`, `describe`, `host`, `build_dir` and
  `changed`, but no dirty-tree flag, OS, compiler, toolchain or Python
  versions, and no environment digest.
- Desired: a provenance block (dirty flag + diff digest, tool versions, runner version, manifest digest). · Phase 2 · Medium · Mandatory

**BLD-04 — Lanes are unregistered or placeholders** · existing (V22)
- Evidence: baseline. perf, stress, soak and live have no suites (see COR-03); `package` is BLOCKED; no binary suite is registered in the template.
- Desired: the project registers real suites; an empty lane is reported as empty (COR-03), not as PASS. · Depends on: COR-03 · Phase 4 · Medium · Mandatory

**BLD-05 — No build model** · new
- Evidence (code): there is no build discovery, no notion of clean vs
  incremental, and no dependency validation beyond `build_health.py`'s tool
  presence check. `--build` is one `make` call.
- Desired: build adapter operations (configure, build incremental, build clean, list artifacts, dependency check) with structured results. · Depends on: GEN-02, GEN-03 · Phase 4 · Medium · Mandatory for CI

**BLD-06 — No artifact collection or retention policy** · new
- Evidence (code): scratch and logs of passing jobs are deleted unless `--keep`; binaries and packages are not recorded. The only retention is the audit's.
- Desired: declared artifacts per suite or lane, collected with digests, and a retention policy. · Phase 4 · Medium · Mandatory for CI

**BLD-07 — No regression or trend tracking** · new
- Evidence: only `cache.json` (last pass) and `durations.json` persist across runs.
- Desired: optional history (pass rate, duration, flakiness per case). · Phase 5 · Future · Optional

**BLD-08 — The build-failure taxonomy exists only in documents** · new
- Evidence: X25. `SOURCE ERROR`, `LINK FAILURE`, `DEPENDENCY FAILURE` etc.
  (build contract) and `build_failed.log` appear in no script.
- Desired: the build adapter classifies failures into the taxonomy and records them. · Depends on: BLD-05 · Phase 4 · Medium · Mandatory for CI

### 5.6 Audit / Evidence

**AUD-01 — Gate results are not recorded** · new
- Evidence: `completion_gate.py` writes nothing (0 writes, X25); its verdict exists only in terminal output.
- Desired: a persisted gate record (work id, request fingerprint, run ids,
  checks, verdict, time, tool version) stored with the evidence. It becomes
  the input of COR-01, COR-09 and COR-16.
- Phase 1 · **High** · Mandatory

**AUD-02 — Evidence records have no schema** · new
- Evidence (code): evidence is free Markdown; the gate only looks for field names and PASS/FAIL/BLOCKED words (X05).
- Desired: required fields with enumerated values (classification from the state model), validated by gate and drift. · Depends on: GEN-01 · Phase 2 · Medium · Mandatory

**AUD-03 — Git history does not link a commit to its verification** · new
- Evidence: X25, trailers `Request-Id`, `Request-File`, `Agent-Id` only.
- Desired: `Work-Id`, `Gate-Record`, `Test-Run` trailers, so "what tests proved this commit" is answerable from git alone. · Depends on: AUD-01 · Phase 3 · Medium · Mandatory

**AUD-04 — The audit's exit parser turns a tool's WARN into PASS** · new
- Evidence: baseline. `build.toolchain` is PASS with the summary "Warn — loop can proceed but triage warnings" (`parse_exit`: rc 0 → PASS).
- Desired: a WARN-aware parser (or JSON output from the tool). · Phase 2 · Low · Optional

**AUD-05 — Historical citations do not resolve** · existing (V21)
- Evidence: X16. 104 distinct WORK/VER/ADR ids are cited and 88 have no record in the extraction.
- Desired: mark them as provenance (e.g. "source-project record") or add a "historical citations" note; never renumber. · Phase 2 · Low · Optional

**AUD-06 — Many process requirements have no identifiable verification** · new
- Evidence: X28. `req.verification-coverage` is UNKNOWN: "64 requirement IDs in 3 documents: 19 cited by tests, 14 only by evidence, 31 with no identifiable verification".
- Desired: a requirement-to-test trace for the system's own requirements (or test names citing the ids). · Phase 2 · Medium · Mandatory

**AUD-07 — Several tools have no tests** · new
- Evidence: §2.1 coverage map. There are no tests for `health_check`,
  `build_health`, `status_report`, `requirement_impact` or `crash_evidence`,
  nor for the processing path of `process_next_request`. COR-05, COR-11,
  COR-12 and COR-13 live there.
- Desired: tests before the Phase 1 fixes to those tools (regression tests first, per REQ-DEVOPS-036). · Phase 1 · Medium · Mandatory

### 5.7 Agent automation

**AUTO-01 — Machine interfaces are incomplete and exit codes inconsistent** · new
- Evidence: X25.
  - `create_request` and `process_next_request` have no `--json` (the id must be parsed from text).
  - Exit codes differ by tool: preflight FAIL = 2; gate/drift FAIL = 1; `run_tests` 0/1/2/3; `claim` 0–7.
  - `doc_integrity` is always 0, and `process_next_request` returns 0 after a timeout.
- Desired: JSON output on every tool and one documented exit-code convention. · Depends on: GEN-01 · Phase 3 · Medium · Mandatory

**AUTO-02 — There is no lifecycle driver beyond claim + preflight** · new
- Evidence: `process_next_request.do_process()` claims, runs preflight and prints 11 manual steps (`CI_CD.md` §6.4).
- Desired: decide which steps become tool transitions in the core (work
  transitions, links, gate record, completion) and which belong to an
  external driver (the ticket engine or another). · Depends on: EXT-01,
  PRO-01, COR-01 · Phase 3 · Medium · Optional (the core provides the
  primitives; the driver is an integration)

**AUTO-03 — No routine maintenance** · new
- Evidence: nothing sweeps stale claims, reports expiring quarantine entries, rotates logs or regenerates mirrors.
- Desired: a `maintenance` command set, safe by default (report-only). · Phase 3 · Low · Optional

### 5.8 Portability

**POR-01 — POSIX-only primitives** · existing (V19)
- Evidence (code): `fcntl.flock` and `os.link` (hard links; also unavailable
  on some filesystems) in `agent_process.py`. Not tested on a non-POSIX host.
- Desired (only if needed): a lock and atomic-move abstraction with POSIX and Windows implementations. · Phase 5 · Low · Optional

**POR-02 — Non-Linux platforms are unvalidated; Linux-specific tools are used** · existing (V24)
- Evidence: `readelf`, `xvfb-run`, `gdb`, `ps`; the audit ran only on Linux.
- Phase 5 · Future · Optional

**POR-03 — Minimum Python and git versions are not declared or checked** · new
- Evidence: X16 and X26. The code needs Python ≥ 3.9 (walrus operator,
  `Path.is_relative_to`, `zoneinfo`) and git ≥ 2.31. Nothing checks either,
  and old git degrades silently (COR-21).
- Desired: a startup check with a clear error. · Phase 2 · Low · Mandatory

### 5.9 External system integration

**EXT-01 — The external ticket engine is described but absent** · existing (V20)
- Evidence: X16. 11 documents (TICKET-ENGINE requirements, ADR-000196/197/198, process docs) describe it. There is no code dependency.
- Desired: decide whether the engine is an optional integration (keep its
  contract, mark it optional) or out of scope (move to history). This
  decides where AUTO-02 lives.
- Phase 2 · Medium · Mandatory (the decision)

**EXT-02 — No bridge to external issue trackers** · new
- Evidence: requests are file-only; no import or export.
- Desired: an optional adapter (tracker issue ↔ `requests/inbox`) that preserves submit-only semantics. · Phase 5 · Future · Optional

**EXT-03 — No status or notification surface for humans** · new
- Evidence: needs-review escalations and stale claims are visible only as files or command output.
- Desired: optional notifications or a status page fed by the JSON interfaces. · Depends on: AUTO-01 · Phase 5 · Future · Optional

### 5.10 Future / optional capabilities

| ID | Capability | Why deferred | Phase · Priority · M/O |
|---|---|---|---|
| FUT-01 | Multi-machine coordination (a shared allocator and claims service) | Single-clone scope is sufficient once documented (CON-03); a service adds a failure domain | 5 · Future · Optional |
| FUT-02 | Historical analytics (lead time, rework, flakiness trends) | Needs stable records (AUD-01/02, BLD-03) first | 5 · Future · Optional |
| FUT-03 | Multilingual or model-assisted classification behind the deterministic validator | English works for current users; the validator must stay authoritative | 5 · Future · Optional |
| FUT-04 | Dependency-aware scheduling (blocked-by, priority aging, capacity) | Needs PRO-01 and PRO-02 first | 5 · Future · Optional |
| FUT-05 | Expanded audit coverage (runtime, performance, deployment audits; ADR-000195 future list) | Needs real lanes (BLD-04) and a build model (BLD-05) | 5 · Future · Optional |

---

## 6. Priority table

| ID | Title | Category | Origin | Priority | Phase | M/O |
|---|---|---|---|---|---|---|
| COR-01 | Completion not enforced by tools | Correctness | new | Critical | 1 | M |
| COR-02 | Gate passes failing/absent verification | Correctness | existing V05+ | Critical | 1 | M |
| COR-03 | Zero-job test run = PASS, accepted by gate | Correctness | new | Critical | 1 | M |
| COR-04 | Release orphans owned uncommitted work | Correctness | new | High | 1 | M |
| COR-05 | Escalated request stays claimed | Correctness | new | High | 1 | M |
| COR-06 | Crash inside claim → stuck request | Correctness | new | High | 1 | M |
| COR-08 | Follow-up lost updates / wrong target | Correctness | existing V12+ | High | 1 | M |
| COR-12 | `health_check` spurious FAIL under load | Correctness | new | High | 1 | M |
| COR-14 | Worktree mode blind / split branches | Correctness | existing V11+ | High | 1/3 | M |
| COR-16 | Drift cannot see ungated completion | Correctness | new | High | 1 | M |
| PRO-01 | Work transitions manual | Process | existing V01 | High | 3 | M |
| CON-01 | Manual heartbeat vs long lanes | Concurrency | existing V10+ | High | 3 | M |
| GEN-01 | Hard-coded, duplicated layout/formats/states | Generalization | existing V16+ | High | 2 | M |
| GEN-02 | qmake-only selection/staleness | Generalization | existing V17 | High | 2 | M |
| BLD-01 | No CI entry point | Build/CI | new | High | 4 | M |
| BLD-02 | Test proof in untracked build trees | Build/CI | new | High | 3 | M |
| AUD-01 | Gate results not recorded | Audit | new | High | 1 | M |
| COR-07 | Paired writes not atomic | Correctness | new | Medium | 1 | M |
| COR-09 | Commit not bound to verified state | Correctness | new | Medium | 1 | M |
| COR-10 | Identity = bare string; token unused | Correctness | new | Medium | 3 | M |
| COR-11 | `crash_evidence` NameError | Correctness | existing V08 | Medium | 1 | M |
| COR-13 | `process_next_request` timeout | Correctness | existing V07 | Medium | 1 | M |
| COR-21 | Old git splits shared state | Correctness | new | Medium | 1 | M |
| PRO-02 | Traceability links manual | Process | new | Medium | 3 | M |
| PRO-03 | State-model transitions unsupported; manual path orphans | Process | new | Medium | 1 | M |
| PRO-04 | Docs claim nonexistent enforcement | Process | existing V02/V04 | Medium | 1 | M |
| PRO-06 | `doc_integrity` never fails | Process | existing V06 | Medium | 1 | M |
| PRO-11 | Structural check vs fresh clone | Process | existing V14 | Medium | 2 | M |
| CON-02 | No liveness verification | Concurrency | existing V04 | Medium | 3 | M |
| CON-03 | Clone-local claims/ids | Concurrency | existing V09 | Medium | 2/5 | M/O |
| CON-04 | Stale claims only reported | Concurrency | new | Medium | 3 | O |
| CON-05 | No consistency-repair tool | Concurrency | new | Medium | 3 | M |
| GEN-03 | Builds hard-coded to `make` | Generalization | new | Medium | 2 | M |
| GEN-04 | Residual suite id `"app"` special case | Generalization | new | Medium | 1 | M |
| GEN-06 | Bespoke runner contract | Generalization | new | Medium | 4 | M |
| GEN-07 | Placeholders read as PASS/UNKNOWN/BLOCKED | Generalization | new | Medium | 2 | M |
| GEN-08 | Root override honoured by half the tools | Generalization | new | Medium | 2 | M |
| BLD-03 | Run records lack provenance | Build/CI | new | Medium | 2 | M |
| BLD-04 | Lanes unregistered/placeholder | Build/CI | existing V22 | Medium | 4 | M |
| BLD-05 | No build model | Build/CI | new | Medium | 4 | M |
| BLD-06 | No artifact collection/retention | Build/CI | new | Medium | 4 | M |
| BLD-08 | Failure taxonomy not implemented | Build/CI | new | Medium | 4 | M |
| AUD-02 | Evidence has no schema | Audit | new | Medium | 2 | M |
| AUD-03 | Git history not linked to verification | Audit | new | Medium | 3 | M |
| AUD-06 | 31/64 process requirement ids unverified | Audit | new | Medium | 2 | M |
| AUD-07 | Untested tools | Audit | new | Medium | 1 | M |
| AUTO-01 | Incomplete JSON / inconsistent exit codes | Automation | new | Medium | 3 | M |
| AUTO-02 | No lifecycle driver beyond claim+preflight | Automation | new | Medium | 3 | O |
| EXT-01 | External engine described but absent | External | existing V20 | Medium | 2 | M |
| COR-15 | Gate status regex misses bold header | Correctness | existing | Low | 1 | M |
| COR-17 | Lock timeout reported as conflict | Correctness | new | Low | 1 | M |
| COR-18 | Index refresh may silently give up | Correctness | new | Low | 1 | O |
| COR-19 | Work locks described as "request ?" | Correctness | new | Low | 2 | O |
| COR-20 | Fingerprint ignores Markdown inputs | Correctness | new | Low | 2 | O |
| PRO-05 | Mirrors hand-maintained, unread | Process | existing V03 | Low | 3 | O |
| PRO-07 | No duplicate-request detection | Process | new | Low | 3 | O |
| PRO-08 | Bootstrap records non-canonical type | Process | existing V15 | Low | 2 | O |
| PRO-09 | Preflight advisory, placeholder warnings | Process | new | Low | 2 | O |
| PRO-10 | Drift scans ignored trees | Process | existing V13 (partial) | Low | 2 | O |
| CON-06 | Unbounded runtime logs | Concurrency | new | Low | 3 | O |
| GEN-05 | ELF/readelf and binary-scan detection | Generalization | new | Low | 2 | O |
| GEN-09 | English-only classifier | Generalization | existing V18 | Low | 5 | O |
| GEN-10 | Audit inventory Qt/qmake-specific | Generalization | new | Low | 2 | O |
| GEN-11 | Crash diagnostics ELF/gdb-specific | Generalization | new | Low | 2 | O |
| GEN-12 | Fixed vocabularies hard-coded | Generalization | new | Low | 2 | O |
| AUD-04 | Audit maps WARN to PASS | Audit | new | Low | 2 | O |
| AUD-05 | Historical citations unresolved | Audit | existing V21 | Low | 2 | O |
| AUTO-03 | No routine maintenance | Automation | new | Low | 3 | O |
| POR-01 | POSIX-only primitives | Portability | existing V19 | Low | 5 | O |
| POR-03 | Python/git minimums unchecked | Portability | new | Low | 2 | M |
| BLD-07 | No regression/trend tracking | Build/CI | new | Future | 5 | O |
| POR-02 | Non-Linux unvalidated | Portability | existing V24 (partial) | Future | 5 | O |
| EXT-02 | No issue-tracker bridge | External | new | Future | 5 | O |
| EXT-03 | No human status/notification surface | External | new | Future | 5 | O |
| FUT-01 | Multi-machine coordination | Future | new | Future | 5 | O |
| FUT-02 | Historical analytics | Future | new | Future | 5 | O |
| FUT-03 | Multilingual/model-assisted classification | Future | new | Future | 5 | O |
| FUT-04 | Dependency-aware scheduling | Future | new | Future | 5 | O |
| FUT-05 | Expanded audit coverage | Future | new | Future | 5 | O |

Totals: 79 findings. Critical 3, High 14, Medium 32, Low 21, Future 9. 24
come from existing limitations; 55 are new.

---

## 7. Dependencies between improvements

```text
                 AUD-07 (tests first for touched tools)
                        │
COR-03 (no empty PASS) ─┼─► COR-02 (gate semantics) ─► AUD-01 (gate record) ─┬─► COR-01 (enforced completion)
                        │                                                     ├─► COR-09 (commit bound) ─► AUD-03 (trailers)
                        │                                                     └─► COR-16 (drift sees ungated completion)
COR-06 + COR-07 (crash-consistent transitions) ─► CON-05 (fsck/repair) ─► CON-04 (policy sweep)
COR-10 (session identity) ─► CON-02 (liveness) ─────────────────────────────┘ (CON-04 requires both)
CON-01 (heartbeat) ─► CON-04
COR-05 + PRO-03 (needs-review semantics)            (independent, Phase 1)
COR-21 + POR-03 (version checks) ─► COR-14 (worktree checks) ─► worktree integration tool (Phase 3)

GEN-01 (config/schema module) ─┬─► GEN-08 (one root resolver) ─► COR-14
                               ├─► AUD-02 (evidence schema) ─► COR-02 (strict form)
                               ├─► PRO-01 (work transitions) ─► PRO-02 (links) ─► AUTO-02
                               ├─► GEN-12, PRO-11, GEN-07 (init/doctor)
                               └─► AUTO-01 (JSON + exit codes) ─► BLD-01 (CI entry) ─► EXT-03
GEN-02 (build-graph adapter) ─► GEN-03, GEN-10, BLD-05 (build model) ─► BLD-08 (taxonomy), BLD-06 (artifacts)
BLD-03 (provenance) ─► BLD-02 (proof attached to evidence) ─► BLD-01
GEN-06 (runner contract/adapters) ─► BLD-04 (real lanes) ─► FUT-05
EXT-01 (engine decision) ─► AUTO-02
```

Phase 1 fixes are deliberately small and local. They do not wait for GEN-01;
GEN-01 later absorbs their constants.

---

## 8. Recommended phased roadmap

### Phase 0 — Current baseline

The extracted system exactly as it is (§2), frozen as the reference:
- 216 tests (212 pass, 4 skip);
- the validation in §2.2;
- the `CI_CD.md` map.

Every later phase must keep the §2.2 results passing, except where a change
intentionally reclassifies a false PASS (COR-03), and must document that
change.

### Phase 1 — Correctness (no false success, no silent loss)

Goal: the tools never report success that did not happen, and never silently lose ownership, state or work.

1. Tests first (AUD-07) for the tools touched below.
2. **No empty PASS** (COR-03). **Gate semantics** (COR-02, COR-15). **Persisted gate record** (AUD-01).
3. **Enforced completion** (COR-01). **Commit bound to the gate record** (COR-09). **Drift detects ungated completion** (COR-16).
4. **Release safety** (COR-04). **needs-review semantics** (COR-05, PRO-03). **Crash-consistent claim and transition** (COR-06, COR-07).
5. **Follow-up correctness** (COR-08). `crash_evidence` fix (COR-11). **Health reliability** (COR-12, COR-13). Lock-timeout code (COR-17). Index-refresh report (COR-18).
6. **Worktree-correct checks** (COR-14, first part) and fail-closed git version handling (COR-21).
7. Remove the residual `"app"` special case (GEN-04). Decide `doc_integrity` FAIL classes (PRO-06). Align protected documents with behaviour (PRO-04).

Governance: steps touching `AGENTS.md`, `development_operations.md`,
`state-model.md`, `escalation.md` or `autonomous-operating-model.md` are
PROCESS-CHANGE work with an ADR and human review, per the system's own rules.

### Phase 2 — Generalization

Goal: adopting the system in another project is configuration, not code editing.

* One configuration and schema module (GEN-01): paths, state machine, id
  formats, header fields, thresholds and vocabularies (GEN-12). Every tool
  uses one root and state resolver (GEN-08).
* A build-graph adapter interface, with qmake as the first adapter and a
  "none/globs" adapter (GEN-02, GEN-03, GEN-05, GEN-10). A crash-artifact
  provider (GEN-11).
* An `init` / `doctor` step: unfilled placeholders → NOT CONFIGURED (GEN-07);
  version checks (POR-03); structural scaffolding for fresh clones (PRO-11).
* Evidence schema (AUD-02), run provenance (BLD-03), requirement-verification
  trace for the system's own requirements (AUD-06), WARN-aware audit parser
  (AUD-04).
* Housekeeping: bootstrap record classification (PRO-08), historical citation
  marking (AUD-05), preflight usefulness (PRO-09), git-scoped drift scan
  (PRO-10), cosmetic COR-19 and COR-20.
* Decisions: the engine's scope (EXT-01), and the single-clone scope made
  explicit (CON-03, documentation part).

### Phase 3 — Automation

Goal: routine lifecycle steps are tool transitions, not instructions.

* Work transitions (PRO-01) and bidirectional links (PRO-02), consulting the
  gate record. Generated or removed mirrors (PRO-05). Duplicate warnings
  (PRO-07).
* Heartbeat automation (CON-01). Session identity and liveness verification
  (COR-10, CON-02). Consistency repair (CON-05). A policy-driven stale sweep
  that never recovers a live session (CON-04). Log rotation (CON-06).
* Test proof attached to evidence automatically (BLD-02). Verification
  trailers in commits (AUD-03).
* JSON and consistent exit codes everywhere (AUTO-01). Maintenance commands
  (AUTO-03). Worktree integration command (COR-14, second part).
* Decide the split for the lifecycle driver (AUTO-02): the core exposes
  transitions; an optional driver (e.g. the external engine) sequences them.

### Phase 4 — Build/CI integration

Goal: an external CI system can build, test, collect and report through a stable contract (§9).

* A CI entry point with a stable contract (BLD-01). A build model: configure,
  incremental and clean builds, dependency check (BLD-05). Failure taxonomy
  (BLD-08).
* A versioned runner contract plus JUnit/TAP/exit adapters (GEN-06). Real
  lanes registered by the project, with empty lanes reported honestly
  (BLD-04). Artifact collection and retention (BLD-06).

### Phase 5 — Advanced agent operations

Optional, each justified on its own:
- multi-machine coordination (FUT-01, CON-03 service);
- analytics and trend tracking (FUT-02, BLD-07);
- scheduling (FUT-04);
- multilingual classification (FUT-03, GEN-09);
- expanded audits (FUT-05);
- tracker bridges and notifications (EXT-02, EXT-03);
- portability (POR-01, POR-02).

### Core vs integration layer

| Core (this system) | Integration layer (adapters, optional) |
|---|---|
| Record formats and schemas, state machine, id allocation | Build-system adapters (qmake, CMake, others) |
| Claims, ownership, owned commits, recovery primitives | Test-result adapters (JUnit, TAP), runner wrappers |
| Gate, gate record, evidence schema, drift, audit | CI-provider glue (pipelines, triggers, runners) |
| Test orchestration (tiers, selection interface, cache, isolation, run records) | Artifact store, notification channels, dashboards |
| Machine-readable CLI and exit-code contract | Issue-tracker bridges, the unattended lifecycle driver (e.g. the ticket engine) |

---

## 9. Future automated build/CI integration model

This is a proposed model, to be refined when Phase 4 starts. It describes
responsibilities and interfaces; it is not a design commitment.

### 9.1 Principles

1. **The repository stays the source of truth.** CI reads records and writes
   evidence through the same tools agents use. It never edits request or work
   state directly.
2. **CI is an agent with its own identity.** It acts through `claim.py`
   primitives only when it owns the request (for example a CI-owned
   verification step), or read-only otherwise.
3. **Independent verification.** CI re-runs the gate on a clean checkout of
   the exact commit and compares its gate record with the agent's.
4. **Honest classification.** Build and test outcomes map onto the existing
   vocabulary (PASS, FAIL, BLOCKED/ENVIRONMENTAL, EXPECTED, FLAKY, plus EMPTY
   from COR-03). Nothing unverifiable is PASS.

### 9.2 Interaction

```text
 agent: request → claim → work → implement → run_tests --request (local) → gate record → commit (trailers: Work-Id, Gate-Record, Test-Run)
                                                                                                   │
 CI trigger (commit on a request branch / main, nightly, manual) ◄────────────────────────────────┘
   1. checkout <commit> (clean)                       ── provenance: dirty=false, tool versions (BLD-03)
   2. doctor / version checks (POR-03, GEN-07)
   3. build adapter: configure + build (incremental or clean) → structured result + failure class (BLD-05, BLD-08)
   4. run_tests --tier t2|t3|lane --paths <commit diff> → run record (+ artifacts, digests) (BLD-06)
   5. gate --work <Work-Id> --independent → CI gate record; compare with the agent's record (AUD-01)
   6. publish: run record + gate record attached to evidence (BLD-02) or artifact store with a digest in evidence
   7. report status (JSON) to the CI provider; notify on FAIL / needs-review (EXT-03)
```

### 9.3 Interfaces the core must offer (by Phase 4)

| Interface | Content | From |
|---|---|---|
| `status --json` | queue, claims (with liveness), work states, pending verification | AUTO-01, CON-02 |
| Gate record schema (versioned) | work, request, fingerprint, commit, runs, checks, verdict, tool version | AUD-01 |
| Run record schema (versioned) | provenance, jobs, cases, classification incl. EMPTY, artifacts | COR-03, BLD-03, BLD-06 |
| Build adapter contract | configure / build / clean / artifacts / dependency check / failure class | BLD-05, BLD-08, GEN-02 |
| Runner contract | case listing, selection, sharding, results (native JSONL, JUnit, TAP, exit) | GEN-06 |
| Exit-code convention | ok / fail / blocked / busy / invalid / not-configured | AUTO-01, COR-17, GEN-07 |
| Evidence attachment | copy or reference with a digest; verified on read | BLD-02 |

### 9.4 What CI must not do

- Move request or work state outside the tools.
- Commit with `git add -A`.
- Treat an empty run as success.
- Quarantine a crash.
- Recover a claim without a verified-dead verdict.

---

## 10. Generalization requirements

Remaining assumptions, separated into framework functionality (keep in the
core, make configurable) and project policy (move to configuration or
adapters).

| Area | Current assumption | Where | Framework or policy | Requirement |
|---|---|---|---|---|
| Project name | `<PROJECT>` placeholders | `AGENTS.md`, docs | policy | filled by `init` |
| Directory layout | `requests/`, `work/<state>/`, `evidence/`, `docs/requirements`, `docs/decisions`, `.agent/state` literals | 18 scripts | framework (layout) + policy (paths) | one config/schema module (GEN-01) |
| Id formats | `REQ-<SRC>-YYYYMMDD-NNN`, `WORK/VER/ADR-NNNNNN`, local date | `agent_process`, `create_request`, regexes | framework | configurable with the same defaults (GEN-01) |
| Record syntax | `**Status:**`, `**Request:**`, `**Evidence:**`, `**Test-Run:**`, `**Type:**`, `**Decision:**` Markdown headers | gate, drift, protection | framework | schema-validated (AUD-02) |
| State machine | sets duplicated in code and doc | 3 scripts + `state-model.md` | framework | single definition (GEN-01) |
| Programming language | C/C++/Qt suffixes in `SOURCE_SUFFIXES`, audit inventory | `run_tests`, `checks.py` | policy | adapter-provided (GEN-02, GEN-10) |
| Build system | qmake graph, Qt 5 check, `make` | `test_selection`, `run_tests`, `build_health`, audit | policy | build adapter (GEN-02, GEN-03) |
| Test framework | `PROJECT_TEST_*` contract, custom JSONL / line format, binary string scan, suite `"app"` | `run_tests` | framework (contract) + policy (adapters) | versioned contract + adapters (GEN-06, GEN-05, GEN-04) |
| Operating system | POSIX `fcntl`, hard links, ELF, `readelf`, `xvfb-run`, gdb | `agent_process`, `run_tests`, crash | framework | declared scope; abstraction only if needed (POR-01/02) |
| Tooling versions | Python ≥ 3.9, git ≥ 2.31 (silent degradation) | all | framework | checked at startup (POR-03, COR-21) |
| Source tree | `third_party/` convention, `*.pro.user` in no-test patterns | `test_selection` | policy | configurable patterns |
| Ticket system | file-based requests only; external engine documented | requests, EXT docs | framework (file records) + integration (bridges) | EXT-01 decision, EXT-02 optional |
| Deployment | `package` lane placeholders | registry | policy | project registers validate/smoke (BLD-04) |
| Hardware / data | `testdata` / `hardware` requirement keys, `PROJECT_TEST_DATA`, `PROJECT_LIVE_HW_ARGS` | `run_tests` | framework (mechanism) + policy (names) | configurable requirement kinds |
| Crash artifacts | `log/dump/*.core`, gdb | `crash_evidence`, docs | policy | crash provider (GEN-11) |
| Terminology | English classifier word lists (some signal-processing words such as "dropped samples", "overruns") | `request_classification.py` | framework (validator) + policy (lexicon) | lexicon packs (GEN-09) |
| Vocabularies | sources, priorities, `REQ-DEVOPS`, protected artifacts | several | framework defaults | configurable, protected (GEN-12) |
| Invariants and policies | placeholders give vacuous PASS | `invariant_check`, audit, preflight, impact | policy | NOT CONFIGURED status (GEN-07) |

---

## 11. Risks and unresolved questions

- **R1 — Governance of the roadmap itself.**
  - The Phase 1 changes touch protected artifacts. The system requires a
    PROCESS-CHANGE request, an ADR and human review for them.
  - Open question: does the system directory govern its own evolution
    through its own request lifecycle (it is not a git repository yet)?
  - Recommendation: yes, after `git init`, so each roadmap item is dogfooded.
- **R2 — Stricter gates will "break" today's passing flows.** COR-02 and
  COR-03 turn some current PASS results into FAIL or EMPTY. That is the
  intent, but the existing agents' habits and the §2.2 baseline must be
  updated deliberately, not silently.
- **R3 — Enforcement vs autonomy.** Enforced completion (COR-01) needs a
  documented human override (a recorded waiver) or legitimate edge cases
  (docs-only, investigation-only requests) will be blocked.
- **R4 — Evidence volume.** Attaching run records (BLD-02) and artifacts
  (BLD-06) grows the repository. It is unresolved whether to commit full
  records or digests plus an external store.
- **R5 — Identity strength.** How strong must session identity be (COR-10)?
  A process/boot marker suffices against accidents, not against malicious
  actors. The threat model is unstated.
- **R6 — Worktree policy.** Worktree integration (COR-14) needs a decision on
  who may update `main` and how (rebase or merge, fast-forward only). In
  the source project the auto-mode classifier sometimes blocked the
  `update-ref` publish (recorded in the source's operating notes).
- **R7 — Engine scope (EXT-01).** Whether the unattended driver becomes part
  of the core decides where AUTO-02 goes and how much automation Phase 3
  owns.
- **R8 — Measurement limits of this audit.**
  - Crash windows (COR-06/07) and old git (COR-21) were simulated, not
    observed in the wild.
  - Load-dependent timing (COR-12) was observed once as a FAIL and measured
    twice at 41.7 s and 44.5 s.
  - Non-Linux behaviour was not tested.
- **R9 — Multi-machine demand is unknown.** CON-03 and FUT-01 are only
  worth it if agents on different clones or hosts must coordinate.

---

## 12. Explicitly not to be implemented yet

* Any change in this audit task. No implementation file was modified.
* A database, a server or a daemon replacing file-based records and `flock`.
* A distributed claim or id service (FUT-01), before the single-clone scope is documented and measured insufficient.
* Automatic recovery of claims (CON-04), before liveness verification (CON-02) and session identity (COR-10) exist.
* Automatic merging or publishing of worktree branches, before a publish policy (R6) exists.
* Coupling to a specific CI provider, before the CI contract (§9.3) exists.
* A Windows port (POR-01), unless a Windows user exists.
* An LLM or multilingual classifier replacing the deterministic validator (FUT-03).
* Turning every WARN into FAIL wholesale. Promotions are per class with a stated reason (PRO-06).
* Renumbering or rewriting historical ids and records (AUD-05).
* Re-including the external ticket engine, before the EXT-01 decision.
* Speculative capabilities (FUT-02, FUT-04, FUT-05, EXT-02, EXT-03) as requirements. They stay ideas until a request justifies them.

---

## 13. Validation performed during this audit

All experiments ran on disposable copies under the session scratch directory
(outside this directory and outside the original project). Each copy was a
`git init` of this directory with one commit. `TMPDIR` pointed to scratch and
`PYTHONDONTWRITEBYTECODE=1` was set.

### 13.1 Baseline (Phase 1 of the audit)

See §2.2. Full commands:
- `python3 -m unittest discover -s scripts/tests -v`;
- `python3 -m unittest discover -s tools/audit_system/tests -v`;
- `drift_detect.py`, `doc_integrity.py`, `invariant_check.py --strict`,
  `verify_development_system.sh`, `health_check.py`, `build_health.py`,
  `worktree_check.py`, `status_report.py`;
- `claim.py --status`, `claim.py --recover`;
- `process_next_request.py --dry-run`, `create_request.py --review-all`;
- `run_tests.py --list`, `--tier t2 --all`, and each lane;
- `./tools/audit quick|standard|baseline`.

### 13.2 What was not validated

- Non-Linux platforms.
- A real git < 2.31 (simulated, X26).
- Real binary test suites, and the hardware, perf, stress, soak and package lanes (none are registered).
- The external ticket engine (not extracted).
- A real CI system.
- `tools/audit --ai`.
- Behaviour under a real `SIGKILL` in the claim window (simulated by
  constructing the post-crash state, X23).

### 13.3 Experiment log

| X | What | Observed |
|---|---|---|
| X01 | `claim.py --claim-work WORK-999999`; `--status`; grep for readers of work locks | CLAIMED, exit 0, nothing moved; described as "work request ? … host ?"; no reader besides `claim.py` |
| X02 | Claim; read lock PID; backdate heartbeat 3 h and set PID 1 | lock PID dead immediately; ACTIVE while fresh; STALE after backdating despite a live PID 1 |
| X03 | Write nonsense into `.agent/state/requests.md` and `work.md`; drift; health | drift PASS; health **FAIL** from "timed out after 60 seconds" (see X27), not from the mirrors |
| X04 | Claim a request (agent A), run preflight on it | "PASS with warnings", exit 0; preflight source contains no "claim" |
| X05 | Work record with keywords only; evidence `Status: FAIL`, "12 tests failed", manifest row `fail`; unrelated staged file | `GATE: PASS`, exit 0, 14 PASS checks incl. "Implementation: git diff shows changes"; drift PASS |
| X06 | Failing records with `**Status:** completed` vs `Status: completed` | "Work claims COMPLETED" message only for the plain form |
| X07 | Broken relative link in `docs/broken.md` | WARN BROKEN_LINK, "0 FAIL, 1 WARN", exit 0 |
| X08 | `process_next_request.py --dry-run`, empty inbox | 30.1 s, "timed out after 30 seconds", exit 0 |
| X09 | `crash_evidence.py --request-id REQ-NOPE-20990101-001` | `NameError: name 'sys' is not defined`, exit 1 |
| X10 | `git clone`; `allocate_id.py WORK` in both clones | A: WORK-000002, WORK-000003; B: WORK-000002 |
| X11 | Claim, `--create-worktree`, backdate heartbeat; drift/verify/health from the worktree | main: STALE_CLAIM WARN; worktree drift PASS; verify "MISSING DIRECTORY .agent/state/claims"; health FAIL; `state_dir()` resolves to main (correct for `claim.py`) |
| X12 | Commit an owned change from the worktree; complete the request from main | code commit on `request/<stem>`; the request's state change had to be committed separately on `main`; worktree still shows the request in `inbox`; no integration tooling (no rebase/merge/update-ref in scripts) |
| X13 | 20 concurrent `--follow-up` to A; follow-up to A while B (processing) mentions A's id; follow-up to a completed request | 17 in `.md` / 19 in `.json`; the marker landed in B (0 in A); completed request appended, exit 0 |
| X14 | Ignored `node_modules/pkg/README.md` with stale path + authority phrase; 20,000 ignored `build/**.md` | drift: 2 WARN from `node_modules` (doc_integrity: PASS); drift 0.59 s vs doc_integrity 0.05 s |
| X15 | `git clone`; `verify_development_system` (unit tests skipped) before/after a `claim.py` call | before: "MISSING DIRECTORY .agent/state/claims", failed; after `claim.py --status`: passed |
| X16 | Static greps: constants, root override, engine refs, cited ids, Python features | state sets in 3 scripts; threshold in 2; 9/18 scripts honour `AGENT_PROCESS_ROOT`; 11 engine docs; 104 cited ids, 88 unresolved; walrus / `is_relative_to` / `zoneinfo` ⇒ Python ≥ 3.9 |
| X17 | CMake project files; `run_tests --plan --tier t1 --paths src/main.c CMakeLists.txt` | "unmapped: CMakeLists.txt, src/main.c", all scope; `make` hard-coded in `run_tests` and `build_health` |
| X18 | `classify()` on French/German/Spanish/English statements | non-English → INVESTIGATION/low; English crash → CRASH/high |
| X19 | Force a preflight FAIL; `process_next_request --agent-id A`; B claims; A re-claims; `--reject-request`; release + manual move per `escalation.md`; B claims | needs-review, claim kept; B "ALREADY CLAIMED"; A "ALREADY OWNED", request stays in needs-review; reject refused "expected exactly 'processing'"; manual move → "processing with no claim (orphaned)" |
| X20 | Claim, own and edit; `--complete-request`; `--release-request` (and `--requeue` variant, X20b) | exit 0 each; request `completed`; edit uncommitted and "unowned"; drift PASS; requeue also leaves the edit "unowned" |
| X21 | Claim, own, edit; `run_tests --tier live --request`; `--tier t1 --suite does-not-exist --request`; link the live run in a work record; gate | both runs "classification: PASS" with 0 jobs; gate "test run …: PASS (tier live…)", "exercised the request's current code (fingerprint match)", `GATE: PASS` |
| X22 | `--agent-id bob` claim; second "bob" session claims and registers a path; grep `claim_token` readers | "ALREADY OWNED (heartbeat refreshed)"; path registered; no authorization use of `claim_token` |
| X23 | Construct the post-crash state (claim lock written, request still in `inbox`); recover/drift/dry-run/claims | request stays in inbox; `--recover` "0 stale, 0 orphaned"; drift PASS; dry-run offers it; other agent CONFLICT; owner retry "ALREADY OWNED", no move |
| X24 | Submit identical text twice | `…-001` and `…-002` created, no warning |
| X25 | Code inspection: `parse_exit`, lock timeout code, commit trailers, `find_run_record`/`--evidence`, gate writes, traceability writers, index reset loop, run record fields, `== "app"`, `describe_owner`, `--json` flags, `os.link`, readelf/needle, `qmake6`, taxonomy terms | as cited in each finding |
| X26 | Simulate git without `--path-format` in a linked worktree; call `state_dir()` | resolves to the worktree's own `.agent/state` (not the main one) |
| X27 | Time `verify_development_system.sh` twice under load (load average 6–10, 16 CPUs) | 44.54 s and 41.66 s (health timeout 60 s, `process_next_request` timeout 30 s) |
| X28 | `./tools/audit baseline` | 11 PASS / 0 DRIFT / 2 UNKNOWN / 2 SKIPPED; `test.unit-tier` ran t1 (2 CACHED, 4 PASS); `req.verification-coverage`: 31 of 64 ids without identifiable verification; `build.toolchain` PASS despite a WARN |

### 13.4 Changes made by this audit

* **Created:** `CI_CD_ROADMAP.md` (this file).
* **`CI_CD.md`, corrected where the audit proved it factually inaccurate:**
  1. §2.2 said a needs-review request "can be re-claimed". It can only be
     re-claimed once no claim is held. After an escalation the escalating
     agent keeps the claim, others are refused, and the owner's re-claim
     does not move the request (X19).
  2. §11 item 10 called the recursive drift scan "slow". The measured cost
     was modest (0.6 s for 20,000 extra files, X14); the noise is the real
     issue.
* **No implementation file changed.** Every file other than these two is
  byte-identical to its state before the audit, verified by comparing
  sha256 checksums taken at the start of the audit.

---

## 15. Implementation status

Status after the implementation of ADR-000199:

- **DONE:** implemented and covered by a test.
- **PARTIAL:** the mandatory part was implemented, and the rest is stated.
- **DEFERRED:** not implemented. The reason is given, and `CI_CD.md` §11
  lists the limitation.

Test references are modules under `scripts/tests/` unless noted.

| ID | Status | Implementation · verification |
|---|---|---|
| COR-01 | DONE | `--complete-request`, `work.py -> completed` and `commit_request.py` require a PASS gate record on the current fingerprint (exit 9); waiver recorded · `test_lifecycle_hardening.Transitions`, `GateAndCommit` |
| COR-02 | DONE | gate FAILs a code change without an executed, passing run of `min_tier`; undefined citations; non-passing evidence · `test_completion_gate_test_run`, `GateAndCommit` |
| COR-03 | DONE | EMPTY (exit 4) and INTERRUPTED (exit 130), never PASS, rejected by the gate · `test_devsys_tooling.Orchestrator` |
| COR-04 | DONE | release refused while owned work is uncommitted; `--abandon <reason>` · `Transitions.test_release_refuses_…` |
| COR-05 | DONE | needs-review parks the claim; owner RESUMES, another agent TAKES OVER · `Transitions` |
| COR-06 | DONE | an interrupted claim is completed by the owner or the sweep, or dropped for a gone owner · `Sweep` |
| COR-07 | PARTIAL | the `.md` / `.json` pair is still two moves; a lone sidecar is detected (drift `LONE_SIDECAR`) and moved back by `--sweep --apply` · `Sweep.test_lone_sidecar_…`, `Audits` |
| COR-08 | DONE | follow-ups: exact id, per-request lock, closed requests refused · `Intake` |
| COR-09 | DONE | the commit of a completed request re-checks the fingerprint; `devsys.py verify-commit` checks it later from the tree · `GateAndCommit.test_commit_after_completion_…`, `test_commit_is_verified_…` |
| COR-10 | PARTIAL | the session process identity is recorded and verified (ALIVE / DEAD); an agent-id string is still the ownership credential and `claim_token` is not checked |
| COR-11 | DONE | `crash_evidence.py` imports `sys`, writes under the request lock, atomically |
| COR-12 | DONE | health check budget from config; a timeout is UNKNOWN (WARN) · `Health` |
| COR-13 | DONE | `process_next_request` timeouts are UNKNOWN; budget from config |
| COR-14 | PARTIAL | drift / health / sweep read the main worktree's claims; committing on a request branch and publishing to `main` remains manual (`CI_CD.md` §11.1) |
| COR-15 | DONE | the gate reads the `**Status:**` token (`devsys_records.status_token`) |
| COR-16 | DONE | drift `UNVERIFIED_COMPLETION` (no resolution / no PASS gate record) · `Audits` |
| COR-17 | DONE | lock timeout is BUSY (exit 8), not a conflict |
| COR-18 | DONE | a failed shared-index refresh is reported (`index_refresh_failed`, WARN) |
| COR-19 | DONE | work locks described by kind |
| COR-20 | DEFERRED | fingerprint still excludes documentation / process records (by design of `no_test_patterns`); a suite's Markdown input changed after its run is not a gate FAIL |
| COR-21 | DONE | main root from `--git-common-dir` resolved against the root (old git); nested repositories are their own root · `NestedRoot` |
| PRO-01 | DONE | `scripts/work.py` create / transition / show / list · `WorkTool` |
| PRO-02 | DONE | work ↔ request links, evidence ↔ work links written by the tools · `WorkTool` |
| PRO-03 | DONE | state model reconciled with `WORK_TRANSITIONS` / `REQUEST_TRANSITIONS`; resolution via the tools (`escalation.md`) |
| PRO-04 | DONE | preflight reads claims; mirrors are generated and checked; docs describe what is enforced |
| PRO-05 | DONE | `devsys.py status --write-mirrors`; drift `MIRROR_STALE` · `Bootstrap.test_status_writes_mirrors_…` |
| PRO-06 | DONE | `doc_integrity.fail_codes` (BROKEN_LINK FAILs) · `Audits` |
| PRO-07 | DONE | duplicate open requests refused, race-safe · `Intake` |
| PRO-08 | DONE | the bootstrap records were removed from the template |
| PRO-09 | PARTIAL | preflight FAILs on a foreign or orphaned claim; keyword hints stay WARN and are configurable |
| PRO-10 | DONE | drift scans the repository's own documentation only |
| PRO-11 | DONE | structure is configuration (`structure.*`); `init` creates what a fresh clone lacks |
| CON-01 | DONE | `claim.py --run`; `run_tests.py --request` heartbeat · `Liveness` |
| CON-02 | DONE | ALIVE / DEAD / ACTIVE / STALE from the recorded session process · `Liveness` |
| CON-03 | PARTIAL | ids: git-ref scan over fetched refs (`Intake.test_ids_on_a_fetched_branch_…`); claims stay per clone (documented scope) |
| CON-04 | DONE | verified-DEAD owners without uncommitted work are requeued by `--sweep --apply`; STALE is never auto-repaired |
| CON-05 | DONE | `claim.py --sweep [--apply]` · `Sweep` |
| CON-06 | DEFERRED | runtime journals are not rotated |
| GEN-01 | DONE | `devsys_config.py` layout, states, transitions, exit codes; `devsys_records.py` formats |
| GEN-02 | DONE | `tests.selection` globs mode (`sources` / `inputs`), source-newer staleness · `Orchestrator` |
| GEN-03 | DONE | `devsys_build.py` adapters · `Build` |
| GEN-04 | DONE | `filter_format` suite key replaces the `"app"` special case |
| GEN-05 | DONE | `supports_case_list` declared per suite; build-id reader kept as an optimisation |
| GEN-06 | DONE | JUnit and TAP results · `Orchestrator` |
| GEN-07 | DONE | NOT CONFIGURED / SKIPPED, never PASS; `doctor` lists placeholders · `Invariants`, audit `Levels` |
| GEN-08 | DONE | every script resolves its root through `devsys_config.repo_root` |
| GEN-09 | DEFERRED | classifier remains English regex |
| GEN-10 | PARTIAL | the audit queries qmake only for qmake builds; the `build_tree_qt` builtin remains for qmake projects |
| GEN-11 | PARTIAL | crash locations are configuration (`crash.*`, `paths.crash_dumps`); analysis still assumes gdb/ELF |
| GEN-12 | PARTIAL | request id prefixes and protected artifacts are configuration; the type vocabulary and priorities remain code |
| BLD-01 | DONE | `devsys.py ci` · `test_end_to_end` |
| BLD-02 | DONE | `evidence.py` copies run records into evidence; the gate reads either · `WorkTool.test_evidence_attaches_run_records` |
| BLD-03 | DONE | run records carry provenance (runner, Python, platform, tool versions) |
| BLD-04 | PARTIAL | `release` lane added; lanes are EMPTY when nothing is registered; `package` stays a project placeholder |
| BLD-05 | DONE | build adapters with configure / build / clean / rebuild / fresh / check |
| BLD-06 | PARTIAL | build records, failure logs and CI records are kept; declared artifacts with digests and retention are not implemented |
| BLD-07 | DEFERRED | future |
| BLD-08 | DONE | failure taxonomy implemented in `devsys_build.classify_failure` · `Build` |
| AUD-01 | DONE | `evidence/gates/<W>.json` append-only records |
| AUD-02 | DONE | `evidence.py` writes the required fields with an enumerated status; the gate checks header and manifest status |
| AUD-03 | DONE | commit trailers `Work-Id`, `Gate-Record`, `Test-Run`; `verify-commit` |
| AUD-04 | DONE | a tool's WARN is PASS "with N warning(s)", the warnings shown; a gate-severity violation is FAIL · audit `Levels` |
| AUD-05 | DONE | provenance note in `docs/decisions/README.md` |
| AUD-06 | PARTIAL | the new and amended requirements name their verification; older REQ-DEVOPS ids still lack a trace |
| AUD-07 | PARTIAL | new tests cover health, build, devsys, work, evidence, preflight, drift lifecycle, audit levels, the processing path end to end; `status_report`, `requirement_impact`, `crash_evidence` remain untested |
| AUTO-01 | PARTIAL | one exit-code table (`devsys_config`), `--json` on the new tools and `create_request`; not every legacy tool has JSON |
| AUTO-02 | PARTIAL | `devsys.py next --claim`, `work.py`, `evidence.py`, `ci` drive most steps; the intellectual steps stay with the agent by design |
| AUTO-03 | PARTIAL | `--sweep` and `status --write-mirrors` exist (report-only by default); no scheduler, no quarantine-expiry report |
| EXT-01 | DONE | interface contract in `docs/process/autonomous-ticket-engine.md`; the engine stays outside |
| EXT-02, EXT-03 | DEFERRED | future (issue tracker, notifications) |
| POR-01 | DONE | primitives isolated in `devsys_platform.py` (`docs/process/portability.md`) |
| POR-02 | DEFERRED | only Linux was validated |
| POR-03 | DONE | `devsys.py doctor` checks Python ≥ 3.9 and git ≥ 2.20 |
| FUT-01..05 | DEFERRED | future |

Totals over the 79 findings: DONE 52, PARTIAL 15, DEFERRED 12. EXT-02 and
EXT-03 share a row; FUT-01..05 is five findings.
