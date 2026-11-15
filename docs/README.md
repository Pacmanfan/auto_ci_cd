# <PROJECT> Documentation — Canonical Entry Point

This is the canonical documentation root. Read `AGENTS.md` first for the
operating constitution, then use this map:

```text
requests/                       human request intake
    |
docs/requirements/              normative behavior
docs/decisions/                 active architecture decisions
    |
work/                           active implementation records
    |
tests + evidence/               executable proof and verification records
    |
docs/architecture/              current system explanation
```

## Entry points

* `docs/requirements/README.md` and `docs/requirements/requirements.txt` —
  normative requirement owners and IDs. See `docs/requirements/development_operations.md` for lifecycle rules (REQ-DEVOPS-001..050).
* `docs/architecture/README.md` — current architecture and subsystem map (PROJECT-SPECIFIC).
* `docs/decisions/README.md` — active ADRs; historical records are under
  `docs/history/decisions/`.
* `CI_CD.md` — technical map of the development-control system as implemented (components, lifecycle, state, verification).
* `docs/process/configuration.md` — `devsys.json`: every project setting, bootstrapping a project (`scripts/devsys.py install / init / doctor`), architecture rules, record formats (ADR-000199).
* `docs/process/portability.md` — supported environment and the platform primitives (`scripts/devsys_platform.py`).
* `docs/process/engineering-rules.md` — practical workflow summary.
* `docs/process/state-model.md` — valid states/transitions (authoritative).
* `docs/process/autonomous-operating-model.md` — operating manual for the autonomous system.
* `docs/process/chat-intake.md` — chat → canonical request convergence.
* `docs/process/crash-investigation.md` — crash investigation workflow.
* `docs/process/build-contract.md` — canonical BUILD→TEST→VERIFICATION contract.
* `docs/process/recovery.md` — failure recovery; `docs/process/escalation.md` — human escalation; `docs/process/process-protection.md` — process protection.
* `docs/process/historical-id-collisions.md` — how an accepted historical id collision is declared and how `drift_detect` classifies a duplicate (ADR-000149).
* `docs/process/worker-orchestrator.md` — the **Worker Orchestrator**: workers that execute this system's requests unattended (one active request per worker, claims through the framework, gated completion, verified recovery), `scripts/devsys.py workers | queue | priority`, the Qt control panel `tools/worker-panel` (WORKER-001..013, `docs/requirements/worker_orchestration.md`, ADR-000200).
* `docs/process/agent-profiles.md` — the agent protocol workers speak, agent profiles per worker slot (`workers.profiles` / `workers.assign`), and the documented vendor-specific profiles (configuration only; adapters in `scripts/workers/profiles/`).
* `docs/process/agent-coordination.md` — **agent coordination**: the roster of agents with friendly names, the messages agents and the operator send each other (`scripts/msg.py`), the web control panel (`tools/panel/web_panel.py`) and the terminal dashboard (`tools/panel/dash.py`), shared across git worktrees; the station (`tools/panel/station.py`) that the first agent starts: push delivery through a local hub, panels that end with the last agent (COORD-001..012, `docs/requirements/agent_coordination.md`, ADR-000201, ADR-000202).
* `docs/history/worker-orchestrator-merge.md` — historical: how the former external orchestrator was imported (phase 1) and merged (phase 2).
* `docs/verification/tests.md` — canonical verification commands.
* `scripts/test_manifest.json` + `scripts/run_tests.py` — the registered test suites and the tiered, affected-test orchestrator (REQ-DEVOPS-046, ADR-000186).
* `docs/audits/current-state.md` — current risks, known gaps and architecture invariants.
* `docs/verification/architecture-audit.md` — the **architecture / conformance audit**: `./tools/audit [quick|standard|baseline]` measures the implementation against the canonical documents (reusing the existing check scripts and the test orchestrator), records PASS / DRIFT / UNKNOWN per documented rule with evidence under `.audit/runs/AUDIT-*/`, compares with the previous audit and writes a compact AI audit packet; read-only and advisory (ARCH-AUDIT-*, `docs/requirements/architecture_audit.md`, ADR-000195).
* `work/README.md` and `work/work.txt` — active work state.
* `evidence/README.md` and `evidence/evidence.txt` — durable verification proof.

PROJECT-SPECIFIC: add one entry per subsystem architecture document,
interface / file-format specification, user manual and developer guide.

## Current architecture location

Current architecture documents are under `docs/architecture/`. The repository
does not use parallel `desired/` and `actual/` trees. Historical migration
documents may mention those former paths; they are not current authority.

## Build/test

Build through the configured adapter (`python3 scripts/devsys.py build build`;
tree `paths.build_dir`, default `build/debug/`). Run the commands in
`docs/verification/tests.md` and record actual results in `evidence/`
(`scripts/evidence.py`). `python3 scripts/devsys.py status` shows what is
happening in the repository.
