# Request: Windows support: make sure the development system, including the new agent coord

**Request ID:** REQ-CHAT-20261114-002
**Source:** chat
**Type:** BUG
**Priority:** HIGH
**Created:** 2026-11-14T23:48:01-05:00
**Status:** completed
**Attachments:** - none

## Original User Statement

> Windows support: make sure the development system, including the new agent coordination (message bus, web control panel, terminal dashboard, agent profiles and adapter), works under Windows and PowerShell. At least read through the code, identify the major issues, fix what can be fixed safely without a Windows machine, and record what remains.

## Classification

- Type: BUG (final; validated by `scripts/request_classification.py`, REQ-DEVOPS-045)
- Priority: HIGH
- Source: chat
- classification:
  - proposed_type: - (heuristic)
  - final_type: BUG
  - heuristic_type: BUG (confidence high)
  - validation: PASS
  - classifier_reason: request statement is framed as bug: fix, fixed
  - evidence: 'fix' [head]; 'fixed' [head]

## Inferred Intent (agent interpretation — not user statement)

Intent: investigate bug, identify root cause, implement correct behavior per requirement, add regression test.
Conversational context: Chat 2026-11-14, two operator messages: 'We need to make sure this is all working under windows - at least read through it and spot any major issues.' and 'it should all work under powershell'. No Windows host is available to this session: findings come from reading the code.

## Observed / Unknown

- Observed: see original statement above.
- Unknown: exact scope — agent to derive
- Follow-ups will be appended under ## Follow-up Context

## Follow-up Context

Chat 2026-11-14, two operator messages: 'We need to make sure this is all working under windows - at least read through it and spot any major issues.' and 'it should all work under powershell'. No Windows host is available to this session: findings come from reading the code.

## Processing Notes

- This is the canonical persistent request artifact. Conversation history is contextual evidence; this file is authoritative.
- After intake, workflow is `requests/inbox -> processing -> intent -> requirement -> work -> implementation -> test -> verification -> documentation -> complete` per `docs/process/state-model.md` and `docs/process/autonomous-operating-model.md`.
- Next: agent claims via `scripts/claim.py`, then `scripts/preflight.py`, then requirement/work linkage.

## Traceability

- Intent: pending (created from this request)
- Requirement: pending
- Work: WORK-000002
- Evidence: pending
