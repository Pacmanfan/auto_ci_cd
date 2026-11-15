# ADR-000199: A configuration layer and a hardened, tool-driven lifecycle for the extracted framework

**Status:** ACCEPTED (implementation of CI_CD_ROADMAP.md: "Combined Hardening, Generalization, and Automation Implementation")
**Date:** 2026-09-24
**Requirements:** `development_operations.md` REQ-DEVOPS-047..051 (new); REQ-DEVOPS-014, -016, -019, -020, -022, -025, -027 (amended)
**Amends:** ADR-000090 (concurrent agent process tooling), ADR-000155 (work-state consistency), ADR-000186 (tiered test execution), ADR-000195 (architecture audit)
**Roadmap items:** see `CI_CD_ROADMAP.md` "Implementation status"

## Context

The development-control system was extracted from a single application
repository (`CI_CD.md` §12). The audit (`CI_CD_ROADMAP.md`) found that the
extracted system:

- still embedded the source project: qmake-only selection, a Qt 5 guard,
  hard-coded paths, and invariants written in code;
- enforced the lifecycle only by convention. Nothing prevented:
  - a request moved to `completed` without a gate;
  - a gate that passed on a run of different code;
  - a manual work-record move;
  - a claim released with uncommitted work;
- judged liveness only by heartbeat age. A crashed session looked "active"
  for two hours, and a silent live one looked "stale";
- reported "nothing ran" as PASS, and a timed-out health check as FAIL.

Its strengths had to be kept:

- file-based state;
- atomic claims;
- owned-path commits;
- explicit, never automatic, recovery;
- the evidence trail.

## Decision

1. **Configuration, not code.**
   - `devsys.json` at the repository root is deep-merged over the defaults in
     `scripts/devsys_config.py`. Keys starting with `_` are comments or
     examples.
   - Every script reads its policy, paths and thresholds from it.
   - `devsys_config.py --validate` rejects unknown keys and invalid values.
   - A fresh project is bootstrapped with `devsys.py install` (copy the
     framework) and `init` (state directories, manifests, `.gitignore`);
     `doctor` names what is still NOT CONFIGURED.
2. **Verified session liveness.**
   - With `--owner-pid` / `$AGENT_OWNER_PID`, a claim records the session
     process's host, boot id, pid and start time (`scripts/devsys_platform.py`).
   - Verdicts:
     - **ALIVE:** verified running, or a `claim.py --run` step running.
     - **DEAD:** verified gone (exited, pid reused, or an earlier boot) and
       silent for `claims.dead_grace_minutes`.
     - **ACTIVE:** unverifiable, recent heartbeat.
     - **STALE:** unverifiable and silent.
   - A verified-ALIVE owner is never taken over.
3. **Safe sweep.**
   - `claim.py --sweep` diagnoses everything.
   - `--apply` performs only repairs that cannot lose or steal work. Every
     one is logged.
   - A DEAD owner with uncommitted work, and any STALE owner, still needs an
     explicit `--recover-request --reason`.
4. **Gate records bind completion to tested code.**
   - `completion_gate.py --record` writes `evidence/gates/<WORK-ID>.json`
     with the *request fingerprint*: a digest of the request's registered
     code paths.
   - Each of these requires a PASS record on the current fingerprint:
     - `work.py transition --to completed`;
     - `claim.py --complete-request`;
     - `commit_request.py` of a completed request.
   - The gate FAILs on:
     - a missing, failing, EMPTY, INTERRUPTED or too-low-tier run for
       changed code;
     - a run of different code;
     - undefined citations;
     - non-passing evidence;
     - a caller who is not the owner.
   - A human override (`--waive-gate <reason>`) is recorded in the request
     and honoured by the commit.
5. **Lifecycle tools instead of hand edits.**
   - `work.py` creates and transitions work records. Transitions are
     validated, journaled and idempotent.
   - `evidence.py` creates evidence records and copies run records into
     `evidence/`.
   - `devsys.py status | next | sweep` gives the operator a view and the
     next eligible request (priority, then arrival).
   - Each tool is a thin front end over the same library, so every
     automated step keeps a manual path.
6. **Honest results.**
   - EMPTY (nothing executed) and INTERRUPTED are never PASS.
   - JUnit and TAP results are parsed.
   - Selection works by `sources` globs outside qmake projects.
   - Failed jobs' logs are kept beside the run record.
   - The audit states are PASS / FAIL / DRIFT / UNKNOWN / SKIPPED:
     - a `severity: "gate"` rule that does not hold is FAIL;
     - a check without a configured mechanism is SKIPPED, not PASS;
     - a check that cannot finish is UNKNOWN.
7. **CI entry point and commit verification.**
   - `devsys.py verify-commit` recomputes the fingerprint from a commit's
     tree over the code paths its gate record names. It shows, from the
     repository alone, that the commit is the code that was verified.
   - `devsys.py ci` runs, in order: configuration, structure, build, a tier,
     the consistency audits, the architecture audit and `verify-commit`. It
     writes a record, and claims, moves and commits nothing.
8. **Declared rules and adapters.**
   - Architecture invariants are declared in `devsys.json` (`forbid_pattern`,
     `require_pattern`, `forbid_dependency`, `layers`).
   - Builds run through adapters: `none`, `make`, `cmake`, `qmake` or
     `custom`, with a failure taxonomy.
9. **Portability boundary.** Process, lock and link primitives live in
   `scripts/devsys_platform.py`. Everything else is portable Python 3.9+,
   git and POSIX shell (`docs/process/portability.md`).
10. **External engine boundary.** The external ticket engine stays outside the
   framework. `docs/process/autonomous-ticket-engine.md` (since ADR-000200: `docs/process/worker-orchestrator.md`) is the interface it
   must use: CLIs, exit codes, the files it may read, and what it must never
   do.

## Rejected alternatives

- **A database or service for claims and state.** It would lose
  reviewability through git and the manual path, and the file locks already
  give atomicity. Rejected by the engineering constraints.
- **Automatic recovery of stale claims.** Silence is not proof of death. Only
  a verified DEAD owner with nothing uncommitted is requeued automatically.
- **Recursive directory ownership.** Rejected again for the reason recorded
  in ADR-000197.
- **Keeping the qmake graph as the only selection.** It is kept for qmake
  projects; other projects use globs.
- **A new status vocabulary for the process scripts.** They keep PASS / WARN
  / FAIL; the audit layer maps them.

## Consequences

- Completing a request needs a gate record: one more command
  (`completion_gate.py --record`), which the tools name in their refusals.
- Existing instances: `enforce_on_transition: false` restores convention-only
  completion. `drift.unverified_completion: "WARN"` keeps old hand-completed
  requests from failing drift.
- Ten defects found by running the framework on a fresh project are fixed,
  and each has a regression test (`CI_CD.md` §13.2).
- Worktree integration tooling, a Windows port and a distributed mode remain
  deferred (`CI_CD_ROADMAP.md`).
