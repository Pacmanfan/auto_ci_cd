# WORK-000005: Root README.md: short orientation pointing to the canonical documentation

**Status:** needs-review
**Type:** documentation
**Request:** requests/processing/2026-11-15-add-a-readme-md-at-the-reposit-req-chat-20261115-003.md
**Requirements:** REQ-DEVOPS-011
**Evidence:** VER-000005

## Scope

Root README.md: short orientation pointing to the canonical documentation

## Implementation

- `README.md` (new, 72 lines): what the system is (requests, claims, gated completion, workers, agent coordination), getting started (`devsys.py doctor` / `status`, submitting a request, starting workers), the panels (`msg.py panels`, web control panel, terminal dashboard, Qt control panel) and pointers to `docs/README.md`, `AGENTS.md`, `docs/process/agent-coordination.md`. Product-neutral; the project name is the distribution's placeholder as in `docs/README.md`.
- `docs/README.md`: one entry naming the root README as an orientation that points to the canonical entry point.
- `docs/requirements/requirements.txt` and its mirror `scripts/templates/requirements.txt`: row `root-readme` (REQ-DEVOPS-011 applied, text unchanged).
- `scripts/tests/test_root_readme.py` (new): the README checked against the repository.
- Process note: `docs/requirements/development_operations.md` was registered and a sentence added to REQ-DEVOPS-011 before it was recognised as a protected artifact. The edit was reverted (the file is identical to HEAD); the registration remains in the claim because `scripts/claim.py` has no command that withdraws one. See "Escalation".

## Tests

- `scripts/tests/test_root_readme.py` (5 cases): exists and at most 80 lines; the three pointers; the requested topics; every repository path named in a code span exists; every command in a bash block names an existing script, a sub-command the tool has a help page for and options that page lists.
- Run 20261115T005237-3454064 (t2) PASS on the current code.
- Run 20261115T004451-3382086 (t2) PASS, superseded (code changed afterwards).
- Run 20261115T004851-3414816 (t2) FAILED, superseded: `test_agent_coordination.WebPanel.test_the_event_stream_sends_a_snapshot_and_again_when_a_message_arrives` timed out reading the event stream; unrelated to this change, it passed in the run before, three times alone and in the run after.

**Test-Run:** 20261115T005237-3454064

## Regression impact and documentation

- Regression impact: none on behaviour; documentation and one test only. The new test fails when a command or path shown in `README.md` stops existing, which is its purpose.
- Documentation updated: `README.md`, `docs/README.md`. `README.md` is not in `FRAMEWORK_PATHS`: `devsys.py install` does not copy it into another project.

## Escalation

The work is done and verified (VER-000005, run 20261115T005237-3454064); the completion gate (`evidence/gates/WORK-000005.json`) passes 12 checks and fails one: the claim registered the protected artifact `docs/requirements/development_operations.md`, and this is a DOCUMENTATION request. The file is not changed (identical to HEAD: the one-sentence edit was reverted), but `scripts/claim.py` cannot withdraw a registration, so the gate cannot pass under this claim. The operator was asked (MSG-20f8febf4b) and did not answer within 300 s.

**Question for the human:** complete this request with a recorded waiver (`scripts/claim.py --complete-request <file> --waive-gate "protected path registered by mistake, file unchanged"`), or hand it back (`--release-request <file> --requeue`) so that a fresh claim without that registration passes the gate unchanged? Separately: should `claim.py` get a command that withdraws the registration of an unchanged path (a process change)?

## Traceability

REQ-CHAT-20261115-003 -> REQ-DEVOPS-011 -> WORK-000005 -> evidence (pending)

## Transitions

- 2026-11-15T05:43:46.178558+00:00 created in active by agent-worker-01-dev-a1e514
- 2026-11-15T06:01:27.882890+00:00 active -> needs-review by agent-worker-01-dev-a1e514: Gate refuses: protected docs/requirements/development_operations.md was registered by mistake (file unchanged, edit reverted) and claim.py cannot withdraw a registration. Human decision: complete with --waive-gate, or requeue for a fresh claim? Work is otherwise complete (VER-000005, t2 run 20261115T005237-3454064 PASS).
