# ADR-000203: This repository runs workers: configured agent profiles, coordination in the constitution

**Status:** ACCEPTED
**Date:** 2026-11-15
**Type:** process change (protected artifacts: `AGENTS.md`, `devsys.json`)
**Requirements:** `docs/requirements/worker_orchestration.md` WORKER-012 (amended); `docs/requirements/agent_coordination.md` COORD-004, COORD-005, COORD-012 (applied)
**Request:** REQ-CHAT-20261115-002 (operator: "do the process change and make this happen")
**Builds on:** ADR-000200, ADR-000201, ADR-000202

## Context

Until now this repository was only the neutral distribution of the
development system: `devsys.json` equalled its template, named no project
and no agent, so no worker could start. The coordination tools existed but
the agent constitution (`AGENTS.md`) did not mention them, so an agent that
only reads the constitution would never use them. The operator decided that
this repository is to be developed by its own workers, with two agent
products side by side.

## Decision

1. **`devsys.json` is configured for this project.** `project.name` is
   `auto_ci_cd` (the repository is now also an installed project: its
   instance files are its own and no longer have to equal the templates).
   `workers.agent` is the first agent CLI's documented profile and runs on
   `worker-01`; the profile for the second agent CLI, wrapped by the
   event-stream adapter with its raw stream kept per attempt, is assigned to
   `worker-02`. Both come from `docs/process/agent-profiles.md`.
2. **`AGENTS.md` gains "Coordination between agents"** under Concurrent
   Agents: the roster, `scripts/msg.py`, the station, when to read the inbox,
   how to ask an owner for a path, and that messages carry no authority.
   No lifecycle, completion, traceability or verification rule changes.
3. **The first agent CLI's project settings carry the message hooks**, so
   unread messages enter a running session after its next tool call and
   before it stops (COORD-012).
4. **WORKER-012 distinguishes the system from a project's configuration.**
   The project's own `devsys.json` and an agent CLI's project settings
   directory may name the products the project runs. Code, defaults,
   templates, environment variables and documentation outside the two
   documented exceptions stay neutral; the identity test checks that.

## Why the rule may change

A neutral system must still be usable, and using it means naming an
executable somewhere. WORKER-012 already said "configuration defaults", not
"a project's configuration"; the identity test was stricter than the
requirement only because this repository had never been configured. Keeping
the template and the defaults under the test preserves what the rule
protects: nothing a project installs carries a vendor.

## Consequences

- `devsys.py doctor` no longer reports the project name and the agent as
  not configured; `build.system`, `source_roots` and `invariants` remain
  unset and are still reported.
- `test_devsys_tooling.Templates` skips its instance-equals-template check
  here (an installed project). The manifests that are still mirrored into
  `scripts/templates/` by hand (requirements, decisions, test and audit
  manifests) are no longer protected by that test: `devsys.py install` from
  this repository takes the templates, so they must be kept current.
- Workers spend the operator's agent subscriptions and run with automatic
  permissions inside this checkout. `PROCESS-CHANGE` requests are still
  excluded from workers (`workers.selection.exclude_types`).
- Hooks in the project settings run for every session of that agent CLI in
  this repository, including headless ones.
- Human review: this change was ordered by the operator in the request; the
  commit is the reviewable record.
