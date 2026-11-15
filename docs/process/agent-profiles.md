# Agent Profiles

**Requirement:** WORKER-012 (vendor-neutral system)
**Code:** `scripts/workers/agent.py` (the generic adapter)

The Worker Orchestrator does not know any particular coding agent. A worker
runs whatever command `devsys.json` configures as `workers.agent.command`,
provided the command:

- reads the prompt on **stdin**;
- writes a **JSON-lines** event stream to **stdout**, with:
  - one final `{"type": "result", ...}` event (read: `subtype`, `is_error`,
    `num_turns`, `duration_ms`, `total_cost_usd`, `result`, `stop_reason`);
  - `{"type": "assistant", "message": {"content": [...]}}` events (`text` and
    `tool_use` blocks);
  - optionally `{"type": "system", "subtype": "init", "cwd": ...}` and
    `{"type": "rate_limit_event", "rate_limit_info": {"status", "resetsAt"}}`;
- works in its current directory (the project checkout or the request's
  worktree), and uses the variables the worker sets
  (`docs/process/worker-orchestrator.md` §4);
- writes its completion report to `$DEVSYS_WORKER_REPORT`, or ends its final
  message with it as a fenced JSON block.

The placeholders `{session_id}` (a UUID), `{name}` (worker and attempt),
`{attempt_dir}` (the attempt's record directory), `{scripts}` (this
system's scripts directory) and `{python}` (the interpreter running the
worker; portable where `python3` is not a command) are substituted in every
argument. On Windows the command's first word is resolved as the shell
would, so a CLI installed as a `.cmd` shim starts under its bare name
(`docs/process/portability.md`). An agent that
speaks another format is wrapped in a small adapter script that translates
to this protocol (`scripts/workers/profiles/`).

## Several agents in one fleet (WORKER-006, ADR-000201)

`workers.agent` is the **default** profile. Further profiles are named under
`workers.profiles`, each overriding the default key by key, and
`workers.assign` gives a worker slot its profile:

```json
"workers": {
  "max_workers": 3,
  "agent":    {"command": ["<default agent>", "..."]},
  "profiles": {"second": {"command": ["<another agent>", "..."], "env_scrub": []}},
  "assign":   {"worker-03": "second"}
}
```

`devsys.py workers status`, the panels and the coordination roster show each
worker's profile. A slot assigned to a profile that is not defined does not
start (exit 10); `devsys.py doctor` reports it.

## Documented exception: vendor-specific profiles

This page, together with the adapters directory
`scripts/workers/profiles/`, is the **only** place in the development system
that names an agent vendor or product (WORKER-012; the identity test excludes
exactly these two, and the request records, which quote people). An external command line genuinely needs a product's executable name,
flags and environment variables, so they live here as configuration to copy
into `devsys.json`. Nothing in the code depends on them.

### Claude Code (`claude` CLI)

The profile the former external orchestrator used in live runs:

```json
"workers": {
  "agent": {
    "command": ["claude", "-p", "--output-format", "stream-json", "--verbose",
                "--permission-mode", "auto", "--permission-prompts", "none",
                "--session-id", "{session_id}", "--name", "{name}", "--add-dir", "{attempt_dir}"],
    "env_scrub": ["CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ID", "CLAUDE_PID",
                  "CLAUDE_EFFORT", "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN",
                  "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_EXECPATH", "AI_AGENT"],
    "rate_limit_patterns": ["usage limit", "rate limit", "limit reached"]
  }
},
"tests": {"no_test_patterns": ["docs/*", "*.md", "requests/*", "work/*", "evidence/*", ".agent/*",
                               "LICENSE", "readme.md", ".gitignore", ".vscode/*", ".claude/*",
                               "*/__pycache__/*", "*.pyc"]}
```

- `env_scrub` removes the variables that tie a process to a parent agent
  session. A worker started from inside an interactive session therefore
  launches normal top-level sessions.
- `--add-dir {attempt_dir}` lets the session write its report outside the
  project.
- `--session-id {session_id}` lets `claude --resume <id>` in the project
  directory reopen a worker's conversation for inspection. The session id is
  in the attempt's `launch.json` and in `workers peek`.
- The agent's project settings directory (`.claude/`) is not code. The
  `no_test_patterns` line above extends the defaults so that changes there
  select no tests.
- The flags are those the former orchestrator used. Check them against the
  installed CLI's `--help` before an unattended run.

### opencode (`opencode` CLI), major versions 1 and 2

`opencode run --format json` writes its own event stream and no final result
event, so the profile wraps it in the adapter
`scripts/workers/profiles/event_stream_adapter.py`, which hands the prompt to
the CLI on stdin, translates text and tool parts into `assistant` events and
writes the `result` event when the CLI exits:

```json
"workers": {
  "profiles": {
    "opencode": {
      "command": ["{python}", "{scripts}/workers/profiles/event_stream_adapter.py", "--session-id", "{session_id}",
                  "--", "opencode", "run", "--format", "json", "--auto", "--title", "{name}"],
      "env_scrub": [],
      "rate_limit_patterns": ["usage limit", "rate limit", "limit reached", "quota"]
    }
  },
  "assign": {"worker-02": "opencode"}
}
```

- `--auto` approves permissions that are not explicitly denied, which an
  unattended session needs; restrict what it may do in the project's
  `opencode.json` permissions. Add `-m <provider>/<model>` to pin a model.
- The session's own id (`ses_...`) is in the transcript's events;
  `opencode run --session <id>` continues it for inspection.
- **Major version 1** (checked against 1.18): events are
  `{"type": "text" | "tool_use" | "step_start" | "step_finish" | "error",
  "sessionID", "part"}`; a prompt on stdin is accepted as the message.
- **Major version 2** was not released when this profile was written. The
  adapter also understands the server's bus-event shape
  (`message.part.updated` / `session.error` with `properties`) and decides
  per event (`--dialect auto`), so the same profile is intended to serve
  both. That mapping is **unverified**: with 2.x installed, run one request
  with `--raw-log {attempt_dir}/raw.jsonl` added before `--`, compare the raw
  stream with the transcript, and check `opencode run --help` for renamed
  flags. `--prompt arg` passes the prompt as the last argument if a version
  no longer reads stdin; `--dialect 1` or `2` pins the mapping.
- To run two versions side by side, define two profiles whose commands name
  the two executables (for example `opencode` and a second installation's
  path) and assign them to different worker slots.

## Messages into a running session: hooks (COORD-012)

An agent CLI with hooks can have unread coordination messages injected into
its running session by `scripts/msg.py hook`, which reads the hook event on
stdin and prints the answer the CLI expects. The command needs to know whose
session it is: start the CLI with `AGENT_ID` already in its environment, or
let it find the roster entry whose owner process (`AGENT_OWNER_PID`) started
the hook.

### Claude Code

In the project's `.claude/settings.json` (checked against the hooks
reference; `PostToolUse` context must be the JSON form, which `msg.py hook`
prints):

```json
{
  "hooks": {
    "SessionStart":     [{"hooks": [{"type": "command", "command": "python3 \"$CLAUDE_PROJECT_DIR/scripts/msg.py\" hook", "timeout": 10}]}],
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "python3 \"$CLAUDE_PROJECT_DIR/scripts/msg.py\" hook", "timeout": 10}]}],
    "PostToolUse":      [{"matcher": "*", "hooks": [{"type": "command", "command": "python3 \"$CLAUDE_PROJECT_DIR/scripts/msg.py\" hook", "timeout": 10}]}],
    "Stop":             [{"hooks": [{"type": "command", "command": "python3 \"$CLAUDE_PROJECT_DIR/scripts/msg.py\" hook", "timeout": 10}]}]
  }
}
```

- After every tool call, at session start and on every prompt, unread
  messages are added to the session's context. Before the session stops,
  unread messages block the stop once and are handed over as the reason.
  Messages are delivered once, so the stop hook cannot loop.
- Hooks also run in headless (`-p`) sessions, so workers get them too. A
  hook process inherits the CLI's environment: a worker's agent has
  `AGENT_ID`; an interactive session that created its id in a shell command
  is found through `AGENT_OWNER_PID` instead.
- This file is not shipped with the system (a settings directory named
  after a product would break the identity rule, WORKER-012): add it to the
  project that uses the system. It has not been exercised end to end with a
  live session here; the hook's output forms are unit-tested
  (`test_agent_coordination.Hooks`).
- On Windows write the command with `python` and `%CLAUDE_PROJECT_DIR%`.

### opencode

No hook configuration is documented here: its plugin interface was not
verified. Its sessions receive messages when they wait (`msg.py wait`), at
their next `claim.py` command, or when they read their inbox.
