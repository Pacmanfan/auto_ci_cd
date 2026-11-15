# Current Engineering State

**Last reviewed:** 2026-09-24 (ADR-000199)
**Evidence:** `evidence/` (this template ships no project evidence; the framework's own
validation is recorded in `CI_CD.md` "Validation")
**Normative behavior:** `docs/requirements/`
**Active work:** `work/work.txt`

## Operating system

* Request intake: `requests/inbox/` (including requests received from command-line/chat sessions).
* Requirements: `docs/requirements/requirements.txt`.
* Active ADRs: `docs/decisions/decisions.txt`.
* Work: `work/work.txt` and `work/tasks/`.
* Evidence: `evidence/evidence.txt`; historical audits: `docs/audits/evidence/` (PROJECT-SPECIFIC, not extracted).
* Machine state: `.agent/state/`.
* Structural verification: `bash scripts/verify_development_system.sh` (`python3 scripts/devsys.py verify-structure`).
* What is happening now: `python3 scripts/devsys.py status`; configuration: `devsys.json` (`docs/process/configuration.md`).

## Architecture invariants

PROJECT-SPECIFIC. List the project's architectural invariants here — the
structural rules that must never silently disappear (module ownership,
framework independence, single integration funnels, lifetime and ownership
rules, protected performance contracts). Declare the checkable ones as
rules in `devsys.json` `invariants` (checked by `scripts/invariant_check.py`
and the audit's `arch.rules`); keep this list, those rules and `AGENTS.md`
"Repository Constraints" in agreement.

## Verification baseline

Use `docs/verification/tests.md` and record actual results in `evidence/`.
(The source project recorded here which suites its bootstrap session had
verified.) Counts and classifications belong in the evidence
record, not in this summary.

## Known process gaps

* Existing domain requirements use stable but historically varied identifier
  namespaces; new process requirements use `REQ-DEVOPS-*`.
* Existing historical task/audit documents contain obsolete paths; they remain
  historical and are being corrected only when they are presented as current
  navigation or authority.
* Full application runs can expose pre-existing live-hardware/environment failures;
  each run must classify them explicitly.
