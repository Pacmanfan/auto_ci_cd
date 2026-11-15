# Crash Investigation Workflow

**Normative rule:** `docs/requirements/development_operations.md` REQ-DEVOPS-013 + Phase 3
**Crash storage:** `log/dump/<APP>_*.core` (PROJECT-SPECIFIC: the application's crash-dump writer)
**Tools:** `scripts/create_request.py`, `scripts/crash_evidence.py`

## Standard Workflow

```
User Report ("I loaded a file and the application crashed.")
  ↓
Crash Request (Type: CRASH, Source: chat|inbox)
  ↓
Evidence Discovery (crash_evidence.py)
  ↓
Reproduction Attempt
  ↓
Crash Analysis (GDB, logs, commits)
  ↓
Root Cause
  ↓
Requirement / Correct Behavior
  ↓
Fix
  ↓
Regression Test (fails before, passes after)
  ↓
Verification (build + tests + evidence VER-NNNNNN)
  ↓
Documentation
  ↓
Complete
```

If reproduction is impossible, do not falsely claim reproduction. Record: reported behavior, evidence available, investigation performed, suspected cause, confidence, verification limitations.

## Required Investigation Questions

1. What was the user doing?
2. What was expected?
3. What actually happened?
4. Is a crash dump available? (`log/dump/` or fallback reported by `crash_dump::DumpDirectory()`)
5. Is there a relevant log?
6. Is there a stack trace?
7. Is it reproducible?
8. What code path was involved?
9. What changed recently? (`git log --oneline -20`, `git diff`)
10. What is the first broken boundary?
11. What is the root cause?
12. What is the smallest correct fix?
13. What regression test is required?

## Evidence Discovery

On crash request creation, automatically run:

```bash
python3 scripts/crash_evidence.py --request requests/inbox/req-crash-*.md
# or --discover for ad-hoc discovery
```

It records:

- newest ELF core under `log/dump/` (or the application's fallback dump directory, `FALLBACK_DUMP_DIR` in `scripts/crash_evidence.py`); file type via `file`; GDB command to run.
- application logs (`log/*.log`)
- recent commits (`git log --oneline -10`)
- changed files (`git status --porcelain`)

If evidence unavailable, record "unavailable" — do not invent. Investigation can still proceed with limited confidence.

For GDB analysis (when core exists):

```bash
file log/dump/<APP>_*.core
gdb build/debug/<APP_BINARY> log/dump/<APP>_*.core
# bt
# thread apply all bt
# info registers
# info sharedlibrary
```

Link the core artifact from the relevant `VER-*` record.

## Distinguish Facts

Separate in the work file and verification record:

- `OBSERVED FACT` — user report, dump existence, log line, stack frame.
- `HYPOTHESIS` — suspected cause before confirmation.
- `CONFIRMED ROOT CAUSE` — verified via reproduction and code path.

Do not document hypothesis as fact.

## Investigation Outcomes

Do not investigate forever. Valid terminal states:

- `ROOT_CAUSE_CONFIRMED` — cause verified, fix implemented.
- `FIX_IMPLEMENTED` — fix verified with regression test.
- `NOT_REPRODUCIBLE` — cannot reproduce; record what was tried.
- `INSUFFICIENT_EVIDENCE` — evidence missing; record what is needed.
- `BLOCKED` — waiting on dependency or human input.
- `FALSE_REPORT` — behavior is correct.
- `DUPLICATE` — links to existing request/work.
- `NEEDS_HUMAN_INPUT` — escalate to `needs-review`.

Set outcome in work file header `Investigation-Outcome:` and in request `Status:`.

## Regression Tests for Defects

Every confirmed defect should produce a regression test that:

- fails before fix,
- passes after fix,
- tests the behavior that was actually broken (not just the changed lines).

If impossible (e.g., environment-dependent crash), document why and provide alternative verification (e.g., manual steps, dump analysis).

## Relationship to Requirements

A crash implies the violated correct-behavior requirement (often in `docs/requirements/error_handling.md`, `pipeline.md`, `source_behaviour.md`, etc.). Link the work to that requirement or extend it if the contract was missing.

## Follow-ups

Subsequent user statements ("It only happens with this file", "I found the crash dump") update the same request via:

```bash
python3 scripts/create_request.py --follow-up REQ-CRASH-... --text "..." --attach path
python3 scripts/crash_evidence.py --request-id REQ-CRASH-...
```

Preserve observation history under `## Follow-up Context`.
