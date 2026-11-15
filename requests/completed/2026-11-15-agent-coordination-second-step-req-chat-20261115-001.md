# Request: Agent coordination, second step: (1) agent names are chosen at random, but the f

**Request ID:** REQ-CHAT-20261115-001
**Source:** chat
**Type:** FEATURE
**Priority:** HIGH
**Created:** 2026-11-15T00:10:26-05:00
**Status:** completed
**Attachments:** - none

## Original User Statement

> Agent coordination, second step: (1) agent names are chosen at random, but the first agent opened is named 'Cody'; (2) messaging is push based: use a lightweight IPC mechanism so that a message reaches waiting agents and the panels at once instead of being polled; (3) the web dashboard and the control panels start automatically when the first agent starts and end when the last agent closes, and their addresses are printed at start; (4) fix all test failures, including tests that fail when a session's agent variables are inherited.

## Classification

- Type: FEATURE (final; validated by `scripts/request_classification.py`, REQ-DEVOPS-045)
- Priority: HIGH
- Source: chat
- classification:
  - proposed_type: FEATURE (override)
  - final_type: FEATURE
  - heuristic_type: BUG (confidence high)
  - validation: PASS
  - classifier_reason: override FEATURE accepted (text alone suggests BUG: request statement is framed as bug: failures, fix)

## Inferred Intent (agent interpretation — not user statement)

Intent: implement requested capability per classification; derive requirement, implement, test, verify.
Conversational context: Operator chat message 2026-11-15: 'ensure name selection is random. - however the first agent opened should be named Cody. I want messageing to be push based. ensure that is true. maybe we can use a lightweight IPC mechanism. Ensure the web dashboard and control panels are both autostarted when the fist agent starts and ends when the last agent closes. print the addresses to start. Fix all test failures. Continue with all previous tasks'

## Observed / Unknown

- Observed: see original statement above.
- Unknown: exact scope — agent to derive
- Follow-ups will be appended under ## Follow-up Context

## Follow-up Context

Operator chat message 2026-11-15: 'ensure name selection is random. - however the first agent opened should be named Cody. I want messageing to be push based. ensure that is true. maybe we can use a lightweight IPC mechanism. Ensure the web dashboard and control panels are both autostarted when the fist agent starts and ends when the last agent closes. print the addresses to start. Fix all test failures. Continue with all previous tasks'

## Processing Notes

- This is the canonical persistent request artifact. Conversation history is contextual evidence; this file is authoritative.
- After intake, workflow is `requests/inbox -> processing -> intent -> requirement -> work -> implementation -> test -> verification -> documentation -> complete` per `docs/process/state-model.md` and `docs/process/autonomous-operating-model.md`.
- Next: agent claims via `scripts/claim.py`, then `scripts/preflight.py`, then requirement/work linkage.

## Traceability

- Intent: pending (created from this request)
- Requirement: pending
- Work: WORK-000003
- Evidence: pending
