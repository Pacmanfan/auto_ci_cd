# <PROJECT> Development-Control System

A development-control system for autonomous coding agents. It turns plain
requests into traceable, verified changes and lets several agents work in one
repository without stepping on each other:

* **Requests** — every piece of work is a persistent record under `requests/`
  (inbox, processing, completed, needs-review, rejected).
* **Claims** — an agent claims one request atomically and registers every
  path before editing it; only its own changes are committed.
* **Gated completion** — a request completes only with a requirement, tests,
  evidence, documentation and a PASS record of the completion gate.
* **Workers** — unattended agent sessions that take requests from the queue,
  one active request per worker.
* **Agent coordination** — a roster of agents with friendly names, messages
  between agents and the operator, and live panels.

This file is an orientation only. `docs/README.md` is the canonical
documentation entry point.

## Getting started

Python 3.9 or later and git are required; commands run from the repository root.

```bash
python3 scripts/devsys.py doctor      # environment, configuration, what is not configured yet
python3 scripts/devsys.py status      # workers, claims, inbox, processing, work by state
```

Submit a request (one statement = one request; it lands in `requests/inbox/`):

```bash
python3 scripts/create_request.py --source chat --text "what you want" [--priority HIGH]
python3 scripts/devsys.py queue       # what the workers will take, in order
```

A Markdown file placed in `requests/inbox/` works too (`requests/README.md`).

Start the workers (they need an agent command configured in `devsys.json`,
see `docs/process/worker-orchestrator.md` and `docs/process/agent-profiles.md`):

```bash
python3 scripts/devsys.py workers status        # workers, liveness, active requests, queue
python3 scripts/devsys.py workers start         # all enabled workers; or name them: worker-01
python3 scripts/devsys.py workers stop [--now]
python3 scripts/devsys.py workers peek worker-01
```

An agent working interactively follows `AGENTS.md` "Request Processing"
(`python3 scripts/devsys.py next --claim` claims the next request).

## Panels

The first agent that joins the roster starts the station, which opens the
panels; they close after the last agent is gone.

```bash
python3 scripts/msg.py panels                   # where the panels and the push hub are right now
python3 tools/panel/web_panel.py --open         # web control panel (loopback, http://127.0.0.1:8765/ by default)
python3 tools/panel/dash.py                     # terminal dashboard (--plain without curses, --once for one frame)
tools/worker-panel                              # Qt control panel (needs PySide6)
python3 scripts/msg.py who                      # the agents, what each holds and where it works
```

## Where to read more

* `docs/README.md` — the documentation map: requirements, decisions, process,
  verification, work and evidence.
* `AGENTS.md` — the operating rules every agent follows.
* `docs/process/agent-coordination.md` — the roster, messages, the station
  and the panels.
* `CI_CD.md` — technical map of the system as implemented.
