# Verification Run State

Test runs are recorded by `scripts/run_tests.py` (REQ-DEVOPS-046, ADR-000186),
not by hand in this file:

* every run: `<build dir>/test-results/runs/<run id>.json` — revision, changed
  paths, tier and scope, per-group status / wall time / cases, and for a
  request-scoped run the fingerprint of the request's code;
* the latest run of a build tree: `<build dir>/test-results/latest.json`;
* the pass cache and per-case durations: `<build dir>/test-results/cache.json`,
  `durations.json`.

Work records link their runs (`**Test-Run:** <run id>`) and
`scripts/completion_gate.py` checks them. This file is kept as the pointer
(the source project's last hand-written run entry was historical and was not
extracted).
