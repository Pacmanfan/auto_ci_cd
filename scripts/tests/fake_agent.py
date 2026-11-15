#!/usr/bin/env python3
"""A stand-in coding agent for the Worker Orchestrator's tests.

It speaks the generic agent protocol of scripts/workers/agent.py (prompt on
stdin, JSON-lines events on stdout, a final ``result`` event, the report
written to $DEVSYS_WORKER_REPORT) and, as a worker's agent, performs the
request lifecycle with the development system's real tools ($DEVSYS_SCRIPTS:
claim.py, work.py, evidence.py, completion_gate.py, commit_request.py), so the
orchestrator is tested against genuine claims, gate records, commits and
state moves.

The behaviour is chosen by a marker in the request text: [fake:<mode>]

  complete         the full gated lifecycle on a documentation page -> COMPLETED
  complete_nogate  write and commit, then try to complete WITHOUT a gate record
                   (refused by the framework) and report COMPLETED anyway
  partial          commit progress, keep the claim -> PARTIAL
  partial_then_complete   PARTIAL on attempt 1, the full lifecycle later
  fail             change nothing -> FAILED
  escalate         park the request in needs-review with the question -> ESCALATE
  timeout          sleep until killed
  crash            exit 3 without a result event
  slow             like complete, after sleeping $FAKE_SLOW_SECONDS (default 3)

$FAKE_PAUSE_AFTER_WRITE (seconds) pauses complete after the page is written
and before anything is committed, so a test can look at the uncommitted work.

Every invocation appends {argv, cwd, env subset} to $FAKE_AGENT_LOG.
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

prompt = sys.stdin.read()
cwd = os.getcwd()
env = os.environ
session = env.get("DEVSYS_WORKER_SESSION", "no-session")
if env.get("FAKE_AGENT_LOG"):
    with open(env["FAKE_AGENT_LOG"], "a") as f:
        f.write(json.dumps({"argv": sys.argv[1:], "cwd": cwd, "env": {k: env.get(k) for k in (
            "AGENT_ID", "AGENT_OWNER_PID", "DEVSYS_WORKER", "DEVSYS_WORKER_REQUEST", "DEVSYS_WORKER_SESSION",
            "DEVSYS_WORKER_ATTEMPT")}}) + "\n")


def emit(obj):
    obj.setdefault("session_id", session)
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def result(text, is_error=False):
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})
    emit({"type": "result", "subtype": "error" if is_error else "success", "is_error": is_error,
          "num_turns": 3, "duration_ms": 1000, "total_cost_usd": 0.0, "result": text})


emit({"type": "system", "subtype": "init", "cwd": cwd, "model": "fake"})

rid, rfile, agent = env["DEVSYS_WORKER_REQUEST"], env["DEVSYS_WORKER_REQUEST_FILE"], env["AGENT_ID"]
scripts = Path(env["DEVSYS_SCRIPTS"])
report_path = Path(env["DEVSYS_WORKER_REPORT"])
root = Path(env.get("AGENT_PROCESS_ROOT") or cwd)
text = Path(env["DEVSYS_WORKER_REQUEST_PATH"]).read_text() if Path(env.get("DEVSYS_WORKER_REQUEST_PATH", "")).is_file() \
    else ""
m = re.search(r"\[fake:(\w+)\]", text)
mode = m.group(1) if m else "complete"
attempt_no = int(re.search(r"attempt (\d+) of", prompt).group(1)) if re.search(r"attempt (\d+) of", prompt) else 1
if mode == "partial_then_complete":
    mode = "partial" if attempt_no == 1 else "complete"
if mode == "slow":
    time.sleep(float(env.get("FAKE_SLOW_SECONDS", "3")))
    mode = "complete"


def tool(script, *args):
    r = subprocess.run([sys.executable, str(scripts / script), *args], cwd=cwd, capture_output=True, text=True)
    with open(report_path.parent / "fake-agent.log", "a") as f:
        f.write(f"$ {script} {' '.join(args)}\nrc={r.returncode}\n{r.stdout}{r.stderr}\n")
    return r.returncode, r.stdout + r.stderr


def request_path():
    for st in ("processing", "completed", "needs-review", "rejected", "inbox"):
        if (root / "requests" / st / rfile).exists():
            return f"requests/{st}/{rfile}"
    return rfile


def commit(msg):
    rc, out = tool("commit_request.py", "--request", request_path(), "-m", msg)
    m2 = re.search(r"COMMITTED ([0-9a-f]{40})", out)
    return m2.group(1) if m2 else None


def report(status, **kw):
    rep = {"request_id": rid, "status": status, "summary": f"fake {mode}", "implementation": "fake",
           "files_changed": kw.get("files", []), "commits": kw.get("commits", []),
           "tests_run": [], "validation": kw.get("validation", "fake"), "known_issues": [],
           "remaining_work": kw.get("remaining", []), "reason_for_status": kw.get("reason", mode),
           "work_ids": kw.get("work_ids", []), "request_state": kw.get("state", "")}
    report_path.write_text(json.dumps(rep, indent=2))
    result("Done.\n\n```json\n" + json.dumps(rep) + "\n```")


page = f"docs/fake/{rid}.md"


def write_page(content):
    tool("claim.py", "--own", request_path(), "--paths", page)
    (Path(cwd) / "docs" / "fake").mkdir(parents=True, exist_ok=True)
    (Path(cwd) / page).write_text(content)


if mode == "complete":
    write_page(f"page for {rid}, attempt {attempt_no}\n")
    time.sleep(float(env.get("FAKE_PAUSE_AFTER_WRITE", "0")))
    rc, out = tool("work.py", "create", "--request", request_path(), "--title", f"page for {rid}",
                   "--requirements", "REQ-DEMO-001")
    wid = (re.search(r"WORK-ID (WORK-\d{6})", out) or [None, None])[1]
    tool("evidence.py", "create", "--work", wid, "--status", "pass", "--command", "fake documentation check",
         "--result", "page written")
    tool("completion_gate.py", "--work", wid, "--record")
    tool("work.py", "transition", wid, "--to", "completed")
    tool("claim.py", "--complete-request", request_path())
    sha = commit(f"fake: complete {rid}")
    tool("claim.py", "--release-request", request_path())
    report("COMPLETED", files=[page], commits=[f"{sha} fake: complete {rid}"] if sha else [], state="completed",
           work_ids=[wid] if wid else [])
elif mode == "complete_nogate":
    write_page(f"ungated page for {rid}\n")
    rc, out = tool("claim.py", "--complete-request", request_path())       # refused: no gate record
    sha = commit(f"fake: ungated page for {rid}")
    report("COMPLETED", files=[page], commits=[f"{sha} ungated"] if sha else [], state="completed",
           validation=f"complete-request exit {rc}")
elif mode == "partial":
    write_page(f"partial page for {rid}\n")
    sha = commit(f"fake: progress on {rid}")
    report("PARTIAL", files=[page], commits=[f"{sha} progress"] if sha else [], remaining=["finish the page"],
           state="processing")
elif mode == "fail":
    report("FAILED", reason="fake failure")
elif mode == "escalate":
    tool("claim.py", "--needs-review-request", request_path(), "--reason", "Which of two conflicting requirements wins?")
    commit(f"fake: escalate {rid}")
    report("ESCALATE", reason="Which of two conflicting requirements wins?", state="needs-review")
elif mode == "timeout":
    time.sleep(3600)
elif mode == "crash":
    sys.stderr.write("fake crash\n")
    sys.exit(3)
else:
    report("FAILED", reason=f"unknown fake mode {mode}")
