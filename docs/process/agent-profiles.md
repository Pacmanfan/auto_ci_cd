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

The placeholders `{session_id}` (a UUID), `{name}` (worker and attempt) and
`{attempt_dir}` (the attempt's record directory) are substituted in every
argument. An agent that speaks another format is wrapped in a small adapter
script that translates to this protocol.

## Documented exception: vendor-specific profiles

This page is the **only** place in the development system that names an
agent vendor or product (WORKER-012; the identity test excludes exactly this
file). An external command line genuinely needs a product's executable name,
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
