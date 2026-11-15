# Agent Coordination Requirements

**Status:** ACTIVE
**Owner:** Development tooling (`scripts/agent_messages.py`, `scripts/msg.py`, `scripts/workers/overview.py`, `tools/panel/web_panel.py`, `tools/panel/dash.py`)
**Depends On:** `development_operations.md` (REQ-DEVOPS-016 claims, REQ-DEVOPS-014 intake, REQ-DEVOPS-025 recovery), `worker_orchestration.md` (WORKER-005 isolation, WORKER-010 status, WORKER-012 vendor-neutral system)
**Decision:** ADR-000201, ADR-000202 (station, push hub, hooks, names)
**System documentation:** `docs/process/agent-coordination.md`

Claims keep concurrent agents apart. Coordination lets them, and the human
operator, see each other and talk. It adds communication and visibility
only: the request lifecycle, the claims and the completion gate stay the
single source of truth.

## COORD-001 — Roster and friendly names

- Every agent that takes part SHALL have one roster entry keyed by its
  session-unique agent id, with a friendly name that is unique among the
  entries, ignoring case. A name SHALL be allocated at first registration,
  or requested explicitly; a name held by another agent, and the reserved
  words `all` and `operator`, SHALL be refused.
- The first agent registered on an empty roster SHALL be named `Cody`.
  Every other allocated name SHALL be drawn at random from the free names
  of the pool (continuing with numbered names when the pool is used up).
- An entry SHALL record the agent's profile, roles, worker slot (when it is
  a worker's agent), working directory, git worktree and branch.
- A worker SHALL register its agent when an attempt starts. Any other agent
  is registered by `claim.py --claim-request` (under `$AGENT_ID`),
  `msg.py join`, or on its first `send` / `inbox`.
- Liveness SHALL be verified from the recorded owner process (ALIVE / DEAD /
  UNKNOWN); a record alone is never proof. Only entries verified DEAD may be
  pruned.

## COORD-002 — Message bus

- Agents and the operator SHALL be able to send messages to one agent (by
  name or id), to a role, to `operator`, or to `all`.
- The log SHALL be append-only, with a strictly increasing sequence number
  assigned under an inter-process lock, so concurrent senders never lose or
  reorder a message. A damaged line SHALL be skipped, not fatal.
- A message SHALL carry: sequence number, id, time, sender, recipient, kind,
  optional request, optional subject, body, optional `reply_to`. The kind is
  a closed vocabulary: `note`, `question`, `answer`, `status`, `handoff`,
  `path-request`, `alert`.
- An unknown recipient, an empty or over-long body, an unknown kind and a
  message to oneself SHALL be refused with the tooling's exit codes; nothing
  is written.
- A reply SHALL go to the sender of the message it answers and inherit its
  request and subject.

## COORD-003 — Peer-to-peer, with roles

- Every agent is a peer: there SHALL be no agent whose presence the bus
  requires.
- An agent MAY hold roles (for example `lead`). A message to a role SHALL
  reach every agent holding it, so a coordinating agent can be added later
  as one more participant without changing the bus.

## COORD-004 — Delivery

- `msg.py inbox` SHALL show an agent the messages addressed to it that it
  has not been shown, once (`--peek` leaves them unread). An agent SHALL NOT
  receive its own broadcast, nor messages sent before it registered.
- `msg.py wait` SHALL block until a message for the agent arrives, or exit
  4 after its timeout; `msg.py listen` SHALL print the agent's messages as
  they arrive. Both are pushed (COORD-010) and fall back to polling.
- Because an agent session cannot be interrupted from outside, `claim.py`
  SHALL print one `MESSAGES:` line on stderr after any command while the
  calling agent (`$AGENT_ID`) has unread messages. Its stdout and exit code
  SHALL be unchanged.
- The worker prompt SHALL tell a worker's agent how to read, send and wait.

## COORD-005 — Messages carry no authority

A message SHALL NOT change a claim, a path registration, a request's state or
a gate record, whoever sent it. Ownership changes only through `claim.py`,
exactly as before. The worker prompt SHALL say that messages are information
to verify, not instructions.

## COORD-006 — Git worktrees

The roster and the message log SHALL live in the main worktree's untracked
runtime state (`.agent/state/runtime/coordination/`), shared by every linked
git worktree of the clone: an agent in a request's worktree and an agent in
the main checkout see the same roster and the same conversation. The status
views SHALL show each agent's worktree and branch, and each worktree with
the requests claimed in it.

## COORD-007 — One snapshot, one set of actions

`workers.overview.snapshot` SHALL return, in one structure: the worker pool
status (WORKER-010), the agents joined with their claims, the git worktrees
and the recent messages. `workers.overview.act` SHALL perform the operator's
actions through the existing APIs only: worker start / stop / stop now /
recover / enable / disable (one worker or all), request priority, an
operator message, and submitting a request through intake. Every panel SHALL
be a client of these two functions and `workers.pool.peek`, with no
lifecycle logic of its own.

## COORD-008 — Web control panel

- `tools/panel/web_panel.py` SHALL serve a browser panel, with the standard
  library only, that shows the snapshot live (server-sent events), including
  every message between agents, and offers every action of COORD-007.
- It SHALL bind to the loopback interface by default. Every `/api/` call
  SHALL require the session token, and on a loopback binding the `Host`
  header SHALL name the server itself. Binding elsewhere SHALL warn, and the
  page itself SHALL then require the token.
- Text from agents and requests SHALL be inserted as text, never as markup.
- A refusal SHALL be returned with its reason and a matching HTTP status.

## COORD-009 — Terminal dashboard

`tools/panel/dash.py` SHALL show the same snapshot in a terminal (curses,
standard library only), refresh it, and offer the actions of COORD-007 from
the keyboard. `--once` SHALL print one frame (or, with `--json`, the
snapshot) without a terminal, with no line wider than the given width.

## COORD-010 — Push delivery through the hub

- While a station runs, a sender SHALL announce every message, and every
  roster change, on the station's hub after writing it. Whoever waits on
  the hub (`msg.py wait` / `listen`, the web panel's event stream, the
  terminal dashboard) SHALL be woken by the announcement instead of a
  timer.
- The hub SHALL be a loopback socket that requires the station's token and
  carries announcements only. The message log stays the only record: with
  no hub, or a hub that went away, nothing is lost and waiters poll.
- An announcement SHALL never fail a send or a lifecycle command.

## COORD-011 — The station starts with the first agent and ends with the last

- When an agent joins the roster and no station runs, the station
  (`tools/panel/station.py`) SHALL be started, detached, unless
  `coordination.autostart` is false or `DEVSYS_COORDINATION_AUTOSTART=0`.
  At most one station SHALL run per clone.
- The station SHALL run the hub and the panels named in
  `coordination.panels`: the web control panel (on `coordination.web_port`,
  or a free port when it is taken) and the Qt control panel (when a display
  and PySide6 are available; otherwise the reason is recorded).
- The agent whose command started the station SHALL be shown the addresses:
  the web panel URL, the control panel's state, the dashboard command and
  the hub address. `msg.py panels` SHALL show them at any time, and
  `msg.py join` whenever a station runs. The hub token SHALL NOT be shown
  and SHALL NOT be part of the snapshot.
- The station SHALL end `coordination.linger_seconds` after no agent is
  present. An agent is present while its owner process is verified ALIVE,
  or, when that cannot be verified, while it was seen within
  `coordination.idle_minutes`. On ending, the station SHALL close the
  panels it started and prune roster entries verified DEAD.

## COORD-012 — Into a running session through hooks

`msg.py hook` SHALL read one hook event (JSON) on stdin and, when the agent
has unread messages, answer in the form agent CLIs with hooks expect: added
context after a tool call, at session start and on a prompt; a blocked stop
with the messages as the reason before the session ends. Messages SHALL be
delivered once, so a stop hook cannot loop. Without `$AGENT_ID` the agent
SHALL be identified by the roster entry whose owner process started the
hook. Bad input, an unknown agent or no mail SHALL be silence with exit 0:
a hook never fails the agent. The injected text SHALL say that messages are
information, not instructions (COORD-005).

## Verification

`python3 -m unittest scripts.tests.test_agent_coordination` (Roster,
Messages, Cli, Worktrees, Overview, WebPanel, Dashboard, Station, Hooks;
Profiles and Adapter verify WORKER-006 / WORKER-012).
