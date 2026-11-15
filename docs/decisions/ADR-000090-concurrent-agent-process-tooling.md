# ADR-000090: Concurrent-agent safety for the process tooling

**Status:** ACCEPTED (process-change — human review required, `docs/process/process-protection.md`)
**Date:** 2026-09-14
**Requirements:** `docs/requirements/development_operations.md` REQ-DEVOPS-003, -014, -016, -025, -038 (amended)
**Work:** WORK-000167
**Request:** `requests/.../2026-09-14-harden-the-process-ticketing-t-req-chat-20260914-001.md`

## Context

Several AI agents work in this repository at once, usually in one shared
worktree. Two failures were observed: an agent's commit swept another agent's
uncommitted source files in under an unrelated message, and intake assigned a
request id that a completed request already held. The audit found these root
causes:

1. `create_request.next_id()` derived the id from the maximum found in
   `inbox/` and `processing/` only (archived ids were reused) and read-then-
   wrote without a lock (concurrent intakes got the same id). A filename
   clash appended a hash and kept the duplicate id. Up to 11 historical files
   share one id.
2. `claim.py` recorded the PID of the short-lived CLI process, so every claim
   looked "dead" (stale) five minutes after it was made; `--recover` deleted
   any claim older than two hours even if its agent was working, and its
   read-then-unlink could delete a claim another agent had just created.
   Release had no ownership check.
3. `process_next_request.py` defaulted the agent id to `agent-<uid>`, which is
   identical for every agent on a host, and moved files with `rename()`, which
   silently overwrites. Completion/rejection moves were manual `mv`.
4. No tooling knew which request owned which repository change; nothing
   provided a safe commit. Agents used `git add`/`git commit`, which commit
   the whole shared index.

## Decision

1. **Concurrency model.** All shared process state lives under
   `<main worktree>/.agent/state` (resolved via `git rev-parse
   --git-common-dir`, so linked worktrees share it) and is mutated only inside
   short inter-process critical sections (`fcntl.flock`) in
   `scripts/agent_process.py`. Files are written atomically (temp + `rename`)
   or created with `O_EXCL`; request moves are no-clobber (`link` + `unlink`).
2. **Ids.** `allocate_id()` returns `max(persistent counter, max id in every
   store) + 1` under the allocator lock: requests (`REQ-<SRC>-YYYYMMDD-NNN`,
   all of `requests/`), `WORK`, `VER`, `ADR` (directories + manifests).
   Monotonic, never reused after archive or deletion. Request files are
   created with `O_EXCL`; an id already held or an existing file is a
   collision: nothing is written, nothing is renumbered.
3. **Ownership record.** The claim lock `.agent/state/claims/request-<file>.lock`
   is the ownership record: request id, session-unique agent id (uid/pid
   defaults refused), claim token, claim time, heartbeat, state, owned paths
   (exclusive or shared), id tokens, commits, worktree, recovery history.
4. **Claims and transitions.** `claim.py --claim-request` checks and creates
   the claim and moves `inbox|needs-review -> processing` in one critical
   section; `--complete/--reject/--needs-review-request` are owner-only and
   require the request to be only in `processing`. Exactly one of any number
   of concurrent attempts succeeds.
5. **Commit boundaries.** Paths are registered to a request before editing
   (`--own`); a path owned exclusively by one request cannot be owned by
   another in the same worktree; registering an already-modified, unowned
   path requires `--adopt-existing-changes`. `commit_request.py` classifies
   every change (this request / other request / unowned), refuses unowned or
   foreign paths, builds the commit in a private index from HEAD with only
   owned paths (shared manifests: only lines naming the request's id tokens),
   and publishes with `git update-ref HEAD <new> <old>` (fails, committing
   nothing, if HEAD moved). The shared index and other changes are untouched.
6. **Staleness and recovery.** A claim is stale when its heartbeat (refreshed
   by every owner operation or `--heartbeat`) is older than the threshold
   (2 h). Staleness only *enables* recovery: `--recover` reports,
   `--recover-request --reason` transfers ownership explicitly (atomic,
   history kept, owned paths carried over), refused for active owners unless
   `--force-active`. Nothing is taken over automatically.
7. **Worktrees.** `claim.py --create-worktree` gives a request its own
   worktree and branch, recorded in the claim; ownership conflicts are
   evaluated per worktree. Recommended when a separate build tree is
   affordable; not required, and not a substitute for 2–6 (ids and claims are
   shared state in either case).

## Consequences

Agents need a session-unique `AGENT_ID`, must register paths before editing,
and commit through `commit_request.py`. Completion is `--complete-request`,
commit, `--release-request`.

## Limitations

* Locks are advisory: an agent that runs raw `git add -A`/`git commit` or edits
  lock files by hand bypasses the protection (documented as forbidden; a
  pre-commit hook could make it mechanical — not added here).
* `flock` is reliable on a local filesystem on one host, not over NFS.
* `git commit-tree` does not run git hooks (the repository has none).
* Shared-file attribution is by lines naming the request's tokens; other
  shared edits must be committed by an exclusive owner.
* Historical duplicate request ids are left as they are (renumbering history
  is out of scope); new ids are unique and larger.
* Heartbeats depend on agents; a recovered request's uncommitted files are
  transferred as they are and must be reviewed by the new owner.

## Verification

`scripts/tests/test_process_concurrency.py`; VER-000165.
