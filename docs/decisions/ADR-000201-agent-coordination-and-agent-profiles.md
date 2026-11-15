# ADR-000201: Agents coordinate through a roster and a message bus; fleets mix agent profiles

**Status:** ACCEPTED
**Date:** 2026-11-14
**Requirements:** `docs/requirements/agent_coordination.md` COORD-001..009 (new); `docs/requirements/worker_orchestration.md` WORKER-006, WORKER-012 (amended)
**Request:** REQ-CHAT-20261114-001
**Builds on:** ADR-000090 (concurrent agents, claims), ADR-000198 (verified liveness), ADR-000200 (Worker Orchestrator)

## Context

Concurrent agents were kept apart by claims and owned paths, and nothing
else: an agent that met another agent's path could only fail, and the
operator could not see what agents would have told each other. Agents were
known only by machine ids. The operator asked for:

- agents that coordinate, peer-to-peer now, with room for a lead agent later;
- friendly names;
- a web control panel with full control, and a terminal dashboard, both
  showing the messages between agents;
- fleets that mix agent products (two products, one of them in two major
  versions), selectable per worker;
- all of it working with git worktrees.

## Decision

1. **Roster and bus as files in shared runtime state.**
   `.agent/state/runtime/coordination/` holds one JSON entry per agent and
   one append-only `messages.jsonl`. It sits in the main worktree
   (`agent_process.state_dir`), so every linked worktree shares it. Writes
   are serialised by the existing inter-process lock; sequence numbers are
   strictly increasing. No daemon, no socket, standard library only: any
   agent that can run a command can take part.
2. **Messages carry no authority.** The bus never touches a claim, a
   registration, a request or a gate record. Ownership still changes only
   through `claim.py`. The worker prompt says messages are information.
3. **Delivery is pull, plus a nudge.** A batch agent session cannot be
   interrupted. Agents read with `msg.py inbox` / `wait`; `claim.py` prints
   a `MESSAGES:` line on stderr while its caller has unread messages, so an
   agent that never polls still learns of them at its next lifecycle
   command.
4. **Peers and roles.** Recipients are an agent, a role, `operator` or
   `all`. A lead agent is later just an agent holding the role `lead`.
5. **One snapshot and one action function** (`workers.overview`) feed every
   new view. The web panel (`http.server`, server-sent events) and the
   terminal dashboard (curses) are clients, like the Qt panel is of
   `workers.pool`.
6. **The web panel is local by default and guarded.** Loopback binding, a
   per-start token on every API call, a `Host` check against DNS rebinding,
   text never inserted as markup.
7. **Agent profiles.** `workers.profiles` names alternative agent settings
   and `workers.assign` maps a worker slot to one; `workers.agent` stays the
   default profile, so existing configurations are unchanged. An assigned
   profile that does not exist is a configuration problem, never a silent
   fallback.
8. **Adapters are the second documented vendor exception.** An agent whose
   output is not the worker protocol is wrapped by an adapter in
   `scripts/workers/profiles/`. That directory, with
   `docs/process/agent-profiles.md`, is where a product may be named.
   Request records quote people verbatim and are input, so the identity
   check does not scan `requests/`.

## Alternatives rejected

- **A broker process or sockets:** one more thing to start, supervise and
  recover; agents in sandboxes may not reach it. Files and the existing lock
  already give ordering and durability.
- **Messages as tracked files in git:** every message would dirty the shared
  checkout and enter commits; the conversation is runtime state, like worker
  events.
- **Injecting messages into a running session through product-specific
  hooks:** not available for every product; left as a possible per-profile
  addition. The `claim.py` nudge works for all.
- **A coordinator agent now:** more cost and a new failure mode before the
  peer bus has been used. Roles keep the door open.
- **A web framework / a terminal UI library:** new dependencies for what the
  standard library does.
- **One adapter per major version:** the adapter decides per event, so one
  profile serves both stream shapes.

## Consequences

- Agents can negotiate (`path-request`), hand work over and ask the operator
  before escalating; the existing BLOCKED / needs-review paths remain the
  fallback.
- The conversation is untracked and local to the clone. It is not evidence;
  anything that must last belongs in the work record.
- Anyone who can run commands in the checkout can write as `operator`. That
  is the same trust boundary as the claims themselves.
- The second major version of the wrapped product was not released when the
  adapter was written: its mapping is unverified and marked so in the
  adapter and in `docs/process/agent-profiles.md`.
- Deferred: a lead agent role with behaviour of its own; message injection
  into running sessions; retention / rotation of `messages.jsonl`.
