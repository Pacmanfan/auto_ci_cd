# Architectural Decisions

This directory contains active architectural decision records (ADRs). An ADR
records a significant choice, its context, alternatives, consequences, and
the requirements or work it affects.

* Normative behavior belongs in `docs/requirements/`.
* Active implementation work belongs in `work/`.
* Verification proof belongs in `evidence/` or `docs/audits/evidence/`.
* Superseded/completed decisions belong in `docs/history/decisions/`.

Use the next stable `ADR-NNNNNN` identifier from `decisions.txt`.

## Provenance of the records in this extraction

The ADRs here (except ADR-000199) were extracted from the source project
(`CI_CD.md` §12). They cite that project's request, work and evidence ids
(`REQ-CHAT-*`, `WORK-*`, `VER-*`) as their history; those records are not
part of the extraction, and a fresh instance's own id series start at 1
(only the request store issues request ids; `REQ-DEVOPS-047`). Test intents
that cite `WORK-000001` refer to the source project's founding work item,
not to a record in this repository.
