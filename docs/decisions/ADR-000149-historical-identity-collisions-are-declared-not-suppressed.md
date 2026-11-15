# ADR-000149: Historical identity collisions are declared against their records, not suppressed by id

**Status:** ACCEPTED
**Date:** 2026-09-20
**Type:** process-change (development-system change, `docs/process/process-protection.md`)
**Requirements:** `docs/requirements/development_operations.md` REQ-DEVOPS-003/014 (unique ids) — enforcement clarified, not changed
**Work:** WORK-000248
**Request:** REQ-CHAT-20260920-010
**Evidence:** VER-000248 (audit: `docs/audits/historical-id-collisions-audit-2026-09-20.md`)
**Process authority:** `docs/process/historical-id-collisions.md`
**Related:** ADR-000090 (concurrent-agent process tooling; the monotonic id allocator)

## Context

Four ids — `WORK-000128`, `VER-000128`, `ADR-000064`, `ADR-000069` — are each
used by two different historical records, all created on 2026-09-12 by
concurrent sessions when ids were still chosen by scanning a subset of the
stores. ADR-000090 / WORK-000167 fixed the allocator two days later. The eight
record files are distinct and intact; only the manifest key collides.

`scripts/drift_detect.py` reported each collision as a plain `FAIL`, and a
documented decision (`.agent/state/requests.md`) was to keep the records as
they are. The two together produced four permanent failures that every
verification run since has had to re-classify by hand as EXPECTED /
PRE-EXISTING — VER-000241, -242, -243 and -245 all do it. That is the real
defect: not the four rows, but a validator whose failure output had to be
routinely ignored, which trains everyone to skim past `drift_detect` instead
of reading it.

The audit also confirmed there was **no** structured exception mechanism
anywhere in the repository — only free-text heuristics keyed on words like
"historical" appearing in prose — so the convention had to be established.

## Decision

### 1. The historical records are not renumbered

Their ids, file names and locations stay exactly as they are. Renumbering
would rewrite the identity of eight completed records, and every reference to
them in commits, evidence and decisions, to buy a tidier key and nothing else.
This keeps the decision already recorded when the allocator was fixed.

### 2. An accepted collision is declared against its records

`docs/process/historical-id-collisions.md` is the single canonical place —
explanation and machine-readable declaration in one document, in the
repository's existing pipe-separated manifest style:

```text
ID | manifest | record ; record [; record ...] | reason
```

Each record is named by the path its own manifest row carries. A declaration
whose record does not exist on disk, which names fewer than two records,
repeats a record, names an unknown manifest or carries no reason is itself a
hard failure.

### 3. A bare id allowlist is explicitly rejected

`ignore WORK-000128` would also swallow a *third* record that later took that
number — exactly the regression this mechanism has to keep catching. A
duplicate is accepted only when its observed record set is **exactly** the
declared set. A third record, a replaced record, or a path that does not match
is `FAIL UNEXPECTED_ID_COLLISION`.

This is load-bearing rather than aspirational: degrading the check to an
id-only allowlist fails five of the new tests (VER-000248 records the
mutation run).

### 3a. A collision is between *distinct records*, not between rows

Comparing raw manifest rows conflates two different defects. `HEAD` listed
one ADR-000069 record on **two** rows under
`ADR-000069` (a superseded row kept beside its amended replacement), and the
same for `ADR-000026` and `ADR-000122`. That is a stale manifest row, not two
records sharing an id. The comparison is therefore made on the set of
**distinct** record paths, and a record repeated under one id is reported
separately as `FAIL DUPLICATE_MANIFEST_ROW` — so a declaration can never
absorb a stale row, and the declaration stays valid whether or not someone has
tidied those rows.

### 4. `ACCEPTED` becomes a third finding level

`drift_detect` findings were `FAIL`/`WARN`. A documented, verified condition
is neither: it is not a failure, and calling it a warning would make it
indistinguishable from drift that still needs attention. The summary counts
the three separately —
`Summary: 0 FAIL, 2 WARN, 4 ACCEPTED, 6 total` — and the exit code still
depends on `FAIL` alone. **An accepted collision is still printed in full,
with its records and reason**: it is reclassified, never hidden.

### 5. The allocator is audited and left alone

`agent_process.allocate_id()` was confirmed correct (inter-process `flock`,
recursive scan of every store across the working and main roots, manifests
included, `max(persistent counter, scanned) + 1`, and `create_request_files()`
refusing a held id outright). No regression was found, so nothing about it
changes here.

### 6. Declaring a collision is not a workflow step

A new duplicate is a defect to be fixed, not declared. The declaration block
exists for four historical rows that predate the allocator fix, and the
document says so.

## Consequences

* `drift_detect` returns to being a signal: `FAIL` means something is wrong.
* The four historical collisions remain visible and auditable in every run.
* Any future duplicate — including a third record on one of these four ids —
  is a hard failure, now covered by tests rather than by intention.
* `drift_detect.py` honours `AGENT_PROCESS_ROOT` (the convention
  `agent_process.py` already established), so the validator can be tested
  end-to-end against synthetic manifests. This is its first test coverage.
* Adding a fourth manifest of id-keyed records means adding one row to
  `ID_MANIFESTS`; duplicate detection then covers it automatically.

## Alternatives considered

* **Renumber the eight records.** Rejected: rewrites historical identity and
  every reference to it, for cosmetic gain, and contradicts the decision taken
  when the allocator was fixed.
* **Id-only allowlist.** Rejected: cannot distinguish the accepted pair from a
  third record taking the same id — the specific hole this work must not open.
* **Suppress duplicate detection for the affected manifests.** Rejected:
  removes the check entirely rather than classifying its output.
* **Keep classifying by hand in every evidence record.** Rejected: that is the
  status quo, and it is what devalued the validator.
