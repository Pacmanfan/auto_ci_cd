# ADR-000150: Submit-without-processing is an explicit, tested guarantee

**Status:** ACCEPTED
**Date:** 2026-09-20
**Work:** WORK-000253 (REQ-CHAT-20260920-018)
**Requirement:** REQ-DEVOPS-043 (new)

## Context

A caller must be able to submit a request into the ticket inbox with no
requirement that the submitting agent also execute the work
(`SUBMIT ≠ CLAIM ≠ PROCESS`). The request asked for an audit plus a
submit-only operation, suspecting creation might be coupled to assignment
or execution.

## Audit finding

The audit (WORK-000253) found submission is already decoupled:
`scripts/create_request.py` writes only the two inbox files and never
touches claims, worktrees, automation, or processing; `inbox` already
means "submitted, not yet claimed"; claiming is explicit and atomic
(ADR-000090); the only auto-claimer (`process_next_request.py` without
`--dry-run`) is opt-in. No `createTicket() → assign → process` chain
exists to refactor.

## Decision

Do not re-architect the lifecycle. Make the existing decoupling explicit
and testable instead:

1. `scripts/create_request.py` **is** the submit-only operation. Its help
   text and creation confirmation now state the guarantee
   (inbox only; never claims, assigns, worktrees, or processes).
2. `scripts/process_next_request.py::_find_next()` honours sidecar
   priority (CRITICAL > HIGH > NORMAL > LOW, lexicographic tiebreak),
   resolving the code/docs disagreement in favour of the documented
   priority-ordered selection.
3. REQ-DEVOPS-043 normatively pins the guarantee; `chat-intake.md`
   documents the `SUBMIT ≠ CLAIM ≠ PROCESS` workflow.
4. `scripts/tests/test_submit_without_processing.py` locks the behaviour.

## Rejected

* A new ticket database or parallel inbox subsystem — the existing
  `requests/` stores plus the allocator already persist submit-only
  tickets across restarts.
* A `--submit-only` flag that changes behaviour — submission is already
  submit-only; a flag would imply a processing mode that does not exist.
* Touching the state machine, claim protocol, allocator, or automation funnel —
  audited and already correct.

## Consequences

* Callers get a documented, tested submit-then-stop contract.
* Priority-ordered `_find_next()` changes which request a bare
  `process_next_request.py` run picks when priorities differ; ties and
  single-priority inboxes are unaffected (lexicographic as before).
* Process-change review applies (human review required).
