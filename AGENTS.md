# <PROJECT> Agent Constitution

This file contains durable operating rules for autonomous agents. It is not a
project encyclopedia. Start with `docs/README.md`; use the canonical artifacts
listed below for project knowledge.

## Authority

```text
requests/                         human input
    |
docs/requirements/                normative behavior
    |
docs/decisions/                   active architectural decisions
    |
work/                             implementation activity
    |
tests                             executable verification
    |
evidence/                         recorded proof
    |
docs/ and subsystem READMEs       current explanation and operations
```

* `docs/requirements/` is the normative behavioral authority (sole source of truth). Use
  `docs/requirements/requirements.txt` to find the owner; extend an existing
  requirement instead of duplicating it.
* PROJECT-SPECIFIC: name the project's wire-schema authority here
  (`<PROJECT_WIRE_SCHEMA>`), if it has one. Generated output derived from it
  must not be hand-edited.
* `docs/decisions/` contains active ADRs. `docs/history/` is historical and
  cannot silently override current requirements or architecture.
* `work/` is the active work authority. Source-local task notes (if any) are
  orientation/history and must not become a competing task system.
* Tests are evidence generators, not requirements. Evidence proves execution;
  it does not redefine behavior.

## Request Processing

When asked to process work, use this sequence (see `docs/process/state-model.md` for valid states, `docs/process/autonomous-operating-model.md` for the full contract):

```text
discover (scripts/devsys.py status; scripts/devsys.py next)
  -> claim one request (scripts/devsys.py next --claim, or scripts/claim.py --claim-request <file>;
     atomic, see Concurrent Agents)
  -> record intent
  -> preflight (scripts/preflight.py --request <file>) — stop on FAIL
  -> select/update requirement (idempotent: find existing -> update/link)
  -> record ADR when architecture changes
  -> create the work record (scripts/work.py create --request <file> --requirements ...)
  -> register every path before editing it (scripts/claim.py --own <file> --paths ...)
  -> inspect and implement
  -> add/update tests
  -> build and run tests (scripts/run_tests.py --tier t2 --request <file> --build)
  -> write evidence (scripts/evidence.py create --work W --status ... --test-run <run id>)
  -> update current documentation
  -> audit traceability and drift (scripts/drift_detect.py, scripts/doc_integrity.py, scripts/invariant_check.py)
  -> completion gate (scripts/completion_gate.py --work W --record) — must PASS on the current code
  -> scripts/work.py transition W --to completed
  -> scripts/claim.py --complete-request | --reject-request --reason | --needs-review-request --reason
  -> commit with scripts/commit_request.py (owned paths only)
  -> release claim (scripts/claim.py --release-request)
```

The completion commands (`work.py ... --to completed`, `--complete-request`,
and `commit_request.py` for a completed request) refuse without a PASS gate
record on the request's current code (exit 9). A human override is
`--waive-gate "<reason>"`, which is recorded; never use it on your own
initiative.

Requests received through chat, command-line, inbox file, crash report, or automation are requests too:
record them via `scripts/create_request.py --source chat|crash|inbox` which creates `requests/inbox/REQ-*` (unified intake per `docs/process/chat-intake.md`; `Source != Workflow`). There is no untracked conversational queue. The request type is a closed vocabulary validated deterministically against the text (REQ-DEVOPS-045): `CRASH` is reserved for an actual runtime crash/fatal termination and is never inferred from generic failure language; `FEATURE` means new capability/integration/enhancement; `BUG` means existing behaviour is incorrect; `REGRESSION` requires evidence that previously working behaviour broke; `PERFORMANCE` requires a performance/resource problem; do not invent evidence to justify a `--type`, and pass `--type` only when you can name that evidence. Request ids, dates and sequence numbers are system-generated metadata (local system date + global allocator): never generate, guess or type them. The persistent request is authoritative; conversation is evidence. `scripts/process_next_request.py --dry-run` reports the plan without mutating.

The persistent state is `.agent/state/` plus `.agent/state/claims/*.lock` for claim ownership. Never mark work complete based only on
compilation. A complete item has requirement, implementation, test, evidence,
documentation, and traceability links and must pass `scripts/completion_gate.py`. Never move request or work files or edit manifests by hand: `scripts/claim.py`, `scripts/work.py` and `scripts/evidence.py` do it and record it. Use explicit classifications:
`PASS`, `EXPECTED / PRE-EXISTING`, `REGRESSION`, or `BLOCKED / ENVIRONMENTAL`. See `docs/process/recovery.md` for crash/restart, `docs/process/escalation.md` for needs-review, `docs/process/process-protection.md` for process-change separation, `docs/process/chat-intake.md` for chat convergence, `docs/process/crash-investigation.md` for crash workflow, and `docs/process/build-contract.md` for BUILD→TEST→VERIFICATION. Never claim a test passed unless it ran.

If requirements, architecture, tests, or code conflict:

1. stop the affected implementation;
2. identify the authoritative owner;
3. record the conflict;
4. update the requirement or ADR explicitly;
5. continue only after the records agree.

## Concurrent Agents

Other agents may be working in this repository at the same time (ADR-000090):

* Use a session-unique id: `export AGENT_ID=$(python3 scripts/claim.py --new-agent-id)`,
  and name your session's long-lived process: `export AGENT_OWNER_PID=<pid>`.
  Your claims are then verified ALIVE while it runs and DEAD once it is gone,
  instead of being guessed from the heartbeat (`docs/process/recovery.md`).
  Wrap long steps in `scripts/claim.py --run <file> -- <command>`.
* Request ids and `WORK`/`VER`/`ADR` ids come from the allocator (intake,
  `scripts/allocate_id.py`) — never pick the next number by hand.
* Ownership is explicit: claim the request, register every path before editing
  it (`scripts/claim.py --own <file> --paths ...`; `--shared` + `--token` for
  manifests). Changes that are not yours are not yours to stage, commit,
  revert or discard.
* Commit only with `scripts/commit_request.py --request <file> -m ...`. Never
  `git add -A`, `git add .` or `git commit -a` in the shared worktree.
* Recover another agent's request only explicitly: `scripts/devsys.py status` /
  `scripts/claim.py --sweep` (report) then `--recover-request <file> --reason ...`
  (`docs/process/recovery.md`). A verified-ALIVE owner is never taken over;
  a STALE one only after you have confirmed it is gone. `--sweep --apply`
  performs only the repairs that cannot lose or steal work.
* Release only when everything you own is committed; to hand a request back
  with uncommitted work, `--release-request <file> --requeue --abandon "<reason>"`.
* Separate worktrees (`scripts/claim.py --create-worktree <file>`) give the
  strongest isolation when a separate build tree is affordable.

## Repository Constraints

PROJECT-SPECIFIC. List the project's durable architectural constraints here —
module ownership, layering and dependency direction, framework boundaries,
single integration funnels, resource-lifetime rules, removed technologies that
must not return. Keep each one short, cite its requirement or ADR, and declare
the checkable ones as rules in `devsys.json` `invariants`
(`docs/process/configuration.md` "Architecture rules"), listed in
`docs/audits/current-state.md`.

## Configuration

Everything project-specific about this process — paths, thresholds, the build
adapter, test selection, completion policy, architecture rules — is in
`devsys.json` (`docs/process/configuration.md`; `python3 scripts/devsys.py
doctor` shows what is not configured). It is a protected artifact.

## Temporary Work and Staging

Use `tmp/` (relative to the repository root) for any temporary work, staging, intermediate files, or scratch output. Do not use the system `/tmp` for project-related temporary work. Create the directory if it does not exist and clean up stale temporary files when work completes. Do not commit temporary staging artifacts unless explicitly required by the task.

## Change Finalization

All changes must be properly commented and committed at the completion of every requested work, through `scripts/commit_request.py` so that only the request's own paths are committed. Do not leave uncommitted or untracked work that was intended to be part of the deliverable; `git status` may still show other agents' changes — leave those alone. Commit messages describe the change.

## Real Test Data

PROJECT-SPECIFIC. If the project verifies against real (not synthetic) data,
name its location here (`$PROJECT_TEST_DATA`, including all subdirectories),
state that it is the primary and default source, that paths must never be
fabricated, and that it is read-only (REQ-DEVOPS-041). Say through which
channel tests and agents exercise it (for example the project's automation
interface, rather than application-internal test-only code paths).

## Build and Tests

Build through the configured adapter (`devsys.json` `build`,
`docs/process/build-contract.md`):

```bash
python3 scripts/devsys.py build build          # or: python3 scripts/run_tests.py ... --build
# Tests: the tiered orchestrator (REQ-DEVOPS-046, ADR-000186). It selects the
# suites your change affects, skips groups whose binary and inputs are unchanged
# since they last passed, runs the rest in parallel shards in private profiles,
# and writes a run record. The t2 tier is the gate before completing work.
python3 scripts/run_tests.py --tier t2 --request requests/processing/<request>.md
python3 scripts/run_tests.py --tier t1 --request <file> --suite <suite>  # iterate on one suite
python3 scripts/run_tests.py --plan --tier t2 --request <file>         # what would run, and why
python3 scripts/run_tests.py --list                                    # every suite / group / tier
```

Tiers are cumulative: t0 static/process checks, t1 + affected unit groups, t2
+ affected integration/GUI groups, t3 everything (nightly/pre-release, on the
Debug and the Release tree). perf, stress, soak, live and package are explicit
lanes (`--tier perf` ...), never implied. Register a new test suite in
`scripts/test_manifest.json` (an unregistered test binary fails
`scripts/tests/test_run_tests.py`). Link the run in the work record:
`**Test-Run:** <run id>` — `scripts/completion_gate.py` fails a linked run that
failed and a request whose code changed after its latest linked run. STALE
means rebuild first (`--build`); ENVIRONMENTAL means a binary was replaced
mid-run (shared build tree: use a per-request tree such as
`build/<name>-debug` or a worktree); FLAKY cases passed only when re-run
alone; EXPECTED failures are listed in `scripts/test_quarantine.json` with an
owner and an expiry. CRASH / TIMEOUT name the case and the rest of its shard
still runs; a crash is never FLAKY or quarantined. Cases that need real
hardware run only in the live lane (`PROJECT_LIVE_HW_ARGS`). Individual runners
still work directly (PROJECT-SPECIFIC: document the project's runner flags
here). Record the run id, classification, and known failures in `evidence/`,
with a copy of the run record (`evidence/VER-NNNNNN/run-<run id>.json`;
`scripts/evidence.py create --test-run <run id>` does both). A run that
executed nothing is EMPTY and a stopped run INTERRUPTED; neither is a pass.

## Deployment

PROJECT-SPECIFIC. Describe the one canonical packaging / deployment mechanism
(command, output location, validation and smoke test run once per deployment)
and what must not be done instead. Packaging does not re-run the development
test suites (ADR-000186); it verifies the artifact once through the `package`
lane of `scripts/test_manifest.json`.

## Crash Diagnostics

When investigating any crash, abort, or fatal signal, inspect the newest ELF
core under `log/dump/` relative to the repository root (`paths.crash_dumps`),
or the application's fallback dump directory if it has one (`crash.fallback_dir`).
Do not rely on logs alone.

```bash
file log/dump/<APP>_*.core
gdb build/debug/<APP_BINARY> log/dump/<APP>_*.core
# bt
# thread apply all bt
# info registers
# info sharedlibrary
```

Use the executable and symbols matching the core. Crash artifacts are evidence
and must be linked from the relevant `VER-*` record.

## Documentation

Keep `docs/README.md`, requirements manifests, decision manifests, work
manifests, and evidence manifests current. Current docs must not point to
deleted documentation locations (PROJECT-SPECIFIC: list them here and in
`devsys.json` `doc_integrity.stale_paths`), `docs/architecture/desired/`, or old contract paths.
Keep useful historical records, but label them historical and do not present
them as current authority. Run the independent documentation cleanup pass
before completing a structural or behavioral change.

## User Documentation

PROJECT-SPECIFIC. If the project has user-facing documentation, say where its
canonical sources live, how it is built and validated, and that generated
output is never the source of truth.

## Reporting

For meaningful work report: requirement IDs/files, decisions, work IDs,
implementation files, tests, evidence IDs/commands/results, documentation, and
outstanding or blocked items. Do not claim a verification that was not run.
