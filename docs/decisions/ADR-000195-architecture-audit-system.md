# ADR-000195: A minimal, manifest-driven architecture / conformance audit

**Status:** ACCEPTED
**Date:** 2026-09-24
**Work:** WORK-000315 (REQ-CHAT-20260924-003)
**Requirements:** `docs/requirements/architecture_audit.md` ARCH-AUDIT-001..010 (new)
**Audit:** `docs/audits/architecture-audit-system-audit-2026-09-24.md`

## Context

The repository already has many deterministic checks: `scripts/drift_detect.py`, `doc_integrity.py`,
`invariant_check.py`, the project's module-boundary and registry scripts,
`build_health.py`, `verify_development_system.sh`, a removed-library check,
the project's warning-policy test, and the test orchestrator `scripts/run_tests.py`
with its run records. They answer single questions and print to the terminal. Nothing records a
repeatable, identified snapshot of the project against its canonical documents. Nothing compares
one snapshot with the previous one, keeps per-finding evidence, or produces a compact summary a
development AI can reason over. Several documented rules have only presence-only heuristics.
Others have no check at all, including the declared build dependencies, requirement-index
coverage, test citations of undefined requirement IDs, and paths named by the architecture
documents. Those rules therefore read as satisfied when nobody has verified them. The request asks
for a Phase 1 foundation that stays small.

## Decision

1. **One small runner over the existing tools.** `tools/audit` (Python standard library) runs the
   checks declared in `.audit/manifest.json`. Existing tools are called as they are, and their verdicts
   are parsed (`exit`, `findings-json`, `test-plan`, `test-run`). `invariant_check.py` runs once and
   is sliced per rule. New logic is limited to a few builtins, each tied to a rule a canonical document
   already states: declared build dependencies and their direction, the requirement index, undefined
   requirement citations, documented paths, the build-tree Makefile generator, and recorded test results. The audit
   invents no rule of its own.
2. **Four states, no score.** PASS / DRIFT / UNKNOWN, plus SKIPPED for a check outside the profile.
   A presence-only or sampled heuristic is UNKNOWN even when it reports nothing. A reused tool's
   warnings stay warnings: counted, compared across runs, never promoted to DRIFT. That tool's own
   classification is preserved, and its known false-positive classes (e.g. `doc_integrity` on generated
   `.html` links) do not become findings.
3. **Profiles by cost class.** Checks are `fast`, `moderate`, `tests` or `expensive`. quick = fast;
   standard = + moderate; baseline = + tests (the unit tier through `run_tests.py`, which caches,
   isolates and records as usual). `expensive` (builds, lanes, containers, hardware, packaging) is in
   no Phase 1 profile; such checks are listed as NOT APPLICABLE so the gap stays visible. Budgets per
   profile; per-check timeouts.
4. **The audit does not build.** Build state comes from the audited tree (`--build-dir`): Makefile
   generators, compiler, and `run_tests.py --plan` freshness. Fresh binaries prove that a `-Werror`
   build of the current sources succeeded. Stale binaries leave build success UNKNOWN. Building
   remains a separate, deliberate step, done in a private tree when the shared one is in use.
5. **Artifacts.** `.audit/runs/AUDIT-YYYYMMDD-NNN/` (`audit.json`, `audit.md`, `audit-packet.md`,
   `evidence/`) is git-ignored. Its ID is reserved by an exclusive `mkdir`. Retention keeps the newest
   N runs plus every baseline. A baseline worth keeping is copied into `evidence/VER-*`.
6. **Read-only, verified per run.** The working tree's status and content hashes, and HEAD, are
   compared before and after each run (`audit.read-only`). In the shared checkout a concurrent change
   is UNKNOWN, not a violation. Subprocesses run with `PYTHONDONTWRITEBYTECODE`.
7. **AI is a consumer, not a dependency.** The packet references evidence paths and states the rules
   of interpretation (UNKNOWN is not PASS; a DRIFT is the rule owner's decision). `--ai` pipes it into
   `$AUDIT_AI_COMMAND`, whatever that command is; the answer is stored as `ai-analysis.md`, apart from
   the evidence.
8. **Requirement owner.** The contract is the new domain document `architecture_audit.md`
   (ARCH-AUDIT-*). It is not a new REQ-DEVOPS item in the protected `development_operations.md`,
   because the audit changes no lifecycle, completion, traceability or verification rule and gates
   nothing (`docs/process/process-protection.md`). If it later becomes a gate (CI, completion), that
   step is a process change with its own ADR.

## Alternatives considered

* **Extend `scripts/health_check.py`.** Rejected. It re-runs the same tools, including the unit tests
  (54 s), and reduces each to PASS/WARN/FAIL. It keeps no IDs, evidence, comparison or packet.
  Wrapping the underlying tools directly is cheaper and keeps their detail.
* **A plugin framework or a database for findings.** Rejected for Phase 1 (the request forbids it). A
  JSON file per run and a manifest entry per check are enough to add checks and compare runs.
* **Build or run all tests inside the audit.** Rejected. It would make every audit a build and would
  disturb other sessions' shared trees. The test orchestrator already owns execution, caching and
  records.
* **A single overall score.** Rejected. It hides UNKNOWN behind PASS.

## Consequences

* Rules that read as satisfied because nobody checks them are now listed as UNKNOWN, each with the
  evidence that would settle it (e.g. the project's integration funnel, runtime-service ownership,
  processing-block contracts, scene ownership, performance contracts).
* Findings are recorded, not fixed. Each DRIFT is left to the owner of the rule; the audit never edits
  code or documents.
* Future capabilities (change-impact selection, evidence caching, finding lifecycle, waivers, CI,
  scheduling, runtime / performance / hardware / deployment audits) are listed in
  `docs/verification/architecture-audit.md` §10. They extend the manifest and the builtins; the
  result model and run layout stay as they are.
