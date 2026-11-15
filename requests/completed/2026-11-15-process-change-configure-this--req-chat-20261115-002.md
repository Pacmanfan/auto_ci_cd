# Request: Process change: configure this repository to run workers and make agent coordina

**Request ID:** REQ-CHAT-20261115-002
**Source:** chat
**Type:** PROCESS-CHANGE
**Priority:** HIGH
**Created:** 2026-11-15T00:38:27-05:00
**Status:** completed
**Attachments:** - none

## Original User Statement

> Process change: configure this repository to run workers and make agent coordination part of the agent constitution. (1) devsys.json: name the project, configure the worker agent profiles (the default agent CLI on worker-01 and the second agent CLI through the event-stream adapter on worker-02); (2) AGENTS.md: tell every agent about the roster, scripts/msg.py, the station and that messages carry no authority; (3) add the agent CLI's project hook settings so messages are pushed into running sessions; (4) WORKER-012: a configured project's own devsys.json and the agent CLI's project settings directory are project configuration, not part of the neutral system. Then run a real worker end to end.

## Classification

- Type: PROCESS-CHANGE (final; validated by `scripts/request_classification.py`, REQ-DEVOPS-045)
- Priority: HIGH
- Source: chat
- classification:
  - proposed_type: PROCESS-CHANGE (override)
  - final_type: PROCESS-CHANGE
  - heuristic_type: PROCESS-CHANGE (confidence high)
  - validation: PASS
  - classifier_reason: request statement is framed as process-change: AGENTS.md, Process change, agent constitution, scripts/msg.py
  - evidence: 'Process change' [head]; 'agent constitution' [head]; 'AGENTS.md' [head]; 'scripts/msg.py' [head]

## Inferred Intent (agent interpretation — not user statement)

Intent: investigate process-change per classification; derive requirement and verify.
Conversational context: Operator, chat 2026-11-15: 'do the process change and make this happen', in answer to the offer to file a PROCESS-CHANGE request adding the two agent profiles to devsys.json and mentioning msg.py in AGENTS.md so real workers can be started from the panel. This is the human authorisation for the process change.

## Observed / Unknown

- Observed: see original statement above.
- Unknown: exact scope — agent to derive
- Follow-ups will be appended under ## Follow-up Context

## Follow-up Context

Operator, chat 2026-11-15: 'do the process change and make this happen', in answer to the offer to file a PROCESS-CHANGE request adding the two agent profiles to devsys.json and mentioning msg.py in AGENTS.md so real workers can be started from the panel. This is the human authorisation for the process change.

## Processing Notes

- This is the canonical persistent request artifact. Conversation history is contextual evidence; this file is authoritative.
- After intake, workflow is `requests/inbox -> processing -> intent -> requirement -> work -> implementation -> test -> verification -> documentation -> complete` per `docs/process/state-model.md` and `docs/process/autonomous-operating-model.md`.
- Next: agent claims via `scripts/claim.py`, then `scripts/preflight.py`, then requirement/work linkage.

## Traceability

- Intent: pending (created from this request)
- Requirement: pending
- Work: WORK-000004
- Evidence: pending
