# ADR-000193: The request commit records the working tree's file mode

**Status:** ACCEPTED (human review 2026-09-24: the user approved the tooling change — "perform REQ-AUTO-20260924-003 . you have my approval to change tooling")
**Date:** 2026-09-24
**Work:** WORK-000314 (REQ-AUTO-20260924-003, process-change)
**Requirements:** `docs/requirements/development_operations.md` REQ-DEVOPS-042 (amended)
**Amends:** ADR-000090 (request-scoped commits through a private index)

## Context

`scripts/commit_request.py` builds each commit in a private index started
from HEAD. For every owned path it writes the request's content as a blob and
stages it with `git update-index --cacheinfo <mode>,<blob>,<path>`
(`scripts/agent_process.py`). The mode came from `_index_mode()`, which is
the mode already in that index (HEAD) or `100644` for a path HEAD does not
have. The working tree was never consulted. Two consequences:

* Every new executable was committed as `100644`. Commits 65f6ea9d and
  b6c518de stored `scripts/build_liquid_runtime.sh` and
  `tools/remote-deploy-test/physical_acceptance.sh` (plus three target-side
  scripts) without `+x`. The documented direct invocations then fail with
  "Permission denied" on a fresh checkout.
* A later `chmod +x` could not be committed at all. The content equals the
  registration baseline, so `_delta_content()` reports "unchanged" and the
  path is skipped ("nothing to commit").

## Decision

1. The mode staged for an owned regular file is the mode `git add` would
   record: `100755` when the working-tree file has the owner-exec bit
   (`S_IXUSR`), `100644` otherwise (`_commit_mode()`).
2. Where git does not trust the executable bit (`core.fileMode=false`), and
   for anything that is not a regular file in the working tree (deleted,
   symlink, gitlink), the indexed mode is kept, as git would keep it.
3. A mode-only change (content unchanged since registration, file in HEAD)
   is staged as HEAD's blob with the working tree's mode. HEAD's content is
   kept, so a foreign commit into the file is never reverted.
4. Content attribution (REQ-DEVOPS-042 baselines, three-way delta, shared
   manifest lines) is unchanged. Mode is a whole-file property: the mode
   committed is the working tree's at commit time.

## Consequences

- Scripts committed through the request tooling keep their executable bit,
  and a `chmod` on an owned path is committable like any other change.
- A mode change another session makes to a file this request also holds
  (concurrent paths) is committed with this request's commit. Modes cannot be
  split per hunk; such a change is rare and visible in the dry-run stat.
- Verification: `scripts/tests/test_process_concurrency.py`
  `GitCommitBoundaries` — `test_new_executable_file_is_committed_executable`,
  `test_mode_only_change_is_committed_with_the_content_unchanged`,
  `test_new_executable_with_an_empty_delta_is_refused_then_adopted_executable`,
  `test_core_filemode_false_keeps_the_recorded_mode` (VER-000314).
