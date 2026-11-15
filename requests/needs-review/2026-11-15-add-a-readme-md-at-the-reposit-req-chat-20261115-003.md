# Request: Add a README.md at the repository root. It should say in a few short sections wh

**Request ID:** REQ-CHAT-20261115-003
**Source:** chat
**Type:** DOCUMENTATION
**Priority:** NORMAL
**Created:** 2026-11-15T00:43:02-05:00
**Status:** needs-review
**Attachments:** - none

## Original User Statement

> Add a README.md at the repository root. It should say in a few short sections what this repository is (a development-control system for autonomous coding agents: requests, claims, gated completion, workers, agent coordination), how to get started (python3 scripts/devsys.py doctor and status, how to submit a request, how to start workers), where the panels are (web control panel, terminal dashboard, Qt control panel; python3 scripts/msg.py panels), and point to docs/README.md, AGENTS.md and docs/process/agent-coordination.md for everything else. Keep it under 80 lines, product-neutral (no agent vendor names), and accurate against the current commands.

## Classification

- Type: DOCUMENTATION (final; validated by `scripts/request_classification.py`, REQ-DEVOPS-045)
- Priority: NORMAL
- Source: chat
- classification:
  - proposed_type: DOCUMENTATION (override)
  - final_type: DOCUMENTATION
  - heuristic_type: DOCUMENTATION (confidence high)
  - validation: PASS
  - classifier_reason: request statement is framed as documentation: README, docs
  - evidence: 'README' [head]; 'docs' [head]; 'README' [head]; 'docs' [head]

## Inferred Intent (agent interpretation — not user statement)

Intent: investigate documentation per classification; derive requirement and verify.

## Observed / Unknown

- Observed: see original statement above.
- Unknown: exact scope — agent to derive
- Follow-ups will be appended under ## Follow-up Context

## Follow-up Context

_No follow-up yet._

## Processing Notes

- This is the canonical persistent request artifact. Conversation history is contextual evidence; this file is authoritative.
- After intake, workflow is `requests/inbox -> processing -> intent -> requirement -> work -> implementation -> test -> verification -> documentation -> complete` per `docs/process/state-model.md` and `docs/process/autonomous-operating-model.md`.
- Next: agent claims via `scripts/claim.py`, then `scripts/preflight.py`, then requirement/work linkage.

## Traceability

- Intent: pending (created from this request)
- Requirement: pending
- Work: WORK-000005
- Evidence: pending
