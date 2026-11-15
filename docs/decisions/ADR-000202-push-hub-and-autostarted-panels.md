# ADR-000202: A station per clone: push delivery through a local hub, panels that live with the agents

**Status:** ACCEPTED
**Date:** 2026-11-15
**Requirements:** `docs/requirements/agent_coordination.md` COORD-001, COORD-004 (amended), COORD-010..012 (new)
**Request:** REQ-CHAT-20261115-001
**Amends:** ADR-000201 (decision 3 "delivery is pull, plus a nudge"; the rejected alternative "a broker process or sockets")

## Context

ADR-000201 made the message log a file and delivery a pull: agents polled,
panels refreshed on a timer, and a broker process was rejected as one more
thing to run. The operator then decided:

- messaging is to be push based, with a lightweight IPC mechanism;
- the web dashboard and the control panels start by themselves when the
  first agent starts and end when the last agent closes, and their
  addresses are printed at start;
- names are drawn at random, and the first agent opened is named Cody.

## Decision

1. **One station process per clone** (`tools/panel/station.py`). The first
   agent to join the roster starts it, detached; `station.lock` keeps it
   single. It ends `coordination.linger_seconds` after no agent is present
   (owner process verified alive, or, where that cannot be verified, seen
   within `coordination.idle_minutes`), closes what it started and prunes
   entries of sessions verified dead. This answers ADR-000201's objection
   to a broker: nobody has to start, supervise or stop it.
2. **The hub is a wake-up channel, not a store.** A loopback TCP socket
   (the one IPC primitive the standard library offers on every supported
   platform), line-delimited JSON, a per-start token kept in
   `station.json` (mode 0600). Senders append to the log first, then
   announce; subscribers (`msg.py wait` / `listen`, the web panel's event
   stream, the dashboard) are woken and read the log. A missing or dead hub
   therefore costs promptness, never a message: every waiter falls back to
   polling.
3. **Into a running session through hooks.** Waking a process that waits is
   push; a coding-agent session that is busy does not wait. `msg.py hook`
   answers a hook event in the form agent CLIs with hooks expect, so unread
   messages are injected after the session's next tool call and before it
   stops. Hook configuration is product-specific and lives in
   `docs/process/agent-profiles.md`. The `claim.py` line of ADR-000201
   stays for agents without hooks.
4. **The station owns the panels.** It serves the web control panel in
   process (its event stream woken by the hub) and starts the Qt control
   panel as a child when a display and PySide6 exist. The terminal
   dashboard needs a terminal of the operator's own: its command is
   printed. The addresses are printed to the agent that started the station
   (`claim.py --claim-request`, `msg.py join`) and by `msg.py panels`.
5. **Claiming joins the roster.** `claim.py --claim-request` registers the
   calling agent, so "the first agent" is recognised without it having to
   run a coordination command.
6. **Names.** The first agent on an empty roster is `Cody`; later agents
   draw at random from the free names. Uniqueness ignores case.
7. **Off switch.** `coordination.autostart: false`, or
   `DEVSYS_COORDINATION_AUTOSTART=0` in the environment. The test harness
   sets the latter, and also removes a calling session's `AGENT_ID` /
   `AGENT_OWNER_PID` from test processes.

## Alternatives rejected

- **Unix domain sockets / named pipes:** two code paths (Windows has no
  portable equivalent in the standard library); loopback TCP with a token
  is one.
- **Carrying message content through the hub:** two sources of truth, and a
  lost connection would lose messages.
- **File-system notification (inotify and its relatives):** per-platform
  APIs, none in the standard library.
- **A station that must be started by hand:** the operator asked for the
  opposite; `--stay` and the off switch cover the manual case.
- **Typing into an agent's terminal from outside:** fragile, product
  specific, and indistinguishable from the human's input.

## Consequences

- A local process listens on two loopback ports while agents are present.
  Anyone who can read `.agent/state/runtime/coordination/station.json` can
  announce on the hub (they could already write to the log).
- An agent whose owner process cannot be verified keeps the station alive
  for `idle_minutes` after it was last seen.
- A session without hooks, busy and not waiting, still learns of a message
  only at its next lifecycle command. That is a property of the agent
  product, stated in `docs/process/agent-coordination.md`.
- The Qt panel opens a window on the operator's desktop when the first
  agent starts; `coordination.panels` chooses which panels start.
