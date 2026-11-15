# Verification Commands

These are the canonical development verification commands. Record actual
results in an `evidence/VER-NNNNNN-*.md` record.

*(Extraction note: the source project's page continued with several hundred
lines of per-subsystem application test commands. Those are project content
and were replaced by the PROJECT-SPECIFIC section at the end; the
development-system sections are kept.)*

## Tiered test execution (REQ-DEVOPS-046, ADR-000186)

```bash
python3 scripts/run_tests.py --tier t2 --request requests/processing/<request>.md   # gate before completing work
python3 scripts/run_tests.py --plan --tier t2 --request <file>                      # what runs and why
python3 scripts/run_tests.py --list                                                 # every registered suite / group
python3 scripts/run_tests.py --tier soak|perf|stress|live|package                   # explicit lanes
```

Every suite below is registered in `scripts/test_manifest.json`; the
orchestrator selects the affected ones, skips unchanged passing groups, runs
the rest in parallel isolated shards and writes a run record to link from
the work record (`**Test-Run:**`). A crash or hang is named with its case and
the rest of the shard still runs; cases that need real hardware run only in
the live lane with `PROJECT_LIVE_HW_ARGS` set. The per-feature commands in
the rest of this page remain valid for iterating on one subsystem.

## Build

```bash
python3 scripts/devsys.py build check     # the configured adapter's tools are present
python3 scripts/devsys.py build build     # devsys.json "build" (docs/process/build-contract.md)
```

Every action is recorded in `<build_dir>/build-results/` with its failure class.

## Framework self-tests (ADR-000199)

* `python3 -m unittest discover -s scripts/tests` — the whole development-system suite (suite `scripts`), including:
  * `scripts.tests.test_lifecycle_hardening` — verified liveness (ALIVE / DEAD / ACTIVE / STALE, reboot, pid reuse, `--run` steps), sweep repairs and their limits, explicit recovery (never from a verified-ALIVE owner), gated completion, recorded waivers and rejections, needs-review takeover, release refusal / `--abandon`, commit binding and trailers, a request committing the same file twice, gate failure after a code change, `work.py` / `evidence.py`, drift and the gate reading under the work and claims locks, doc-integrity skipping a file moved mid-scan, duplicate and concurrent intake, follow-ups, next-request order, id allocation across clones and from the request store only, nested repositories;
  * `scripts.tests.test_devsys_tooling` — configuration and validation, `install` / `init` / `doctor` / `status` on a fresh project, build adapters (none, make, custom, cmake), invariant rule kinds and NOT CONFIGURED, doc-integrity FAIL, lifecycle drift, health UNKNOWN, EMPTY / INTERRUPTED / JUnit / TAP results, kept failure logs, configured and absolute build trees, globs selection, preflight;
  * `scripts.tests.test_end_to_end` — the acceptance scenario: a fresh project bootstrapped and configured (make + C + TAP), concurrent intake, a dead session detected and its work recovered and finished, a resumed request whose gate fails after a code change, a rejection, tiers t0..t3, audits and the architecture audit, commit isolation with a human's unrelated edits left byte-identical (needs `make` and `cc`).
  * `scripts.tests.test_workers` — the Worker Orchestrator (WORKER-001..013):
    - chat intake and priority (persisted, changed, queue order, closed requests refused);
    - worker slots, enable / disable, start refused without an agent or beyond the maximum, the root a spawned worker serves;
    - completion only through the gate (`verify-commit` VERIFIED), an ungated completion refused and escalated after the maximum attempts;
    - PARTIAL resumed under the same claim before new work (committed, claim released), two workers never sharing a request, three workers completing through the gate at once (validation PASS, every commit VERIFIED, clean tree), per-worker state;
    - stop now (INTERRUPTED, claim kept), a killed worker recovered (orphaned session ended only with `--force`) and resumed, timeouts, agent escalation;
    - worktree isolation, no vendor identity or machine paths in the system, `paths.project_root`;
  * `scripts.tests.test_agent_coordination` — agent coordination (COORD-001..009) and agent profiles (WORKER-006, WORKER-012): roster names, uniqueness and verified liveness; direct / role / broadcast / operator messages, delivery once, replies, refusals, strictly increasing sequence numbers under concurrent senders, a torn log line; messages change no claim; the `msg.py` CLI (`wait` exit 4, exit codes) and the `MESSAGES:` line of `claim.py`; one roster and one conversation across a linked git worktree; profile selection per worker slot, an undefined profile refused, a worker running its profile and joining the roster; the event-stream adapter (both stream shapes, error results, the wrapped command's prompt and exit code); the snapshot and operator actions; the web panel over HTTP (token, Host check, actions, refusals with status, the live event stream); the terminal dashboard (`--once`, frame width, actions); names (the first agent is Cody, the others drawn at random, unique ignoring case); the station (started by the first agent with the addresses shown, one per clone, a message pushed to a waiting agent, a wrong hub token refused, ended when the last agent is gone, polling fallback, the off switch); the dashboard launcher (`station.py --open` / `--stop`, the two desktop entries installed and removed); `msg.py hook` (context after a tool call, a stop blocked once, the agent found by its session process, silence on bad input).
  * `scripts.tests.test_devsys_tooling` `WindowsBranches` / `NoShellNeeded` — portability (PORT-001..006): the Windows liveness, pid-reuse, tree-termination and shim-resolution logic with the Win32 calls replaced (not a run on Windows); no registered command needs bash, and `python3` in a suite command is the running interpreter.
  * `scripts.tests.test_worker_panel` — the Qt control panel headless (`QT_QPA_PLATFORM=offscreen`): the `.ui` loads; workers, queue, working and finished requests are shown; details on selection; priority changed through the framework; enable / disable / start / stop now through the worker API (skipped without PySide6).
* The regression tests for the defects found while running the framework on a fresh project (B1..B11) are listed in `CI_CD.md` §13.2.

## Development-system integrity (REQ-DEVOPS-003/014, ADR-000149, VER-000248)
* `python3 -m unittest discover -s scripts/tests` — the whole development-system suite: identity-collision validation (`test_drift_id_collisions.py`), concurrent request/work tooling and the id allocator (`test_process_concurrency.py`), request classification, the completion gate, process protection, doc-integrity scope, work-state drift, submit-without-processing and the test orchestrator. Also run by `scripts/verify_development_system.sh` (VER-000248)
* `python3 -m unittest scripts.tests.test_drift_id_collisions` — accepted historical collisions vs unexpected ones: the declared collisions (`REPO_DECLARED`) accepted against the real repository with both records named, a third record on any of them failing, a brand-new duplicate failing, unique records clean, malformed declarations failing without hiding the duplicate they named, a substituted record detected, a stale declaration warning only, and a record repeated across manifest rows failing as `DUPLICATE_MANIFEST_ROW` (VER-000248)
* `python3 scripts/drift_detect.py` — must report `0 FAIL` (plus one `ACCEPTED` line per declared historical id collision); a `FAIL` here is real drift, not something to classify away (VER-000248)
* `python3 -m unittest scripts.tests.test_doc_integrity_scope -v` — `scripts/doc_integrity.py` audits the repository's own documentation: tracked and new, not-ignored `*.md` minus `docs/history/` (never `node_modules`, `tmp/` or build output), with and without git (REQ-DEVOPS-026, ADR-000184, WORK-000303 / VER-000303).

## Architecture / conformance audit (ARCH-AUDIT-*, ADR-000195, WORK-000315, VER-000316)
* `./tools/audit quick` / `./tools/audit` (standard) / `./tools/audit baseline --build-dir build/<tree>` — read-only measurement against the canonical documents; PASS / DRIFT / UNKNOWN per documented rule, evidence and reports under `.audit/runs/AUDIT-*/` (`docs/verification/architecture-audit.md`). Advisory: it gates nothing and replaces none of the commands in this file (VER-000316)
* `python3 -m unittest discover -s tools/audit_system/tests` — the audit's own tests on synthetic repositories (suite `audit_py`): result states, parsers, builtins, run identity, comparison, retention, read-only, controlled change (VER-000316); `Levels`: gate severity → FAIL, NOT CONFIGURED → SKIPPED, `--fail-on`, an audit without git (ADR-000199)

## Concurrent request ownership and change attribution (REQ-DEVOPS-042, WORK-000252)

* `python3 -m unittest scripts.tests.test_process_concurrency` — id allocation,
  claim ownership, recovery and commit boundaries. The attribution tests added
  by WORK-000252: `GitCommitBoundaries/test_shared_prose_without_ids_commits_only_the_committing_request`
  (two requests hold one user-facing document that contains no request id
  anywhere; committing one leaves the other's work uncommitted),
  `test_shared_prose_baseline_survives_a_foreign_commit_in_between` (a commit
  another request landed in the same file is not reverted),
  `test_overlapping_edits_are_refused_not_guessed` (a genuine overlap is
  refused with the path named and nothing is staged),
  `test_exclusive_path_commits_only_the_delta_since_registration`, and
  `Recovery/test_recovery_sweep_leaves_a_healthy_session_alone` (a healthy
  session whose helper processes have exited survives repeated recovery
  sweeps). All five were proved to fail against the previous behaviour — see
  VER-000252.

## PROJECT-SPECIFIC verification commands

Add one section per subsystem: the test runner invocations that iterate on it
(`build/debug/<TestBinary> --unit --filter <prefix>`), the requirement IDs and
evidence records they prove, live-lane scripts and the real data or hardware
they need (`PROJECT_TEST_DATA`, `PROJECT_LIVE_HW_ARGS`), and the project's
build-warning policy checks (REQ-DEVOPS-040).
