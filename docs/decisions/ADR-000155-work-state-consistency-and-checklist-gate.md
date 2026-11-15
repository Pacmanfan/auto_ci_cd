# ADR-000155: Work-state consistency and open items are checked, not remembered

**Status:** ACCEPTED (human review 2026-09-23: approved with the fixes recorded under Review amendment)
**Date:** 2026-09-21
**Work:** WORK-000267 (REQ-CHAT-20260921-013, process-change)
**Requirement:** `docs/requirements/development_operations.md` REQ-DEVOPS-044 (new), REQ-DEVOPS-020 (extended)

## Context

The REQ-CHAT-20260921-011 stuck-ticket audit found 14 WORK records left in
`work/active` or `work/needs-review` although their requests were already
completed. Four of them had `Status: completed` in their own header. One
record pointed at a request file that no longer existed, and an untracked
record was missing from `work.txt`. `drift_detect.py` reported only the
last of these. `completion_gate.py` returned PASS for all 15 open WORK items,
including items with explicitly unchecked phases. The process relied on
agents remembering to keep three views of a record's state in agreement (its
directory, its header and its manifest row) and on reading their own
checklists. Neither held.

## Decision

1. **A record may not contradict itself.** Drift is a FAIL when a WORK
   record's directory disagrees with its `Status` header or with its
   `work.txt` status column. The directory is the canonical location
   (state model), so the fix is always to bring the other two to it.
2. **Links to requests are resolved by identity, not path.** A request moves
   between state directories while the WORK record keeps the path it was
   written with. The same file name in another state is the same request.
   Only a request that exists nowhere is a FAIL.
3. **Transitional disagreements are warnings.** An open WORK record with a
   closed request, a stale processing claim (heartbeat over the 2 h recovery
   threshold), and an evidence status outside the state model are WARN. They
   can legitimately exist for minutes in a correct flow, and the point is
   that they are *seen*.
4. **The completion gate reads the checklist.** An unchecked or partial
   item, or a phase marked not done or not started, fails the gate unless the
   same line says it was waived. A waiver is an explicit, visible decision,
   never an omission.
5. **Existing records are normalised, not grandfathered.** The 20 historical
   records whose header still said `active` inside `completed/` or
   `cancelled/` had their header brought to the directory, with a dated note.
   WORK-000255 now names its deliberately deleted request instead of a dead
   path. `drift_detect` starts from 0 FAIL.

`AGENTS.md` also names the wire-schema authority where it actually lives
(the project's schema directory; WORK-000193 Outstanding).

## Consequences

* A session that closes a request but leaves its WORK record open now shows
  a warning in every agent's audit, and a record that contradicts itself
  blocks every audit, until it is fixed.
* Work cannot be completed with unchecked phases hidden in its own record.
  Deferring scope requires writing "waived" (and, by convention, the
  follow-up request id) on the item.
* No change to the state model, the claim protocol or any application code.

## Alternatives rejected

* **Reporting stale request paths (112 historical records).** This is noise:
  the path is stale by design once a request moves. Identity resolution
  answers the real question.
* **Making every new check a FAIL.** Transitional states occur in correct
  flows, and a stale claim is an operator decision (REQ-DEVOPS-025
  recovery), not a defect of the tree.

## Review amendment (2026-09-23)

The operator reviewed the change on 2026-09-23 after two days on `main` and
approved it with three fixes, made inside WORK-000267 before completion:

6. **The gate resolves a record by its file name only.** It used to fall back
   to any record in an earlier state directory that merely mentioned the id,
   and reached `cancelled/` only by a last recursive name search. 11 of 267 ids
   resolved to another record; WORK-000239 was gated through WORK-000267's
   record, which cites it, and passed with `[~]` and `[ ]` items open. The
   gate now looks for `WORK-NNNNNN-<slug>.md` in every state directory
   including `cancelled/`, prints the record it gated, and fails when the id
   names no record or more than one (the declared WORK-000128 collision is
   the only such id).
7. **An item is a list item or a table row.** "not done" / "not started"
   counted anywhere in a record, so headings and prose that state scope
   honestly (`## Deliberately not done`, "was not done here") and quoted
   output (`Processing: NOT STARTED`) failed the gate: 20 of 253 completed
   records, about half of them for that reason. Headings, prose, inline code
   and fenced blocks are now narrative. A waiver word inside inline code no
   longer waives. After the change 11 of 255 completed records are flagged,
   every one for a real unchecked or partial checklist item.
8. **The checks' tests never assert on the shared repository.** The case
   that ran `drift_detect` against the real checkout failed whenever another
   session's in-flight record was mid-change (WORK-000301 at review time).
   The repository's state is the workflow's drift audit, not a unit test.

Not changed: drift severities (a self-contradicting record in the shared
checkout still fails every audit until fixed, as decided in point 1), the
waiver rule itself, and how the gate links evidence (a VER record that
mentions a WORK id still counts as linked evidence; it is reported in
VER-000267 as an observation, not a defect of this change).
