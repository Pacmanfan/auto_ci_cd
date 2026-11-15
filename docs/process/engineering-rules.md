# Engineering Rules

The normative process is `docs/requirements/development_operations.md` and the
durable operating constitution is `AGENTS.md`. This file explains the practical
workflow without duplicating those authorities.

## Lifecycle

```text
requests/inbox
  -> intent and requirement owner
  -> docs/decisions/ when architecture changes
  -> work/active
  -> implementation and tests
  -> evidence/
  -> current documentation and traceability audit
  -> requests/completed or requests/rejected
```

Requests received in command-line or chat sessions enter the same lifecycle and
must be recorded before implementation begins.

## Rules

* Requirements live in `docs/requirements/`; use `requirements.txt` to find the
  owner and preserve stable domain IDs.
* Active ADRs live in `docs/decisions/`; history is under `docs/history/`.
* Active work lives in `work/`; source-local task notes (if any) are orientation or
  historical material.
* Tests prove requirements. Evidence records commands, results, revision,
  environment, artifacts, and failure classification.
* Build through the configured adapter (`python3 scripts/devsys.py build build`,
  `devsys.json` `build`; tree `paths.build_dir`, default `build/debug/`); use
  `docs/verification/tests.md` for the canonical commands.
* PROJECT-SPECIFIC durable code rules (the source project: static UI in `.ui`
  files, runtime behavior in C++; its automation wire schema and runtime
  discovery as the only automation contract, no parallel command/tool protocol).

## State model

Valid states and transitions are defined in `docs/process/state-model.md`.
Status strings outside that set are drift (`scripts/drift_detect.py`).

## Claim / lock

Before processing `requests/inbox/` or `work/queued/`, an agent must claim
the item via `scripts/claim.py` (or `devsys.py next --claim`). Lock files live
in `.agent/state/claims/` (`request-*.lock`, `work-*.lock`) and carry the
agent id, heartbeat, owned paths and — with `--owner-pid` / `AGENT_OWNER_PID`
— the identity of the session's process, from which every reader derives
ALIVE / DEAD / ACTIVE / STALE. See `docs/process/recovery.md` for recovery.
Do not process a claimed item without owning the lock.

## Preflight and completion

* `scripts/preflight.py --request <file> --work <id>` before implementation.
* Work and evidence records through `scripts/work.py` and `scripts/evidence.py`,
  never by moving or editing manifests by hand.
* `scripts/completion_gate.py --work WORK-XXXXXX --record` before marking
  complete; the completion commands require its PASS record.
* `scripts/drift_detect.py`, `scripts/doc_integrity.py`, `scripts/invariant_check.py`, `scripts/health_check.py` are safe to run at any time.

## Human escalation

`needs-review` is the explicit escalation state (`requests/needs-review/`,
`work/needs-review/`). Conditions are listed in
`docs/process/escalation.md`. Do not leave ambiguous work in `blocked`.

## Conflict and completion

Stop on requirement/architecture/code/test conflict, record the conflict, and
resolve it through the normative owner or an ADR. Compilation is not completion:
requirements, implementation, tests, evidence, documentation, and traceability
must agree before a work item is marked complete.

## Process protection

Application changes vs development-system changes are distinct
(`docs/process/process-protection.md`). Modifying `AGENTS.md`,
`docs/requirements/development_operations.md`, `docs/process/state-model.md`,
or lifecycle/completion/traceability rules requires an explicit
`process-change` work item and ADR — never as a side effect of feature work.

## Dry run

`scripts/process_next_request.py --dry-run` reports intended requirement,
work, scope, tests, and docs without mutating the repository.
