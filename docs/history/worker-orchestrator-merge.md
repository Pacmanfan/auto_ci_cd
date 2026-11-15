# Worker Orchestrator merge (historical record)

> **HISTORICAL.** How the former external orchestrator became the development
> system's Worker Orchestrator. Current authority:
> `docs/requirements/worker_orchestration.md`, ADR-000200,
> `docs/process/worker-orchestrator.md`.

## Phase 1: map and copy (2026-09-24)

The former orchestrator was a separate installation with its own git
repository (`<FORMER_ORCHESTRATOR_REPO>`, HEAD `61a48e6`, version 1.2.0) that
processed the source application's requests unattended.

- **Copied:** its 34 tracked files were inventoried and 32 were copied into
  `worker_orchestrator/`.
- **Left external:** the application-specific instance configuration and
  project prompt.
- **Not imported:** runtime state (run records, transcripts, test sandboxes),
  bytecode and history.
- **Sanitized:** 18 files, where application paths became placeholders and
  entry points were pinned to the copy.

Its own 63 tests passed against this system's scripts only with the
ADR-000199 completion gate relaxed. The stand-in worker completed requests
without a gate record. Four conflicts were recorded:

| # | Conflict |
|---|---|
| C1 | TICKET-ENGINE-001 required the engine outside the repository |
| C2 | copy-anywhere launcher and panel |
| C3 | one worker, no worktrees (ADR-000196) |
| C4 | the engine's ticket ledger as a second ticket system |

The original repository was never modified.

## Phase 2: merged (2026-09-24, ADR-000200)

All four conflicts are resolved by ADR-000200 and WORKER-001..013. The
imported tree was removed after its parts were merged:

| Imported component | Disposition |
|---|---|
| launcher script, terminal control-panel script | retired; replaced by `scripts/devsys.py workers …` and the Qt panel (`tools/panel/`, `tools/worker-panel`) |
| package `__init__` / `__main__` | `scripts/workers/__init__.py` |
| CLI (`start`, `run-once`, `status`, `pause`, `resume`, `stop`, `queue`, `inspect`, `audit`, `runs`, `clear`, `review`, `recover`) | `devsys.py workers start / stop [--now] / status / peek / recover / run [--once] / enable / disable`, `devsys.py queue`, `devsys.py priority`; `pause`/`resume` are stop / start; `clear` (a ledger hold) is obsolete, as holds are `needs-review` in the framework; `audit`, `runs`, `review` are deferred |
| orchestration loop (single worker, runs, ledger, holds) | `scripts/workers/runner.py`, rewritten: one process per worker, no ledger, outcomes from request states, escalation through the framework |
| attempt lifecycle (prompt, launch, wait, finish, report parsing, commit attribution) | `scripts/workers/runner.py` (`start_attempt`, `wait_attempt`, `finish_attempt`) and `scripts/workers/prompt.py` |
| agent session module (vendor CLI command, environment scrub, transcript parsing, identity, verify, kill tree) | `scripts/workers/agent.py`, a generic JSON-lines adapter whose command is configuration; identity, verification, zombie and marker checks and `kill_tree` merged into `scripts/devsys_platform.py` |
| state store (atomic JSON, JSON lines, engine lock) | `scripts/workers/state.py` over `agent_process.atomic_write_text` / `exclusive` and `devsys_platform.lock` |
| run records, ledger, operator control | per-worker records (`state.py`); run records and the ledger dropped; operator control is `desired` / `stop_now` per worker |
| recovery of stale RUNNING records | `runner.recover_active`, `pool.recover` |
| project adapter (ticket listing, ordering by file name, claim-file reading) | retired: the framework library is used directly; ordering is `devsys.queue` (priority, then arrival) |
| validation (git sanity, request state, completion gate, build tree, commands) | `scripts/workers/validation.py`: git sanity, request state, commands; the completion-gate step is replaced by `commit_binding` (`verify-commit`); the build-tree provenance check is deferred |
| read-only git helpers | `scripts/workers/gitops.py` |
| supervisor review, run audit | deferred (ADR-000200); can return as clients of the same API |
| configuration module and instance configuration | `devsys.json` `workers` section (`devsys_config.py`); agent command lines in `docs/process/agent-profiles.md` |
| worker, project and supervisor prompts | `scripts/workers/prompts/worker.md` (gated lifecycle: `work.py`, `evidence.py`, `completion_gate.py --record`); the supervisor prompt is deferred with the supervisor |
| tests (63) with a stand-in agent | `scripts/tests/test_workers.py`, `test_worker_panel.py`, stand-in `scripts/tests/fake_agent.py` |
| documentation | `docs/process/worker-orchestrator.md`, `agent-profiles.md`, WORKER-001..013, ADR-000200; the old contract is in `docs/history/requirements/autonomous_ticket_engine.md` |

Every former vendor-specific name has a neutral replacement:

| Former | Now |
|---|---|
| product name | Worker Orchestrator |
| session-marker variable | `DEVSYS_WORKER_SESSION` |
| ticket variables | `DEVSYS_WORKER_REQUEST`, `DEVSYS_WORKER_REQUEST_FILE`, `DEVSYS_WORKER_REQUEST_PATH` |
| report and attempt variables | `DEVSYS_WORKER_REPORT`, `DEVSYS_WORKER_ATTEMPT` |
| (none) | `DEVSYS_WORKER`, `DEVSYS_SCRIPTS` |
| home, config and panel variables | retired; the system derives its root from its location, and configuration lives in `devsys.json` |

The one documented exception is the agent profile
(`docs/process/agent-profiles.md`).
