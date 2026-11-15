# Autonomous Operating Model

**Normative rule:** `docs/requirements/development_operations.md` REQ-DEVOPS-015..027
**Constitution:** `AGENTS.md` (rules), this file (explanation — not authority)

This is the operating manual for the autonomous development system. Keep it separate from `AGENTS.md` (which contains only durable rules).

## 1. Artifact Types

| Artifact | Location | ID |
|---|---|---|
| Request | `requests/inbox|processing|completed|rejected|needs-review/` | `REQ-IN-YYYY-MM-DD-NNN` or filename |
| Intent | `requests/processing/INT-NNNNNN*.md` | `INT-NNNNNN` |
| Requirement | `docs/requirements/*.md` | `REQ-DEVOPS-*`, `ARCH-AUDIT-*`, `TICKET-ENGINE-*`, and the project's domain namespaces |
| Decision | `docs/decisions/ADR-NNNNNN*.md` | `ADR-NNNNNN` |
| Work | `work/queued|active|blocked|needs-review|verification|completed|cancelled/` | `WORK-NNNNNN` |
| Test intent | `docs/verification/test-intents/TEST-*.md` | `TEST-*` |
| Evidence | `evidence/VER-NNNNNN*.md` | `VER-NNNNNN` |
| State mirror | `.agent/state/*.md` + `.agent/state/claims/*.lock` | — |

Indexes/manifests: `docs/requirements/requirements.txt`, `docs/decisions/decisions.txt`, `work/work.txt`, `evidence/evidence.txt`, `.agent/state/{requests,work,runs}.md`.

## 2. Lifecycle

```
Human -> requests/inbox/<human>.md
          -> (agent) claim via scripts/claim.py -> requests/processing/
          -> intent + preflight (scripts/preflight.py)
          -> requirement (find existing -> update/link, else create)
          -> decision (if architecture changes)
          -> work (scripts/work.py create -> WORK-NNNNNN in work/active/)
          -> implementation + scope guard (register paths first: claim.py --own)
          -> build (scripts/devsys.py build build, or run_tests.py --build)
          -> tests (scripts/run_tests.py --tier t2 --request <file>: affected suites, parallel, recorded)
          -> evidence (scripts/evidence.py create --test-run <run id> -> VER-NNNNNN)
          -> documentation (docs/README, architecture, READMEs)
          -> drift/doc/invariant/health checks
          -> completion gate (scripts/completion_gate.py --work W --record -> evidence/gates/W.json)
          -> work.py transition W --to completed; claim.py --complete-request
          -> scripts/commit_request.py (owned paths only) -> claim.py --release-request
          |-> on ambiguity/conflict -> needs-review
          |-> on stale lock -> recovery
```

## 3. States and Transitions

All valid states are enumerated in `docs/process/state-model.md`. That file is authoritative; this file only summarizes.

- Request: `inbox -> processing -> completed | rejected | needs-review`
- Work: `queued -> active -> blocked | needs-review | verification -> completed | cancelled`
- Verification: `pending -> pass | fail | blocked | pre-existing`

`completed -> active` requires new WORK referencing predecessor (no silent history rewrite).

## 4. Claiming

- Identity: `export AGENT_ID=$(python3 scripts/claim.py --new-agent-id)` — one id per session; uid/pid defaults are refused. Also `export AGENT_OWNER_PID=<pid of the session's long-lived process>`, so the claim's liveness is verified (ALIVE / DEAD) instead of inferred from the heartbeat (ACTIVE / STALE); see `docs/process/recovery.md`.
- Next request: `python3 scripts/devsys.py next --claim` (highest priority, then arrival).
- Claim before reading/writing: `python3 scripts/claim.py --claim-request <file>` — atomic claim + move to `processing/`. Second agent gets `ALREADY CLAIMED` with the owner (exit 1) and must stop; a stale owner or orphan gives exit 2 (explicit recovery only).
- Ownership record: `.agent/state/claims/request-<file>.lock` (agent, token, claim time, heartbeat, state, owned paths, id tokens, commits, worktree, history). Inspect with `--status` / `--show`.
- Register paths before editing: `claim.py --own <file> --paths ...` (`--shared` for manifests, together with `--token <file> --tokens WORK-… VER-… ADR-…`). Allocate ids with `scripts/allocate_id.py WORK|VER|ADR`. Registering a path also records its **baseline** — what the file looked like before this request touched it — which is what change attribution is computed from (REQ-DEVOPS-042). Register a path *before* editing it, or the edits already in the file become this request's.
- Three ownership modes: **exclusive** (the default — nobody else may register the path), **`--shared`** (an append-only manifest; only lines naming your id tokens are committed) and **`--concurrent`** (ordinary content several requests may change — prose, code, configuration — attributed by your delta). Use `--concurrent`, not `--shared`, for a document that carries no request ids.
- Record your version when an edit round is done: `claim.py --snapshot <file> [--paths ...]`. What another agent writes into the same file afterwards is then not swept into your commit. `--snapshot --rebaseline` does the opposite — it declares the file's current content someone else's, so only what you change from then on is yours; use it after another request commits into a file you also hold. `--snapshot --from-head` adopts the current diff (everything the file gained since the last commit is yours), which is what to use when you registered a path after editing it. When the working copy of a shared file also holds work that is not yours and must not be disturbed, prepare your version in the scratch area (`paths.scratch`, default `tmp/`) and record it with `--snapshot --paths <path> --from-file <prepared>`.
- Two requests may legitimately hold the same file (`--shared`), including prose and code that carry no request id. `commit_request.py` commits only your delta over the baseline, so another request's uncommitted work stays uncommitted and its committed work is not reverted. A genuine overlap is refused with the path named; reconcile it, `--snapshot --rebaseline` that path, and commit again.
- Commit only owned changes: `python3 scripts/commit_request.py --request <file> -m "..."`. Never `git add -A`, `git add .` or `git commit -a` in the shared worktree.
- Finish: `claim.py --complete-request <file>` (needs a current PASS gate record of every linked work item; or `--reject-request --reason` / `--needs-review-request --reason`), commit, `claim.py --release-request <file>` (refused while owned work is uncommitted).
- Liveness and recovery: ALIVE / DEAD / ACTIVE / STALE (`claims.stale_hours`, default 2 h). `--recover` and `--sweep` report; `--sweep --apply` performs only safe repairs; transfer with `--recover-request <file> --reason ...` — never from a verified-ALIVE owner.
- Optional isolation: `claim.py --create-worktree <file>`. See `docs/process/recovery.md` and ADR-000090.

## 5. Idempotency

Before creating any artifact, search for existing:

- Requirement: grep `docs/requirements/requirements.txt` + `docs/requirements/*.md` for ID or title.
- Work: grep `work/work.txt` + `work/**/*.md` for WORK-ID or request filename.
- Decision / Evidence: grep respective manifests.

Prefer `find existing -> update/link` over `create another copy`. Duplicate IDs are drift (`scripts/drift_detect.py`).

## 6. Preflight

Run `scripts/preflight.py --request <file> --work <id>` before implementation.

It checks: request exists/interpretable, requirement not duplicated, architecture impact considered, work exists, dependencies, affected tests/docs. Exit 2 = FAIL (do not proceed — escalate to needs-review). See `docs/process/escalation.md`.

## 7. Completion Gate

`scripts/completion_gate.py --work WORK-XXXXXX --record` checks the work record, the request link and ownership, cited requirements and decisions, evidence status, the linked test runs (PASS, executed, at least `completion.min_tier`, on the request's current code), open items, protected artifacts and the drift / doc-integrity FAIL findings, and appends the verdict to `evidence/gates/WORK-XXXXXX.json`. Only `completed` after PASS: `work.py`, `claim.py --complete-request` and `commit_request.py` check that record. Warnings are manual review items.

## 8. Drift, Integrity, Invariants

- `scripts/drift_detect.py` — requirement↔work, completed↔evidence, broken manifest links, duplicate IDs, stale status, orphaned requests/work, contradictory authority, stale deleted paths.
- `scripts/doc_integrity.py` — broken markdown links, stale paths, duplicate authority, obsolete terms, inconsistent naming.
- `scripts/invariant_check.py` — the project's architectural invariants, declared in `devsys.json` `invariants` (NOT CONFIGURED when none), and process protection (protected artifacts only through PROCESS-CHANGE requests).
- `scripts/requirement_impact.py --requirement <file|ID>` / `--diff` — affected impl/tests/work/docs report.
- `scripts/run_tests.py --plan --tier t2 --request <file>` — the test suites a change affects (qmake project graph + `scripts/test_manifest.json`), then `--tier t2` runs them in parallel isolated shards with a pass cache and a run record the completion gate checks (REQ-DEVOPS-046, ADR-000186).

All are deterministic, script-based, and cheap to run. Run before marking complete.

## 9. Process Protection and No Silent Override

`docs/process/process-protection.md` distinguishes `APPLICATION CHANGE` vs `DEVELOPMENT-SYSTEM CHANGE`. Protected artifacts (`AGENTS.md`, `development_operations.md`, `state-model.md`, etc.) require explicit `Type: process-change` work + ADR. Silent override of requirement/decision/contract is forbidden — STOP and surface conflict.

## 10. Escalation

`docs/process/escalation.md` defines `needs-review` conditions and procedure. Use `requests/needs-review/` and `work/needs-review/` + `needs-review` status. Do not leave ambiguous work in `blocked`.

## 11. Failure Recovery

`docs/process/recovery.md` documents crash, reboot, stale lock, failed test, partial change recovery. Restart flow:

```
health_check -> devsys.py status -> claim.py --sweep [--apply] -> drift_detect -> process_next_request --dry-run -> devsys.py next --claim
```

## 12. Scope and Stop-When-Done

- Scope guard: preflight estimates affected files/subsystems; completion gate compares actual `git diff --stat` vs estimate; flag explosion for re-evaluation.
- Stop when done: once `completion_gate` passes and requirement satisfied, STOP. Unrelated cleanup becomes separate request/work.

## 13. Health, Dry-Run, Process-Next-Request Contract

- `scripts/health_check.py` — aggregates all checks into `PASS/WARN/FAIL` with actionable findings; a check that cannot finish is UNKNOWN (overall WARN), never PASS.
- `scripts/devsys.py status` — workers and liveness, claims, recoverable work, inbox and next eligible, needs-review, work by state, last success / failure.
- `scripts/process_next_request.py --dry-run` — reports intended requirement/work/scope/tests/docs without mutation.
- Safe contract (19 steps): see this file §2 plus `scripts/process_next_request.py --help`. If any stage fails, stop at appropriate state (completed not falsely advanced; claim released or escalated).

## 14. AI Experience — Fast Discovery

Agent starting from scratch reads in order:

1. `AGENTS.md` (rules, 1 min)
2. `docs/README.md` (map)
3. `docs/process/state-model.md` + this file (lifecycle)
4. `requests/inbox/` (next work) or `.agent/state/*.md` (mirrors)
5. Relevant `docs/requirements/requirements.txt` entry -> owning doc
6. `work/work.txt` for active work, `evidence/evidence.txt` for verification

No need to read entire repository.

## 15. Human Experience

Human files a request with `python3 scripts/create_request.py --source chat --text "..."` (or writes plain Markdown to `requests/inbox/` using `requests/request-template.md`). No IDs, no work creation required. Progress visible via `python3 scripts/devsys.py status`, `requests/completed/`, `work/work.txt`, `evidence/VER-*.md`, and `scripts/health_check.py`.

## 16. Avoiding Bloat

Every mechanism must: prevent a real failure mode, be automatable, justify complexity, be expressed as simple files/scripts, and reduce agent reasoning burden. Prefer files/scripts over databases; prefer deterministic checks over prose.


