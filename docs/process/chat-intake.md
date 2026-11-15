# Chat Request Intake

**Normative rule:** `docs/requirements/development_operations.md` REQ-DEVOPS-014 + Phase 3 chat unification
**State model:** `docs/process/state-model.md`
**Canonical artifact:** `requests/inbox|processing/*.md`
**Tool:** `scripts/create_request.py`

## Architecture

```
                ┌─────────────────┐
                │  Human / Chat   │
                └────────┬────────┘
                         │
                ┌────────▼────────┐
                │ Request Intake  │  ← scripts/create_request.py --source chat
                └────────┬────────┘
                         │
          ┌──────────────▼──────────────┐
          │ Canonical Request Artifact │  ← requests/inbox/REQ-CHAT-*.md + .json
          └──────────────┬──────────────┘
                         │
                     Intent
                         │
                   Requirement
                         │
                Architecture/Decision
                         │
                      Work
                         │
                 Implementation
                         │
                       Test
                         │
                   Verification
                         │
                 Documentation
                         │
                     Complete
```

`Source != Workflow.` Chat, inbox file, crash, automation all converge at `requests/inbox/`.

## How Chat Creates a Request

Human says in chat:

> "I loaded a file and the application crashed."

Agent executes (no manual file creation by human):

```bash
python3 scripts/create_request.py --source chat --text "I loaded a file and the application crashed."
# optionally: --type CRASH --priority HIGH --attach path/to/dump --context "conversation turn ..."

# for follow-up in same investigation:
python3 scripts/create_request.py --follow-up REQ-CHAT-20260908-001 --text "It only happens with this particular file."
```

Result:

- `requests/inbox/<date>-<slug>-req-chat-*.md`  (canonical, persistent)
- `requests/inbox/<slug>-req-chat-*.json`       (machine sidecar)
- Fields: `Request ID`, `Source: chat`, `Created`, `Type`, `Priority`, `Original User Statement` (verbatim preserved), `Inferred Intent` (separate), `Follow-up Context`, `Attachments`, `Traceability`.
- `Request ID` is allocated atomically by the global monotonic allocator (REQ-DEVOPS-003, ADR-000090): it is never an id held by any request in any state, even when several intakes run at once. The files are created exclusively; a collision fails and writes nothing — requests are never renumbered after creation.

The conversation is contextual evidence; the persistent file is authoritative. After intake, the agent (or another agent later) processes via:

```bash
python3 scripts/process_next_request.py --dry-run   # plan
python3 scripts/process_next_request.py            # claim + stage
python3 scripts/preflight.py --request requests/processing/...
python3 scripts/completion_gate.py --work WORK-NNNNNN
```

## Submit ≠ Claim ≠ Process

Submission and processing are separate operations (REQ-DEVOPS-043,
ADR-000150). Intake is submit-only: `scripts/create_request.py` validates,
allocates an id, persists the inbox ticket, confirms
`Status: inbox (SUBMITTED), Processing: NOT STARTED`, and stops. It never
claims, assigns, worktrees, or executes the request, and the submitting
agent does not need to remain active — the ticket survives as files.

### Submitter

```text
Create request (create_request.py --source chat --text "...")
    ↓
Receive ticket ID
    ↓
Stop
```

### Reviewer

```text
List inbox (process_next_request.py --dry-run; CRITICAL first)
    ↓
Review and inspect the ticket (listing/reading never claims)
    ↓
Claim explicitly (claim.py --claim-request; exactly one winner)
    ↓
Normal workflow continues
```

All safety guards (requirements, claim/lock, scope, completion gate, verification, human escalation) apply identically to chat-created work — see `docs/process/process-protection.md`. Chat does not bypass safety.

### "Do not perform it right now" is an intake instruction

Users often file a request with a phrase such as "add this as an inbox
ticket, do not perform it right now" or "record this but DO NOT perform the
work at this time". The phrase is kept verbatim in `## Original User
Statement`, but it is addressed to the session doing the intake: submit,
don't start. It is not a property of the ticket. Once the request is in the
inbox, it is runnable like any other request. Whoever later processes it,
whether an interactive agent or the autonomous engine (TICKET-ENGINE-002),
ignores the phrase. Nobody holds the request, skips it, or asks about it
because of that phrase. (User statement of 2026-09-24, REQ-CHAT-20260924-013,
ADR-000197.) A constraint that is not about intake timing still applies, for
example "needs a human decision", hardware, or process-change approval.

## Natural Language → Structured Intent

The request preserves verbatim user text under `## Original User Statement`. Agent interpretation is under `## Inferred Intent` and includes `Observed` vs `Unknown` vs `Intent`. The agent must NOT assume root cause.

Example for crash:

- Observed: application crashed while loading a file.
- Unknown: which file/format, crash location, reproducibility, dump availability, recent changes.
- Intent: investigate, identify root cause, fix minimally, add regression test, verify.

## Classification

**Normative rule:** REQ-DEVOPS-045 (ADR-000173). Implementation: `scripts/request_classification.py`, used by every source of `scripts/create_request.py` and by `scripts/preflight.py`.

The request type is a closed vocabulary:

| Type | Meaning |
|---|---|
| `FEATURE` | new capability, integration, enhancement, workflow |
| `BUG` | existing behaviour is incorrect or fails under a defined condition (a deterministic test failure is a BUG) |
| `CRASH` | an actual fatal runtime failure only: crash, segmentation fault, abort, fatal exception, unexpected termination |
| `REGRESSION` | previously working behaviour has become broken — needs that evidence ("used to work", "no longer … since commit", "broke after") |
| `PERFORMANCE` | correct but too slow / too much CPU, GPU, memory, dropped samples, poor responsiveness |
| `REFACTOR` | internal restructuring, no material behaviour change |
| `BUILD` | the tree does not build / compile / link |
| `DOCUMENTATION` | documentation is the deliverable |
| `INVESTIGATION` | a question or analysis |
| `PROCESS-CHANGE` | a change to the development system itself (`process-protection.md`) |

Rules the classifier and the validator obey:

* `CRASH` is never inferred from generic failure language — *fails, failure, broken, error, issue, problem, does not work, test/integration/reception/validation failure* are `BUG` language. "The application must not crash", "crash detection", "if the process was killed, mark the run failed", "decoder crashes must be visible" are mentions *about* crashes, not reports of one, and are not evidence.
* `REGRESSION` needs evidence of a previous working state; "regression tests", "regression suite", "no fps regression" are not evidence, and modifying existing functionality is not a regression.
* `PERFORMANCE` needs a performance/resource problem as the primary problem; a "## Performance" section in a specification is not one.
* The whole request is read (title, description, acceptance criteria, symptoms); the head (title + opening statement + first Objective/Mission/Summary/Problem section) decides between the non-symptom types. Nothing is invented: with no classifiable statement the result is `INVESTIGATION`, `confidence: low`.
* `--type` is a **proposal** (from a human or from the agent). The deterministic validator is authoritative: unknown values (`FAILURE`, `CRASH?`) are rejected; `crash`/`Crash `/`DEFECT` are normalised **and the normalisation is recorded**; `CRASH`/`REGRESSION`/`PERFORMANCE`/`BUILD` without supporting evidence are corrected to what the text supports or, when that is ambiguous, rejected so the caller classifies again. A rejected request writes nothing (exit code `INVALID`).
* Every ticket records why: sidecar `classification` (`proposed_type`, `proposed_source`, `normalized_type`, `heuristic_type`, `final_type`, `confidence`, `validation: PASS|CORRECTED|REJECTED`, `classifier_reason`, `correction_reason`, `evidence[]`) and the markdown `## Classification` block.
* **Request id, sequence number and date are system metadata.** The date is the local system date at creation (`agent_process.request_date()`, host timezone — not UTC; `REQ-CHAT-20260923-001` was filed at 20:32 local on 2026-09-22 by the old code), the number comes from the global allocator (ADR-000090). The agent must not generate, guess or type them; no option accepts them.

Existing records are reviewed, never rewritten silently:

```bash
python3 scripts/create_request.py --file requests/inbox/<record>.md            # OK | DIFFERS | NON-CANONICAL | INVALID-VOCABULARY | UNSUPPORTED
python3 scripts/create_request.py --review-all [--strict]                      # every record under requests/
python3 scripts/create_request.py --file <record> --reclassify [--type T]      # explicit correction; history kept in the sidecar
```

`scripts/preflight.py` warns at claim time when a request's stored type is invalid or unsupported. Classification guides workflow (e.g. CRASH → `crash-investigation.md`) but does not prevent investigation of ambiguous requests.

## Follow-ups and Attachments

If user provides additional info ("It only happens when playback is stopped", crash dump path), use `--follow-up <REQ-ID> --text ... --attach ...`. This appends to `## Follow-up Context` of the same request rather than creating duplicate requests. The follow-up names exactly one request by id; it is refused for a completed or rejected request (file a new one that references it), and concurrent follow-ups to one request are all kept (a per-request lock). A new request whose normalised statement equals an open request's (inbox, processing, needs-review) is refused as a DUPLICATE (exit 7) naming that request; `--allow-duplicate` overrides it deliberately. The check and the creation are one critical section, so of several identical submissions at the same instant exactly one is created (REQ-DEVOPS-014, ADR-000199).

Attachments (dumps, logs, screenshots, data files, stack traces) are referenced as paths under `Attachments:` and `follow_ups`; large binaries are not copied unnecessarily.

## Crash Reports

Crash requests enter investigation workflow (`docs/process/crash-investigation.md`). On creation, the system runs:

```bash
python3 scripts/crash_evidence.py --request requests/inbox/req-chat-*.md
```

which auto-discovers `log/dump/*.core`, logs, recent commits, changed files (if unavailable, records unavailable — does not invent evidence).

## Integration with the project's automation interface

Chat intake may in future be exposed as a development capability deriving from the project's automation discovery (PROJECT-SPECIFIC; not implemented in the source project — the only request-creation path is `scripts/create_request.py`, REQ-DEVOPS-045):

- ObjectType `development`
- Capability `development.create_request` (args: text, source, type, priority, attachments, context)

This would use the project's existing automation funnel (`<PROJECT_AUTOMATION_FUNNEL>`); no parallel RPC. The project's wire schema (`<PROJECT_WIRE_SCHEMA>`) remains authority; the capability is added as a new object type without a protocol version bump (minor addition), and AI tool definitions derive from the automation discovery response.

## Priority and Dependencies

Priority: `CRITICAL|HIGH|NORMAL|LOW` (`--priority`). Queue selection in `scripts/process_next_request.py` respects priority (CRITICAL first, then HIGH, then lexicographic). Priority does not bypass verification.

Dependencies: work file header may include `Depends: WORK-XXXXXX`; blocked work uses `work/blocked/` and `needs-review` per state-model.

## Why Persistent

Chat intake creates a file so that:

- conversation may end but work persists;
- another agent/session can continue;
- autonomous processing may happen later;
- auditability requires persistent state.

The repository is the system of record.

## Safety

Chat-created work obeys the same rules as inbox work:

- requirements must be identified;
- architecture reviewed;
- claim/lock owned;
- scope guard checked;
- completion gate passed;
- verification evidence produced;
- documentation updated;
- human escalation via `needs-review` when ambiguous.

See `docs/process/process-protection.md`, `docs/process/escalation.md`, `docs/process/recovery.md`.
