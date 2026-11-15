# Request Intake

This is the human-facing entry point for new development requests. A request
may describe a feature, defect, question, capability, or improvement in plain
language. You do not need to create a requirement or work item.

All request sources converge here — chat, inbox file, crash report, automation.
Use `scripts/create_request.py --source chat --text "..."` for chat intake (see `docs/process/chat-intake.md`; `Source != Workflow`). Do not create a parallel chat queue.

## Submit

1. **Inbox file:** Create a Markdown file in `requests/inbox/` (e.g., `2026-09-08-fix-chart-pan-direction.md`) describing desired outcome, observed behavior, reproduction steps, and evidence. Template: `requests/request-template.md`.
2. **Chat:** Let the agent run `scripts/create_request.py --source chat --text "your statement"` — it creates the same canonical `requests/inbox/REQ-*.md` artifact with `Source: chat` and preserves your verbatim statement. Follow-ups use `--follow-up REQ-... --text "..."`.

A short request is valid. Crash reports automatically discover evidence via `scripts/crash_evidence.py` (see `docs/process/crash-investigation.md`).

## Processing

An autonomous agent claims one inbox request (`scripts/claim.py`), moves it to `requests/processing/`, assigns a stable `REQ-*`/`WORK-*` trace, and performs:

```text
request -> intent -> requirement -> decision (if needed) -> work
         -> implementation -> test -> evidence -> documentation -> complete
```

The request remains linked to resulting records. Do not edit a request
while it is in `processing/` unless coordinating with the claiming agent.

## Outcomes

* Completed requests move to `requests/completed/` with links to evidence.
* Rejected requests move to `requests/rejected/` with reason and replacement link.
* Blocked work remains linked from the request and `work/blocked/`; `needs-review` is for human decisions (`docs/process/escalation.md`).

Current lifecycle rules are normative in
`docs/requirements/development_operations.md` and operationally summarized in
`docs/process/engineering-rules.md` + `docs/process/state-model.md` (valid states) + `docs/process/autonomous-operating-model.md` (full contract). Build contract is `docs/process/build-contract.md`.
