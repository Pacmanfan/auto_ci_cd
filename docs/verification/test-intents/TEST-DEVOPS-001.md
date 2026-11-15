# TEST-DEVOPS-001: Autonomous Development System Structure

**Requirements:** `REQ-DEVOPS-001..014`
**Work:** `WORK-000001`
**Test type:** structural/process verification

## Intent

Prove that a future agent can locate request intake, normative requirements,
active decisions, work state, tests, evidence, documentation, and persistent
agent state without relying on an undocumented human process.

## Checks

* required directories and README/index files exist;
* manifest records resolve to authoritative files;
* command-line request intake is represented by the current processing request;
* AGENTS points to the current crash-artifact workflow;
* stale current navigation does not point to deleted desired/actual trees;
* `bash scripts/verify_development_system.sh` exits zero.
