# Human-Review Escalation Path

**State:** `needs-review` (`requests/needs-review/`, `work/needs-review/`)
**Normative rule:** `docs/requirements/development_operations.md` REQ-DEVOPS-015

## When to Escalate

Autonomy does not mean pretending ambiguity does not exist. Move work/request to `needs-review` when any of these holds:

- conflicting normative requirements (two domain docs demand incompatible behavior)
- destructive migration (data format, config, persistence incompatibility)
- security-sensitive changes (auth, network exposure, file parsing)
- major architectural change (new subsystem, ownership boundary shift)
- public interface / protocol change (wire schema, automation protocol, file format)
- unclear user intent (request ambiguous and intent cannot be derived)
- inability to verify a requirement (no test path)
- inability to reproduce a defect
- changes to the autonomous-development process itself (`docs/process/process-protection.md`)
- changes that could invalidate large portions of the system
- preflight FAIL that cannot be resolved autonomously
- completion gate FAIL that requires human scope decision

Do not leave such work appearing merely `blocked`. `blocked` means waiting on a known dependency; `needs-review` means a human decision is required.

## How to Escalate

Use the tools; they move the files, write the headers and manifests, and record
the reason (ADR-000199):

1. Park the work item, if one exists:
   `python3 scripts/work.py transition WORK-XXXXXX --to needs-review --reason "<decision needed>"`
2. Park the request:
   `python3 scripts/claim.py --needs-review-request <file> --reason "<decision needed>"`
   The claim stays with you, marked *parked*; the reason is written into the
   request's sidecar `resolution` and the claim history.
3. Write the question into the work record (or the request if there is no work yet):
   ```
   ## Escalation 2026-09-08
   Reason: conflicting requirement <DOMAIN-A>-* vs <DOMAIN-B>-*
   Evidence: docs/requirements/<domain_a>.md:42 vs docs/requirements/<domain_b>.md:88
   Options: (A) ... (B) ...
   ```
4. Commit (`scripts/commit_request.py --request requests/needs-review/<file> -m ...`)
   and optionally release (`--release-request`), so the parked request is
   visible to every session (`devsys.py status` lists it under "needs review").
5. Do not proceed until a human has answered.

## Resolution

The human answers in the request or work record (decision, the requirement or
ADR changed, or the reason for rejection). Then:

```
python3 scripts/claim.py --claim-request <file>        # the owner RESUMES; any other agent TAKES OVER the parked claim
python3 scripts/work.py transition WORK-XXXXXX --to active
```

or `--reject-request <file> --reason ...` / `--complete-request <file>` (the
completion gate applies as usual). A request in needs-review that is still
held by an owner who has not parked it (an in-flight transition) is not taken
over.

## Automated Detection

- `scripts/preflight.py` returns exit 2 on FAIL — caller should escalate to needs-review.
- `scripts/completion_gate.py` FAIL blocks every completion path (exit 9 from `work.py` / `claim.py` / `commit_request.py`); when the fix needs a scope decision, escalate. A human may instead complete with `--waive-gate "<reason>"`, which is recorded.
- `scripts/drift_detect.py` flags contradictory authority and stale paths as WARN for human triage.
- `scripts/process_next_request.py` moves to needs-review if protected files touched without process-change or if completion gate fails after retries.
