# ADR-000001: Autonomous Development Artifact Model

**Status:** Accepted
**Date:** 2026-09-08
**Owners:** Repository maintainers
**Requirements:** `docs/requirements/development_operations.md`
**Work:** `work/active/WORK-000001-establish-autonomous-development-system.md`

## Context

The repository already has useful requirements, architecture, tests, automation
interfaces, and historical audits, but request intake, active decisions,
work state, verification evidence, and traceability are split or absent. The
existing migration also left references to paths that are no longer present.

Future agents need to process a request from repository state without relying
on undocumented human memory.

## Decision

Use simple Markdown records and small text manifests rather than a database:

```text
requests/inbox/
        |
        v
docs/requirements/  <---- docs/decisions/
        |
        v
work/queued|active|blocked/
        |
        v
tests and application code
        |
        v
evidence/  --->  docs/  --->  requests/completed/
```

`docs/requirements/` remains the only normative behavior root. Existing domain
requirement IDs remain stable; the development-process contract uses the
`REQ-DEVOPS-*` namespace. `docs/decisions/` is the active ADR root, while
`docs/history/decisions/` is historical. `work/` is the active work root.

## Alternatives considered

### Database-backed workflow

Rejected. It adds operational dependencies and hides state from ordinary
repository review. Stable Markdown IDs and manifests are sufficient.

### Make `AGENTS.md` the complete project database

Rejected. It would make the constitution large, stale, and difficult to audit.
Detailed knowledge belongs in canonical documentation and records.

### Mass rename all existing domain requirement IDs

Rejected. Existing IDs are referenced by code, tests, audits, and history.
Preserve them and provide an aggregate manifest; normalize only new process
records unless a domain owner intentionally revises an existing contract.

## Consequences

* A request can be submitted without knowing internal requirement syntax.
* Agents can discover eligible work from `work/work.txt` and `.agent/state/`.
* Completion requires evidence and traceability, not compilation alone.
* Historical documents remain available without competing with current authority.
* Indexes require maintenance when records are added or retired.

## Review triggers

Revisit this decision if the repository needs concurrent multi-agent locking,
cross-repository request federation, or evidence volume that makes Markdown
manifests inadequate.
