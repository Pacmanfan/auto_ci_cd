#!/usr/bin/env bash
# Structural verification of the development-control system (REQ-DEVOPS-001..014).
#
#   * the tracked scaffold, manifest targets and requirement index
#     (python3 scripts/devsys.py verify-structure; structure.* in devsys.json);
#     untracked runtime state is created on first use, so a fresh clone passes;
#   * project checks listed in devsys.json structure.extra_checks (run there);
#   * the process tooling's own unit tests, unless DEVSYS_SKIP_UNITTESTS=1
#     (scripts/run_tests.py sets it: the `scripts` suite runs them, REQ-DEVOPS-046).
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="${AGENT_PROCESS_ROOT:-$ROOT}"
failures=0

if ! AGENT_PROCESS_ROOT="$ROOT" python3 "$(dirname -- "${BASH_SOURCE[0]}")/devsys.py" verify-structure; then
    failures=$((failures + 1))
fi

if [[ "${DEVSYS_SKIP_UNITTESTS:-0}" != "1" ]] && \
   ! python3 -m unittest discover -s "$(dirname -- "${BASH_SOURCE[0]}")/tests" -q >/dev/null 2>&1; then
    printf 'PROCESS TOOLING TESTS FAILED (python3 -m unittest discover -s scripts/tests -v)\n' >&2
    failures=$((failures + 1))
fi

if [[ "$failures" -ne 0 ]]; then
    printf 'Development system verification failed: %d issue(s)\n' "$failures" >&2
    exit 1
fi

printf 'Development system verification passed\n'
printf 'Root: %s\n' "$ROOT"
printf 'Request intake: %s/requests/inbox\n' "$ROOT"
printf 'Requirements: %s/docs/requirements/requirements.txt\n' "$ROOT"
printf 'Work manifest: %s/work/work.txt\n' "$ROOT"
printf 'Evidence manifest: %s/evidence/evidence.txt\n' "$ROOT"
