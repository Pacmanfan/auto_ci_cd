"""The worker process: select, claim, attempt, validate, conclude.

One worker process per worker slot (its ``worker.lock``), one active request
per worker. Each iteration:

1. **Select.** First the request this worker's agent id already holds in
   ``processing`` (a PARTIAL or interrupted attempt is resumed under the same
   claim); otherwise the framework's queue (``devsys.queue``: unclaimed inbox
   requests by priority, then arrival), skipping excluded types and requests
   whose attempts are exhausted.
2. **Claim** through the framework (``agent_process.claim_request``) with the
   worker's agent id and the worker process as the claim's owner, so the claim
   is verified ALIVE while the worker runs. The claim is atomic: when two
   workers reach for one request, one wins and the other moves on.
3. **Attempt**: a fresh agent session (``agent.AgentSession``) in the project
   checkout, or in the request's own worktree (``workers.isolation``).
4. **Validate** independently (``validation``).
5. **Conclude** from the request's authoritative state: completed, rejected
   or needs-review ends the worker's involvement; otherwise the attempt is
   retried under the same claim until ``max_attempts_per_request``, after
   which the worker escalates the request through the framework
   (``transition_request -> needs-review``, with the reason). The worker keeps
   no ticket status of its own.
"""
from __future__ import annotations

import json
import os
import re
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import agent_process as proc  # noqa: E402
import devsys_config as cfgmod  # noqa: E402
import devsys_platform as plat  # noqa: E402

from workers import agent, gitops, prompt, state, validation  # noqa: E402

NOT_COUNTED = ("INTERRUPTED", "RATE_LIMITED", "LAUNCH_FAILED")
TERMINAL = ("completed", "rejected", "needs-review", "escalated", "released", "lost-claim")


class AlreadyRunning(RuntimeError):
    pass


class LaunchFailed(RuntimeError):
    """The agent command could not be started (not found, not executable):
    nothing ran, so the attempt is not the request's failure."""

    def __init__(self, attempt: dict, error: str):
        super().__init__(error)
        self.attempt, self.error = attempt, error


class Stop(BaseException):
    """SIGTERM / SIGINT to the worker process."""


def _stop(signum, _frame):
    raise Stop(signal.Signals(signum).name)


def settings(root: Path) -> dict:
    return cfgmod.get("workers", root, {}) or {}


# ---------------------------------------------------------------------- requests
def request_info(root: Path, name: str) -> dict | None:
    """What a worker needs to know about a request, read from its files."""
    locs = proc.locate_request(root, name)
    if not locs:
        return None
    st, md = locs[0]
    text = md.read_text(encoding="utf-8", errors="replace")
    side = state.read_json(md.with_suffix(".json")) or {}

    def header(field):
        m = re.search(rf"^\*\*{field}:\*\*\s*(.+)$", text, re.MULTILINE)
        return m.group(1).strip() if m else ""

    first = next((line for line in text.splitlines() if line.strip()), "")
    title = re.sub(r"^#\s*(Request:\s*)?", "", first).strip() or (side.get("original_text") or "")[:120]
    return {"request": name, "request_id": side.get("request_id") or header("Request ID") or md.stem,
            "state": st, "path": str(md), "type": str(side.get("type") or header("Type") or "").upper(),
            "priority": str(side.get("priority") or header("Priority") or "NORMAL").upper(),
            "title": title[:160], "text": text, "created_utc": side.get("created_utc") or ""}


def own_processing(root: Path, agent_id: str) -> list[str]:
    out = []
    for c in proc.all_claims(root):
        if c.get("kind", "request") == "request" and c.get("agent_id") == agent_id:
            locs = proc.locate_request(root, c.get("target", ""))
            if locs and locs[0][0] == "processing":
                out.append(c["target"])
    return sorted(out)


def candidates(root: Path, rec: dict) -> list[tuple[str, bool]]:
    """(request, resume) in the order the worker tries them."""
    import devsys
    s = settings(root)
    out = [(name, True) for name in own_processing(root, rec["agent_id"])]
    excluded = {t.upper() for t in (s.get("selection") or {}).get("exclude_types") or []}
    limit = int((s.get("attempt") or {}).get("max_attempts_per_request", 2))
    for item in devsys.queue(root):
        if item["type"] in excluded or state.counted_attempts(root, item["request_id"]) >= limit:
            continue
        out.append((item["request"], False))
    return out


def claim_next(root: Path, wid: str) -> tuple[str, str] | None:
    """Assign one request to this worker, or None. Serialised across workers
    (``workers-assign``) so a worker's record and its claim change together;
    the claim itself is the framework's atomic claim."""
    with proc.exclusive(root, "workers-assign"):
        rec = state.load(root, wid)
        if rec.get("active"):
            return rec["active"]["request"], "active"
        taken = {w.get("active", {}).get("request") for w in (state.load(root, x) for x in state.slot_ids(root))
                 if w.get("active") and w["id"] != wid}
        for name, resume in candidates(root, rec):
            if name in taken:
                continue
            try:
                _data, how = proc.claim_request(root, name, rec["agent_id"], owner_pid=os.getpid())
            except proc.ProcessError as e:
                if e.code in (proc.CONFLICT, proc.STALE, proc.BAD_STATE, proc.NOT_FOUND, proc.BUSY):
                    continue
                raise
            return name, "resume" if resume else how
    return None


# ---------------------------------------------------------------------- attempts
def previous_attempts(root: Path, request_id: str) -> list[dict]:
    out = []
    for e in state.journal_entries(root, request_id):
        if e.get("event") in ("escalated", "done"):
            out = []
        elif e.get("event") == "attempt":
            a = state.read_json(state.attempt_dir(root, e["worker_id"], e["attempt_id"]) / "attempt.json")
            if a and a.get("status") == "ENDED":
                out.append(a)
    return out


def _work_dir(root: Path, name: str, agent_id: str) -> Path:
    """Where the agent works: the project checkout, or the request's own git
    worktree (``workers.isolation: worktree``, created on first use)."""
    project = cfgmod.project_root(root)
    if settings(root).get("isolation") != "worktree":
        return project
    claim = proc.read_claim(proc.lock_path(root, name)) or {}
    if claim.get("worktree"):
        return Path(claim["worktree"])
    return Path(proc.create_worktree(root, name, agent_id)["path"])


def start_attempt(root: Path, wid: str, name: str) -> dict:
    rec = state.load(root, wid)
    req = request_info(root, name)
    s = settings(root)
    a = s.get("attempt") or {}
    seq = int((rec.get("counters") or {}).get("attempts", 0)) + 1
    aid = f"A{seq:04d}-{req['request_id']}"
    adir = state.attempt_dir(root, wid, aid)
    adir.mkdir(parents=True, exist_ok=True)
    report_path = adir / "report.json"
    cwd = _work_dir(root, name, rec["agent_id"])
    runtime_min = float(a.get("max_runtime_minutes", 180))
    max_attempts = int(a.get("max_attempts_per_request", 2))
    prev = previous_attempts(root, req["request_id"])
    text = prompt.build(root, worker_id=wid, request=req, request_path=req["path"], agent_id=rec["agent_id"],
                        attempt_no=state.counted_attempts(root, req["request_id"]) + 1, max_attempts=max_attempts,
                        prev=prev, report_path=report_path, work_dir=cwd, max_runtime_minutes=runtime_min)
    # The agent's tools act on the system directory of *its* checkout: the
    # request's worktree has its own copy, and a root inherited from the
    # worker (AGENT_PROCESS_ROOT, set by pool.start) would send its records
    # and commits to the main checkout instead.
    env = {"AGENT_PROCESS_ROOT": str(proc.system_dir_in(root, cwd)),
           "AGENT_ID": rec["agent_id"], "AGENT_OWNER_PID": os.getpid(), "DEVSYS_WORKER": wid,
           "DEVSYS_WORKER_REQUEST": req["request_id"], "DEVSYS_WORKER_REQUEST_FILE": name,
           "DEVSYS_WORKER_ATTEMPT": aid, "DEVSYS_WORKER_REPORT": str(report_path),
           "DEVSYS_WORKER_REQUEST_PATH": req["path"],
           "DEVSYS_SCRIPTS": str(prompt.agent_scripts_dir(root, cwd))}
    sess = agent.AgentSession(root, cwd=cwd, out_dir=adir, prompt=text, name=f"{wid} {aid}", env_extra=env)
    attempt = {"attempt_id": aid, "worker_id": wid, "request": name, "request_id": req["request_id"],
               "request_title": req["title"], "agent_id": rec["agent_id"], "status": "RUNNING", "result": None,
               "started_at": proc.now_iso(), "ended_at": None, "deadline_epoch": time.time() + runtime_min * 60,
               "max_runtime_minutes": runtime_min, "cwd": str(cwd), "report_path": str(report_path),
               "git_before": gitops.snapshot(cwd), "session": {"session_id": sess.session_id}}
    state.write_json(adir / "attempt.json", attempt)        # before the launch: recovery can find it
    try:
        attempt["session"] = sess.start()
    except OSError as e:
        # The attempt id is used (its record stays): the next attempt gets a new one.
        state.update(root, wid, counters={"attempts": seq})
        raise LaunchFailed(attempt, f"{sess.cmd[0]}: {e.strerror or e}") from e
    state.write_json(adir / "attempt.json", attempt)
    state.update(root, wid, status="working", status_reason=f"working on {req['request_id']}",
                 active={"request": name, "request_id": req["request_id"], "attempt_id": aid, "cwd": str(cwd),
                         "started_at": attempt["started_at"], "deadline_epoch": attempt["deadline_epoch"],
                         "session": attempt["session"]},
                 counters={"attempts": seq})
    state.event(root, wid, "attempt.started", request_id=req["request_id"], attempt=aid,
                pid=attempt["session"]["pid"], cwd=str(cwd))
    attempt["_popen"] = sess.proc
    return attempt


def wait_attempt(root: Path, wid: str, attempt: dict) -> dict:
    """Block until the session ends; refresh the claim's heartbeat, keep a
    snapshot of the claim (it disappears on release) and honour stop-now."""
    name, agent_id = attempt["request"], attempt["agent_id"]
    adir = state.attempt_dir(root, wid, attempt["attempt_id"])
    hb_every = min(60.0, float(cfgmod.get("claims.heartbeat_interval_seconds", root, 300)))
    last_hb = [0.0]

    def tick(_elapsed):
        if time.time() - last_hb[0] >= hb_every:
            last_hb[0] = time.time()
            claim = proc.read_claim(proc.lock_path(root, name))
            if claim and claim.get("agent_id") == agent_id:
                state.write_json(adir / "claim-snapshot.json", claim)
                try:
                    proc.heartbeat(root, name, agent_id, owner_pid=os.getpid())
                except proc.ProcessError:
                    pass
        return "stop_now" if state.load(root, wid).get("stop_now") else None

    a = settings(root).get("attempt") or {}
    return agent.wait(attempt["session"], popen=attempt.get("_popen"), deadline=attempt["deadline_epoch"],
                      poll_seconds=min(1.0, float(settings(root).get("poll_seconds", 5))),
                      grace_seconds=float(a.get("kill_grace_seconds", 30)), tick=tick)


def _attributed(request_id: str, name: str, commits: list[dict], snaps: list[dict], report: dict | None):
    shas = {c.get("sha") for s in snaps for c in (s or {}).get("commits") or [] if c.get("sha")}
    reported = " ".join(str(c) for c in ((report or {}).get("commits") or []))
    mine, others = [], []
    for c in commits:
        text = f"{c['subject']}\n{c['body']}"
        if c["sha"] in shas or request_id in text or name in text or (reported and c["sha"][:7] in reported):
            mine.append(c)
        else:
            others.append(c)
    return mine, others


def finish_attempt(root: Path, wid: str, attempt: dict, w: dict) -> dict:
    adir = state.attempt_dir(root, wid, attempt["attempt_id"])
    cwd = Path(attempt["cwd"])
    info = agent.parse_transcript(adir / "transcript.jsonl")
    report, source, problems = prompt.parse_report(Path(attempt["report_path"]), info)
    git_after = gitops.snapshot(cwd)
    before = attempt["git_before"]
    snaps = [state.read_json(adir / "claim-snapshot.json")]
    claim_now = proc.read_claim(proc.lock_path(root, attempt["request"]))
    if claim_now:
        snaps.append(claim_now)
        state.write_json(adir / "claim-after.json", claim_now)
    mine, others = _attributed(attempt["request_id"], attempt["request"],
                               gitops.commits_between(cwd, before["head"], git_after["head"]), snaps, report)
    owned_dirty = []
    if claim_now and claim_now.get("agent_id") == attempt["agent_id"]:
        # The framework's own notion: this request's work that is not
        # committed (other requests' rows in shared manifests are theirs).
        owned_dirty = proc.uncommitted_owned_paths(proc.system_dir_in(root, cwd), claim_now,
                                                   include_request_files=False)
    locs = proc.locate_request(root, attempt["request"])
    request_state = locs[0][0] if locs else None
    reset_at = agent.rate_limited(root, info)
    res_ev = info.get("result") or {}
    stale = w.get("stale")
    interruption = None
    if w.get("launch_error"):
        result = "LAUNCH_FAILED"
        reason = f"the agent command could not be started ({w['launch_error']}); not counted"
    elif w.get("interrupted"):
        result, reason, interruption = "INTERRUPTED", "stopped by the operator (stop now)", "operator-stop-now"
    elif reset_at and (report is None or report["status"] != "COMPLETED"):
        result = "RATE_LIMITED"
        reason = f"agent usage limit; resets at {time.strftime('%Y-%m-%d %H:%M', time.localtime(reset_at))}"
    elif w.get("timed_out"):
        result = "TIMEOUT"
        reason = f"no exit within {attempt['max_runtime_minutes']:g} minutes; session and its processes ended"
    elif report is not None:
        result, reason = report["status"], f"agent reported {report['status']}"
    elif not res_ev and stale:
        interruption = "reboot" if "boot" in str(stale.get("reason")) else "unobserved-exit"
        result = "INTERRUPTED"
        reason = f"the session ended while no worker was attached ({stale.get('reason')}); not counted"
    elif not res_ev:
        result = "CRASHED"
        reason = f"session ended without a result event (exit code {w.get('exit_code')})"
    else:
        result, reason = "INVALID_REPORT", "; ".join(problems) or "unusable report"
    if info.get("init") and info["init"].get("cwd") and Path(info["init"]["cwd"]).resolve() != cwd.resolve():
        problems.append(f"session cwd {info['init']['cwd']} is not the attempt's directory {cwd}")
    attempt.pop("_popen", None)
    attempt.update({
        "status": "ENDED", "result": result, "result_reason": reason, "ended_at": proc.now_iso(),
        "exit_code": w.get("exit_code"), "timed_out": bool(w.get("timed_out")),
        "interrupted": bool(w.get("interrupted")), "adopted": bool(w.get("adopted")), "interruption": interruption,
        "recovery": stale, "report": report, "report_source": source, "report_problems": problems,
        "transcript": {"events": info["events"], "bad_lines": info["bad_lines"], "tool_uses": info["tool_uses"],
                       "result": res_ev, "init": info.get("init")},
        "rate_limit_reset_epoch": reset_at, "git_after": {k: v for k, v in git_after.items() if k != "dirty"},
        "git_delta": gitops.attempt_delta(before, git_after), "commits": mine,
        "other_commits": [{"sha": c["sha"], "subject": c["subject"], "files": c.get("files") or []} for c in others],
        "owned_uncommitted": owned_dirty, "request_state": request_state,
        "claim_owner": (claim_now or {}).get("agent_id"),
    })
    state.write_json(adir / "attempt.json", attempt)
    return attempt


def conclude(root: Path, wid: str, attempt: dict, w: dict | None) -> dict:
    """Finish, validate and decide. The outcome is derived from the request's
    authoritative state; the only state change the worker itself makes is an
    escalation through the framework (processing -> needs-review)."""
    if attempt.get("status") != "ENDED":
        attempt = finish_attempt(root, wid, attempt, w or {})
    v = validation.validate(root, wid, attempt)
    attempt["validation"] = v
    state.write_json(state.attempt_dir(root, wid, attempt["attempt_id"]) / "attempt.json", attempt)
    s = settings(root)
    limit = int((s.get("attempt") or {}).get("max_attempts_per_request", 2))
    res, st, name, rid = attempt["result"], attempt["request_state"], attempt["request"], attempt["request_id"]
    counted = res not in NOT_COUNTED and st == "processing"
    state.journal(root, {"event": "attempt", "worker_id": wid, "attempt_id": attempt["attempt_id"],
                         "request_id": rid, "request": name, "result": res, "request_state": st,
                         "counted": counted, "validation": v["overall"]})
    reason = attempt.get("result_reason") or ""
    rep = attempt.get("report") or {}
    if st in ("completed", "rejected"):
        outcome = st
        state.journal(root, {"event": "done", "request_id": rid, "worker_id": wid})
    elif st == "needs-review":
        outcome = "needs-review"
        state.journal(root, {"event": "escalated", "request_id": rid, "worker_id": wid, "by": "agent"})
    elif st != "processing":
        outcome = "released"          # the agent released or requeued the request
    elif res in NOT_COUNTED:
        outcome = "resume"
    else:
        escalate = res in ("BLOCKED", "ESCALATE") or state.counted_attempts(root, rid) >= limit
        if not escalate:
            outcome = "retry"
        else:
            why = (f"worker {wid}: {state.counted_attempts(root, rid)} attempt(s) without completion; last: {res}: "
                   f"{reason}" + (f"; agent: {rep.get('reason_for_status')}" if rep.get("reason_for_status") else ""))
            claim = proc.read_claim(proc.lock_path(root, name))
            if claim and claim.get("agent_id") == attempt["agent_id"]:
                proc.transition_request(root, name, attempt["agent_id"], "needs-review", reason=why[:1000])
                outcome = "escalated"
                state.journal(root, {"event": "escalated", "request_id": rid, "worker_id": wid, "by": "worker",
                                     "reason": why[:500]})
            else:
                outcome = "lost-claim"
    success = outcome in ("completed", "rejected")
    rec = state.load(root, wid)
    c = rec.get("counters") or {}
    counters = {"consecutive_failures": 0 if success else int(c.get("consecutive_failures", 0)) + (1 if counted else 0)}
    if outcome == "completed":
        counters["completed"] = int(c.get("completed", 0)) + 1
    if outcome == "escalated":
        counters["escalated"] = int(c.get("escalated", 0)) + 1
    keep = outcome in ("resume", "retry")
    state.update(root, wid, active=None, status="idle", status_reason=f"last: {rid} {outcome}",
                 last={"request_id": rid, "attempt_id": attempt["attempt_id"], "result": res, "outcome": outcome,
                       "validation": v["overall"], "at": proc.now_iso(), "reason": reason[:300],
                       "claim_kept": keep},
                 counters=counters)
    state.event(root, wid, "attempt.concluded", request_id=rid, attempt=attempt["attempt_id"], result=res,
                outcome=outcome, request_state=st, validation=v["overall"])
    return {"outcome": outcome, "result": res, "request_state": st, "validation": v["overall"],
            "rate_limit_reset_epoch": attempt.get("rate_limit_reset_epoch")}


# ---------------------------------------------------------------------- recovery
def recover_active(root: Path, wid: str, *, force: bool = False, kill_alive: bool = False) -> str | None:
    """Settle the worker's recorded active attempt. A record that says an
    attempt runs is never trusted: the session is verified first.

    * DEAD: concluded (INTERRUPTED without a result; from its report if it
      finished while nobody was attached); the claim stays with the worker.
    * ALIVE: re-adopted when called from the worker itself (waited for, then
      concluded); from outside, left alone unless ``kill_alive`` (ends the
      verified session, then concludes it INTERRUPTED).
    * UNKNOWN: left alone unless ``force``.
    Returns None when settled, else why it is not."""
    rec = state.load(root, wid)
    act = rec.get("active")
    if not act:
        return None
    adir = state.attempt_dir(root, wid, act["attempt_id"])
    attempt = state.read_json(adir / "attempt.json")
    if attempt is None:
        state.update(root, wid, active=None, status_reason="active attempt had no record; cleared")
        return None
    if attempt.get("status") == "ENDED":
        conclude(root, wid, attempt, None)
        return None
    verdict, why = agent.verify(attempt.get("session"))
    if verdict == plat.ALIVE:
        if not kill_alive and os.getpid() == (rec.get("process") or {}).get("pid"):
            state.event(root, wid, "attempt.readopted", attempt=act["attempt_id"], reason=why)
            w = wait_attempt(root, wid, attempt)
            w["adopted"] = True
            conclude(root, wid, attempt, w)
            return None
        if not kill_alive:
            return f"{act['attempt_id']}: its session is still running ({why})"
        plat.kill_tree(attempt["session"]["pid"], float((settings(root).get("attempt") or {}).get("kill_grace_seconds", 30)))
        conclude(root, wid, attempt, {"exit_code": None, "interrupted": True})
        return None
    if verdict == plat.UNKNOWN and not force:
        return f"{act['attempt_id']}: cannot verify whether its session runs ({why}); recover --force once you know"
    conclude(root, wid, attempt, {"exit_code": None, "stale": {"verdict": verdict, "reason": why}})
    return None


# ---------------------------------------------------------------------- the worker process
def run(root: Path, wid: str, *, once: bool = False, exit_when_idle: bool = False) -> str:
    """The worker process. Returns why it stopped."""
    d = state.worker_dir(root, wid)
    d.mkdir(parents=True, exist_ok=True)
    fd = os.open(d / "worker.lock", os.O_RDWR | os.O_CREAT, 0o644)
    try:
        plat.lock(fd)
    except plat.WouldBlock:
        os.close(fd)
        raise AlreadyRunning(f"{wid} already has a running worker process")
    rec = state.ensure(root, wid)
    if not rec.get("enabled", True):
        os.close(fd)
        raise RuntimeError(f"{wid} is disabled")
    old = {s: signal.signal(s, _stop) for s in (signal.SIGTERM, signal.SIGINT)}
    s = settings(root)
    a = s.get("attempt") or {}
    reason = "stopped"
    state.update(root, wid, process={**plat.session_identity(os.getpid()), "started_at": proc.now_iso()},
                 desired="running", stop_now=False, status="idle", status_reason="started")
    state.event(root, wid, "worker.started", pid=os.getpid())
    current = None
    try:
        blocked = recover_active(root, wid)
        if blocked:
            reason = f"not started: {blocked}"
            return reason
        while True:
            rec = state.load(root, wid)
            if rec.get("desired") != "running" or not rec.get("enabled", True):
                reason = "operator stop" if rec.get("enabled", True) else "disabled"
                break
            if int((rec.get("counters") or {}).get("consecutive_failures", 0)) >= int(a.get("max_consecutive_failures", 3)):
                reason = "circuit breaker: consecutive attempts without success"
                state.update(root, wid, desired="stopped")
                break
            pick = claim_next(root, wid)
            if pick is None:
                if once or exit_when_idle:
                    reason = "no eligible request"
                    break
                state.update(root, wid, status="idle", status_reason="waiting for a request")
                time.sleep(float(s.get("poll_seconds", 5)))
                continue
            name, how = pick
            state.event(root, wid, "request.assigned", request=name, how=how)
            try:
                current = start_attempt(root, wid, name)
            except LaunchFailed as e:
                # Retrying cannot help until the environment changes: record
                # the attempt (not counted; the claim is kept for the next
                # start) and stop with the reason.
                conclude(root, wid, e.attempt, {"exit_code": None, "launch_error": e.error})
                reason = f"agent could not be started: {e.error}"
                state.update(root, wid, desired="stopped")
                break
            w = wait_attempt(root, wid, current)
            out = conclude(root, wid, current, w)
            current = None
            if out["outcome"] == "resume" and out.get("rate_limit_reset_epoch"):
                wait_s = max(0.0, out["rate_limit_reset_epoch"] - time.time())
                if wait_s > float(a.get("max_rate_limit_wait_minutes", 720)) * 60:
                    reason = "agent usage limit resets too far ahead"
                    state.update(root, wid, desired="stopped")
                    break
                end = time.time() + wait_s
                while time.time() < end and state.load(root, wid).get("desired") == "running":
                    time.sleep(min(5.0, end - time.time()) if end > time.time() else 0)
            if state.load(root, wid).get("stop_now"):
                reason = "operator stop now"
                break
            if once:
                reason = f"one attempt done ({out['outcome']})"
                break
    except Stop as e:
        reason = f"worker process received {e}"
        if current is not None:
            plat.kill_tree(current["session"]["pid"], float(a.get("kill_grace_seconds", 30)))
            conclude(root, wid, current, {"exit_code": None, "interrupted": True})
    finally:
        for sig, h in old.items():
            signal.signal(sig, h)
        rec = state.load(root, wid)
        state.update(root, wid, process=None, stop_now=False,
                     status="stopped", status_reason=reason,
                     desired="stopped" if rec.get("desired") == "running" and (once or exit_when_idle) else rec.get("desired"))
        state.event(root, wid, "worker.stopped", reason=reason)
        plat.unlock(fd)
        os.close(fd)
    return reason
