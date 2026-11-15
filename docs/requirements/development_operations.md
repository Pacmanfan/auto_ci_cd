# Autonomous Development Operating Contract

**Contract ID:** DEVOPS
**Status:** Normative
**Owner:** Repository development process
**Scope:** Request intake, requirements, decisions, work, tests, evidence, documentation, and autonomous agent state

This contract defines the durable development operating system for <PROJECT>. It governs how human requests become verified repository changes. It does not define application runtime behavior; subsystem behavior remains owned by the applicable domain requirement.

## 1. Canonical Object Model

The repository SHALL distinguish these objects:

| Object | Meaning | Canonical location |
|---|---|---|
| Request | Human statement of a desired behavior, capability, defect, or question | `requests/` |
| Intent | Interpreted problem, outcome, scope, and non-goals derived from a request | `requests/processing/` or the linked request record |
| Requirement | Normative, observable behavior the system must provide | `docs/requirements/` |
| Decision | Rationale and consequences of a significant architectural choice | `docs/decisions/` |
| Work item | Concrete implementation activity satisfying requirements | `work/` |
| Test | Executable verification of behavior | the project's test projects (registered in `scripts/test_manifest.json`) |
| Evidence | Recorded result of an executed verification | `evidence/` and `docs/audits/evidence/` |
| Documentation | Current explanatory or operational material | `docs/`, subsystem documentation, and focused READMEs |

### REQ-DEVOPS-001 — Object separation

Requests SHALL NOT be treated as requirements, work items, tests, evidence, or architectural decisions without an explicit conversion record. Work items and evidence SHALL NOT redefine normative behavior.

### REQ-DEVOPS-002 — One normative owner

Each externally observable behavior SHALL have one normative requirement owner. Related documents MAY link to that owner but SHALL NOT create competing copies.

## 2. Stable Identity and Indexes

### REQ-DEVOPS-003 — Stable identifiers

Every meaningful request, intent, requirement, decision, work item, test intent, and verification run SHALL have a stable identifier. Existing domain requirement identifiers remain valid; new process requirements use the `REQ-DEVOPS-*` namespace. Request, `WORK`, `VER` and `ADR` identifiers SHALL be unique for the whole lifetime of the record, across every state (inbox, processing, completed, rejected, needs-review, cancelled and any other store) and after deletion: they SHALL be allocated atomically by the monotonic allocator (`scripts/agent_process.py` `allocate_id`, CLI `scripts/allocate_id.py`, used by intake) that takes the maximum of a persistent counter and every existing id under an inter-process lock, never by scanning a subset of directories. An unexpected collision SHALL fail without overwriting, reusing or renumbering any record (ADR-000090). Verification: `scripts/tests/test_process_concurrency.py` `RequestIdAllocation`. The date in a request series (`REQ-<SRC>-YYYYMMDD`) and in the request file name SHALL be the local system date at creation (`scripts/agent_process.py` `request_date()`, the host's configured timezone), never the UTC date and never a value taken from the request text or supplied by the caller; the sidecar SHALL record `id_date`, `timezone`, `created` (local, with offset) and `created_utc` (amended 2026-09-22, REQ-DEVOPS-045, ADR-000173).

### REQ-DEVOPS-004 — Index manifests

The canonical object directories SHALL have small index/manifest files. An index SHALL point to the authoritative record and SHALL NOT replace the records with an unbounded prose database.

Required manifests are:

* `docs/requirements/requirements.txt`
* `docs/decisions/decisions.txt`
* `work/work.txt`
* `evidence/evidence.txt`
* `.agent/state/README.md` and its state files

## 3. Request Lifecycle

### REQ-DEVOPS-005 — Human request intake

A human SHALL be able to submit a plain Markdown request by placing it in `requests/inbox/` without creating requirements, decisions, work items, or tests manually. The intake README SHALL explain accepted format, processing, completion, rejection, and escalation.

### REQ-DEVOPS-006 — Autonomous lifecycle

An agent processing the next request SHALL follow:

```text
RECEIVED
  -> INTENT
  -> REQUIREMENT
  -> DECISION (when needed)
  -> WORK
  -> IMPLEMENT
  -> TEST
  -> VERIFY
  -> DOCUMENT
  -> COMPLETE
```

The agent SHALL persist the state transition and links between the objects.

### REQ-DEVOPS-007 — Conflict handling

Conflicting requirements, architecture documents, tests, or implementation evidence SHALL be recorded as a conflict. An agent SHALL identify the authoritative source and update the requirement or decision before proceeding; it SHALL NOT silently choose a contract.

## 4. Work and Verification

### REQ-DEVOPS-008 — Work traceability

Each meaningful work item SHALL identify its requirement(s), intended outcome, scope, affected subsystems, tests, verification, documentation, dependencies, and status. Active work SHALL be discoverable from `work/work.txt`.

### REQ-DEVOPS-009 — Verification evidence

Completion SHALL require recorded evidence containing the command or method, repository revision/dirty-state context, result, classification, and artifact paths where applicable. Compilation alone SHALL NOT constitute completion.

*Amended 2026-09-23 (REQ-DEVOPS-046, ADR-000186):* test execution evidence SHALL name the `scripts/run_tests.py` run(s) it rests on (`**Test-Run:** <run id>` in the work record or its VER record). The run record (`<build>/test-results/runs/<run id>.json`, kept with the evidence as `evidence/VER-NNNNNN/run-<run id>.json`) carries the revision, the changed paths, per-group status / time / cases and, for a request-scoped run, a fingerprint of the request's registered code; `scripts/completion_gate.py` SHALL fail a linked run that failed and a request whose code changed after its latest linked run, and SHALL warn when no run is linked.

### REQ-DEVOPS-010 — Completion gate

An item SHALL NOT be marked complete until its applicable requirement, implementation, test, verification evidence, documentation, and traceability links agree. Known pre-existing or environmental failures SHALL be classified explicitly rather than hidden.

## 5. Documentation and Agent Constitution

### REQ-DEVOPS-011 — Documentation authority

`docs/README.md` SHALL be the canonical documentation entry point. Current architecture, requirements, decisions, work, and evidence SHALL be separated. Historical material SHALL be retained under clearly marked historical locations and SHALL NOT be presented as current authority.

### REQ-DEVOPS-012 — Durable agent rules

`AGENTS.md` SHALL remain a concise operating constitution containing authority hierarchy, lifecycle, completion rules, repository-wide invariants, and diagnostic expectations. Detailed subsystem knowledge SHALL live in canonical documentation rather than being duplicated into `AGENTS.md`.

### REQ-DEVOPS-013 — Crash evidence

When investigating a crash or fatal signal, the agent SHALL inspect the most recent ELF crash artifact under the configured repository-relative `log/dump` path (or the logged fallback), using the matching executable and GDB before concluding. Logs alone are insufficient evidence.

### REQ-DEVOPS-014 — Unified request intake

Requests received through chat, command-line, inbox file, crash report, or automation SHALL converge at the same intake. A persistent canonical request artifact SHALL be created in `requests/inbox/` before implementation. `Source != Workflow` — source determines entry point only; lifecycle is unified. `scripts/create_request.py --source chat|crash|inbox` is the intake mechanism; it allocates the request id per REQ-DEVOPS-003 and creates the request files with exclusive creation, so concurrent intakes never share an id or overwrite a file. `scripts/process_next_request.py` selects next eligible from all sources. Every source SHALL pass through the same classification and validation stage (REQ-DEVOPS-045); there is no source-specific type assignment. An open request (inbox, processing, needs-review) with the same normalised statement SHALL be refused as a duplicate (exit 7) unless `--allow-duplicate` is given, and the duplicate check and the creation SHALL be one critical section, so of N identical concurrent submissions exactly one is created; a follow-up SHALL name exactly one request by id, SHALL be refused for a completed or rejected request (a new request instead), and concurrent follow-ups to one request SHALL all be kept (amended 2026-09-24, ADR-000199).

## 6. Phase 2 Safety Mechanisms

### REQ-DEVOPS-015 — Machine-operable state model

`docs/process/state-model.md` SHALL define the sole valid states and transitions for REQUEST, WORK, and VERIFICATION. Status strings outside that model SHALL be treated as drift.

### REQ-DEVOPS-016 — Claim / lock protection

An agent SHALL claim a request or work item via `scripts/claim.py` before processing. The claim (`.agent/state/claims/request-<file>.lock`) is the ownership record: request id, session-unique agent id (uid/pid defaults are refused), claim token, claim time, heartbeat, state, owned repository paths (exclusive or shared), id tokens, commits, optional worktree and recovery history. Claiming SHALL be atomic — checking for an existing owner, creating the claim and moving `inbox|needs-review -> processing` happen in one inter-process critical section — so exactly one of any number of concurrent claimants succeeds and the others receive the owner's identity (exit 1). Release and the transitions `processing -> completed|rejected|needs-review` SHALL be owner-only and no-clobber (`--complete-request`, `--reject-request`, `--needs-review-request`, `--release-request`); a request SHALL be in exactly one state directory. A repository path owned exclusively by one active request SHALL NOT be registered by another request in the same worktree, and an already-modified path nobody owns SHALL be adopted only explicitly (`--adopt-existing-changes`). A registration SHALL name exactly one file: `--own` SHALL refuse an existing directory, the repository root and a path written as a directory (trailing `/`) with a message telling the agent to register each file, and SHALL refuse before recording anything, so a call naming several paths registers none of them; adoption SHALL stay per file (amended 2026-09-24, REQ-CHAT-20260924-013, ADR-000197: a directory registration had owned none of the files written under it). Stale ownership is handled by REQ-DEVOPS-025 (ADR-000090). Verification: `scripts/tests/test_process_concurrency.py` `ClaimOwnership`, `DirectoryRegistration`. A claim SHALL record, when the session gives it (`--owner-pid` or `$AGENT_OWNER_PID`), the identity of the session's long-lived process (host, boot id, pid, kernel start time), and every reader SHALL report a liveness verdict: ALIVE (that process, or a step started with `claim.py --run`, verified running), DEAD (verified gone — exited, pid reused, earlier boot — and the claim silent for `claims.dead_grace_minutes`), ACTIVE (no verifiable process, heartbeat within `claims.stale_hours`) or STALE (silent longer); a verified-ALIVE owner SHALL NOT be taken over by any option. A request parked in needs-review by its owner MAY be claimed by another agent (TAKEN OVER, recorded); the owner re-claiming its own request SHALL resume it, and SHALL complete a claim interrupted before the request moved. `--release-request` SHALL be refused while an owned path is uncommitted, unless `--abandon <reason>` records that the changes are left behind unowned (amended 2026-09-24, ADR-000199).

### REQ-DEVOPS-017 — Idempotency

Before creating requirement, work, decision, or evidence artifacts, an agent SHALL search for existing corresponding artifacts. `find existing -> update/link` SHALL be preferred over `create duplicate`.

### REQ-DEVOPS-018 — Preflight check

Before implementation, an agent SHALL run `scripts/preflight.py` verifying request existence/interpretability, requirement non-duplication, architecture impact, work existence, dependencies, affected tests/docs. On FAIL, work SHALL NOT silently proceed.

### REQ-DEVOPS-019 — Completion gate

A work item SHALL NOT transition to `completed` unless `scripts/completion_gate.py` passes: requirement identified, implementation complete, tests created/updated, tests executed, results recorded, regression considered, documentation and traceability complete, final diff reviewed, no unresolved blocker. The gate SHALL be automated where practical. `completion_gate.py --record` SHALL append the verdict, the request fingerprint (a digest of the request's registered code paths), the linked test runs and the evidence to `evidence/gates/<WORK-ID>.json` (owner only; the file is registered in the claim). A gate FAIL SHALL include: a code-changing request without a linked run that is PASS, executed and of at least `completion.min_tier`; a run whose fingerprint differs from the current code; EMPTY or INTERRUPTED runs; undefined requirement or missing decision citations; a missing or non-passing evidence status; the caller not owning the request; drift or doc-integrity FAIL findings. `work.py transition --to completed`, `claim.py --complete-request` and `commit_request.py` of a completed request SHALL require a PASS record on the current fingerprint for every linked work item; a human override SHALL be explicit and recorded (`--waive-gate <reason>`); INVESTIGATION requests without work MAY complete with a reason (amended 2026-09-24, ADR-000199).

### REQ-DEVOPS-020 — Drift detection

`scripts/drift_detect.py` SHALL deterministically detect requirement with no work, work with no requirement, completed work without evidence, broken links, duplicate IDs, stale status, orphaned requests/work, contradictory authority. Work-state consistency, stale claims and evidence status are added by REQ-DEVOPS-044. It SHALL also report requests completed outside the tooling (no recorded resolution: UNVERIFIED_COMPLETION, level `drift.unverified_completion`), completed work without a PASS gate record, a request id held by several files, completed work whose evidence is not passing, a request sidecar away from its markdown, a claim whose recorded state differs from the request's directory, and generated state mirrors that differ from canonical state (MIRROR_STALE) (amended 2026-09-24, ADR-000199).

### REQ-DEVOPS-044 — Work-state consistency and open items (added 2026-09-21)

`scripts/drift_detect.py` SHALL also report: a WORK record whose state
directory disagrees with its `Status` header or its `work/work.txt` status
column (FAIL: the record contradicts itself); a WORK record whose linked
request exists in no request state directory (FAIL; the same request file in
another state is the same request, not drift); a WORK record still
`queued`/`active`/`blocked`/`needs-review` while its request is `completed` or
`rejected` (WARN); a `processing` request whose claim heartbeat is older than
the recovery threshold, 2 h (WARN); and an `evidence/evidence.txt` status whose
first word is outside the state model (WARN). `scripts/completion_gate.py`
SHALL fail a work record with an unchecked (`[ ]`) or partial (`[~]`)
checklist item, or a phase or item marked "not done" / "not started", unless
the same line says it was waived. An item is a list item or a table row;
headings, prose, inline `code` and fenced code blocks are narrative and are
not read as status, and a waiver word inside inline `code` does not waive.
The gate SHALL resolve the record by file name (`WORK-NNNNNN-<slug>.md`) in
any work state directory, never through another record that mentions the id,
SHALL name the record it gated, and SHALL fail when the id names no record or
more than one. Motivation: the REQ-CHAT-20260921-011
stuck-ticket audit found 14 finished WORK records left open, none reported.
The gate SHALL count as a work item's evidence only what is declared for it:
an `evidence/evidence.txt` row whose links column names the item, a VER
record named in the work record's `Evidence` header or whose own `Work`
header names the item; a VER record that merely mentions the id is not its
evidence (added 2026-09-23, ADR-000185).
Decision: ADR-000155 (amended by the 2026-09-23 human review: record
resolution, item scope). Verification: `scripts/tests/test_drift_work_state.py`,
`scripts/tests/test_completion_gate_checklists.py`; both run on synthetic
trees only, never on the shared repository.

### REQ-DEVOPS-021 — Requirement change impact

When a requirement changes, an agent SHALL produce an impact report (`scripts/requirement_impact.py`) identifying affected implementation, tests, work, architecture, and docs. It SHALL NOT silently modify all callers.

### REQ-DEVOPS-022 — Architectural invariant checking

Project-wide invariants (ownership, processing contracts, automation-interface authority, etc.) SHALL be separated from normative requirements and SHALL have automated or explicit audit checks (`scripts/invariant_check.py`). The project's rules SHALL be declared in `devsys.json` `invariants` (kinds `forbid_pattern`, `require_pattern`, `forbid_dependency`, `layers`; severity fail or warn) and evaluated by `scripts/invariant_check.py`, which SHALL report NOT CONFIGURED — never PASS — when no rule is declared; the framework SHALL embed no project-specific rule (amended 2026-09-24, ADR-000199).

### REQ-DEVOPS-023 — Process protection and no silent override

`docs/process/process-protection.md` SHALL define protected artifacts (`AGENTS.md`, lifecycle, completion/traceability rules). An agent SHALL NOT modify them as a side effect of application work; it SHALL create an explicit `process-change` work item + ADR. An agent SHALL never silently override requirements, decisions, contracts, or test expectations.

Enforcement (added 2026-09-23, ADR-000185): the explicit protected files SHALL be listed once (`devsys.json` `protected_artifacts`, read by `scripts/process_protection.py`; amended 2026-09-24, ADR-000199) and checked from what a request has registered in its claim, not from the working tree. `scripts/completion_gate.py` SHALL fail a work item whose request registered a protected artifact unless the work record is `Type: process-change` with a `Decision:` ADR; `scripts/invariant_check.py` SHALL report `PROTECTED_FILES_TOUCHED_WITHOUT_PROCESS_CHANGE` for a `processing` request that registered one and is not a `PROCESS-CHANGE` request, as a WARN and, with `--strict`, a FAIL. Verification: `scripts/tests/test_process_protection.py`.

### REQ-DEVOPS-024 — Human escalation

`docs/process/escalation.md` SHALL define conditions requiring `needs-review` and the escalation procedure. Ambiguity SHALL be escalated, not left as `blocked`.

### REQ-DEVOPS-025 — Failure recovery

The workflow SHALL survive agent crash, reboot, interrupted build, failed tests, stale locks, and incomplete docs. Recovery is documented in `docs/process/recovery.md` and starts with `scripts/health_check.py` + `scripts/claim.py --status`/`--recover`. A claim is stale when its heartbeat is older than the threshold (default 2 h); staleness SHALL only enable recovery, never cause it: `--recover` reports stale claims and orphaned `processing/` requests without removing anything, and ownership SHALL be transferred only by an explicit `scripts/claim.py --recover-request <file> --reason ...`, atomically, keeping the previous owner in the claim history and carrying its owned paths over. Recovery of an owner with a fresh heartbeat SHALL be refused unless `--force-active` is given (ADR-000090). Verification: `scripts/tests/test_process_concurrency.py` `Recovery`. `claim.py --sweep` SHALL report every inconsistency and, with `--apply`, perform only repairs that cannot lose or steal work: completing an interrupted claim of a live owner, dropping an interrupted claim of a gone owner that owns no uncommitted change, synchronising a claim's recorded state, moving a lone sidecar, removing work locks of gone owners, and requeueing the request of a verified-DEAD owner with nothing uncommitted; a DEAD owner with uncommitted work and every STALE owner SHALL be left to explicit recovery; applied repairs SHALL be logged (`.agent/state/runtime/sweeps.jsonl`) (amended 2026-09-24, ADR-000199).

### REQ-DEVOPS-026 — Scope, stop-when-done, and integrity

A scope guard (`scripts/preflight.py` + completion gate) SHALL detect scope explosion. Once requirement is satisfied and verification complete, the agent SHALL stop; further improvements become separate requests. `scripts/doc_integrity.py` SHALL provide repeatable documentation audit. Its scope SHALL be the repository's own documentation — every `*.md` file git tracks or would track (not ignored), excluding `docs/history/` — so third-party trees (`node_modules`), scratch copies and build-only worktrees (`tmp/`) and build output (`build/`, except the tracked `build/README.md`) are never reported as the repository's findings, while a new, not yet committed document is; without git the same trees SHALL be skipped by name (amended 2026-09-23, WORK-000303, ADR-000184, REQ-AUTO-20260923-004; verification `scripts/tests/test_doc_integrity_scope.py`). Implementation changes without requirement/work linkage SHALL be detectable (`scripts/requirement_impact.py --diff` / drift detection).

### REQ-DEVOPS-027 — Health, dry-run, and process-next-request contract

`scripts/health_check.py` SHALL report PASS/WARN/FAIL for REQUESTS/REQUIREMENTS/ARCHITECTURE/DECISIONS/WORK/TESTS/EVIDENCE/DOCUMENTATION/STATE. `scripts/process_next_request.py --dry-run` SHALL report intended changes without mutation. `scripts/process_next_request.py` SHALL implement the 19-step safe `process_next_request` contract documented in `docs/process/autonomous-operating-model.md`. A check that cannot finish within its budget (`health.structure_timeout_seconds`) SHALL be UNKNOWN, never PASS or FAIL, and makes the overall result WARN; `scripts/devsys.py status` SHALL show workers and their liveness, stale / dead / recoverable claims, the inbox and the next eligible request (priority, then arrival), needs-review, work by state, and the last success and failure (amended 2026-09-24, ADR-000199).

## 7. Phase 3 Chat and Crash Integration

### REQ-DEVOPS-028 — Chat is a request producer

A human chat utterance such as "I loaded a file and the application crashed" SHALL be convertible via `scripts/create_request.py --source chat --text "..."` into the same canonical `requests/inbox/` artifact used by inbox files. The original user statement SHALL be preserved verbatim; interpretation belongs in separate fields. Chat SHALL NOT be a second workflow.

### REQ-DEVOPS-029 — Classification and intent separation

Every request SHALL preserve `Original User Statement` vs `Agent Interpretation`. A lightweight classifier (`CRASH|FEATURE|BUG|...`) SHALL route CRASH to investigation workflow without preventing investigation of ambiguous requests.

### REQ-DEVOPS-030 — Crash investigation workflow

CRASH requests SHALL follow `docs/process/crash-investigation.md`: User Report -> Evidence Discovery -> Reproduction Attempt -> Analysis -> Root Cause -> Requirement -> Fix -> Regression Test -> Verification. Evidence discovery SHALL automatically look in `log/dump/` and record unavailable vs invented. Hypotheses SHALL NOT be documented as facts.

### REQ-DEVOPS-031 — Investigation stop conditions

Investigation SHALL terminate with one of: `ROOT_CAUSE_CONFIRMED|FIX_IMPLEMENTED|NOT_REPRODUCIBLE|INSUFFICIENT_EVIDENCE|BLOCKED|FALSE_REPORT|DUPLICATE|NEEDS_HUMAN_INPUT`. Insufficient evidence SHALL be recorded, not fabricated.

### REQ-DEVOPS-032 — Follow-up and attachments

Follow-up utterances in the same conversational context SHALL update the same request via `scripts/create_request.py --follow-up REQ-XXX --text "..."`. Attachments (dumps, logs, screenshots, data files) SHALL be associated with the request; large binaries referenced, not unnecessarily copied.

### REQ-DEVOPS-033 — Persistence and chat truth

The persistent request artifact is authoritative; conversation history is contextual evidence. Once created, the request MUST survive chat end and be continuable by another agent/session. See `docs/process/chat-intake.md`.

### REQ-DEVOPS-034 — Automation interface convergence

Chat intake SHALL use the project's canonical automation funnel (`<PROJECT_AUTOMATION_FUNNEL>`) when exposed via AI tools. A development object type `development.create_request` derived from the project's automation discovery SHALL be the tool contract; no parallel RPC shall be introduced. The project's wire-schema files (`<PROJECT_WIRE_SCHEMA>`) remain wire-schema authority. *(Extraction note: project-specific in the source project; replace or delete for a project without an automation interface.)*

### REQ-DEVOPS-035 — Canonical build contract

`docs/process/build-contract.md` SHALL be the sole authoritative BUILD -> TEST -> VERIFICATION contract, specifying the shadow build directory (`build/debug`), the build command (`<PROJECT_BUILD_COMMAND>`, e.g. `make -C build/debug`), the project's test runner invocations, and `scripts/build_health.py` / `scripts/worktree_check.py`. Build failures SHALL be structured (SOURCE|TEST|LINK|DEPENDENCY|ENVIRONMENT|TOOL|TIMEOUT|RESOURCE|UNKNOWN). `NEVER CLAIM A TEST PASSED UNLESS IT RAN` is a hard invariant.

### REQ-DEVOPS-036 — Regression tests for defects

Confirmed defects SHALL produce a regression test that fails before fix and passes after. It shall test the broken behavior, not merely the changed lines. If impossible, rationale shall be documented.

### REQ-DEVOPS-037 — Priorities, dependencies, and status reporting

Priority model `CRITICAL|HIGH|NORMAL|LOW` influences queue selection but never bypasses verification. Dependencies are stable `WORK-XXXX` links; `work/blocked/` vs `work/needs-review` are distinct. `scripts/status_report.py` and `scripts/health_check.py` SHALL answer status from persistent artifacts alone.

### REQ-DEVOPS-038 — Build health and worktree safety

`scripts/build_health.py` SHALL answer "can the system build/verify?" distinguishing infrastructure vs application failure. `scripts/worktree_check.py` SHALL determine safe-to-mutate worktree (uncommitted changes, unrelated work, generated files, build artifacts), classify every change by owning request (this request / other active request / unowned), report ownership conflicts (exit 3 with `--strict`) and never overwrite unrelated human work. In a shared worktree, broad staging (`git add -A`, `git add .`, `git commit -a`) SHALL NOT be used for request work: request changes SHALL be committed with `scripts/commit_request.py`, which refuses paths not owned by the request or owned by another request, builds the commit in a private index containing only the request's own delta on its owned paths (REQ-DEVOPS-042; for shared manifests whose lines name its id tokens, only those lines) and publishes it only if HEAD did not move. Separate git worktrees per request (`scripts/claim.py --create-worktree`) are the recommended stronger isolation where practical; ids and claims remain shared state either way (ADR-000090). Verification: `scripts/tests/test_process_concurrency.py` `GitCommitBoundaries`.

### REQ-DEVOPS-039 — Transaction-like recovery and reopening

Stages SHALL be recoverable: an agent crashing after implementation but before documentation SHALL be resumable without duplicate artifacts (idempotency + `claims` + `health_check`). Completed work shall not be edited as history; reopening requires new WORK referencing predecessor with reason, preserving evidence.

### REQ-DEVOPS-040 — Warnings as errors

*(Extraction placeholder.)* In the source project this requirement fixed one compiler-warning policy (warnings enabled at a high level and treated as errors) per build system, defined once and used by every first-party target, with no blanket exceptions, third-party code as system includes, warnings fixed at their source, and a static test that every project uses the central policy. Define the project's own build-warning policy here (`<PROJECT_WARNING_POLICY>`), or delete this requirement if the project has none. The development-system tooling does not enforce it.

### REQ-DEVOPS-042 — Change attribution from repository state (added 2026-09-20)

Two active requests MAY legitimately change different parts of one file, including files whose content carries no request identifier at all — user-facing prose, source, `.ui` files, configuration and generated output. Which changes belong to which request SHALL therefore be determined from recorded repository state, never only from request ids embedded in the file's text.

A registered path SHALL carry one of three ownership modes: **exclusive** (no other request may register it), **shared** (an append-only manifest whose changed lines name their owner's id tokens) and **concurrent** (`--concurrent`: ordinary content — prose, source, configuration, generated output — that several requests may legitimately change). A request that is the only holder of a path MAY correct that path's mode; changing it while another request also holds the path SHALL be refused.

When a request registers a path (`scripts/claim.py --own`) the tooling SHALL record that path's content at that moment as the request's **baseline**. `scripts/claim.py --snapshot` SHALL let a request record the current content of the paths it holds as its own work (so an edit another agent makes afterwards is not swept into its commit); `--snapshot --rebaseline` SHALL move the baseline to the current content (only what changes from that point on is the request's) and `--snapshot --from-head` SHALL move it to HEAD (everything the file has gained since the last commit is the request's), which is how a path registered after it was already edited is adopted; and `--snapshot --from-file` SHALL record a request's version of one path from a prepared file, so a shared file whose working copy also holds work that is not this request's can be committed without touching that working copy.

`scripts/commit_request.py` SHALL commit, for each owned path, only that request's **delta**: the three-way merge of the baseline, the content committed at HEAD, and the request's own version (its snapshot, or the working tree when it has none). A commit SHALL therefore neither publish another request's uncommitted work nor revert a commit another session landed in the same file while the request held it. Where the request's delta and what has been committed since genuinely overlap, the commit SHALL be refused with the path and the reason named — never resolved by guessing — and nothing SHALL be staged. A shared append-only manifest whose changed lines name the request's id tokens SHALL keep the exact line-level attribution, and a claim recorded before this requirement (no baseline) SHALL fall back to it.

A registered path that is a directory in the working tree (registered before it existed, or recorded before ADR-000197) owns nothing. While changed files under it are registered by no request, `scripts/commit_request.py` SHALL refuse the commit, naming those files and the remedy (register each file this request wrote, adopting it explicitly), instead of leaving them out of the commit without a word; a file another request registered under it stays that request's and does not block the commit (amended 2026-09-24, REQ-CHAT-20260924-013, ADR-000197).

The mode committed for an owned regular file SHALL be the mode a plain `git add` would record from the working tree: `100755` when the file's owner-exec bit is set, `100644` otherwise. Where the repository does not trust the executable bit (`core.fileMode=false`), or the path is not a regular file in the working tree, the indexed mode SHALL be kept. A mode-only change of an owned path (content unchanged since registration) SHALL be committed with HEAD's content unchanged. (Amended 2026-09-24, REQ-AUTO-20260924-003, ADR-000193: new scripts were committed without their executable bit, and a later `chmod +x` could not be committed at all.)

Verification: `scripts/tests/test_process_concurrency.py` `GitCommitBoundaries` — `test_shared_prose_without_ids_commits_only_the_committing_request`, `test_shared_prose_baseline_survives_a_foreign_commit_in_between`, `test_overlapping_edits_are_refused_not_guessed`, `test_exclusive_path_commits_only_the_delta_since_registration`, `test_new_executable_file_is_committed_executable`, `test_mode_only_change_is_committed_with_the_content_unchanged`, `test_new_executable_with_an_empty_delta_is_refused_then_adopted_executable`, `test_core_filemode_false_keeps_the_recorded_mode`; `DirectoryRegistration` (11 cases: individual files, existing / trailing-slash / nested directories and the repository root refused atomically, new files under new nested directories registered before creation, files created after the claim but never registered, a path that became a directory refusing the commit then committed after explicit per-file adoption, adoption never extending to a directory, a directory entry not taking another request's file, protected artifacts not registrable through their directory, `--status` marking a directory entry); `Recovery`/`test_recovery_sweep_leaves_a_healthy_session_alone`. Audit: `docs/audits/concurrency-and-request-ownership-audit-2026-09-20.md`.

### REQ-DEVOPS-041 — Test isolation from canonical source data (added 2026-09-20)

The project's canonical real test data (`$PROJECT_TEST_DATA`, see `AGENTS.md` "Real Test Data") are read-only *inputs*. Tests SHALL exercise them and SHALL NOT modify them or their metadata sidecars.

* Every test binary SHALL protect the canonical data root at start-up (the project's protected-path mechanism, `<PROJECT_PROTECTED_PATHS_HOOK>`), and SHALL export that protection so a child process it starts is protected too.
* A test that needs to write beside a canonical input SHALL use an isolated working copy: the data symlinked into a temporary directory (large inputs SHALL NOT be copied) with any sidecar copied beside the link, so every write lands in the temporary directory.
* A test SHALL distinguish the immutable source fixture from its working copy: the path it opens SHALL be the working copy, never the canonical path.
* A regression test SHALL prove that a write targeting a protected root is refused, that the protected file is unchanged byte-for-byte, that the refusal is recorded, and that a write through a symlink in a working directory still succeeds.

This requirement exists because, in the source project, an application auto-save wrote a loaded recording's own metadata sidecar, so tests that loaded canonical data silently rewrote it. *(Extraction note: the enforcement mechanism is application code, not part of the extracted tooling.)*

### REQ-DEVOPS-043 — Submit without processing (added 2026-09-20)

`scripts/create_request.py` SHALL be a submit-only operation: it validates the request, allocates a ticket id, persists the inbox artifact (`requests/inbox/*.md` + `.json` sidecar, `Status: inbox`), reports the ticket id, and stops. Submission SHALL NOT claim, assign, worktree, invoke processing, or modify product state, and its help text and creation confirmation SHALL state that guarantee. Listing or reading a ticket SHALL NOT claim it. Claiming (`scripts/claim.py --claim-request`, atomic and owner-checked per ADR-000090) SHALL be the separate, explicit transition `inbox -> processing`. Inbox selection in `scripts/process_next_request.py` SHALL honour sidecar priority `CRITICAL > HIGH > NORMAL > LOW` with lexicographic filename tiebreak. The submitter workflow is submit → receive id → stop; the reviewer workflow is list inbox → inspect → claim → normal workflow. Verification: `scripts/tests/test_submit_without_processing.py` (submit, no-processing, persistence, inbox discovery, claim, competing claim, existing lifecycle). Audit: WORK-000253, ADR-000150.

### REQ-DEVOPS-045 — Request classification vocabulary, semantics and validation (added 2026-09-22)

Request types SHALL be the closed vocabulary `FEATURE`, `BUG`, `CRASH`, `REGRESSION`, `PERFORMANCE`, `REFACTOR`, `BUILD`, `DOCUMENTATION`, `INVESTIGATION`, `PROCESS-CHANGE` (`scripts/request_classification.py` `RequestType`). Case and whitespace variants and the documented aliases (`DEFECT`/`BUGFIX`/`FIX` → `BUG`, `PROCESS` → `PROCESS-CHANGE`, `PERF`, `DOC`/`DOCS`, `ENHANCEMENT`, `REFACTORING`) SHALL be normalised and the normalisation reported; any other value SHALL be rejected. Priority SHALL be `CRITICAL|HIGH|NORMAL|LOW`; other values SHALL be rejected.

Semantics: `FEATURE` is a new capability, integration, enhancement or workflow; `BUG` is existing behaviour that is incorrect or fails under a defined condition (a deterministic test failure is a `BUG`); `CRASH` is reserved for an actual fatal runtime failure (crash, segmentation fault, abort, fatal exception, unexpected termination) and SHALL NOT be inferred from generic failure language (fails, failure, broken, error, issue, does not work, test/integration/reception/validation failure); `REGRESSION` requires evidence that previously working behaviour has become broken (used to work, no longer works since, broke after, restore the previous behaviour) and SHALL NOT be inferred from "regression test(s)/suite/gate" or from a request merely modifying existing functionality; `PERFORMANCE` requires a performance or resource problem (latency, throughput, CPU/GPU/memory, dropped samples, responsiveness, startup time) as the primary problem; `REFACTOR` is internal restructuring without a material behaviour change; `BUILD` is a tree that does not build/compile/link; `DOCUMENTATION` has documentation as the deliverable; `INVESTIGATION` is a question or analysis; `PROCESS-CHANGE` changes the development system (REQ-DEVOPS-015..019).

Classification SHALL consider the whole request (title, description, acceptance criteria, symptoms, referenced ids) but SHALL NOT invent evidence: a symptom mention that is negated, conditional, a handling/robustness specification, a capability noun, the subject of a requirement, an example or condition list item, fenced code, a quoted span, a heading or the prose of a section titled with a type name is not evidence. A proposed type (`--type`, whether from a human or an LLM) SHALL be validated deterministically before persistence: unknown vocabulary is rejected; `CRASH`, `REGRESSION`, `PERFORMANCE` or `BUILD` without supporting evidence in the text is corrected to the type the text supports when that is confident and otherwise rejected so the caller classifies again; a rejected request writes nothing. Every request SHALL record structured diagnostics (`classification: proposed_type, proposed_source, normalized_type, heuristic_type, final_type, confidence, validation PASS|CORRECTED|REJECTED, classifier_reason, correction_reason, evidence`) in its sidecar and `## Classification` block. Request id, sequence number and date are system metadata (REQ-DEVOPS-003): the intake SHALL generate them and SHALL offer no way for the caller or the text to supply them. Existing records SHALL NOT be rewritten silently: `scripts/create_request.py --file <record>` / `--review-all` report a stored type against its text, `--reclassify` is the explicit, history-keeping correction of one record, and `scripts/preflight.py` warns about an invalid or unsupported stored type at claim time. The classifier SHALL generalise (semantic rules, not title keywords for known examples). Verification: `scripts/tests/test_request_classification.py` (vocabulary, every example of REQ-CHAT-20260923-004, negation/meta handling, the real records REQ-CHAT-20260921-025/026/028/031/020 and REQ-CHAT-20260922-005/006, REQ-CHAT-20260923-001, CLI validation before persistence for every source, local-date ids under `TZ`, ids the text cannot influence, uniqueness under concurrent creation, review/reclassify/preflight). Audit: `docs/audits/request-classification-audit-2026-09-22.md`, WORK-000288, ADR-000173.

### REQ-DEVOPS-046 — Tiered, affected, parallel and recorded test execution (added 2026-09-23)

Goal: the least test time for the coverage a change needs (REQ-CHAT-20260923-015, ADR-000186).

1. **Registry.** Every test suite (one runner) and group (one invocation) SHALL be registered in `scripts/test_manifest.json` with its tiers, the qmake project that builds it, the projects whose binaries it executes (`uses`), its non-code inputs, its expected duration and its requirements (display, real test data, live hardware, tools). A test binary the build produces without a registered suite SHALL fail `scripts/tests/test_run_tests.py`.
2. **Tiers.** `scripts/run_tests.py` SHALL run cumulative tiers — **t0** static and process checks (always), **t1** + the unit groups of the affected suites, **t2** + their integration / GUI groups (the gate before a work item completes), **t3** every suite and group (nightly / pre-release; on the Debug and the Release tree) — and the explicit lanes **perf**, **stress**, **soak**, **live** and **package**, which no tier implies. Throughput / timing assertions SHALL be in the perf lane and run alone. Cases that open real hardware SHALL run only in the live lane, opted into with `PROJECT_LIVE_HW_ARGS` (a group can take or leave part of its runner's cases: `cases` / `exclude_cases`).
3. **Affected selection.** Suites SHALL be selected from the changed paths (a request's registered paths, or the working-tree diff) through the qmake project graph (`scripts/test_selection.py`: SUBDIRS, nested subdirs, included `.pri` files, referenced paths, `.depends`) and the manifest inputs. A changed path that nothing claims and that is not documentation or a process record SHALL select every suite.
4. **No redundant execution.** A group whose binary (GNU build id), executed binaries, inputs and arguments equal its last passing run SHALL be reported CACHED instead of re-run. Suites SHALL run concurrently, split into shards balanced by recorded per-case duration (`PROJECT_TEST_CASES`, `PROJECT_TEST_SHARD`), longest first.
5. **Isolation.** Every test process SHALL get private XDG data / config / cache / state / runtime and TMP directories, and every runner SHALL keep application data in a private root (the runners own that); no test SHALL write the invoking user's profile or a fixed shared path (`/tmp/<name>`, working-directory-relative scratch). Extends REQ-DEVOPS-041.
6. **Trustworthy results.** A binary older than the sources its build uses (the inputs its Makefiles depend on; `.pro` / `.pri` files against the Makefiles generated from them) SHALL be reported STALE and not run (unless explicitly allowed, which the record states); a binary replaced during its run SHALL make the result ENVIRONMENTAL; a case that failed under parallel load SHALL be re-run alone and reported FLAKY when it then passes; known failures SHALL be listed in `scripts/test_quarantine.json` with owner, reason and expiry and reported EXPECTED / PRE-EXISTING — never hidden. A runner process that crashes or hangs SHALL NOT end its shard: the case it ended in SHALL be named (CRASH / TIMEOUT) and the shard's remaining cases run in a continuation process; a crash SHALL NOT be reported FLAKY or quarantined. Every runner that reports JSON results (`"results": "json"` in the registry) SHALL honour cumulative selection flags, repeatable `--filter`, a per-case watchdog and the environment contract `PROJECT_TEST_RESULTS` / `PROJECT_TEST_SHARD` / `PROJECT_TEST_CASES` / `PROJECT_TEST_LIST` (implemented by the project's test framework, `<PROJECT_TEST_FRAMEWORK_HEADER>`); small runners report PASS / FAIL lines, which the orchestrator parses, and are re-run whole.
7. **Record.** Every run SHALL write `<build>/test-results/runs/<run id>.json` (REQ-DEVOPS-009).
8. **Packaging.** Packaging SHALL NOT re-run the development suites; it SHALL verify the artifact once — the project's package validation (`<PROJECT_PACKAGE_VALIDATE>`), then its asserting smoke test (`<PROJECT_PACKAGE_SMOKE>`) — and a failing stage SHALL stop the deployment (build → package → verify → deploy → deployment smoke).

### REQ-DEVOPS-047 — Project configuration and bootstrap (added 2026-09-24, ADR-000199)

Everything project-specific SHALL be configuration, not code: `devsys.json` at the repository root, deep-merged over the defaults of `scripts/devsys_config.py` (project name and source roots, paths, claim thresholds, id sources, completion policy, build adapter, test registry and selection mode, doc-integrity levels, invariants, crash evidence locations, structure checks), validated by `devsys_config.py --validate` (unknown keys, invalid values, invalid regular expressions). `scripts/devsys.py install --target DIR` SHALL copy the framework (not this instance's records) into a project without overwriting existing files unless `--force`, and SHALL write the instance files (configuration, test registry and quarantine, audit manifest, requirement and decision manifests) from the framework's templates (`scripts/templates/`) and copy only the framework's own ADRs, so installing from a configured project never carries its configuration, suites, requirement rows or decisions; `init` SHALL create the state directories, manifests, configuration and `.gitignore` entries (runtime state, the build tree, the scratch area) without overwriting; `doctor` SHALL report the environment, the configuration and every setting still NOT CONFIGURED. A fresh instance SHALL start every id series at 1: only the request store issues request ids. Verification: `scripts/tests/test_devsys_tooling.py`, `scripts/tests/test_end_to_end.py`.

### REQ-DEVOPS-048 — Work and evidence tooling (added 2026-09-24, ADR-000199)

Work records SHALL be created and moved only through `scripts/work.py` (`create`, `transition`, `show`, `list`): owner-checked; requirements and decisions validated at creation; the request linked both ways; transitions limited to `devsys_config.WORK_TRANSITIONS`, a reason required for blocked / needs-review / cancelled, each transition written to the record, the manifest and `.agent/state/runtime/transitions.jsonl`, and an interrupted transition finished by repeating the command. Evidence records SHALL be created through `scripts/evidence.py`, which allocates the id, writes the fields the gate reads, links the work record and copies each linked run record into `evidence/<VER>/run-<id>.json`. Every file either tool writes SHALL be registered in the request's claim first; a shared manifest SHALL be changed only on the lines naming the request's ids. Verification: `scripts/tests/test_lifecycle_hardening.py` (`WorkTool`, `GateAndCommit`).

### REQ-DEVOPS-049 — Build adapters (added 2026-09-24, ADR-000199)

`scripts/devsys_build.py` (and `devsys.py build`) SHALL run the configured build (`none`, `make`, `cmake`, `qmake`, `custom`; commands overridable with `{build_dir}`, `{source_dir}`, `{jobs}`), configure a tree that is not configured, record every action in `<build_dir>/build-results/` with the log, and classify a failure as SOURCE, LINK, DEPENDENCY, TOOL, ENVIRONMENT, TIMEOUT, RESOURCE or UNKNOWN; `none` SHALL be NOT CONFIGURED (exit 10), never success. `run_tests.py --build` SHALL use it. Verification: `scripts/tests/test_devsys_tooling.py` `Build`.

### REQ-DEVOPS-050 — Honest test results (added 2026-09-24, ADR-000199; extends REQ-DEVOPS-046)

A run in which nothing executed and nothing was a cached pass SHALL be EMPTY (exit 4) and a run stopped by a signal INTERRUPTED (exit 130); neither SHALL satisfy the gate. Suites MAY report JUnit XML (`$PROJECT_TEST_JUNIT`) or TAP; a failed case SHALL fail its group even when the process exits 0. Outside a qmake project, suites SHALL be selected by their `sources` / `inputs` globs (`tests.selection`). The log of every job that did not pass SHALL be kept beside the run record, which SHALL carry its provenance (runner version, host, platform, tool versions), and run records SHALL be found in the configured build tree as well as in evidence. Verification: `scripts/tests/test_devsys_tooling.py` `Orchestrator`.

### REQ-DEVOPS-051 — CI entry point and commit verification (added 2026-09-24, ADR-000199)

`scripts/devsys.py verify-commit [REV ...]` SHALL establish from the repository alone whether a completed request's commit carries exactly the code its gate verified — the gate record committed with it names the fingerprint and the code paths, and the fingerprint is recomputed from the commit's tree — reporting VERIFIED, WAIVED (a recorded human waiver), NOT_APPLICABLE (not a completed request's commit), FAIL or UNKNOWN. `scripts/devsys.py ci` SHALL be the non-interactive verification entry point for CI jobs and humans: configuration, structure, build, a test tier, drift / doc integrity / invariants, the architecture audit (`--fail-on fail`) and verify-commit over a commit range, each step PASS / FAIL / SKIPPED / UNKNOWN, the record written to `<build_dir>/ci-results/`, exit 0 only when nothing failed or is unknown; it SHALL claim, move and commit nothing. Verification: `scripts/tests/test_lifecycle_hardening.py` (`GateAndCommit`), `scripts/tests/test_end_to_end.py`.

## 8. Acceptance and Verification

This contract is verified by:

* structural checks that required directories, indexes, and state files exist;
* link/path checks for canonical index entries;
* a dry-run lifecycle request showing request -> requirement -> work -> test -> evidence links;
* the tiered test orchestrator for application changes (`python3 scripts/run_tests.py --tier t2 --request <file>`, REQ-DEVOPS-046), its run linked from the work record;
* manual review of conflict classification and final traceability for non-trivial changes;
* automated checks: `bash scripts/verify_development_system.sh`, `python3 scripts/drift_detect.py`, `python3 scripts/doc_integrity.py`, `python3 scripts/invariant_check.py`, `python3 scripts/health_check.py`.

Acceptance requires that a future agent can determine the next eligible request, its governing requirements, active work, remaining verification, and completion state from repository files alone.
