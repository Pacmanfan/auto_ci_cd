# ADR-000185: Protected artifacts are enforced from claims; evidence is linked only where declared

**Status:** ACCEPTED (human review 2026-09-23: approved)
**Date:** 2026-09-23
**Work:** WORK-000304 (REQ-CHAT-20260923-014, process-change)
**Requirements:** `docs/requirements/development_operations.md` REQ-DEVOPS-023 (extended), REQ-DEVOPS-035, REQ-DEVOPS-044 (extended)
**Related:** ADR-000090 (claims and path ownership), ADR-000155 (completion gate open items), ADR-000162 (test gate commands), ADR-000184 (documentation audit scope)

## Context

The human review of WORK-000267 (REQ-CHAT-20260921-013, 2026-09-23) and its
completion turned up defects outside that change:

* `docs/process/process-protection.md` §Enforcement in Scripts described three
  checks: `invariant_check.py --strict` with
  `PROTECTED_FILES_TOUCHED_WITHOUT_PROCESS_CHANGE`, a completion-gate refusal
  when protected files are dirty in non-process-change work, and a protected-file
  check in `process_next_request.py`. None exists; `invariant_check.py` has never
  accepted `--strict` (VER-000274 noticed this and left it). REQ-DEVOPS-023
  requires the protection but names no mechanism, so the document described
  enforcement the process did not have.
* `completion_gate.py` counted as a work item's evidence every
  `evidence/VER-*.md` that mentioned its id: WORK-000239 was shown VER-000267,
  which only cites it.
* `invariant_check.py` checked the generated-protobuf invariant in
  the project's former wire-schema directory, which no longer existed, so the
  check passed vacuously. It is the same stale path WORK-000267 corrected in
  AGENTS.md.
* `doc_integrity.py` asked git about any root it was given. For a root nested
  inside another work tree (a synthetic tree under the gitignored
  `tmp/`), git answered with the enclosing repository's index and ignore
  rules and listed nothing, so `test_doc_integrity_scope` failed whenever
  `TMPDIR` pointed there.

## Decision

1. **Build the enforcement the document describes, once.**
   `scripts/process_protection.py` lists the explicit protected files and answers
   one question: which of them has a request **registered** in its claim
   (`claim.py --own`)? The completion gate fails a work item whose request
   registered one unless the record is `Type: process-change` with a `Decision:`
   ADR. `invariant_check.py` reports `PROTECTED_FILES_TOUCHED_WITHOUT_PROCESS_CHANGE`
   for every processing request that registered one and is not a `PROCESS-CHANGE`
   request: a WARN by default, a FAIL with the new `--strict`.
   `process_next_request.py` completes work only through the gate, so it gets no
   parallel check; the document now says so.
2. **Claims, not the working tree.** In the shared checkout a dirty protected
   file may be another session's in-flight process change (ADR-000090). A check
   on `git diff` would fail unrelated work, or pass work whose protected edit
   another session had already committed. Registration is the ownership record
   and is required before editing (AGENTS.md, Concurrent Agents). A protected
   file edited without registration is not seen: that breaks the registration
   rule, and `commit_request.py` leaves such an edit out of the request's
   commit (it commits registered paths only).
3. **Evidence is what is declared.** The gate links an `evidence.txt` row whose
   links column names the work item, a VER record named in the work record's
   `Evidence` header or whose own `Work` header names the item, and the file of
   a matching row. Mentioning an id is citation, not evidence.
4. **Point the protobuf invariant at the authority and report its absence.**
   (the project's wire-schema directory, PROJECT-SPECIFIC); a missing directory is a WARN rather than a
   silent pass.
5. **git answers only for its own top level.** `doc_integrity.py` uses
   `git ls-files` only when the root is the work tree's top level
   (`git rev-parse --show-toplevel`), otherwise its documented fallback walk.
   The repository's own scan is unchanged (940 WARN before and after).

## Consequences

* A request that edits AGENTS.md, the state model or another protected file as
  ordinary work cannot complete, and every audit that runs
  `invariant_check --strict` sees it while it is in progress.
* Gate results for historical records change in one case: WORK-000148 has no
  evidence record of its own and was linked only through records that mention
  it. It is reported, not rewritten: evidence cannot be created after the fact.
* No change to the claim protocol, the state model, drift severities or any
  application code.

## Alternatives rejected

* **Correct the document to "not enforced".** REQ-DEVOPS-023 protects these
  files because agents edit them; an unenforced rule is the situation the
  stuck-ticket audit (REQ-CHAT-20260921-011) found for work state.
* **A protected check inside `process_next_request.py` as well.** It would be a
  second implementation of the gate's rule on the same path.
* **Checking dirty protected files in the working tree.** Wrong in a shared
  checkout, for the reasons in Decision 2.
