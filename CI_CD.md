# CI_CD — Technical Map of the Development-Control System

This document describes the development-control system in this directory as
it is implemented **after** the hardening, generalization and automation
work (ADR-000199). It was written from the code and checked by running the
tools on scratch copies and on a freshly bootstrapped example project (§13).
Where documentation and implementation still disagree, the disagreement is
listed in §11.

The system was extracted from a Qt/C++ application repository (§12). The
audit of that extraction is `CI_CD_ROADMAP.md`. Its implementation status is
§14 below and the "Implementation status" section of the roadmap.

---

## 1. Overview

The system is a **file-based, git-backed development operating system for
autonomous (AI) and human agents working in one repository, possibly at the
same time**:

- Every unit of development has a persistent record in the repository.
- Deterministic command-line tools move records through a fixed lifecycle.
- The tools enforce ownership between concurrent agents, verify liveness,
  bind completion to tested code, and audit the repository for drift.

It has no server, database, daemon or CI service. It needs Python ≥ 3.9
(standard library only), git ≥ 2.20 and a POSIX shell. A CI job, a human or
its own workers (§15) drive it through the same commands.

| Part | What it is | Where |
|---|---|---|
| Constitution | The operating rules every agent session reads first | `AGENTS.md` |
| Configuration | Everything project-specific, deep-merged over code defaults | `devsys.json`, `scripts/devsys_config.py` |
| Records | Requests, requirements, decisions, work items, evidence, gate records (Markdown + pipe manifests + JSON) | `requests/`, `docs/requirements/`, `docs/decisions/`, `work/`, `evidence/` |
| Lifecycle tools | Intake, claims and liveness, work and evidence records, owned-path commits, the gate, the operator view | `scripts/create_request.py`, `claim.py`, `work.py`, `evidence.py`, `completion_gate.py`, `commit_request.py`, `devsys.py` |
| Consistency checks | Drift, documentation integrity, declared invariants, structure, health | `scripts/drift_detect.py`, `doc_integrity.py`, `invariant_check.py`, `verify_development_system.sh`, `health_check.py` |
| Build | Adapters for none / make / cmake / qmake / custom with a failure taxonomy | `scripts/devsys_build.py` |
| Test orchestrator | Tiered, affected-only, cached, parallel, recorded test execution | `scripts/run_tests.py`, `test_selection.py`, `test_manifest.json`, `test_quarantine.json` |
| CI entry point | One non-interactive verification run and commit verification | `scripts/devsys.py ci`, `verify-commit` |
| Architecture audit | Read-only, manifest-driven conformance snapshot | `tools/audit`, `tools/audit_system/`, `.audit/manifest.json` |
| Worker Orchestrator | Workers that execute requests unattended: one active request each, claims through the framework, gated completion, verified recovery | `scripts/workers/`, `scripts/devsys.py workers` |
| Control panel | Qt window over the worker API: workers, their requests, queue, controls | `tools/panel/`, `tools/worker-panel` |
| Platform layer | Locks, no-clobber links, process identity and verification, process groups and trees | `scripts/devsys_platform.py` |
| Machine state | Claims, counters, locks, journals (untracked) | `.agent/state/` |

Authority chain (`AGENTS.md`):

```text
requests/ -> docs/requirements/ -> docs/decisions/ -> work/ -> tests -> evidence/ -> docs/
```

Normative behaviour of the system itself is
`docs/requirements/development_operations.md` (`REQ-DEVOPS-001..051`),
`architecture_audit.md` (`ARCH-AUDIT-001..010`) and
`worker_orchestration.md` (`WORKER-001..013`, the **Worker Orchestrator**;
§15). The former external engine's contract (TICKET-ENGINE-*) is historical
(`docs/history/requirements/`).

---

## 2. System Workflow

### 2.1 Lifecycle as implemented

```text
 human / chat / crash / automation
     │  scripts/create_request.py --source chat|inbox|crash|automation --text …
     │  (type validated; duplicate of an open request refused, exit 7; REQ id allocated;
     │   .md + .json created with O_EXCL; never claims)
     ▼
 requests/inbox/<date>-<slug>-req-<src>-<yyyymmdd>-<nnn>.md + .json
     │  scripts/devsys.py next --claim   (priority, then arrival)  |  claim.py --claim-request <file>
     │  identity: AGENT_ID + AGENT_OWNER_PID (session process → verified liveness)
     ▼
 requests/processing/…   .agent/state/claims/request-<file>.lock
     │  scripts/preflight.py --request <file>         FAIL (exit 2) → claim.py --needs-review-request --reason
     │  requirement found / extended (docs/requirements/…)     ADR when architecture changes
     │  scripts/work.py create --request <file> --requirements … → work/active/WORK-…md (+ work.txt, links)
     │  scripts/claim.py --own <file> --paths …   (register BEFORE editing: git baseline blob)
     │  implement + tests
     │  scripts/run_tests.py --tier t2 --request <file> --build
     │        └─ <build>/test-results/runs/<run-id>.json  (request fingerprint, provenance)
     │  scripts/evidence.py create --work W --status pass --test-run <run-id> …
     │        └─ evidence/VER-…md + evidence.txt + evidence/VER-…/run-<id>.json; work record linked
     │  scripts/work.py transition W --to verification
     │  drift_detect · doc_integrity · invariant_check
     │  scripts/completion_gate.py --work W --record
     │        └─ evidence/gates/W.json  (verdict, fingerprint, code paths, runs, evidence, head)
     │  scripts/work.py transition W --to completed          (needs PASS on the current fingerprint)
     │  scripts/claim.py --complete-request <file>           (same check; exit 9 otherwise)
     ▼
 requests/completed/…   (sidecar "resolution")
     │  scripts/commit_request.py --request <file> -m "…"  (owned delta only; gate re-checked;
     │        trailers Request-Id, Request-File, Agent-Id, Work-Id, Gate-Record, Test-Run)
     │  scripts/claim.py --release-request <file>          (refused while owned work is uncommitted)
     ▼
 claim archived to .agent/state/runtime/claims-history.jsonl
     │  later, anywhere: scripts/devsys.py verify-commit <sha>  → VERIFIED (tree re-fingerprinted)
```

The agent performs the intellectual steps: requirement, ADR, implementation
and tests. Every record movement is done by a tool, and every tool refuses
what the state model forbids. `process_next_request.py` claims and runs
preflight, then prints this sequence. `devsys.py ci` verifies the result
non-interactively (§8.6).

### 2.2 Request states (`claim.py`, `agent_process.transition_request`)

```text
                 claim                     complete (gate) / reject (reason) / needs-review (reason)
 inbox ─────────────────────► processing ───────────────────────────────────────► completed | rejected | needs-review
   ▲                              │   ▲                                                                   │
   └── release --requeue ─────────┘   └── claim: owner RESUMES, another agent TAKES OVER a parked claim ──┘
       (or sweep --apply for a verified-DEAD owner with nothing uncommitted)
```

- **One state at a time:** a request lives in exactly one of
  `requests/{inbox,processing,completed,rejected,needs-review}/`. The
  directory is the state.
- **Records of each move:**
  - the headers and the sidecar `status` are rewritten;
  - a `resolution` (state, reason, waiver, gates, agent, time) goes into the
    sidecar;
  - an entry goes into the claim history.
- **Completing** needs, for every work item linked to the request, a PASS
  gate record whose fingerprint equals the current one. Exceptions: a
  recorded `--waive-gate`, an exempt type (INVESTIGATION) with a reason, or
  `completion.enforce_on_transition: false`.
- **Rejecting and parking** each need `--reason`. Parking in needs-review
  marks the claim `parked`.
- **No-clobber moves:** a move is a hard link plus unlink (the rename
  fallback lives in `devsys_platform`), done inside the `claims` lock.
- **Recovery:** a claim interrupted between lock and move is completed by the
  owner re-claiming it, or by the sweep.
- **Closed states:** `completed` and `rejected` are final. Reopening means a
  new request.

### 2.3 Work states (`work.py`)

`work/{queued,active,blocked,needs-review,verification,completed,cancelled}/`,
the status column of `work/work.txt`, and the record's `**Status:**` header are
changed together by `work.py transition`:

- transitions follow `WORK_TRANSITIONS` in `devsys_config.py`, which is
  mirrored in `docs/process/state-model.md`;
- a reason is required for blocked, needs-review and cancelled;
- `-> completed` needs a PASS gate record on the current fingerprint;
- each transition is appended to the record's `## Transitions` section and to
  `.agent/state/runtime/transitions.jsonl`;
- repeating an interrupted transition finishes it.

`drift_detect.py` reports any remaining disagreement as `WORK_STATE_MISMATCH`.

### 2.4 Verification states

The `evidence.txt` classification and the record `**Status:**` are one of
`pending|pass|fail|blocked|pre-existing` (`evidence.py` validates it). Only
`completion.passing_evidence_statuses` (pass, pre-existing) satisfy the gate.
Test runs are classified PASS, FAIL, BLOCKED, EMPTY or INTERRUPTED (§8.3).
Only an executed PASS run of at least `completion.min_tier` satisfies the gate
for a request that changed code.

---

## 3. Directory / File Tree

```text
auto_ci_cd/
├── AGENTS.md                         agent constitution (protected)
├── CI_CD.md · CI_CD_ROADMAP.md       this map · the audit and its implementation status
├── devsys.json                       project configuration template (protected)
├── .gitignore                        runtime state, audit runs, tmp/
├── .agent/state/                     README · runs.md · requests.md / work.md (generated mirrors)
│                                     claims/ and runtime/ are created on use and untracked
├── .audit/                           README · manifest.json (15 checks, profiles, retention)
├── requests/                         README · request-template.md · inbox/ processing/ completed/ rejected/ needs-review/
├── work/                             README · work.txt · backlog.md · tasks/ · queued/ active/ blocked/
│                                     needs-review/ verification/ completed/ cancelled/
├── evidence/                         README · evidence.txt   (gates/ and VER-*/ are created on use)
├── docs/
│   ├── README.md                     documentation entry point
│   ├── requirements/                 requirements.txt · development_operations.md (REQ-DEVOPS-001..051, protected)
│   │                                 architecture_audit.md · worker_orchestration.md (WORKER-001..013)
│   ├── decisions/                    README · decisions.txt · 16 process ADRs (000001 … 000200)
│   ├── process/                      state-model · autonomous-operating-model · recovery · escalation
│   │                                 process-protection · chat-intake · crash-investigation · build-contract
│   │                                 engineering-rules · historical-id-collisions · configuration · portability
│   │                                 worker-orchestrator · agent-profiles
│   ├── verification/                 tests.md · strategy.md · architecture-audit.md · test-intents/ · requirements-trace/
│   ├── audits/current-state.md · architecture/README.md · work/README.md
│   ├── history/                      README · worker-orchestrator-merge.md · requirements/ (superseded contracts)
├── scripts/
│   ├── devsys_config.py              defaults, layout, states, exit codes, loading, validation
│   ├── devsys_platform.py            OS primitives (locks, links, process identity, process groups)
│   ├── devsys_records.py             record formats (headers, manifests, requirement ids)
│   ├── agent_process.py              core library: roots, locks, ids, claims, liveness, sweep, moves,
│   │                                 fingerprints, owned-path commits
│   ├── devsys.py                     install · init · doctor · status · sweep · next · verify-structure
│   │                                 · build · verify-commit · ci · queue · priority · workers
│   ├── claim.py · commit_request.py · allocate_id.py · worktree_check.py
│   ├── create_request.py · request_classification.py · preflight.py · process_next_request.py
│   ├── work.py · evidence.py · completion_gate.py · process_protection.py
│   ├── drift_detect.py · doc_integrity.py · invariant_check.py · verify_development_system.sh
│   ├── health_check.py · build_health.py · devsys_build.py · status_report.py
│   ├── requirement_impact.py · crash_evidence.py
│   ├── run_tests.py · test_selection.py · test_manifest.json · test_quarantine.json
│   ├── templates/                    instance-file templates written by install (devsys.json, registries,
│   │                                 audit manifest, requirement / decision manifests)
│   ├── workers/                      Worker Orchestrator: state · agent · gitops · prompt (+ prompts/worker.md)
│   │                                 · validation · runner (the worker process) · pool (the API)
│   └── tests/                        15 unittest modules + devsys_testlib.py + fake_agent.py (self-tests)
└── tools/
    ├── audit                         ./tools/audit [quick|standard|baseline] [--fail-on …]
    ├── audit_system/                 audit.py · checks.py · report.py · tests/test_audit.py
    ├── worker-panel                  the Qt control panel (launcher)
    └── panel/                        worker_panel.ui (Qt Designer) · worker_panel.py (controller)
```

Created at runtime (never tracked):

- `.agent/state/claims/*.lock`;
- `.agent/state/runtime/`: `id-counters.json`, `id-allocations.log`,
  `ref-id-cache.json`, `claims-history.jsonl`, `transitions.jsonl`,
  `sweeps.jsonl`, `locks/`;
- `.audit/runs/AUDIT-*/`;
- `<build_dir>/`: `build-results/`, `test-results/{runs/,latest.json,cache.json,durations.json}`, `ci-results/`;
- `tmp/tr/<run>/` (test scratch);
- per-request worktrees.

---

## 4. Important Files

### 4.1 Constitution, configuration and normative documents

| File | Role |
|---|---|
| `AGENTS.md` | Durable agent rules: authority chain, the tool-driven request sequence, concurrency and liveness rules, configuration pointer, temp area, finalization, build/test commands, crash diagnostics, documentation and reporting rules. PROJECT-SPECIFIC placeholders for constraints, test data, deployment and user docs. **Protected.** |
| `devsys.json` | The project's configuration (§10). Shipped with a `_examples` block. **Protected.** |
| `docs/requirements/development_operations.md` | The normative contract, REQ-DEVOPS-001..051. Of these, 047..051 are new with ADR-000199, and 014/016/019/020/022/025/027 were amended. **Protected.** |
| `docs/requirements/requirements.txt` | Owner index: `domain \| owner document \| id namespace \| verification`. |
| `docs/decisions/*.md` + `decisions.txt` | The process ADRs: 000001, 000090 (concurrency), 000149 (id collisions), 000150 (submit-only), 000155 (work-state checks), 000173 (classification), 000184 (doc scope), 000185 (protected artifacts), 000186 (tiered tests), 000193 (file mode), 000195 (audit), 000196–198 (external engine), **000199 (configuration layer and lifecycle hardening)**. |

### 4.2 Process documents

| File | Content |
|---|---|
| `docs/process/state-model.md` | Authoritative state sets and transitions, and which tool performs each. Mirrors the tables in `devsys_config.py`. **Protected.** |
| `docs/process/autonomous-operating-model.md` | Operating manual: lifecycle with commands, claiming, ownership modes, gate, checks. **Protected.** |
| `docs/process/recovery.md` | Liveness verdicts, the safe sweep, explicit recovery, reboot, release refusal, worked examples. |
| `docs/process/escalation.md` | When to use needs-review; the tool-based parking and resolution. **Protected.** |
| `docs/process/configuration.md` | Every `devsys.json` setting, bootstrapping (`install` / `init` / `doctor`), architecture rules, record formats, adopting an existing instance. |
| `docs/process/portability.md` | Supported environment, platform primitives, degradation, porting checklist. |
| `docs/process/build-contract.md` | Build adapters, tiers and lanes, result formats, EMPTY / INTERRUPTED, the CI entry point, the failure taxonomy, "never claim a test passed unless it ran". |
| `docs/process/process-protection.md` | Protected artifacts need a PROCESS-CHANGE request, `Type: process-change` work and an ADR. **Protected.** |
| `docs/process/chat-intake.md` · `crash-investigation.md` · `engineering-rules.md` · `historical-id-collisions.md` | Intake (duplicates, follow-ups), the crash workflow, a practical summary, accepted id collisions. |
| `docs/process/worker-orchestrator.md` | The Worker Orchestrator: responsibility boundaries, request and worker state model, commands, configuration, runtime state, recovery, isolation, project-subdirectory installation. |
| `docs/process/agent-profiles.md` | The agent protocol and the one documented vendor-specific profile (configuration only). |

### 4.3 Lifecycle tooling (Python ≥ 3.9, standard library)

| Script | Does | Key behaviour |
|---|---|---|
| `devsys_config.py` | Defaults, layout, state and transition tables, exit codes; `load`, `get`, `validate`, `placeholders`. CLI `--get`, `--validate`. | Deep merge; keys starting with `_` are ignored; unknown keys, invalid enums, numbers, prefixes, source roots and regexes are reported. `AGENT_PROCESS_ROOT` overrides the root (tests). |
| `devsys_platform.py` | `lock`/`unlock`, `link_no_clobber`, `boot_id`, `process_start`, `pid_alive`, `session_identity`, `verify_session`, `group_popen_kwargs`, `signal_group`. | POSIX implementation; Windows branches best-effort (§9, `portability.md`). |
| `devsys_records.py` | Header read/write, requirement id grammar (ranges, definitions), manifest rows, upsert that keeps every other byte. | Shared by gate, drift, work and evidence tools. |
| `agent_process.py` | The library behind every mutating tool. | Critical sections are flock files: `claims`, `id-allocator`, `git-commit`, `intake`, `work`, `request-<name>`. A timeout raises BUSY (exit 8). Also: atomic writes, O_EXCL creation, no-clobber moves; id allocation (store scan, counter, git refs); claims with session identity, liveness, sweep, recovery, transitions with gate checks, release refusal; fingerprints (working tree and commit tree); owned-path commits. |
| `claim.py` | CLI: `--new-agent-id`, `--claim-request`, `--own` (`--shared` / `--concurrent` / `--adopt-existing-changes`), `--token`, `--snapshot`, `--heartbeat`, `--run`, `--complete/--reject/--needs-review-request` (`--reason`, `--waive-gate`), `--release-request` (`--requeue`, `--abandon`), `--status`, `--show`, `--recover`, `--sweep [--apply]`, `--recover-request` (`--force-active`, `--expect-owner`), `--create-worktree`, `--claim-work`/`--release-work`. | `--owner-pid` / `$AGENT_OWNER_PID` records the session process. Exit codes 0–10 (§10). |
| `devsys.py` | Operator front end: `install` (framework files; instance files from `scripts/templates/`), `init`, `doctor`, `status [--write-mirrors]`, `sweep`, `next [--claim]`, `verify-structure`, `build …`, `verify-commit`, `ci`. | Every command is a thin layer over the library; each automated step has a manual equivalent. |
| `work.py` | `create`, `transition`, `show`, `list` for work records. | Owner-checked; validates requirements and ADRs; registers every file before writing; journaled; idempotent. |
| `evidence.py` | `create`, `attach-run`, `show`. | Allocates VER, writes the fields the gate reads, links the work record, copies run records into `evidence/VER-*/`. Refuses an unknown run before writing anything. |
| `completion_gate.py` | Gate for a work item; `--record` appends to `evidence/gates/<W>.json` (gate version 3). | FAIL rules in §7.1. Keyword checks are WARN only. |
| `commit_request.py` | Commits only the request's owned delta. | Private index; per-path three-way merge against the baseline, or shared-manifest lines naming the request's tokens; refuses overlaps and unowned paths; completed requests are re-gated; CAS `update-ref`; trailers; `--dry-run`, `--report`. |
| `create_request.py` | Submit-only intake, `--follow-up`, `--file [--reclassify]`, `--review-all`. | Duplicate refusal and creation in one critical section; follow-ups under a per-request lock; closed requests refused. |
| `allocate_id.py` | Next `WORK`/`VER`/`ADR`/`REQ-<SRC>-<date>` id. | `max(counter, store scan, git-ref scan) + 1`. Only the request store issues request ids. |
| `preflight.py` | Pre-implementation check (exit 2 on FAIL). | FAIL: missing or short request, missing requirement/decision directories, a request claimed by another agent or orphaned. WARN: stale/dead owner, no agent id, referenced requirement ids (outside the generated classification section), keyword hints. |
| `process_next_request.py` | Next inbox request (via `devsys.next_eligible`): `--dry-run` plan, or claim + preflight + printed checklist. | Health check budget from configuration; a timeout is UNKNOWN. |
| `drift_detect.py` | Deterministic drift report (exit 1 on FAIL). | Adds lifecycle checks: UNVERIFIED_COMPLETION, DUPLICATE_REQUEST_ID, COMPLETED_EVIDENCE_NOT_PASSING, LONE_SIDECAR, CLAIM_STATE_MISMATCH, MIRROR_STALE, and STALE_CLAIM from the liveness verdicts. Scans the repository's own documentation only. |
| `doc_integrity.py` | Broken links, stale paths, obsolete terms, duplicate authority, over git-tracked-or-new `*.md`. | `doc_integrity.fail_codes` FAIL (BROKEN_LINK by default); the rest WARN. |
| `invariant_check.py` | Declared architecture rules plus process protection. | NOT CONFIGURED without rules; `--strict` makes a protection finding FAIL; `--json`. |
| `verify_development_system.sh` | `devsys.py verify-structure`, then the unit suite unless `DEVSYS_SKIP_UNITTESTS=1`. | t0 suite `devsys`. |
| `health_check.py` | PASS / WARN / FAIL across structure, drift, doc integrity, invariants, config, process docs, claim state. | A check exceeding `health.structure_timeout_seconds` is UNKNOWN → WARN. |
| `build_health.py` · `worktree_check.py` · `status_report.py` · `requirement_impact.py` · `crash_evidence.py` | Build readiness (adapter tools, configured tree, generated-code staleness, dump dir); ownership of every change; status from artifacts; impact report; crash evidence discovery. | Config-driven; paths may be absolute. |

### 4.4 Build, tests, CI

| File | Role |
|---|---|
| `scripts/devsys_build.py` | Adapters `none` / `make` / `cmake` / `qmake` / `custom`: `configure`, `build`, `clean`, `rebuild`, `fresh`, `info`, `check`. Records in `<build_dir>/build-results/`, failure classes. |
| `scripts/run_tests.py` | Tiers and lanes, affected selection, cache, shards, isolation, retries, quarantine, EMPTY / INTERRUPTED, JUnit / TAP / JSON / line / exit results, provenance, kept failure logs, heartbeat, `--build`, `--plan`, `--list`, `--evidence`. |
| `scripts/test_selection.py` | qmake project graph or suite globs (`tests.selection`); changed paths → affected suites. |
| `scripts/test_manifest.json` | Suite registry: `devsys` and `audits` (t0), `scripts` and `audit_py` (t1; the framework's own tests), a `package` lane placeholder; `_example_binary_suite`. |
| `scripts/devsys.py ci` / `verify-commit` | CI entry point and commit verification (§8.6). |

### 4.5 Architecture audit

`tools/audit` runs `tools/audit_system/audit.py`:

- profiles, run identity, the read-only self-check, comparison, retention,
  `--fail-on`, `--ai`;
- `checks.py` holds the states PASS / FAIL / DRIFT / UNKNOWN / SKIPPED, the
  parsers and the builtins, including `architecture_rules`;
- a fallback walks the tree when there is no git;
- `report.py` writes the reports.

`.audit/manifest.json` holds the 15 checks. `arch.rules`, `doc.integrity`,
`proc.drift`, `proc.structure`, `test.unit-tier` and `test.recorded-results`
have `severity: "gate"`.

---

### 4.6 Worker Orchestrator (ADR-000200)

| File | Role |
|---|---|
| `scripts/workers/state.py` | Per-worker records (`worker.json`), events, attempt directories, and the shared attempts journal. Also the worker slots (`workers.max_workers`) and the fixed agent id per worker. |
| `scripts/workers/agent.py` | The generic agent adapter. It launches `workers.agent.command` (prompt on stdin, JSON-lines transcript to disk, own process group), waits with timeout and stop-now, parses the transcript and detects usage limits. Session verification runs through `devsys_platform.verify_session` with the `DEVSYS_WORKER_SESSION` marker. |
| `scripts/workers/runner.py` | The worker process. It selects (its own claim first, then `devsys.queue`), claims atomically with the worker as owner, runs the attempt, finishes it (report, commits attributed, request state), validates, and concludes from the request's state (retry / escalate via needs-review). It also recovers or re-adopts a recorded attempt, and handles the stop / circuit breaker / usage-limit wait. |
| `scripts/workers/validation.py` | `git_sanity` (against the attempt baseline), `request_state`, `commit_binding` (`verify-commit`), and configured command steps. |
| `scripts/workers/prompt.py`, `prompts/worker.md` | The worker prompt (the gated lifecycle, tools named relative to the working directory) and parsing of the agent's report. |
| `scripts/workers/gitops.py` | Read-only git inspection (snapshots, attempt delta, commits between). |
| `scripts/workers/pool.py` | The API for the CLI and the panel: `status`, `start` (detached, refused when disabled, not configured or at the maximum), `stop [now]`, `enable` / `disable`, `recover [force]`, `peek`. |
| `tools/panel/worker_panel.ui`, `worker_panel.py`, `tools/worker-panel` | The Qt control panel: the Designer layout plus a controller that only calls the API. |

## 5. Data / State

### 5.1 Tracked records

| Record | Format | Identity | Index |
|---|---|---|---|
| Request | `requests/<state>/<date>-<slug>-req-<src>-<yyyymmdd>-<nnn>.md` (headers Request ID, Source, Type, Priority, Created, Status; sections Original User Statement, Classification, Inferred Intent, Follow-up Context, Traceability) + JSON sidecar (`request_id, source, type, priority, created(_utc), status, original_text, text_sha256, classification, follow_ups, links.work, resolution`) | `REQ-<SRC>-YYYYMMDD-NNN`, prefix per source from `ids.request_sources` | directory = state |
| Requirement | `docs/requirements/<domain>.md`, defined by heading, table row or bold lead | domain namespaces | `requirements.txt` |
| Decision | `docs/decisions/ADR-NNNNNN-<slug>.md` | `ADR-NNNNNN` | `decisions.txt` |
| Work item | `work/<state>/WORK-NNNNNN-<slug>.md`: Status, Type, Request, Requirements, Decision, Evidence, Test-Suites, Test-Run headers; `## Transitions` | `WORK-NNNNNN` | `work.txt`: `WORK-ID \| status \| title \| record \| requirements \| evidence` |
| Evidence | `evidence/VER-NNNNNN-<slug>.md` (Status, Requirements, Work, Command, Result, Test-Run; Environment; Notes) + `evidence/VER-NNNNNN/run-<id>.json` | `VER-NNNNNN` | `evidence.txt`: `VER-ID \| classification \| date \| requirements/work \| record` |
| Gate record | `evidence/gates/WORK-NNNNNN.json`: `{"work", "runs": [{at, verdict, gate_version, work, request, request_id, request_fingerprint, request_code_paths, test_runs, evidence, head, agent_id, fails, warns, passes}]}` (append-only history) | per work item | commit trailer `Gate-Record` |
| Collision declarations | `docs/process/historical-id-collisions.md` | — | parsed by `drift_detect` |

### 5.2 Untracked machine state (`.agent/state/`, main worktree of a clone)

- **Claim lock** `claims/request-<file>.lock` (JSON). It holds:
  - identity: `kind`, `target`, `request_id`, `agent_id`, `claim_token`,
    `claimed_at`;
  - liveness: `heartbeat_at`, `host`, `session {host, boot_id, pid, start}`,
    `session_at`, `step {host, boot_id, pid, start, label, started_at}`;
  - state: `state`, `parked`, `request_path`, `worktree` / `branch`;
  - ownership: `files {<path>: {mode, baseline_blob, mine_blob, …}}`,
    `tokens`, `commits`;
  - `history`.
- **Work lock** `claims/work-<WORK-ID>.lock`.
- **`runtime/` files:**
  - `id-counters.json` and `id-allocations.log`;
  - `ref-id-cache.json`, a per-commit id summary, versioned;
  - `claims-history.jsonl`, with every release and recovery;
  - `transitions.jsonl`, the work journal;
  - `sweeps.jsonl`, the applied repairs;
  - `locks/*.lock` and transient private indexes;
  - `workers/<id>/` (record, lock, events, attempts) and
    `workers/attempts.jsonl`: the Worker Orchestrator's per-worker state
    (`docs/process/worker-orchestrator.md` §5).
- **Mirrors:** `requests.md` / `work.md` are regenerated by
  `devsys.py status --write-mirrors`, and `drift_detect` reports a stale
  mirror (MIRROR_STALE).

### 5.3 Generated artifacts

- **Build record** `<build_dir>/build-results/<id>.json` + `.log` +
  `latest.json`. It holds the action, system, steps (argv, rc, seconds),
  status, failure class and detail.
- **Test run record** `<build_dir>/test-results/runs/<run-id>.json`:
  - identity and scope: `run_id`, `runner_version` (2), `tier`, `scope`,
    `changed`, `head`, `describe`;
  - `provenance`: Python, platform, git and tool versions;
  - the request: `request_fingerprint`, `request_code_paths`;
  - results: `classification`, `groups`, `jobs_detail` (every case, plus
    the log path; the log of a job that did not pass is kept in
    `runs/<run-id>/`).
- **CI record** `<build_dir>/ci-results/<id>.json` with steps, overall,
  head, dirty, host and seconds.
- **Audit run** `.audit/runs/AUDIT-YYYYMMDD-NNN/`.

---

## 6. Agent / Processing Model

### 6.1 Identity and liveness

- **Identity:** `export AGENT_ID=$(python3 scripts/claim.py --new-agent-id)`
  gives `agent-<host>-<time>-<hex>`. uid/pid-shaped, blank or overlong ids
  are refused.
- **Session process:** `export AGENT_OWNER_PID=<pid of the session's
  long-lived process>`, or `--owner-pid`. Every owner operation records host,
  boot id, pid and kernel start time.
- **Verdicts** (`agent_process.claim_liveness`):
  - **ALIVE:** the session process, or a `claim.py --run` step, is verified
    running.
  - **DEAD:** the recorded process is verified gone (exited, pid reused,
    other boot) and the claim was silent for `claims.dead_grace_minutes`.
  - **ACTIVE:** unverifiable, heartbeat within `claims.stale_hours`.
  - **STALE:** unverifiable and silent longer.
- **Keeping a claim alive:** long steps run through
  `claim.py --run <file> -- <cmd>` (ALIVE while running, heartbeat
  refreshed). `run_tests.py --request` refreshes the heartbeat itself.

### 6.2 Claiming and ownership

- **`--claim-request`** runs in one critical section:
  1. create the lock (O_EXCL);
  2. move to `processing`;
  3. register the request files.

  What happens depends on who holds the claim:
  - **the owner again:** completes an interrupted claim, or RESUMES a parked
    needs-review request;
  - **another agent:** gets exit 1 (owned) or 2 (stale or orphan), except
    for a parked needs-review claim, which it may TAKE OVER (recorded).
- **`--own`** registers files: exactly one file per path, never a directory.
  Modes:
  - `exclusive` (default);
  - `shared`: manifests, committed line by line by the request's tokens;
  - `concurrent`: ordinary content, committed as the request's delta.

  An already-modified path needs `--adopt-existing-changes`.
- **`--snapshot` variants:** `--rebaseline`, `--from-head`, `--from-file` as
  before.
- **Worktrees:** `--create-worktree` gives a per-request worktree. It is
  integrated manually (§11).

### 6.3 Recovery

- **Sweep:** `claim.py --sweep` (or `devsys.py sweep`) diagnoses. `--apply`
  performs only the safe repairs:
  - finish an interrupted claim of a live owner;
  - drop an interrupted claim of a gone owner with nothing uncommitted;
  - synchronise a claim's state;
  - move a lone sidecar;
  - remove gone work locks;
  - requeue a verified-DEAD owner's request with nothing uncommitted.

  The repairs are logged. Everything else is a finding.
- **Explicit recovery:** `--recover-request <file> --reason …` transfers
  ownership atomically, keeping history, paths and baselines.
  - It is refused for a verified-ALIVE owner, even with `--force-active`.
  - An ACTIVE owner needs `--force-active`.
  - DEAD, STALE and orphaned requests are recoverable.
- **Release:** `--release-request` refuses while an owned path is
  uncommitted. `--abandon <reason>` leaves the changes behind, unowned and
  recorded.

### 6.4 Processing

`devsys.py next --claim` or `process_next_request.py` starts a request. The
agent then follows §2.1. Each tool refuses out-of-order use:

| Refusal | Exit |
|---|---|
| `work.py` without owning the claim | 3 |
| a completion without a gate record | 9 |
| a commit of unowned paths | 3 |
| a release with uncommitted work | 4 |
| a rejection without a reason | 6 |

### 6.5 Escalation and failure

- **Escalating:**
  - `work.py transition W --to needs-review --reason …`;
  - `claim.py --needs-review-request <file> --reason …`;
  - then commit, and optionally release.
- **Human resolution:** the owner resumes, or another agent takes over.
- **Failed tests:** the work stays in verification or active, and evidence
  is recorded with `--status fail`.
- **Process changes:** a request that registers a protected artifact needs
  `Type: process-change` work with a `Decision:` ADR (gate), and must be a
  PROCESS-CHANGE request (`invariant_check --strict`).

### 6.6 Concurrency guarantees (enforced and tested)

- **Unique ids:** concurrent callers never share an id, and ids of deleted
  or archived records and of fetched-branch commits are never reused. A
  fresh instance starts at 1.
- **One winner:** exactly one of N concurrent claim, transition or recovery
  attempts succeeds. Exactly one of N identical intake submissions is
  created, and concurrent follow-ups are all kept.
- **Commits:** a commit contains only its request's own delta. Others'
  hunks, including a human's unrelated edits, stay uncommitted, and overlaps
  are refused. A shared manifest is changed only on the request's lines, and
  HEAD moving mid-commit aborts the commit.
- **Liveness:** a verified-ALIVE owner can never be displaced. Automatic
  repair never takes uncommitted work.
- **Scope:** one clone and its worktrees. Separate clones coordinate only
  through git (id scan of fetched refs).

---

## 7. Audit / Evidence Model

### 7.1 Evidence and the gate

**Durable proof:**

- `evidence/VER-*.md`, with a row in `evidence.txt`;
- `evidence/VER-*/run-<id>.json`, copies of the run records that outlive
  the build tree;
- `evidence/gates/<W>.json`, the gate history.

**The gate FAILs when:**

- the work record is missing or duplicated, or is not
  active / verification / completed;
- the `**Request:**` link is missing or dangling, the request is not
  processing / needs-review / completed, or the caller is not its owner;
- a cited requirement is undefined, or a cited ADR is missing;
- evidence is missing or unlinked, or its status (header or manifest) is
  not passing;
- the request changed code and has no linked run that is PASS, executed and
  at least `min_tier`;
- a linked run is FAIL, EMPTY or INTERRUPTED, or its record is missing
  where a run is needed;
- the fingerprint differs from the latest request-scoped run;
- checklist items are open;
- a protected artifact is registered without process-change work and an
  ADR;
- drift or doc-integrity reports a FAIL finding;
- when configured, the audit profile has no clean run on HEAD.

Keyword checks (regression, docs, traceability) are WARN.

**What requires the record:**

- `--record` needs the claim's owner. The record is registered in the claim
  and committed with the request.
- Completion (`work.py`, `--complete-request`) and the commit of a completed
  request require the latest record to be PASS on the current fingerprint.
- `verify-commit` re-checks this from the commit tree at any later time.
- Trailers link each commit to its request, agent, work, gate record and
  test runs.

**Crash evidence:** `crash_evidence.py` finds the newest core
(`paths.crash_dumps`, `crash.core_glob`), logs and recent changes, and
appends them under the request lock.

### 7.2 Consistency audits

Four checks are t0 suites of every tiered run and steps of `devsys.py ci`:

- `drift_detect.py`;
- `doc_integrity.py`;
- `invariant_check.py`;
- `verify_development_system.sh`.

`health_check.py` aggregates them with a time budget.

### 7.3 Architecture / conformance audit

- **Profiles:** `quick` / `standard` / `baseline` by cost.
- **States:** PASS; FAIL (a gate-severity rule violated); DRIFT (an
  advisory rule violated); UNKNOWN (cannot be established); SKIPPED (not in
  the profile, or NOT CONFIGURED).
  - A tool's WARN is PASS, with the warnings shown.
  - A rule declared `severity: warn` never fails.
- **Runs:**
  - read-only, with a self-check;
  - compared with the previous run;
  - retention of 30 runs, baselines kept.
- **Exit code:** advisory (exit 0) unless `--fail-on fail|drift|unknown`.
- **Without git:** it runs on a directory that is not a git repository.

---

## 8. Build / Test Integration

### 8.1 Build

`devsys_build.py` (`devsys.py build …`, `run_tests.py --build`,
`devsys.py ci`) runs the adapter from `build.system`:

| System | Configure | Build | Clean |
|---|---|---|---|
| `none` | — | NOT CONFIGURED, exit 10 | — |
| `make` | — | `make -C <build_dir with Makefile, else source_dir> -j<jobs>` | `make clean` |
| `cmake` | `cmake -S <source_dir> -B <build_dir>` | `cmake --build <build_dir> -j <jobs>` | `--target clean` |
| `qmake` | `qmake6\|qmake <root_project>` in `<build_dir>` | `make -C <build_dir>` | `make clean` |
| `custom` | `build.configure` | `build.build` | `build.clean` |

- Any adapter command can be overridden with an argv list (`{build_dir}`,
  `{source_dir}`, `{jobs}`).
- `build` configures an unconfigured tree first, and `fresh` deletes the
  tree.
- Each step is bounded by `build.timeout_seconds`.
- Failures are classified TOOL, LINK, DEPENDENCY, RESOURCE, SOURCE,
  ENVIRONMENT, TIMEOUT or UNKNOWN from the output.

### 8.2 Tiers and lanes

- `t0` ⊂ `t1` ⊂ `t2` ⊂ `t3`:
  - **t0** is static and process checks;
  - **t1** adds the unit groups of affected suites;
  - **t2** adds their integration / GUI groups, and is the default gate
    (`completion.min_tier`);
  - **t3** runs everything, affected or not.
- Lanes `perf`, `stress`, `soak`, `live`, `package` and `release` run only
  when named.
- Unit, integration, system, package and release verification are separate
  groups and lanes of the registry.

### 8.3 Selection, execution, results

- **Changed paths:** a request's registered paths (`--request`), `--base`,
  `--paths`, or the working-tree diff.
- **Selection:** the qmake graph (`tests.selection` qmake/auto with a root
  `.pro`), or each suite's `sources` / `inputs` globs. An unmapped
  non-documentation path selects everything.
- **Stale and cached groups:**
  - a binary older than its inputs is STALE (not run);
  - a group identical to its last PASS is CACHED (build id or sha256 of the
    binary, plus inputs, command and environment).
- **Execution:**
  - parallel workers; shards balanced by recorded durations;
  - private XDG / TMP directories per job, each in its own process group
    (`devsys_platform`);
  - a `wrapper` (e.g. xvfb-run).
- **Results:** exit code, `PASS|FAIL|SKIP|TIMEOUT <name>` lines, JSON lines
  (`PROJECT_TEST_RESULTS`), JUnit XML (`PROJECT_TEST_JUNIT`) or TAP.
- **Job outcomes:**
  - PASS and CACHED;
  - FAIL, TIMEOUT, CRASH (the case named; the shard continues);
  - FLAKY (passed alone), EXPECTED (quarantined until expiry);
  - STALE, MISSING, BLOCKED (unmet `requires`), ENVIRONMENTAL (binary
    replaced mid-run), NO_TESTS.
- **Run classification:** FAIL > BLOCKED > **EMPTY** (nothing executed) >
  PASS (FLAKY / EXPECTED) > PASS. A signal gives **INTERRUPTED**.
- **Exit codes:** 0 PASS, 1 FAIL, 3 BLOCKED, 4 EMPTY, 130 INTERRUPTED.
  EMPTY and INTERRUPTED never satisfy the gate.

### 8.4 Evidence link

- **Run record:** every run writes one with provenance.
- **Request-scoped runs:**
  - they carry the request fingerprint;
  - `evidence.py --test-run` copies the record into evidence;
  - the gate reads it from either place;
  - the commit trailer names it.

### 8.5 Packaging and deployment

The `package` and `release` lanes are placeholders that the project fills in.
The framework ships no deployment mechanism, and packaging does not re-run
the development suites (ADR-000186).

### 8.6 CI entry point

`python3 scripts/devsys.py ci [--tier t2] [--base REF] [--no-build]
[--audit standard|none] [--verify-commits HEAD|A..B|none]` runs, in order:

1. configuration validity;
2. structure;
3. the build adapter;
4. the test tier;
5. drift / doc integrity / invariants;
6. the architecture audit (`--fail-on fail`);
7. `verify-commit` over the range.

Each step is PASS / FAIL / SKIPPED / UNKNOWN. The record goes to
`<build_dir>/ci-results/`. Exit codes: 0 PASS, 1 FAIL, 3 UNKNOWN. It claims,
moves and commits nothing.

`verify-commit` reports, for a completed request's commit:

| Result | Meaning |
|---|---|
| VERIFIED | the commit's tree re-fingerprints to its gate record |
| WAIVED | completion was waived, with a recorded reason |
| NOT_APPLICABLE | a rejection, a parking, or a commit without request trailers |
| FAIL | the gate record is missing or not PASS, or the commit holds different code |
| UNKNOWN | the gate record predates gate version 3 |

There is still no hosted CI service. A CI job calls `devsys.py ci` on a
clean checkout. Git hooks never run, because `commit_request.py` uses
`commit-tree`; enforcement lives in the tools.

---

## 9. Dependencies

| Dependency | Used by | Required? |
|---|---|---|
| Python ≥ 3.9, standard library | everything | yes (`doctor` checks) |
| git ≥ 2.20 (`rev-parse --git-common-dir`, `hash-object`, `merge-file`, `commit-tree`, `update-ref`, `cat-file`, `for-each-ref`, `worktree`) | ids, claims, commits, selection, verify-commit, audit | yes, for the lifecycle; the audit and doc checks also run without git |
| bash, coreutils | `verify_development_system.sh`, `tools/audit` callers | yes |
| `/proc` (Linux) | boot id and process start time for DEAD/ALIVE verification | optional: without it liveness degrades safely (`portability.md`) |
| make / cmake / qmake / compilers | the chosen build adapter | per project |
| `readelf` | build-id cache keys (sha256 fallback) | optional |
| `xvfb-run`, gdb, `file` | GUI wrappers, crash investigation | optional |
| A coding-agent CLI speaking the JSON-lines protocol | workers (`workers.agent.command`; `docs/process/agent-profiles.md`) | only to run workers |
| PySide6 (with QtUiTools) | the control panel `tools/panel/` and its tests (skipped without it) | optional |

**Environment variables:**

| Variable | Effect |
|---|---|
| `AGENT_ID` | the agent identity (or `--agent-id`) |
| `AGENT_OWNER_PID` | the session process recorded for liveness (or `--owner-pid`) |
| `AGENT_PROCESS_ROOT` | overrides the repository root (tests) |
| `AUDIT_ROOT` | overrides the audit's root |
| `AUDIT_AI_COMMAND`, `AUDIT_AI_TIMEOUT` | the audit's optional AI step |
| `DEVSYS_SKIP_UNITTESTS=1` | the structure check skips the unit suite |
| `PROJECT_TEST_DATA`, `PROJECT_LIVE_HW_ARGS` | opt-ins for `testdata` / `hardware` requirements (names configurable) |
| `PROJECT_TEST_{LIST,CASES,SHARD,RESULTS,JUNIT}` | the runner contract, set per job |
| `TMPDIR`, `XDG_*` | set per job |

---

## 10. Configuration

`devsys.json` holds only what differs from the defaults in
`scripts/devsys_config.py`, deep-merged. `docs/process/configuration.md`
documents every key. The main groups are:

| Group | Controls |
|---|---|
| `project` | name, source roots (the default scope of architecture rules) |
| `paths` | scratch, build tree, crash dumps (relative or absolute) |
| `claims` | stale hours, DEAD grace, lock timeout, heartbeat interval |
| `ids` | request id prefix per source, git-ref scan |
| `intake` | duplicate refusal |
| `protected_artifacts` | files only a PROCESS-CHANGE request may change |
| `completion` | enforcement on transition, exempt types, min tier, passing evidence statuses, which gate rules apply, optional audit profile |
| `build` | adapter, dirs, jobs, command overrides, timeout, generated-code staleness rules |
| `tests` | registry and quarantine files, selection mode, qmake root and globals, no-test patterns, Qt 5 guard, scrubbed env, requirement opt-in variables |
| `doc_integrity` | FAIL codes, stale paths, obsolete terms |
| `invariants` | declared architecture rules: `forbid_pattern`, `require_pattern`, `forbid_dependency`, `layers` |
| `workers` | max workers, isolation (shared / worktree), attempt limits, excluded types, the agent command / scrubbed env / rate-limit patterns / prompt, validation steps |
| `paths.project_root` | the project the system serves, relative to the system directory (`.`; `..` as `<project>/<system-directory>/`) |
| `preflight`, `requirement_impact`, `crash`, `drift`, `health`, `structure` | keywords, impact domains, crash locations, unverified-completion level, check budget, required scaffold and extra structural checks |

Other configuration files are `scripts/test_manifest.json` (suites, tiers,
groups, inputs / sources, requirements, shards, wrappers, result format),
`scripts/test_quarantine.json` and `.audit/manifest.json` (audit checks,
profiles, severities).

`devsys.py doctor` names every setting still NOT CONFIGURED: template
placeholders `<NAME>`, empty source roots, build system `none`, no
invariants.

**Exit codes, common to the tools:**

| Code | Meaning |
|---|---|
| 0 | ok |
| 1 | conflict / FAIL |
| 2 | stale / orphan |
| 3 | not owner (`ci` and `verify-commit`: UNKNOWN) |
| 4 | bad state (`run_tests`: EMPTY) |
| 5 | not found |
| 6 | invalid |
| 7 | collision / duplicate |
| 8 | busy |
| 9 | gate required |
| 10 | not configured |
| 130 | interrupted |

---

## 11. Known Limitations

These are the limits that remain after ADR-000199. Everything the original
§11 listed is either fixed (§14) or restated here.

1. **Worktree integration is manual.** `--create-worktree` isolates a
   request, but committing on its branch and publishing to `main` (rebase,
   `update-ref`) has no tool; `commit_request.py` from the main checkout
   treats the worktree's paths as unowned. The state checks do see worktree
   claims (main-worktree state directory).
2. **Coordination is per clone.** Claims, counters and locks live in one
   clone's `.agent/state/`. Separate clones avoid id reuse through the git-ref
   scan only for ids that were committed and fetched; there is no
   cross-machine claim.
3. **Liveness needs the session to name its process.** Without
   `AGENT_OWNER_PID`, a claim is judged by its heartbeat (ACTIVE / STALE) as
   before. A reused pid on systems without `/proc` start times can keep a dead
   claim ALIVE (blocks recovery, never enables a wrong one).
4. **The gate's semantic checks are still partly textual.** Regression
   impact, documentation and traceability are keyword WARNs; the hard rules
   are structural (§7.1). The fingerprint covers the request's registered code
   paths, not documentation or process records (`tests.no_test_patterns`), so
   a Markdown input of a suite changed after its run does not fail the gate
   (roadmap COR-20).
5. **Classification is English-regex heuristics** (conservative; can
   misclassify). The type vocabulary is fixed in code.
6. **Runtime journals grow without bound** (`claims-history.jsonl`,
   `transitions.jsonl`, `sweeps.jsonl`, `id-allocations.log`); they are
   untracked and small, but nothing rotates them (roadmap CON-06).
7. **Build and run artifacts have no retention policy** (`build-results/`,
   `test-results/runs/`, `ci-results/`); evidence copies what must be kept.
8. **Windows is not supported.** The platform layer has best-effort branches
   that are untested; `verify_development_system.sh` needs bash
   (`portability.md`). macOS is expected to work with heartbeat-and-existence
   liveness but was not tested.
9. **Worker Orchestrator scope** (§15, ADR-000200):
   - **Worktree isolation** runs each request in its own worktree and branch,
     but publishing request branches to `main` is manual (COR-14), and
     request transitions happen in the main checkout.
   - **Not ported:** the former engine's supervisor review, run audit,
     build-tree provenance check and per-run named-ticket lists are
     deferred.
   - **Scheduling is deliberately simple:** each worker takes the next
     eligible request; there are no dependencies between requests.
   - **Untested with a real agent:** the vendor agent profile
     (`agent-profiles.md`) is configuration carried over from the former
     engine and was not exercised here (the tests use a stand-in agent).
9a. **Project-subdirectory installation is prepared, not complete.**
    `paths.project_root` and the workers are project-relative, but claims
    and owned-path commits still compute repository paths relative to the
    system directory. Installing as `<project>/<system-directory>/` needs
    those paths made relative to the git top level first.
10. **Historical citations.** The extracted ADRs, script comments and the
    engine requirements cite the source project's REQ/WORK/VER ids as
    provenance; they do not resolve here (`docs/decisions/README.md`).
11. **Some tools are exercised only indirectly** (`status_report.py`,
    `requirement_impact.py`, `worktree_check.py`, `crash_evidence.py`
    beyond its fixed `NameError`); their behaviour is unchanged from the
    extraction apart from configuration.
12. **The audit's placeholder checks stay UNKNOWN** until the project adds
    them (`static.warning-policy`; `req.verification-coverage` is UNKNOWN by
    nature).

---

## 12. Extraction Notes

### 12.1 System boundary

The boundary was found by tracing the lifecycle scripts, their imports and
subprocess calls, the documents they read and require, the documentation
entry points, the runtime-state `.gitignore` rules and the requirement and
ADR owners of `REQ-DEVOPS`, `ARCH-AUDIT` and `TICKET-ENGINE`. 111 files were
copied, byte-identical at copy time, then sanitized; `.gitignore`,
`docs/architecture/README.md`, `work/active/.gitkeep`, the claims directory
and this document were created.

**Excluded:** application source, build files, test projects and protos;
the project's own requests, work, evidence, non-process ADRs, requirement
documents and history; application build/packaging/architecture scripts and
their tests; deployment and user-documentation tooling; live runtime state
(including another session's live claim); a local permission file; **the
external ticket engine** (a separate repository; excluded at the user's
direction — only its in-repo contract is kept). There were no git hooks.

### 12.2 Sanitization and generalization

The extraction replaced project names, paths and variables with placeholders
(`<PROJECT>`, `tmp/`, `build/debug`, `log/dump`, `PROJECT_TEST_*`,
`<ENGINE_HOME>`) and cut the project's invariants to a placeholder.
ADR-000199 then turned the remaining hard-coded choices into configuration:

- the qmake root and global files, the Qt 5 guard, the scrubbed environment,
  the crash locations, the keyword maps, the stale paths and obsolete terms,
  the structural scaffold and the protected artifacts are `devsys.json` keys;
- the invariants are declared rules instead of code;
- the bootstrap records (a rejected request, INT-000001, WORK-000001,
  VER-000001) the structural check required by name were removed — a fresh
  instance starts with empty stores and ids at 1;
- the suite-id special case (`"app"`) became a `filter_format` suite key.

Retained on purpose: ADR/REQ-DEVOPS numbers as provenance, the `REQ-DEVOPS`
namespace, qmake/Qt support as one build adapter and selection mode, the
engine's name inside its own contract.

### 12.3 Placeholders to fill when adopting

`devsys.py doctor` lists the configuration still NOT CONFIGURED. Beyond
`devsys.json`: the PROJECT-SPECIFIC sections of `AGENTS.md`, `docs/`,
`scripts/test_manifest.json` (`package` lane: `<PACKAGE_STAGING_DIR>`,
`<PROJECT_PACKAGE_VALIDATE_SCRIPT>`, `<PROJECT_PACKAGE_SMOKE_SCRIPT>`;
`live_scripts`: `<LIVE_SCRIPTS_DIR>`, `<APP_PROJECT>`) and
`.audit/manifest.json` (`project`, module map, `_example_checks`),
`<ENGINE_HOME>` in the engine documents, and the project's own requirements,
architecture documents and test suites.

### 12.4 Dependencies on the original environment

None in executable code: a search for the source project's name, paths,
binaries, services, hardware terms and the engine path finds only generic
words and synthetic fixture names. Nothing reads the original tree.

---

## 13. Verification

### 13.1 Self-tests

| Suite | Modules | What it proves |
|---|---|---|
| `scripts` (t1) | 15 modules under `scripts/tests/` | the whole lifecycle toolchain; see `docs/verification/tests.md` |
| — `test_lifecycle_hardening` | liveness, sweep, recovery, transitions, gate / commit binding, verify-commit, work / evidence tools, intake, ids, nested roots | ADR-000199 decisions 2–5, 7 |
| — `test_devsys_tooling` | configuration, bootstrap, build adapters, invariants, audits, health, orchestrator results, preflight | decisions 1, 6, 8 and the B-regressions |
| — `test_end_to_end` | the acceptance scenario on a freshly installed C project | §13.3 |
| — `test_workers` | the Worker Orchestrator with a stand-in agent: intake and priority, slots and control, gated completion, retries, concurrency, recovery, worktree isolation, vendor identity, `paths.project_root` | WORKER-001..013, §13.5 |
| — `test_worker_panel` | the Qt control panel, headless | WORKER-011 |
| — the extracted modules | concurrency, commit boundaries, classification, gate checklists and test-run linkage, protection, doc scope, drift, submit-only, orchestrator | the pre-existing contract (updated where ADR-000199 changed behaviour deliberately) |
| `audit_py` (t1) | `tools/audit_system/tests` | audit states, parsers, builtins, runs, comparison, retention, read-only, `Levels` (gate → FAIL, NOT CONFIGURED → SKIPPED, `--fail-on`, no git, `arch.rules`) |

### 13.2 Defects found by running the framework on a fresh project

Running the framework on a freshly installed example project (make + C +
TAP), including its own self-tests inside that project, found these defects. Each is fixed and has a regression test:

| # | Defect | Regression test |
|---|---|---|
| B1 | `doctor` reported `<stdio.h>` in an invariant regex as a NOT CONFIGURED placeholder | `test_devsys_tooling.Configuration.test_placeholders_ignore_regular_expressions` |
| B2 | `init` did not git-ignore the build tree, so build and run records were committable | `Bootstrap.test_install_init_doctor_on_a_fresh_project` |
| B3 | The manifest consistency test required a qmake `project` on every binary suite (KeyError in globs mode) | `Orchestrator.test_globs_mode_binary_suite_needs_sources_not_a_qmake_project` |
| B4 | Run records outside `build/*` were invisible to the gate and to evidence | `Orchestrator.test_configured_build_tree_is_found_by_gate_and_evidence` |
| B5 | Failed jobs' logs lived only in the run scratch (the system temp dir for long paths) | `Orchestrator.test_tap_and_junit_cases_are_counted_and_failures_seen` |
| B6 | The git-ref id scan took request ids *cited* in manifests as issued, so a fresh project started at `-019` | `test_lifecycle_hardening.Intake.test_fresh_repository_ignores_request_ids_cited_in_manifests`, `Bootstrap…fresh_project` |
| B7 | Next-eligible ordered same-priority requests by slug, not arrival | `Intake.test_next_is_priority_then_arrival` |
| B8 | Preflight warned about the intake's own classification boilerplate in every request | `Preflight.test_intake_boilerplate_is_not_a_requirement_reference` |
| B9 | The framework self-test suite re-ran on every project requirement edit (over-broad inputs) | manifest change; `test_end_to_end` plans |
| B10 | The manifest upsert dropped the header's trailing blank line, which left a non-request hunk dirty after every commit and blocked release | `WorkTool.test_evidence_links_work_and_keeps_manifest_bytes`, `GateAndCommit.test_full_lifecycle_with_trailers`, `test_end_to_end` (no NOTE lines) |
| B11 | `run_tests.py` crashed at argument parsing when `paths.build_dir` was absolute and outside the repo | `Orchestrator.test_absolute_build_and_dump_directories_outside_the_repository` |
| B12 | `install` copied the source instance's own `devsys.json` (and would have copied its suites, requirement rows and ADRs), so the self-tests failed inside an installed project; the instance files now come from `scripts/templates/` | `Templates.test_install_from_a_configured_project_does_not_leak_it`, `Templates.test_distribution_instance_files_equal_their_templates` |

Further issues found and fixed while hardening, with regression tests:

- **COR-18:** a silent index-refresh failure is now reported.
- **Dead settings wired:** `ids.request_sources` and `project.source_roots`
  were declared but unused; both are now read.
- **Advisory rules:** a `severity: warn` rule no longer fails the audit.

### 13.3 Final validation

The final validation ran last, after every change. It used a clean scratch
environment:

1. The system directory was copied to a directory that is not a git
   repository.
2. From that copy, `devsys.py install --git-init` created a new project.
3. The project was configured as a small C library: make build, a TAP test
   suite, requirements REQ-CALC-*, two invariants, and a 3-second DEAD grace
   so the dead-agent step does not wait 5 minutes.

A self-checking script (97 checks, each comparing an exit code or an
output) drove the steps below. The same scenario runs as the automated test
`scripts/tests/test_end_to_end.py`.

| # | Required demonstration | How it was shown | Result |
|---|---|---|---|
| 1 | Bootstrap a fresh instance | `devsys.py install --target … --project-name calc --build-system make --git-init`; no `__pycache__` copied | PASS |
| 2 | Configure a generic project | `devsys.json` (source roots, globs selection, invariants), suite `calc` (TAP), requirements; `--validate` OK; `doctor` PASS with nothing NOT CONFIGURED; make adapter builds; build tree git-ignored | PASS |
| 3 | Multiple and concurrent requests | 5 intakes released at one instant: 4 created with ids 001..004 (no gap, no reuse), the identical fifth refused DUPLICATE (exit 7) | PASS |
| 4 | Process the lifecycle | next eligible = first arrival; claims (a second claimant refused, exit 1); preflight; path registration; `work.py`; `--run` build+t2; `evidence.py`; transitions; gate record; complete; commit; release; needs-review parking; rejection (without a reason refused, exit 6) | PASS |
| 5 | Simulate a dead agent | agent B's session process killed with uncommitted edits; `devsys.py status` shows it **DEAD** and the request recoverable | PASS |
| 6 | Detect and recover | `--sweep --apply` reports it and repairs nothing (uncommitted work); the survivor cannot take B's file (exit 1); a verified-ALIVE owner cannot be taken over even with `--force-active` (exit 1); agent C `--recover-request --reason` keeps B's paths and edits | PASS |
| 7 | Resume processing | C finishes B's request; the survivor A then registers the freed file and finishes its own; a parked needs-review request is TAKEN OVER by agent E and completed | PASS |
| 8 | Run the tiers | t2 per request (with `--build`); t0, t1, t2, t3 `--all` PASS, t1 executed the framework's 280 self-tests **inside the installed project** (103 s) and the audit tests; an empty `perf` lane is EMPTY (exit 4), not PASS | PASS |
| 9 | Produce evidence | `evidence.py` records with run records copied into `evidence/VER-*/`; `attach-run` after a re-test | PASS |
| 10 | Run audits | drift, doc_integrity, invariants (2 rules) PASS; `./tools/audit standard --fail-on fail`: 11 PASS, 0 FAIL, 0 DRIFT, 2 UNKNOWN (placeholders), 3 SKIPPED | PASS |
| 11 | Pass the gate | completing before any gate refused (exit 9); a code change after the tested run → gate FAIL naming the stale run, `work.py -> completed` refused (exit 9); re-test → PASS; `verify-commit` over the five request commits: 3 VERIFIED (two code, one documentation), 2 NOT_APPLICABLE (parking, rejection); `devsys.py ci --tier t2 --verify-commits HEAD~5..HEAD`: **CI: PASS** | PASS |
| 12 | Commit only owned changes | every commit holds only its request's paths; no `NOTE` of work left behind; trailers name request, agent, work, gate record, test runs | PASS |
| 13 | Leave unrelated changes untouched | a human's edit of a tracked file and a new untracked file: byte-identical (sha256) at the end, the only entries of `git status`, in no request commit | PASS |
| 14 | Pass the full framework suite | on a scratch copy of the final code: `scripts/tests` **280 tests OK** (4 skipped), `tools/audit_system/tests` **26 OK**; and inside the installed project (row 8) | PASS |
| 15 | Leave the original project unchanged | see §13.4 | PASS |

The first run of this validation found B12 (§13.2). Its fix, regression test
and the re-run (97/97 checks) came before these results.

### 13.4 Results

| Check | Result |
|---|---|
| `python3 -m unittest discover -s scripts/tests` | 280 tests, OK, 4 skipped. The skips are the classifier's real-record fixtures, which name source-project requests; they are skipped when absent, as in the extraction baseline. |
| `python3 -m unittest discover -s tools/audit_system/tests` | 26 tests, OK |
| New tests in this work | 81 in `scripts/tests` (`test_lifecycle_hardening` 50, `test_devsys_tooling` 30, `test_end_to_end` 1) + 5 audit tests (`Levels`); the extracted tests were updated where ADR-000199 changed behaviour deliberately (gate required, reasons required, release refusal, duplicate refusal, messages) |
| Final validation (§13.3) | 97 / 97 checks PASS |
| Final audit **of the system directory itself** (in place; not a git repository, so non-git mode) | `drift_detect` PASS · `doc_integrity` PASS · `invariant_check` NOT CONFIGURED (the template declares no rules) · `devsys_config --validate` OK · `verify-structure` OK · `./tools/audit standard --fail-on fail` exit 0: **8 PASS, 0 FAIL, 0 DRIFT, 3 UNKNOWN, 5 SKIPPED**. UNKNOWN: requirement coverage by citation (38 of 69 ids cited by tests), the project's warning-policy placeholder, no recorded test results in the template's build tree. SKIPPED: `arch.rules` and `build.health` NOT CONFIGURED in the template, the package and runtime lanes, the unit tier (not in `standard`). Read-only self-check PASS. The only change to the directory is the audit's own git-ignored `.audit/runs/` (AUDIT-20260924-001, and -002 re-run after this section was written: same results). No `__pycache__` anywhere. |
| Original source project (`<SOURCE_PROJECT>`) | No command wrote into it; every scratch tree was under the session scratchpad. HEAD moved `da96d39d` → `2ccd10a0` through two commits by other concurrent agent sessions (`WORK-000325`, `agent-dev-…872e21`; `WORK-000322`, `agent-dev-…fb6cda`). Of the 111 extracted source files, 105 are byte-identical to the extraction checksums. The 6 that differ (`work.txt`, `evidence.txt`, `requirements.txt`, `decisions.txt`, `tests.md`, `test_quarantine.json`) were changed by those two commits and by the uncommitted work of a live claim (REQ-CHAT-20260921-025) held by another session. |

**Not verified:**

- The `live` / `perf` / `stress` / `soak` / `package` / `release` lanes with
  real suites. None is registered; the empty lane was shown to be EMPTY.
- The `baseline` audit profile and `--ai`.
- The qmake adapter against a real qmake project. Its selection code is
  covered by the extracted `test_run_tests` fixtures.
- Per-request worktrees end to end. Only the extracted concurrency tests
  cover them.
- macOS and Windows.
- The external ticket engine, which was not part of this system at the time.
  It has since been merged as the Worker Orchestrator (§13.5, §15).

### 13.5 Worker Orchestrator verification (merge phase 2)

Everything below ran on a git-initialised scratch copy of the final code,
because this directory is not a repository. The agent was the stand-in
`scripts/tests/fake_agent.py`. It speaks the JSON-lines protocol and does
its work with the real framework tools: work and evidence records, the gate
record, `--complete-request`, the owned-path commit and release.

| Check | Result |
|---|---|
| `python3 -m unittest discover -s scripts/tests` | 306 tests OK, 4 skipped (the classifier's real-record fixtures, as in §13.4). Run in full three times after the last fixes |
| `python3 -m unittest discover -s tools/audit_system/tests` | 26 tests OK |
| New tests in this phase | 26: `test_workers` 19, `test_worker_panel` 4, `test_lifecycle_hardening` 3 |
| Three workers, each completing a full gated lifecycle at the same time | 6 runs one after another, and 12 runs four at a time (under load). Each run: every request completed, every validation PASS, every commit VERIFIED, a clean tree, no claim left |
| Multi-worker demonstration: 5 requests from CRITICAL to LOW, 3 detached workers (`workers start`) | See the list below the table |
| This directory in place | `drift_detect` PASS, `doc_integrity` PASS, `invariant_check` NOT CONFIGURED, `devsys_config --validate` OK, `verify-structure` OK. `doctor` FAIL only for "not the top of a git work tree" (expected here), and NOT CONFIGURED placeholders, now including `workers.agent.command`. `./tools/audit standard` on the scratch copy: 8 PASS, 0 FAIL, 0 DRIFT, 3 UNKNOWN, 5 SKIPPED |
| Vendor identity | The vendor name appears only in `docs/process/agent-profiles.md`. No machine-specific path appears in the system (`test_workers.Identity`) |
| The originals | The imported orchestrator's source repository: all 4721 files byte-identical to the phase-1 checksums, HEAD unchanged. The source project: no command wrote into it. Its HEAD and status changed only through other sessions' commits |

The multi-worker demonstration showed:

- **Queue order:** CRITICAL, HIGH, NORMAL, NORMAL, LOW.
- **Concurrency:** at most 3 workers working at once, never on the same
  request.
- **Outcomes:**
  - the CRITICAL request was escalated by its agent to needs-review;
  - a PARTIAL attempt was retried and completed;
  - two requests completed;
  - a completion without a gate record was refused twice, then escalated
    by the worker.
- **Commits:** 3 VERIFIED; 3 NOT_APPLICABLE (made while the request was
  in processing / needs-review).
- **Records:** drift PASS and doc-integrity PASS.
- **Panel:** it showed the final state.

Running workers concurrently found these defects. Each is fixed and has a
regression test:

| # | Defect | Fix | Regression test |
|---|---|---|---|
| W1 | The gate's drift check read another agent's work record between its move and its manifest row (`WORK_STATE_MISMATCH`) | `work.py create` and `evidence.py create` write under the "work" lock (transitions already did); drift reads under it | `test_lifecycle_hardening.GateAndCommit.test_drift_reads_under_the_work_and_claims_locks` |
| W2 | The gate ran drift *without* the lock (only drift's CLI took it), and saw a request in `completed/` before its resolution was written (`UNVERIFIED_COMPLETION`) | `drift_detect.consistent_check()` takes the work lock, then the claims lock, for both the CLI and the gate | the same test; it fails without the fix |
| W3 | The gate crashed when another agent moved a request file while `doc_integrity` scanned | a file that vanished after the listing is skipped | `GateAndCommit.test_doc_integrity_skips_a_file_moved_while_it_scans` |
| W4 | A request committing the same file a second time conflicted with its own first commit (the baseline did not advance) | `commit_request` advances each committed path's baseline | `GateAndCommit.test_a_request_commits_the_same_file_twice` |
| W5 | Release was refused because shared manifests held other requests' rows | `uncommitted_owned_paths` attributes shared paths by the request's tokens; the worker uses it | `test_workers.WorkerModel.test_concurrent_workers_complete_through_the_gate` |
| W6 | A completed request with uncommitted work passed validation | `commit_binding` FAILs a completed request whose owned work is uncommitted | `WorkerModel.test_partial_keeps_the_claim_and_the_worker_resumes` (asserts the committed content and the release) |
| W7 | Git sanity warned about another request's committed paths, which looked staged while that commit refreshed the index | the window's other commits are excluded (and reported) | `test_concurrent_workers_complete_through_the_gate` (validation PASS) |
| W8 | A worker started by the panel served the framework's own root, not the panel's | `pool.start` passes the root to the worker | `WorkerModel.test_start_serves_the_given_root_not_the_callers` |
| W9 | `pool.start` dropped the running worker's process handle, so a long-running panel kept exited workers as zombies (a `ResourceWarning` in the suite) | a reaper thread waits for the worker | the suite runs without the warning |

**Not verified in this phase:**

- A real coding agent. The vendor profile in
  `docs/process/agent-profiles.md` has not been run against the installed
  CLI.
- Worktree isolation beyond the test in which each request gets its own
  worktree. Publishing a request branch is manual (COR-14).
- An installation as a project subdirectory end to end. Only the path
  resolution is tested (`test_project_root_is_relative_to_the_system_directory`).
- The panel on a real display. It was only run offscreen.

## 14. Roadmap implementation status

See `CI_CD_ROADMAP.md` §15 for the item-by-item status. Of the 79
findings, 52 are DONE, 15 PARTIAL and 12 DEFERRED. In short:

- **Critical items:** all implemented.
- **High items:** implemented, except the manual part of worktree
  integration (COR-14).
- **Deferred items:** Windows port, multi-machine coordination,
  issue-tracker and notification bridges, analytics, multilingual
  classification, retention and rotation of runtime logs and artifacts.

---

## 15. Worker Orchestrator (merged, ADR-000200)

The unattended request processor that used to run as a separate
installation was imported in merge phase 1 and merged in phase 2. It is now
the **Worker Orchestrator** of this system: `scripts/workers/`,
`devsys.py workers | queue | priority`, and the Qt control panel
`tools/panel/`. `docs/process/worker-orchestrator.md` describes it, and
WORKER-001..013 specify it.

**The model:**

- **One source of truth:** a request is the only unit of work, and its
  state lives only in the request lifecycle. There is no ledger and no
  worker-side ticket status. An escalation is a `needs-review` transition
  through the framework, with the reason.
- **Workers:** up to `workers.max_workers` workers run as separate
  processes, each with a fixed agent id and its own runtime directory.
- **One request each:** each worker has at most one active request. It
  claims through the framework with its own process as the claim's owner,
  so the claim is verified ALIVE / DEAD. Assignment is serialised on top of
  the atomic claim, so two workers never share a request.
- **Isolation:** `shared` (owned-path commits in one checkout) or
  `worktree` (the request's own worktree and branch; publishing manual).
- **Gates unchanged:** the prompt teaches the gated lifecycle, validation
  re-checks commit binding, and a refused completion is an attempt without
  completion.
- **Vendor-neutral:** the agent is configuration speaking a JSON-lines
  protocol. The vendor-specific profile is the one documented exception
  (`docs/process/agent-profiles.md`).
- **Interfaces:** the API (`workers.pool`) is shared by the CLI and the
  panel. The panel is a client only.

What was merged, renamed, retired or deferred, component by component, is
recorded in `docs/history/worker-orchestrator-merge.md`. The phase-2
verification is in §13.5.
