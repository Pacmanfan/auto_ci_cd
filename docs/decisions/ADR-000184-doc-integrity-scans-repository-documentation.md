# ADR-000184: The documentation audit scans the repository's own documentation — what git tracks or would track

**Status:** ACCEPTED
**Date:** 2026-09-23
**Work:** WORK-000303 (REQ-AUTO-20260923-004, process-change)
**Requirement:** `docs/requirements/development_operations.md` REQ-DEVOPS-026 (amended)

## Context

`scripts/doc_integrity.py` walked every `*.md` under the repository root
(`ROOT.rglob`), skipping only `.git` and `docs/history`. On a working
machine that includes trees that are not the repository's documentation:

* the web front end's `node_modules` after a local `npm ci` — hundreds of third-party
  READMEs and CHANGELOGs (611 findings on 2026-09-23);
* build-only git worktrees and scratch copies under `tmp/` — each a
  second copy of `docs/` (2815 findings with three such worktrees present);
* build output under `build/` (staged documentation, generated manuals: 38).

Of 4445 warnings, 944 concerned the repository's documentation; the rest
buried them and changed with whatever happened to be on disk. WORK-000296
recorded the defect and split it out because the audit belongs to the
development system (`docs/process/process-protection.md`).

## Decision

1. The audit's scope is the repository's own documentation: every `*.md`
   file `git ls-files --cached --others --exclude-standard` lists — tracked,
   or new and not ignored — minus `docs/history/`. What the repository
   ignores is by definition not its documentation; a document written but
   not yet committed is still audited.
2. Without git (an exported tree) the same trees are skipped by name:
   directories named `node_modules` or `.git`, `tmp/`, `build/`
   except `build/README.md`.
3. The check's rules are unchanged; only the file set changes.

## Consequences

* On 2026-09-23 the audit went from 4445 to 940 findings, none of them from
  `node_modules`, `tmp/` or build output; the 940 are the
  pre-existing findings in tracked documentation (933 are `docs/user` links
  to generated `.html` pages and screenshots that exist only in the built
  manual).
* A scratch worktree or `npm ci` no longer changes the audit's result.
* The audit relies on `.gitignore` being correct; a tree that should not be
  audited must be ignored (it should be anyway).

## Alternatives rejected

* **A hard-coded skip list only** — drifts as new scratch or build trees
  appear; kept only as the fallback without git.
* **Scanning tracked files only** — would miss a new document until it is
  committed, which is exactly when the audit should catch its links.

## Links

* Requirement: REQ-DEVOPS-026
* Work: WORK-000303 · Evidence: VER-000303
* Request: REQ-AUTO-20260923-004 (from WORK-000296 / REQ-AUTO-20260922-001 item 3)
