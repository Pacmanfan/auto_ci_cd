# Worker Orchestrator

**Requirements:** `docs/requirements/worker_orchestration.md` WORKER-001..013
**Decision:** ADR-000200 (carries ADR-000197 and ADR-000198 forward); ADR-000201 (agent profiles, coordination)
**Code:** `scripts/workers/` (package), `scripts/devsys.py workers | queue | priority` (CLI), `tools/panel/` (Qt control panel)
**Agent configuration:** `docs/process/agent-profiles.md`
**Coordination and the other panels:** `docs/process/agent-coordination.md` (roster, messages between agents, web control panel, terminal dashboard)

Workers execute this development system's requests unattended. Each worker
works through the same lifecycle an interactive agent follows (AGENTS.md):

- it claims a request;
- a fresh coding-agent session does the work, with records, tests,
  evidence, the gate and an owned-path commit;
- the worker checks the result independently, and moves on.

**The request lifecycle stays the single source of truth.** Workers add
execution, not a second ticket system.

## 1. Responsibility boundaries

| Part | Owns | Never does |
|---|---|---|
| Request lifecycle (`requests/`, `agent_process.py`, `claim.py`, `work.py`, `evidence.py`, `completion_gate.py`, `commit_request.py`) | request states, claims and liveness, work and evidence records, gate records, commits, priority | know about workers |
| Worker (`scripts/workers/runner.py`, one process per worker) | selecting and claiming one request, running the agent session, independent validation, deciding retry / escalation from the request's state | keep a request status, commit, reset or discard anything, touch another worker's state or claim |
| Agent session (the configured coding agent) | the work itself, through the framework tools, under the worker's agent id | claim another request, take over another session's claim |
| Worker API (`scripts/workers/pool.py`) | status, start, stop, enable, disable, recover, peek | decide request state |
| Clients (`devsys.py workers`, the control panel) | presentation and operator input | contain lifecycle logic |

## 2. State model

**Request states** are the framework's (`docs/process/state-model.md`).
The worker views name them:

| View | Request state |
|---|---|
| queued | `inbox`, unclaimed, ordered by priority, then arrival (`devsys.py queue`) |
| working | `processing`, claimed; the claim's agent id names the worker |
| finished | `completed`, `rejected`, or `needs-review` (waiting for a human) |

**Worker states** (`worker.json`, per worker; never a request status):

| Status | Meaning |
|---|---|
| `stopped` | no worker process. A record that still names an active attempt is settled by `recover`. |
| `idle` | the worker process runs and waits for a request |
| `working` | an agent session runs on the worker's active request |
| `stopping` | a stop was requested; the current attempt finishes first |

`enabled` (a disabled worker is never started) and `desired` (running /
stopped) are the operator's settings. Every reader verifies the recorded
process, and the active session with its `DEVSYS_WORKER_SESSION` marker, as
ALIVE / DEAD / UNKNOWN. A record saying something runs is not trusted.

**Attempt results** come from the agent's report, or from what the worker
observed:

| Result | Meaning | Counted |
|---|---|---|
| COMPLETED, PARTIAL, BLOCKED, FAILED, ESCALATE | the agent's own report | PARTIAL and FAILED yes |
| TIMEOUT | no exit within `workers.attempt.max_runtime_minutes`; the session and its process tree were ended | yes |
| CRASHED | the session ended without a result event | yes |
| INVALID_REPORT | a result, but no usable report | yes |
| INTERRUPTED | stop now, or the session ended while no worker was attached | no |
| RATE_LIMITED | the agent hit a usage limit; the worker waits (bounded) | no |

**Outcome**, decided from the request's state *after* the attempt:

- **`completed` / `rejected`:** done.
- **`needs-review`:** the agent escalated; it waits for a human.
- **Still `processing`:**
  - an uncounted result is resumed;
  - a counted one is retried under the same claim;
  - after `max_attempts_per_request` counted attempts, or on BLOCKED /
    ESCALATE, the worker escalates the request itself
    (`processing -> needs-review` with the reason).
- **COMPLETED report while still `processing`:** the framework refused the
  completion, for example with no gate record. This is an attempt without
  completion, never a success.
- **A worker's own escalation** moves the request to `needs-review` and
  parks its claim. It does not commit the move, because workers never
  commit. The human (or agent) who takes the parked request over commits it
  with the resolution, through `commit_request.py` as for any request.

## 3. Using it

```bash
python3 scripts/create_request.py --source chat --text "..." --priority HIGH   # one chat prompt = one request
python3 scripts/devsys.py queue                       # what the workers will take, in order
python3 scripts/devsys.py priority <request> CRITICAL # change an open request's priority (recorded)

python3 scripts/devsys.py workers status [--json]     # workers, liveness, active requests, queue
python3 scripts/devsys.py workers start [ID ...]      # all enabled workers when no id is given
python3 scripts/devsys.py workers stop [ID ...] [--now] [--wait S]
python3 scripts/devsys.py workers enable|disable ID
python3 scripts/devsys.py workers recover [ID ...] [--force]
python3 scripts/devsys.py workers peek ID             # attempt, validation, transcript tail, events
python3 scripts/devsys.py workers run ID [--once]     # the worker process itself, in the foreground
tools/worker-panel                                    # the Qt control panel
python3 tools/panel/web_panel.py [--open]             # the web control panel (docs/process/agent-coordination.md)
python3 tools/panel/dash.py                           # the terminal dashboard
python3 scripts/msg.py who | log --follow             # the agents and what they tell each other
```

The **control panel** (`tools/panel/worker_panel.ui`, `worker_panel.py`)
shows:

- the maximum and running counts;
- a card per worker (status, active request with priority, elapsed time and
  session liveness);
- the selected worker's details: process, active request, attempt,
  validation steps, transcript tail, events;
- tabs for queued, working and finished requests.

Its buttons call the same API: start, stop, stop now, recover,
enable / disable, start / stop all, and set the priority of a queued request.

## 4. Configuration (`devsys.json` `workers`)

| Key | Default | Meaning |
|---|---|---|
| `max_workers` | 2 | worker slots `worker-01` .. and the most that run at once |
| `isolation` | `shared` | `shared`: all workers in the project checkout (claims and owned-path commits keep them apart). `worktree`: each request in its own git worktree and branch |
| `poll_seconds` | 5 | idle worker's queue check interval |
| `attempt.max_runtime_minutes` | 180 | per agent session |
| `attempt.max_attempts_per_request` | 2 | counted attempts before escalation |
| `attempt.kill_grace_seconds` | 30 | TERM before KILL when ending a session |
| `attempt.max_consecutive_failures` | 3 | a worker stops itself after this many |
| `attempt.max_rate_limit_wait_minutes` | 720 | longest usage-limit wait before the worker stops |
| `selection.exclude_types` | `["PROCESS-CHANGE"]` | request types workers never take |
| `agent.command` | `[]` | the agent's argv (`{session_id}`, `{name}`, `{attempt_dir}`); empty = NOT CONFIGURED |
| `agent.env_scrub` | `[]` | variables removed from the agent's environment |
| `agent.rate_limit_patterns` | usage / rate limit words | how an error result is recognised as a usage limit |
| `agent.prompt` | `scripts/workers/prompts/worker.md` | the worker prompt |
| `profiles` | `{}` | named agent profiles: each overrides `agent` key by key (`command`, `env_scrub`, `rate_limit_patterns`); `{scripts}` in a command is this system's scripts directory |
| `assign` | `{}` | worker slot -> profile name (`{"worker-02": "<name>"}`); a slot without an entry runs `agent` (profile `default`); an undefined name is a configuration problem and that worker does not start |
| `validation` | git-sanity, request-state, commit-binding | steps after every attempt (builtins, or commands with `when` and `exit_codes`) |

Every worker's agent gets these variables:

- `AGENT_ID`, and `AGENT_OWNER_PID` (the worker process);
- `DEVSYS_WORKER`;
- `DEVSYS_WORKER_REQUEST`, `DEVSYS_WORKER_REQUEST_FILE`,
  `DEVSYS_WORKER_REQUEST_PATH`;
- `DEVSYS_WORKER_ATTEMPT`, `DEVSYS_WORKER_REPORT`;
- `DEVSYS_WORKER_SESSION` (the liveness marker);
- `DEVSYS_SCRIPTS` (the tools, as seen from its working directory).

## 5. Runtime state

Everything is under `.agent/state/runtime/workers/` (untracked, main
worktree):

```text
<worker-id>/worker.json          the worker's record (atomic)
<worker-id>/worker.lock          held by the live worker process (one process per worker)
<worker-id>/events.jsonl         its events
<worker-id>/process.log          output of a detached worker process
<worker-id>/attempts/<attempt>/  prompt.md, launch.json, transcript.jsonl, stderr.log, report.json,
                                 attempt.json, claim-snapshot.json, validation/<step>.log
attempts.jsonl                   concluded attempts of all workers (counts only; see WORKER-002)
```

## 6. Recovery

A worker process that dies leaves its record saying it was working.
`workers status` shows it as stopped (process DEAD). To settle it:

```bash
python3 scripts/devsys.py workers recover worker-01           # a DEAD session: concluded INTERRUPTED
python3 scripts/devsys.py workers recover worker-01 --force   # also end a still-running orphaned session
python3 scripts/devsys.py workers start worker-01             # resumes its request under the same claim
```

A worker that starts again first settles or re-adopts its own recorded
attempt. A running worker is never touched by recovery. A session that
cannot be verified (UNKNOWN) is left alone without `--force`.

For the claims themselves, `claim.py --sweep` and `--recover-request` apply
as for any agent (`docs/process/recovery.md`). A worker's claims carry its
process as owner: while the worker runs they are ALIVE, and after it dies
they are DEAD.

## 7. Isolation and the shared checkout

- **Shared** is the default. Workers work in the project checkout like
  concurrent human sessions: they register every path before editing and
  commit only their own delta. A path owned by one request cannot be taken
  by another.
- **Worktree** gives each request its own worktree
  (`<project>-worktrees/<request stem>`, branch `request/<stem>`). The agent
  runs the worktree's own copy of the tools, so its commits land on the
  request branch.
  - **Publishing:** merging request branches back is manual
    (`docs/process/recovery.md`, roadmap COR-14).
  - **Request moves:** request transitions happen in the main checkout's
    `requests/`.
  - **Status:** worktree mode is suitable for code changes that should not
    disturb the main checkout's build. It is not yet a complete unattended
    path to `main`.

## 8. Installation as a project subdirectory

The system derives every path from its own location. `paths.project_root`
names the project it serves, relative to the system directory:

- `.` when the system *is* the project root (this layout);
- `..` for `<project>/<system-directory>/`.

Workers run agents in the project root, or the request's worktree.
`DEVSYS_SCRIPTS` and the prompt name the tools relative to that directory.
The remaining step for a full subdirectory installation is repository-path
attribution relative to the project root (`CI_CD.md` §11).
