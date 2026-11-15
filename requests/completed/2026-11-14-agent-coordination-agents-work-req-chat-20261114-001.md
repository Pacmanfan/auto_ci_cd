# Request: Agent coordination: agents working in this repository should coordinate with eac

**Request ID:** REQ-CHAT-20261114-001
**Source:** chat
**Type:** FEATURE
**Priority:** HIGH
**Created:** 2026-11-14T23:00:33-05:00
**Status:** completed
**Attachments:** - none

## Original User Statement

> Agent coordination: agents working in this repository should coordinate with each other instead of only excluding each other. Add (1) friendly agent names and a roster of who is working on what; (2) an inter-agent message bus (direct, broadcast and per-request messages) that works peer-to-peer now and lets a coordinator/lead agent be added later as just another participant; (3) a web control panel run by a Python script, with full control (start/stop/recover/enable/disable workers, set priority, send operator messages), localhost by default; (4) a live CLI dashboard; both must show the messages agents send to each other; (5) support for mixed fleets: Claude Code and opencode major versions 1 and 2, selectable per worker; (6) all of it must work with git worktrees (shared state across worktrees, each agent's worktree/branch visible).

## Classification

- Type: FEATURE (final; validated by `scripts/request_classification.py`, REQ-DEVOPS-045)
- Priority: HIGH
- Source: chat
- classification:
  - proposed_type: FEATURE (override)
  - final_type: FEATURE
  - heuristic_type: FEATURE (confidence high)
  - validation: PASS
  - classifier_reason: request statement is framed as feature: Add, added, enable, must show
  - evidence: 'Add' [head]; 'added' [head]; 'enable' [head]; 'must show' [head]; 'support for' [head]

## Inferred Intent (agent interpretation — not user statement)

Intent: implement requested capability per classification; derive requirement, implement, test, verify.
Conversational context: Chat brainstorm 2026-11-14. Operator decisions: opencode 1 and 2 = two major versions; coordination = peer-to-peer now, lead later; panel scope = full control; process = baseline commit then full request process. Follow-up chat message: 'and support git work trees'.

## Observed / Unknown

- Observed: see original statement above.
- Unknown: exact scope — agent to derive
- Follow-ups will be appended under ## Follow-up Context

## Follow-up Context

Chat brainstorm 2026-11-14. Operator decisions: opencode 1 and 2 = two major versions; coordination = peer-to-peer now, lead later; panel scope = full control; process = baseline commit then full request process. Follow-up chat message: 'and support git work trees'.

## Processing Notes

- This is the canonical persistent request artifact. Conversation history is contextual evidence; this file is authoritative.
- After intake, workflow is `requests/inbox -> processing -> intent -> requirement -> work -> implementation -> test -> verification -> documentation -> complete` per `docs/process/state-model.md` and `docs/process/autonomous-operating-model.md`.
- Next: agent claims via `scripts/claim.py`, then `scripts/preflight.py`, then requirement/work linkage.

## Traceability

- Intent: pending (created from this request)
- Requirement: pending
- Work: WORK-000001
- Evidence: pending
