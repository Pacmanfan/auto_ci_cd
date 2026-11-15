# Autonomous Agent State

This directory is the minimal machine-operable state for repeated agents.

* `requests.md` records request counts and the next eligible request.
* `work.md` records the active claim and blockers.
* `runs.md` points to the test run records `scripts/run_tests.py` writes (`<build>/test-results/runs/<run id>.json`, REQ-DEVOPS-046); runs are no longer recorded here by hand.
* `claims/request-<file>.lock` is the ownership record of a request (agent, heartbeat, owned paths, commits, history; ADR-000090). Use `scripts/claim.py --status/--show/--recover/--recover-request`. Untracked.
* `runtime/` holds the id allocator counter, inter-process locks and claim history. Untracked; shared by all worktrees of the clone.

State is advisory metadata except claim locks (which are enforced). The request, requirement, work, decision, test, and
evidence records remain authoritative. An agent must preserve state when it
discovers a conflict rather than silently advancing it.

Valid states are defined in `docs/process/state-model.md`. Operational procedures: `docs/process/autonomous-operating-model.md`, `docs/process/recovery.md`, `docs/process/escalation.md`.
