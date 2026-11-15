# Request: Desktop icons: put an icon on the operator's desktop that launches the worker co

**Request ID:** REQ-CHAT-20261115-004
**Source:** chat
**Type:** FEATURE
**Priority:** NORMAL
**Created:** 2026-11-15T00:50:12-05:00
**Status:** completed
**Attachments:** - none

## Original User Statement

> Desktop icons: put an icon on the operator's desktop that launches the worker control panel (the Qt worker UI), and an icon for the web dashboard link (it opens the web control panel in the browser, starting the station when none is running).

## Classification

- Type: FEATURE (final; validated by `scripts/request_classification.py`, REQ-DEVOPS-045)
- Priority: NORMAL
- Source: chat
- classification:
  - proposed_type: FEATURE (override)
  - final_type: FEATURE
  - heuristic_type: INVESTIGATION (confidence low)
  - validation: PASS
  - classifier_reason: override FEATURE accepted (text alone suggests INVESTIGATION: no classifiable statement; recorded for investigation)

## Inferred Intent (agent interpretation — not user statement)

Intent: implement requested capability per classification; derive requirement, implement, test, verify.
Conversational context: Operator chat 2026-11-15: 'put a icon to launch the worker UI on my desktop, and an Icon for the dashboard link'

## Observed / Unknown

- Observed: see original statement above.
- Unknown: exact scope — agent to derive
- Follow-ups will be appended under ## Follow-up Context

## Follow-up Context

Operator chat 2026-11-15: 'put a icon to launch the worker UI on my desktop, and an Icon for the dashboard link'

## Processing Notes

- This is the canonical persistent request artifact. Conversation history is contextual evidence; this file is authoritative.
- After intake, workflow is `requests/inbox -> processing -> intent -> requirement -> work -> implementation -> test -> verification -> documentation -> complete` per `docs/process/state-model.md` and `docs/process/autonomous-operating-model.md`.
- Next: agent claims via `scripts/claim.py`, then `scripts/preflight.py`, then requirement/work linkage.

## Traceability

- Intent: pending (created from this request)
- Requirement: pending
- Work: WORK-000006
- Evidence: pending
