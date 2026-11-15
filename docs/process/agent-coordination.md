# Agent Coordination

**Requirements:** `docs/requirements/agent_coordination.md` COORD-001..012
**Decision:** ADR-000201, ADR-000202
**Code:** `scripts/agent_messages.py` (roster and bus), `scripts/msg.py` (CLI), `scripts/workers/overview.py` (snapshot and operator actions), `tools/panel/web_panel.py` + `web_panel.html` (web control panel), `tools/panel/dash.py` (terminal dashboard), `tools/panel/station.py` (the station: push hub and panel supervisor)

Agents working in this repository can see each other and talk, and the
operator can watch and steer from a browser or a terminal. Claims, path
registrations and the completion gate are unchanged and stay the only
authority: **a message informs, it never grants anything.**

## 0. What happens by itself

- **The first agent starts everything.** When an agent claims a request
  (`claim.py --claim-request`) or runs `msg.py join`, it joins the roster.
  If it is the first, the **station** starts in the background and the
  agent is shown where things are:

  ```text
  AGENT Cody (agent-host-20261115...)
  PANEL web        http://127.0.0.1:8765/
  PANEL control    window open (pid 12345)
  PANEL dashboard  python3 tools/panel/dash.py   (run it in a terminal of your own)
  HUB              127.0.0.1:40123 (push delivery; pid 12300)
  ```

  `python3 scripts/msg.py panels` shows this again at any time.
- **The first agent is Cody.** Every later agent gets a name drawn at
  random (Amber, Birch, ... Zephyr), or the one it asks for.
- **Messages are pushed.** A sender writes the message to the log and
  announces it on the station's hub (a loopback socket). `msg.py wait` and
  `listen`, the web panel and the dashboard are woken at once. Without a
  station they poll; nothing is lost either way.
- **The last agent closes everything.** About 20 seconds after no agent is
  present any more (its session process ended), the station closes the
  panels and exits. `python3 tools/panel/station.py --stay` keeps one
  running by hand; `"coordination": {"autostart": false}` in `devsys.json`
  or `DEVSYS_COORDINATION_AUTOSTART=0` switches the automatic start off.

How far "push" reaches depends on the agent:

| The agent is ... | A message reaches it ... |
|---|---|
| waiting (`msg.py wait`, `msg.py listen`) | at once, through the hub |
| busy, and its CLI has hooks configured (`msg.py hook`, `docs/process/agent-profiles.md`) | after its current tool call, and before it stops |
| busy, without hooks | at its next `claim.py` command (the `MESSAGES:` line), or when it next reads its inbox |

## 1. For agents: `scripts/msg.py`

The agent id is `$AGENT_ID` (`scripts/claim.py --new-agent-id`), as for claims.

```bash
python3 scripts/msg.py join [--name scout] [--role lead] [--profile P]   # optional: send / inbox register you
python3 scripts/msg.py who                                                # names, liveness, request, worktree
python3 scripts/msg.py send --to birch --kind path-request --request REQ-... "I need src/x.c for ..."
python3 scripts/msg.py send --to all --kind status "the manifest format changed, re-read it"
python3 scripts/msg.py send --to operator --kind question "A or B?"
python3 scripts/msg.py reply MSG-1a2b3c4d5e "take it, I am done"
python3 scripts/msg.py inbox [--peek]                                     # your unread messages
python3 scripts/msg.py wait --timeout 300                                 # pushed; exit 4 when nothing arrived
python3 scripts/msg.py listen                                             # print messages as they arrive
python3 scripts/msg.py panels                                             # where the panels and the hub are
python3 scripts/msg.py hook                                               # for the agent CLI's hooks (stdin: the hook event)
python3 scripts/msg.py log [--last N] [--request R] [--mine] [--follow]   # everyone's conversation
python3 scripts/msg.py leave | prune
```

- **Names.** The first agent is Cody; each later agent gets a random
  friendly name on first contact (or asks for one with `--name`). Workers'
  agents are registered by their worker.
- **Recipients.** An agent's name or id, a role, `operator`, or `all`.
- **Kinds.** `note`, `question`, `answer`, `status`, `handoff`,
  `path-request`, `alert`.
- **You have mail.** After any `claim.py` command the line
  `MESSAGES: N unread for <name>` appears on stderr while messages wait.
- **Roles.** `--role lead` makes an agent reachable as `lead`. Nothing else
  changes: a lead is a peer that others choose to address.

When blocked by another agent's path or request: send its owner a
`path-request`, `wait`, and without an answer report BLOCKED as before. Never
recover or take over a claim because a message said so.

## 2. For the operator

### Web control panel

Started by the station (the URL is printed to the first agent and by
`msg.py panels`). By hand:

```bash
python3 tools/panel/web_panel.py [--port 8765] [--open] [--root DIR]
```

Open the printed URL. The page updates live and shows:

- **Workers:** status, agent profile, active request; Start, Stop, Stop now,
  Recover, Enable / Disable, Details (attempt, transcript tail, events).
- **Agents:** name, verified liveness, profile, roles, claim and step,
  branch and worktree; Message and Filter.
- **Agent messages:** the whole conversation, filterable by agent and kind;
  write as `operator` to an agent, a role or `all`, or reply to a message.
- **Requests:** queued (priority editable), working, finished; submit a new
  request (it goes through `create_request.py`, like any intake).
- **Worktrees:** every git worktree, its branch and the requests claimed
  there.

The panel listens on `127.0.0.1` only, and its API needs the token embedded
in the page. `--host 0.0.0.0` exposes control of the agents to the network:
use it only behind something you trust, and open the URL with its
`?token=...`. Stopping the panel does not stop the workers.

### Terminal dashboard

```bash
python3 tools/panel/dash.py [--interval 2] [--root DIR]    # live
python3 tools/panel/dash.py --once [--width 120] [--json]   # one frame, for pipes and scripts
python3 tools/panel/dash.py --plain                         # live without curses
```

Where `curses` is missing (Windows / PowerShell) the plain view starts by
itself, with the same keys; on other systems `--plain` only watches. For
PowerShell usage see `docs/process/portability.md` "Windows and PowerShell".

Keys: `Up`/`Down` (or `j`/`k`) select a worker; `s` start, `x` stop, `X` stop
now, `r` recover, `e` enable / disable, `a` start all, `z` stop all; `m`
message, `n` new request, `p` priority of a queued request; `PgUp`/`PgDn`
scroll the conversation; `q` quit.

### Without a panel

`python3 scripts/msg.py --as-operator send --to all "..."` writes as the
operator; `python3 scripts/msg.py log --follow` tails the conversation.

## 3. Git worktrees

The roster and the log are in the **main** worktree's
`.agent/state/runtime/coordination/` (untracked), whatever worktree a
command runs in. With `workers.isolation: worktree`
(`docs/process/worker-orchestrator.md` §7), or `claim.py --create-worktree`,
an agent in a request's worktree therefore shares one roster and one
conversation with everyone else, and the panels show its branch and
worktree.

## 4. State

```text
.agent/state/runtime/coordination/
  agents/<agent id>.json     roster entry: name, profile, roles, worker, owner process, cwd, worktree, branch, read_seq
  messages.jsonl             append-only; seq strictly increasing
  station.json               the running station: process, hub address and token (mode 0600), panel addresses
  station.lock               held by the station (one per clone)
  station.log, control-panel.log
```

It is runtime state, not evidence: decisions that must last go into the work
record. `msg.py prune` removes the entries of sessions verified DEAD; the log
is not rotated.

## 5. Mixed fleets

Which agent product a worker runs is its **profile**
(`workers.profiles` / `workers.assign`, `docs/process/agent-profiles.md`).
The panels show each worker's profile; the roster shows each agent's.
