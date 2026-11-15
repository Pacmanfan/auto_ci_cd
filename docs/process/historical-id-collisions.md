# Historical Identity Collisions

**Status:** current process authority
**Normative rule:** `docs/requirements/development_operations.md` REQ-DEVOPS-003/014 (unique ids)
**Decision:** ADR-000149 · **Work:** WORK-000248 · **Evidence:** VER-000248
**Enforced by:** `scripts/drift_detect.py` (`HISTORICAL_ID_COLLISION`,
`UNEXPECTED_ID_COLLISION`, `MALFORMED_COLLISION_DECLARATION`)
**Audit:** the source project's historical-id-collisions audit (not extracted)

This document is the **only** place an accepted identity collision is
declared. It is both the explanation and the machine-readable declaration:
`scripts/drift_detect.py` parses the block below.

## Why these collisions exist

Before WORK-000167 / ADR-000090, an id was chosen by scanning a *subset* of
the stores — `create_request.py` looked only at `requests/inbox/` and
`requests/processing/` — so two sessions working at the same time read the
same maximum and both took the next number. In the source project this
happened four times on one day, in pairs minutes apart; one pair covered two
entirely unrelated subjects, which is the signature of concurrent sessions
rather than a copy-paste error.

**Nothing was overwritten.** All eight record files existed on disk under
distinct names and were intact. The collision was in the manifest key alone.

## Why they are preserved rather than renumbered

Renumbering would rewrite the identity of eight completed historical records
and every reference to them in commits, evidence and decisions, to buy nothing
but a tidier key. The original decision — recorded in `.agent/state/requests.md`
when the allocator was fixed — was that the duplicates stay and that the
**record file names remain their unique keys**. This document keeps that
decision and gives it a validator-enforced form.

## How an accepted collision is declared

One line per collision, in the repository's pipe-separated manifest style:

```text
Format: `ID | manifest | record ; record [; record ...] | reason`
```

* **ID** — the duplicated `WORK-`/`VER-`/`ADR-` identifier.
* **manifest** — the manifest the duplicate appears in, repository-relative.
* **record** — each colliding row's record path, written **exactly as that
  manifest row writes it** (relative to the manifest's own directory).
* **reason** — why the collision is accepted.

The declaration names the **records**, never just the id. That is the whole
point: a bare id allowlist would also swallow a *third* record that later took
the same number, which is precisely the failure this mechanism must still
catch.

### Accepted historical collisions

*(Extraction note: the source project declared its four collisions here. The
extracted template holds none of those records, so it declares none; keep
`REPO_DECLARED` in `scripts/tests/test_drift_id_collisions.py` equal to this
block. A declaration line has this shape — shown inside a comment-style
example so the validator does not parse it:)*

```text
# WORK-000128 | work/work.txt | completed/WORK-000128-first.md ; completed/WORK-000128-second.md | <date> subset-scan allocator collision, fixed by WORK-000167 / ADR-000090
```

## How the validator treats a duplicate

`scripts/drift_detect.py` finds **every** duplicate id in every manifest, then
classifies each one:

| Observed | Level | Code |
|---|---|---|
| The duplicate's record set is **exactly** the declared set | `ACCEPTED` | `HISTORICAL_ID_COLLISION` |
| Any other duplicate — undeclared id, a third record on a declared id, one side replaced, a record path that does not match | `FAIL` | `UNEXPECTED_ID_COLLISION` |
| A declaration naming a missing record, fewer than two records, a repeated record, or an unknown manifest | `FAIL` | `MALFORMED_COLLISION_DECLARATION` |
| A declaration whose id is no longer duplicated | `WARN` | `STALE_COLLISION_DECLARATION` |
| The **same record** listed on several rows under one id | `FAIL` | `DUPLICATE_MANIFEST_ROW` |

A collision is about **distinct records sharing an id**. One record listed on
several rows under the same id is a different defect — a superseded manifest
row that was never removed — and is reported as `DUPLICATE_MANIFEST_ROW` on
its own terms, so a declaration can never absorb it. (The source project's
`HEAD` carried exactly that for three ADR ids.)

An id is **never silently ignored**: an accepted collision is still printed,
with its records, so anyone auditing the system can see it. It simply is not
counted as a failure, and the summary reports the three classes separately:

```text
Summary: 0 FAIL, 2 WARN, 4 ACCEPTED, 6 total
```

The exit code still depends on `FAIL` alone.

## Why no new collision can be created this way

The mechanism that produced these four is gone. `agent_process.allocate_id()`
runs inside an `fcntl` inter-process lock, scans every store directory
recursively (including `completed/`, `cancelled/` and `needs-review/`) across
the working root *and* the main root, reads the manifests as well, and then
takes `max(persistent counter, scanned) + 1` — so an id is never reissued even
if its record is later deleted. `create_request_files()` refuses outright,
rather than overwriting or renumbering, if an id is already held. That work is
WORK-000167 / ADR-000090 and is covered by
`scripts/tests/test_process_concurrency.py`.

Adding a line to the declaration block above is therefore **not** a normal
part of any workflow. A new duplicate is a defect to be fixed, not declared.
