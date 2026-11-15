# Process Protection

**Normative rule:** `docs/requirements/development_operations.md` REQ-DEVOPS-023 (process protection), REQ-DEVOPS-015..019
**Enforcement:** protected artifacts — `scripts/completion_gate.py` and `scripts/invariant_check.py --strict`, both through `scripts/process_protection.py` (ADR-000185); no silent override — `scripts/invariant_check.py`, `scripts/drift_detect.py`

## Protected Artifacts

The autonomous agent must not casually modify the rules governing autonomous development:

- `AGENTS.md`
- `docs/requirements/development_operations.md`
- `docs/process/state-model.md`
- `docs/process/process-protection.md` (this file)
- `docs/process/escalation.md`
- `docs/process/autonomous-operating-model.md` (system description)
- Lifecycle definitions, completion rules, traceability rules, verification rules, canonical directory definitions

## Distinction

```
APPLICATION CHANGE          — behavior under docs/requirements/<domain>.md, code under the project's source directories
DEVELOPMENT-SYSTEM CHANGE   — changes to artifacts above
```

An ordinary feature/defect request must not modify protected artifacts.

## Required Mechanism for Development-System Change

1. Create a request file in `requests/inbox/` explicitly describing the process change.
2. Create a work item with `Type: process-change` in header and link to an ADR under `docs/decisions/`.
3. The ADR must explain why the lifecycle/authority/verification rule must change and its impact.
4. Register every protected artifact the request changes (`scripts/claim.py --own`). The completion gate refuses the work unless its record is `Type: process-change` with a `Decision:` ADR (see Enforcement in Scripts).
5. Human review required for all `process-change` work (see `docs/process/escalation.md`).

## No Silent Override Rule

An agent must never silently override:

- requirements
- architectural decisions
- process rules
- existing contracts (proto, file formats)
- test expectations

If implementation appears to require violating a requirement, the agent must STOP, record the conflict in the work file, and surface via `needs-review`. If the requirement is wrong, change the requirement explicitly (with work linkage). If architecture is wrong, create/update ADR explicitly. Never make code the hidden authority.

Automated check: `scripts/invariant_check.py` flags structural invariant violations; `scripts/drift_detect.py` flags requirement with no implementation/work reference and vice versa.

## Enforcement in Scripts

The explicit files under Protected Artifacts are listed once, in
`scripts/process_protection.py` (`PROTECTED_ARTIFACTS`). Both checks read what a
request has **registered** in its claim (`scripts/claim.py --own`), not what is
dirty in the working tree: in a shared checkout a modified protected file may be
another session's work (ADR-000090, ADR-000185).

- `scripts/completion_gate.py` fails a work item whose request registered a
  protected artifact unless the work record is `Type: process-change` with a
  `Decision:` header naming an ADR.
- `scripts/invariant_check.py` reports `PROTECTED_FILES_TOUCHED_WITHOUT_PROCESS_CHANGE`
  for every `processing` request that registered a protected artifact and whose
  request type is not `PROCESS-CHANGE`: a WARN by default, a FAIL with `--strict`.
- `scripts/process_next_request.py` completes work only through the completion
  gate (its steps 12 and 15), so it inherits the gate's check; it has no check
  of its own.

A protected artifact changed without being registered is not seen by either
check; registering every path before editing it is already required
(AGENTS.md, Concurrent Agents).

## Reopening vs History Rewrite

Completed work that would require changing a protected artifact must not be reopened by moving files back to `active`. Create new WORK that `Supersedes: WORK-XXXXXX` and preserve history.
