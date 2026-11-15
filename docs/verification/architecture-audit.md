# Architecture / Conformance Audit

**Requirements:** `docs/requirements/architecture_audit.md` (ARCH-AUDIT-001..010) · **Decision:** ADR-000195 ·
**Work:** WORK-000315 · **Evidence:** VER-000316 (baseline) ·
**Pre-implementation audit:** `docs/audits/architecture-audit-system-audit-2026-09-24.md`

```bash
./tools/audit quick                   # ~10 s: static checks, existing audit scripts, consistency
./tools/audit                         # standard, ~30 s: + build-tree probes, toolchain, structure
./tools/audit baseline --build-dir build/<tree>   # + the unit tier through scripts/run_tests.py
./tools/audit --list                  # every check and the profiles that run it
```

Each run writes `.audit/runs/AUDIT-YYYYMMDD-NNN/` and prints where the report is.

## 1. Purpose

The audit repeatedly measures how far the implementation matches the project's own canonical
documents: requirements, architecture, the constitution (`AGENTS.md`), ADRs, the build and test
contracts. It records every result with evidence, so the next audit, a developer or a development AI
can see what holds, what drifted, and what nobody can currently verify.

It is a measurement, not a gate and not a fixer. It never edits code or documents, and nothing in the
request lifecycle depends on it.

## 2. Scope of Phase 1

**Checked:**

| Domain | Checks (manifest IDs) | Canonical source |
|---|---|---|
| Architecture (PROJECT-SPECIFIC) | `arch.rules` (builtin `architecture_rules`, severity gate): every rule declared in `devsys.json` `invariants`, SKIPPED (not configured) while there is none (ADR-000199); `_example_checks` in `.audit/manifest.json` shows further kinds the source project used: module-boundary scripts (`command`), `invariant_check.py` slices (`invariant`), declared build dependencies and their direction (`builtin` `declared_dependencies` / `dependency_direction`), and documented rules no tool can verify yet (`unverified`, always UNKNOWN) | AGENTS.md Repository Constraints, the project's requirements |
| Requirements | `req.index-coverage`, `req.undefined-references`, `req.verification-coverage` (UNKNOWN by nature) | `requirements.txt`, REQ-DEVOPS-004/008/009 |
| Documentation | `doc.referenced-paths` (backticked repository paths in `AGENTS.md`, `docs/README.md`, the "Current references" of `docs/architecture/README.md` and every architecture document `docs/README.md` names), `doc.integrity` | AGENTS.md, `docs/architecture/README.md`, REQ-DEVOPS-026 |
| Build | `build.health` (`scripts/build_health.py` over the configured adapter), `build.freshness` (+ `build.qt6-only` as an example toolchain check) | build contract, REQ-DEVOPS-035/046/049 |
| Tests | `test.recorded-results`, `test.unit-tier` (baseline) | REQ-DEVOPS-046 |
| Static | `static.warning-policy` (UNKNOWN placeholder until the project adds its policy test) | REQ-DEVOPS-040 |
| Process | `proc.drift`, `proc.structure` | REQ-DEVOPS-020/044, REQ-DEVOPS-001..014 |
| Audit | `audit.read-only` | ARCH-AUDIT-006 |

The run also records an **inventory**: facts, not rule evaluations. It covers:

* git state;
* environment;
* per-module file / source / test / documentation counts (module map in the manifest);
* the declared qmake project graph;
* documentation counts, and the architecture documents not listed as current;
* requirement counts per document;
* the test registry by tier;
* build metadata (tree, configuration, compiler and version, Qt versions, warnings-as-errors);
* TODO / FIXME counts;
* static-analysis or CI configuration present.

**Not checked in Phase 1** (listed as `SKIPPED` / `NOT APPLICABLE` where a check entry exists):

* builds (the source project listed a framework-free container build here);
* release packaging and smoke (`build.release-package`);
* integration / GUI tiers and the perf, stress, soak, live (hardware) and package lanes (`test.runtime-lanes`);
* runtime behaviour, performance, hardware, deployment targets;
* prose in architecture documents that no longer matches the code, which only a reader (human or AI) can judge.

## 3. Principles

* Read-only by default. Evidence before conclusions; deterministic checks before AI interpretation.
* No automatic fixes and no edits to source, requirements, architecture or documentation.
* PASS / FAIL / DRIFT / UNKNOWN / SKIPPED are the result states. Inability to verify is never PASS. There is no overall score.
* Every finding has evidence. Every check names the documented rule it evaluates and the document that
  states it. The audit adds no architecture rule of its own.
* Reuse the project's tools: wrap them, parse their verdict, keep their severity. Do not duplicate
  the build or test infrastructure.
* Bounded cost: cost classes, profile budgets, per-check timeouts. Nothing that builds, transmits,
  opens hardware or runs long lanes.
* The runner stays small and readable: `tools/audit_system/audit.py` (runs, IDs, comparison,
  retention), `checks.py` (parsers, builtins, inventory), `report.py` (Markdown).

## 4. Profiles and cost classes

| Cost class | Meaning | quick | standard | baseline |
|---|---|:-:|:-:|:-:|
| `fast` | deterministic, seconds, reads the repository | ✓ | ✓ | ✓ |
| `moderate` | reads the build tree or runs several tools, under a minute | | ✓ | ✓ |
| `tests` | runs existing tests through `scripts/run_tests.py` (unit tier `t1` by default) | | `--run-tests` | ✓ (`--no-tests` to omit) |
| `expensive` | builds, lanes, containers, hardware, packaging | | | |
| `ai` | `--ai`: interpretation of the packet, never evidence | opt-in | opt-in | opt-in |

* **quick**: for frequent use while working, about 10 s. Budget 60 s.
* **standard**: the normal development audit, about 30 s. Adds the build-tree boundary check
  (project-specific), the toolchain, test-binary freshness and
  the development-system structure. Budget 180 s.
* **baseline**: a snapshot meant to be kept. Adds the unit tier of every registered suite, run
  through `scripts/run_tests.py` (cached groups are not re-run; stale binaries are reported STALE and
  never built). Budget 1200 s. Baseline runs are never pruned.

Options:

* `--build-dir DIR`: the build tree to inspect (default `build/debug`; it is never built).
* `--run-tests t0|t1|t2`: execute tests in any profile.
* `--no-tests`: a baseline without test execution.
* `--check ID`: run only this check (repeatable).
* `--previous AUDIT-ID`: compare with a specific run.
* `--json`: print `audit.json`.
* `--ai`: see §8.

To know the build and test state of the current sources, build a tree first (a private one such as
`build/<name>-Debug` when the shared tree is in use) and pass it with `--build-dir`. The audit reports
a stale tree as UNKNOWN rather than building it.

## 5. Result states

| State | Meaning | Examples |
|---|---|---|
| `PASS` | the rule was evaluated and holds | the boundary script exits 0; every documented path exists |
| `FAIL` | a rule the manifest marks `"severity": "gate"` was evaluated and does not hold (ADR-000199) | `proc.drift`, `proc.structure`, `doc.integrity`, `arch.rules`, `test.recorded-results` finding a violation |
| `DRIFT` | an advisory rule was evaluated and the implementation (or a document it names) deviates | the root project declares a dependency the architecture does not allow; a test cites an undefined requirement ID |
| `UNKNOWN` | the rule could not be evaluated, or the only check cannot show it holds | presence-only invariant heuristics; stale build tree; requirement coverage by citation; a tool that timed out |
| `SKIPPED` | not part of this profile, or its mechanism is not configured (the tool says NOT CONFIGURED or exits 10); the reason is recorded | `moderate` checks in quick; `expensive` checks always (NOT APPLICABLE); `arch.rules` without rules |

The audit is advisory by default (exit 0). `./tools/audit <profile> --fail-on fail|drift|unknown`
exits 1 when a check has that state or a worse one, for use as a gate in CI. The audit also runs on a
copy that is not a git repository (file walk instead of `git ls-files`).

Reused tools with their own severities: their FAIL class is DRIFT. Their WARN / ACCEPTED classes
are counted in the metrics and compared across audits, but never promoted. For example,
`doc_integrity.py`'s 900+ WARNs are almost all `docs/user` links to generated pages.

A DRIFT means the implementation and a canonical document disagree. The audit does not decide which
one is wrong. The owner of the rule does, and records it as a requirement or ADR change or as a fix
request.

## 6. Evidence model and identity

* **Audit ID:** `AUDIT-<local date>-<NNN>`, the next number of the day, reserved by an exclusive
  `mkdir` so concurrent runs never collide. It appears in every artifact.
* **Run record** `audit.json`:
  * `audit_id`, `tool_version`, `manifest_version`, `profile`, `profile_costs`;
  * `started_at` / `finished_at` / `duration_seconds` / `budget_seconds`;
  * `git` (branch, commit, describe, commit date, modified / untracked counts);
  * `environment`, `canonical_sources`, `summary` (per domain and totals);
  * `checks[]`, `inventory`, `comparison`, `retention_removed`.
* **Check result** (`checks[]`):
  * `id`, `domain`, `title`, `rule`, `source`, `kind`, `cost`, `method`;
  * `status`, `summary`, `details[]`, `metrics{}`, `seconds`;
  * `evidence_file`, and `future_evidence` for UNKNOWN.
* **Evidence file** `evidence/<check id>.txt`: header (audit, check, rule, source, status, summary),
  then the exact command with exit code, duration and full output, or the data a builtin examined:
  the declared graph, the missing paths with document:line, the undefined IDs with the citing files,
  the per-group test records.
* **Comparison** (with the latest completed audit of the same profile, else any):
  * status changes;
  * coverage changes (to or from SKIPPED);
  * metric and finding changes;
  * commits and files since the previous commit;
  * working-tree changes;
  * touched modules.

## 7. Reports

* `audit.md`: the header (ID, profile, commit, duration) and per-domain counts. It lists the DRIFT
  findings with evidence paths and the changes since the previous audit. It covers build and tests,
  the unverified areas (why and what evidence would settle each), and next actions.
* `audit-packet.md`: for a development AI (§8).
* The console prints the totals, every DRIFT, the status changes and the paths of both reports.

## 8. AI integration

The deterministic audit needs no AI. `audit-packet.md` contains:

* the rules for interpreting the results;
* the identity and the canonical sources;
* the module inventory and the declared dependency graph;
* the architecture documents not listed as current;
* the requirements summary and every check result with its evidence path;
* DRIFT details, unknowns with future evidence, build, tests, changes and the previous audit's totals;
* a short list of suggested questions.

It references evidence files instead of repository content. An assistant reads it, then opens the
evidence or source files it needs.

`--ai` is optional and vendor-neutral. It pipes the packet into `$AUDIT_AI_COMMAND` (any command
that reads a prompt on stdin; timeout `$AUDIT_AI_TIMEOUT`, default 900 s) and stores the answer as
`ai-analysis.md`, headed "interpretation; not evidence". Without the variable, `--ai` only prints
where the packet is.

## 9. Artifacts, git, safety and runtime

* `.audit/manifest.json` and `.audit/README.md` are tracked. `.audit/runs/` is git-ignored.
  Retention keeps the newest `retention.max_runs` runs (30, current included) plus every baseline run.
  Only audit run directories are ever removed.
* A baseline to be preserved goes into an evidence record: copy `audit.json`, `audit.md` and
  `audit-packet.md` into `evidence/VER-*/` (VER-000316 holds the first). Do not commit whole run
  directories.
* Read-only: the audit's only writes are its run directory. With test execution,
  `scripts/run_tests.py` also writes its usual run record and cache in the build tree, and its
  scratch under `tmp/tr/`. Each run compares the working tree's status and content hashes
  and HEAD before and after (`audit.read-only`). A change made by another session during the run is
  reported UNKNOWN with the paths.
* Never done: building; network or hardware access; writing application data, user
  profiles or test data. Subprocesses run with `PYTHONDONTWRITEBYTECODE=1`.

## 10. Future expansion (not implemented)

The manifest and the check kinds are the extension points. None of the following needs a new result
model:

* **Change-impact selection:** changed file → module (manifest map) → requirement → checks, using
  `scripts/test_selection.py` as `run_tests.py` does, so quick audits run only affected checks.
* **Evidence caching:** keyed by file hashes, commit, tool version and manifest version.
* **More architecture rules:** explicit machine-verifiable contracts. For example, turn each
  UNKNOWN above into a real check, following its `future_evidence`.
* **Runtime, performance and hardware audits:** safe probes on a test host; perf-lane baselines
  (throughput, latency, CPU / GPU, queue depth, UI responsiveness); hardware / driver capability.
* **Deployment audits:** target platforms; bundled dependencies, packaging, installer (reusing the
  project's deployment and package-validation scripts).
* **Finding lifecycle and exceptions:** NEW / ACKNOWLEDGED / IN_PROGRESS / RESOLVED / REOPENED /
  WAIVED / SUPERSEDED; expiring documented exceptions (like `scripts/test_quarantine.json`).
* **History and trending:** regressions, resolved, persistent and new unknowns over many runs.
* **AI-assisted review:**
  * architecture drift in prose;
  * requirement completeness;
  * cross-module consistency;
  * suspicious duplication;
  * boundary violations the static checks miss.
* **CI and scheduling:** selected profiles unattended. A check that gates anything becomes a process
  change (ADR-000195 §8).

## 11. Adding a check

1. Find the rule in its canonical document. If no document states it, the rule belongs there first
   (requirement or ADR), not in the audit.
2. Prefer an existing tool. Add a manifest entry with `kind: command`, the command and a `parse`
   (`exit`, `findings-json`, `test-plan`, `test-run`); `{build_dir}` and `{run_tests_tier}` are
   substituted. For an `invariant_check.py` rule use `kind: invariant` with `match` prefixes and
   `strength: enforced | heuristic`.
3. Otherwise write a small builtin in `tools/audit_system/checks.py` (`(ctx, params, check) ->
   result(...)`), register it in `BUILTINS`, and reference it with `kind: builtin`, `function` and
   `params`.
4. If nothing can verify the rule yet, add `kind: unverified` with a `reason` and `future_evidence`,
   so the gap stays visible.
5. Give it a `domain`, `rule`, `source`, `cost` (and `timeout_seconds`). Add a case to
   `tools/audit_system/tests/test_audit.py` (suite `audit_py`). Run `./tools/audit --check <id>`.

## 12. Baseline

*(Extraction note: the source project recorded its first baseline audit here — its result counts,
the genuine pre-existing DRIFTs it found (a declared dependency against the architecture rule, a
decoder definition without a command, an unindexed requirement document, a test citing an undefined
requirement ID, stale paths in architecture documents) and two build-tree DRIFTs of a shared tree.
Record this project's first baseline here and preserve it in an evidence record.)*
