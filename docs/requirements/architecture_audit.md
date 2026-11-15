# Architecture / Conformance Audit Contract

**Status:** ACTIVE (Phase 1)
**Owner:** Development tooling: `tools/audit`, `tools/audit_system/`, `.audit/manifest.json`
**Depends On:** `development_operations.md` (REQ-DEVOPS-020 drift detection, REQ-DEVOPS-022 invariant checking, REQ-DEVOPS-026 documentation integrity, REQ-DEVOPS-038 build health, REQ-DEVOPS-046 test execution: the mechanisms the audit reuses), `analyst_core.md` (CORE-PROJ-*, CORE-QT-*)
**Decision:** ADR-000195
**Work:** WORK-000315 (REQ-CHAT-20260924-003)
**System documentation:** `docs/verification/architecture-audit.md`

The architecture audit is a repeatable, read-only measurement. It compares the
implementation with the project's own canonical documents and records the
result as evidence. It is advisory. It adds no lifecycle, completion,
traceability or verification rule, and no gate depends on it. It evaluates
only rules that other documents already state. Each rule stays owned by its
source document; this contract owns only how the audit measures and reports.

## ARCH-AUDIT-001 — One command, three profiles

`tools/audit [quick|standard|baseline]` SHALL run the audit from the
repository root without an AI service, a database or a network connection.
Profiles SHALL select checks by declared cost class:

* **quick**: fast deterministic checks only;
* **standard**: quick plus moderate checks that read the build tree or run several tools;
* **baseline**: standard plus the existing tests, run through `scripts/run_tests.py`.

A check whose cost class the profile does not include SHALL be reported
`SKIPPED` with the reason. Checks of the `expensive` class (builds, perf /
stress / soak / live / package lanes, containers, hardware) SHALL NOT run in
any Phase 1 profile and SHALL be reported `SKIPPED` as `NOT APPLICABLE`. Each
profile SHALL have a time budget; a check that would start after the budget is
spent SHALL be `SKIPPED` and say so.

## ARCH-AUDIT-002 — Manifest

The checks, the canonical sources, the module map, the profiles and retention
SHALL be declared in one JSON manifest, `.audit/manifest.json`. A check SHALL
be one of:

* an existing project command with a named result parser;
* a slice of `scripts/invariant_check.py`;
* a named builtin in `tools/audit_system/checks.py`;
* `unverified` (a documented rule without a verification mechanism).

Every check SHALL name the rule it evaluates and the document that states it.
Adding a check of an existing kind SHALL need only a manifest entry.

## ARCH-AUDIT-003 — Result states

Every check SHALL end in exactly one state:

| State | Meaning |
|---|---|
| `PASS` | the rule was evaluated and holds |
| `DRIFT` | the rule was evaluated, and the implementation (or a document it names) deviates from it |
| `UNKNOWN` | the rule could not be evaluated, or the only check available cannot show that it holds (presence-only or sampled heuristics, citation counts) |
| `SKIPPED` | the check is not part of the profile (reason recorded) |

Inability to verify SHALL NOT be reported as `PASS`. A tool that cannot run,
times out, or produces unparseable output SHALL give `UNKNOWN`. For a reused
tool with its own severities, only its failure class SHALL be `DRIFT`; its
warnings SHALL be counted and compared, not promoted. There SHALL be no
overall score.

## ARCH-AUDIT-004 — Evidence

Every check that ran SHALL write its evidence to
`evidence/<check id>.txt` in the run directory: the command and its full
output, or the data a builtin examined. The result SHALL reference that
file. `audit.json` SHALL record for each check its ID, domain, title, rule,
source document, method, cost class, state, summary, details, metrics,
duration and evidence file, and for `UNKNOWN` the evidence that would settle
the rule.

## ARCH-AUDIT-005 — Identity and comparison

Every run SHALL get a unique ID `AUDIT-<local date YYYYMMDD>-<NNN>`, reserved
atomically, and every artifact SHALL carry it. A run SHALL record the
timestamp, the git branch, commit and describe, the working-tree state, the
profile, the tool and manifest versions, and an environment summary. A run
SHALL compare itself with the previous completed audit (by default the latest
of the same profile, else the latest of any profile):

* status changes per check (transitions to or from `SKIPPED` reported separately as coverage differences);
* metric and finding changes;
* commits and files changed since the previous audit's commit;
* working-tree changes;
* the modules those changes touch.

## ARCH-AUDIT-006 — Read-only

The audit SHALL NOT modify source code, requirements, architecture or other
documentation, configuration, application or user data, or hardware state. It
SHALL NOT build, open hardware, or transmit. Its only writes SHALL be its
own run directory under `.audit/runs/`, plus the run record the test
orchestrator itself writes when a profile executes tests. The audit SHALL
compare the working tree (status and content hashes) and HEAD before and
after the run and report the result as check `audit.read-only`. In a
checkout shared with other sessions, a change during the run cannot be
attributed, so it SHALL be reported `UNKNOWN` with the changed paths, never
`PASS`.

## ARCH-AUDIT-007 — Reports

A run SHALL write `audit.json` (all structured results, the inventory and the
comparison), `audit.md` (a concise human report), and `audit-packet.md` (see
ARCH-AUDIT-008). The human report SHALL contain:

* per-domain counts of the four states;
* the DRIFT findings with their evidence paths;
* the changes since the previous audit;
* the build and test state;
* the unverified (UNKNOWN) areas with the evidence that would settle them;
* next actions.

The report SHALL NOT propose changes to requirements or architecture; for each
DRIFT it SHALL leave the decision to the owner of the rule.

## ARCH-AUDIT-008 — AI audit packet

`audit-packet.md` SHALL summarise the deterministic results for a development
AI and reference evidence files instead of repository content:

* project identity, profile, commit and canonical sources;
* the module and declared-dependency inventory;
* the requirements summary;
* every check result;
* DRIFT details and unknowns;
* build, test and change information;
* the previous audit's summary.

The deterministic audit SHALL NOT depend on an AI. `--ai` SHALL be optional
and vendor-neutral: it pipes the packet into the command named by
`AUDIT_AI_COMMAND` and stores the answer as `ai-analysis.md`, labelled as
interpretation, apart from the evidence.

## ARCH-AUDIT-009 — Artifacts and retention

Run directories SHALL be git-ignored (`/.audit/runs/`). The manifest and
`.audit/README.md` are tracked. Retention SHALL bound the number of kept runs
(`retention.max_runs`, current run included). Runs of a kept profile
(`baseline`) SHALL never be removed automatically. Only audit run directories
SHALL ever be removed. A baseline that must be preserved beyond the local
checkout SHALL be copied into an `evidence/VER-*` record.

## ARCH-AUDIT-010 — Phase 1 baselines

The Phase 1 manifest SHALL establish these baselines, each tied to its
canonical source:

* **Architecture:** PROJECT-SPECIFIC — the project's module boundaries (source and build tree), framework-independence invariants, declared build dependencies and their direction, and the remaining AGENTS.md constraints, reported as UNKNOWN until a mechanism exists (the source project's baselines covered its core-library framework boundary, library independence, a generation-pipeline invariant set, a removed-library check, declared dependencies and direction, and its command registry).
* **Requirements:** requirement index coverage; test citations of undefined requirement IDs; verification coverage by citation, reported as UNKNOWN.
* **Documentation:** documented paths in the constitution, the documentation index, the current architecture references and the architecture documents the index names; `scripts/doc_integrity.py`.
* **Build:** toolchain; (PROJECT-SPECIFIC) the supported generator of the audited tree; freshness of the test binaries. The audit does not build, so a stale tree leaves build success UNKNOWN.
* **Tests:** the latest recorded result of every registered group; the unit tier through `scripts/run_tests.py` in baseline.
* **Static:** the central warnings-as-errors policy (PROJECT-SPECIFIC, REQ-DEVOPS-040).
* **Process:** `scripts/drift_detect.py`; the development-system structure.

Verification of this contract: `tools/audit_system/tests/test_audit.py`
(suite `audit_py`), and VER-000316 (baseline, repeatability,
controlled change).
