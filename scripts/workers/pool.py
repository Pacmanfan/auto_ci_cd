"""The Worker Orchestrator's API: what the CLI (``devsys.py workers``) and the
control panel (tools/panel/) call. Nothing here decides request state: the
queue, the working set and the completed list are read from the framework;
worker records only say which worker executes which request.

    status(root)                 everything a person or agent needs to see
    start(root, wid)             spawn the worker process (detached)
    stop(root, wid, now=False)   finish the current attempt, then stop; now:
                                 end the running session at once (INTERRUPTED)
    enable / disable(root, wid)  a disabled worker is never started
    recover(root, wid, force)    settle a stopped worker's recorded attempt
    peek(root, wid)              details: active request, liveness, events,
                                 transcript tail, last report and validation
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import agent_process as proc  # noqa: E402
import devsys_config as cfgmod  # noqa: E402
import devsys_platform as plat  # noqa: E402

from workers import agent, runner, state  # noqa: E402

SCRIPTS_DIR = Path(__file__).resolve().parent.parent


class WorkerError(proc.ProcessError):
    pass


def _check(root: Path, wid: str) -> None:
    if wid not in state.slot_ids(root):
        raise WorkerError(proc.NOT_FOUND, f"no worker {wid} (workers.max_workers = "
                                          f"{cfgmod.get('workers.max_workers', root, 0)})")


def _command(root: Path, wid: str) -> tuple[list, str | None]:
    """(agent argv template of the worker's profile, why it cannot run | None)."""
    try:
        command = list(agent.agent_settings(root, wid).get("command") or [])
    except agent.NotConfigured as e:
        return [], str(e)
    profile = agent.profile_name(root, wid)
    if not command:
        return [], (f"the agent command of profile {profile!r} is empty (workers.agent.command is empty or "
                    "workers.profiles): no agent to run (docs/process/agent-profiles.md)")
    exe = str(command[0]).format(scripts=str(SCRIPTS_DIR), session_id="", name="", attempt_dir="",
                                python=sys.executable)
    if not shutil.which(exe):
        # The worker inherits this PATH; it could claim a request and then not
        # launch its agent.
        return command, (f"the agent command {exe!r} (profile {profile!r}) is not an executable on PATH "
                         f"({os.environ.get('PATH', '')})")
    return command, None


def process_liveness(rec: dict) -> tuple[str, str]:
    if not rec.get("process"):
        return "NONE", "no worker process recorded"
    return plat.verify_session(rec["process"])


def is_running(rec: dict) -> bool:
    return process_liveness(rec)[0] == plat.ALIVE


def _age(iso: str | None) -> float | None:
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds()
    except (TypeError, ValueError):
        return None


def worker_view(root: Path, wid: str) -> dict:
    rec = state.load(root, wid)
    verdict, why = process_liveness(rec)
    view = {k: rec.get(k) for k in ("id", "enabled", "desired", "status", "status_reason", "agent_id", "counters",
                                    "last")}
    view["profile"] = agent.profile_name(root, wid)
    view["configured"] = _command(root, wid)[1] is None
    view["process"] = {"verdict": verdict, "reason": why, "pid": (rec.get("process") or {}).get("pid")}
    if rec.get("status") in ("idle", "working", "stopping") and verdict != plat.ALIVE:
        view["status"] = "stopped"
        view["status_reason"] = f"worker process not running ({why}); recover to settle it"
    act = rec.get("active")
    if act:
        sv, swhy = agent.verify(act.get("session"))
        req = runner.request_info(root, act["request"]) or {}
        view["active"] = {**{k: act.get(k) for k in ("request", "request_id", "attempt_id", "cwd", "started_at")},
                          "priority": req.get("priority"), "title": req.get("title"), "request_state": req.get("state"),
                          "elapsed_seconds": _age(act.get("started_at")),
                          "seconds_left": max(0.0, act["deadline_epoch"] - time.time()) if act.get("deadline_epoch") else None,
                          "session": {"verdict": sv, "reason": swhy, "pid": (act.get("session") or {}).get("pid")},
                          "transcript": str(state.attempt_dir(root, wid, act["attempt_id"]) / "transcript.jsonl")}
    else:
        view["active"] = None
    return view


def status(root: Path) -> dict:
    """Workers, and the request lifecycle as the framework records it."""
    import devsys
    workers = [worker_view(root, wid) for wid in state.slot_ids(root)]
    owner = {w["agent_id"]: w["id"] for w in workers}
    working = []
    for c in proc.all_claims(root):
        if c.get("kind", "request") != "request":
            continue
        locs = proc.locate_request(root, c.get("target", ""))
        if not locs or locs[0][0] != "processing":
            continue
        info = runner.request_info(root, c["target"]) or {}
        working.append({"request": c["target"], "request_id": info.get("request_id"), "priority": info.get("priority"),
                        "title": info.get("title"), "agent_id": c.get("agent_id"),
                        "worker": owner.get(c.get("agent_id")),
                        "liveness": proc.claim_liveness(c, proc.stale_hours(root))[0]})
    done = []
    for st in ("completed", "rejected", "needs-review"):
        for md in sorted((cfgmod.path(root, "requests") / st).glob("*.md"), key=lambda p: p.stat().st_mtime,
                         reverse=True)[:25]:
            if md.name == "README.md":
                continue
            info = runner.request_info(root, md.name) or {}
            done.append({"request": md.name, "request_id": info.get("request_id"), "state": st,
                         "priority": info.get("priority"), "title": info.get("title"),
                         "at": datetime.fromtimestamp(md.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")})
    done.sort(key=lambda d: d["at"], reverse=True)
    s = cfgmod.get("workers", root, {}) or {}
    return {
        "system_root": str(Path(root).resolve()), "project_root": str(cfgmod.project_root(root)),
        "max_workers": int(s.get("max_workers", 0)), "isolation": s.get("isolation"),
        "agent_configured": bool(workers) and all(w["configured"] for w in workers if w["enabled"]),
        "profiles": sorted({w["profile"] for w in workers}),
        "running": sum(1 for w in workers if w["process"]["verdict"] == plat.ALIVE),
        "workers": workers, "queue": devsys.queue(root), "working": working,
        "finished": done[:40],
    }


def enable(root: Path, wid: str) -> dict:
    _check(root, wid)
    state.ensure(root, wid)
    return state.update(root, wid, enabled=True)


def disable(root: Path, wid: str) -> dict:
    _check(root, wid)
    state.ensure(root, wid)
    return state.update(root, wid, enabled=False, desired="stopped")


def start(root: Path, wid: str, *, wait_seconds: float = 15.0) -> dict:
    """Spawn the worker process (``devsys.py workers run <wid>``) detached from
    the caller. Refused for a disabled worker, when the agent is not
    configured, or when ``workers.max_workers`` workers already run."""
    _check(root, wid)
    rec = state.ensure(root, wid)
    if not rec.get("enabled", True):
        raise WorkerError(proc.INVALID, f"{wid} is disabled (enable it first)")
    if is_running(rec):
        return {"worker": wid, "started": False, "reason": "already running", "pid": rec["process"]["pid"]}
    _, problem = _command(root, wid)
    if problem:
        raise WorkerError(cfgmod.NOT_CONFIGURED, problem)
    running = [w for w in state.slot_ids(root) if w != wid and is_running(state.load(root, w))]
    if len(running) >= int(cfgmod.get("workers.max_workers", root, 0)):
        raise WorkerError(proc.CONFLICT, f"{len(running)} worker(s) already running (workers.max_workers)")
    if rec.get("active"):
        blocked = runner.recover_active(root, wid)
        if blocked:
            raise WorkerError(proc.STALE, f"{wid}: {blocked}")
    state.update(root, wid, desired="running", stop_now=False)
    log = open(state.worker_dir(root, wid) / "process.log", "ab")
    # The worker process must serve *this* root even when the caller's code
    # lives elsewhere (a control panel given --root, a test harness).
    env = {**os.environ, "AGENT_PROCESS_ROOT": str(Path(root).resolve())}
    try:
        p = subprocess.Popen([sys.executable, str(SCRIPTS_DIR / "devsys.py"), "workers", "run", wid],
                             cwd=str(root), env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                             **plat.group_popen_kwargs())
    finally:
        log.close()
    # Reap the worker when it exits. A long-running caller (the control panel)
    # would otherwise keep every exited worker as a zombie.
    threading.Thread(target=p.wait, name=f"reap-{wid}", daemon=True).start()
    end = time.time() + wait_seconds
    while time.time() < end:
        if (state.load(root, wid).get("process") or {}).get("pid") == p.pid:
            return {"worker": wid, "started": True, "pid": p.pid}
        if p.poll() is not None:
            break
        time.sleep(0.1)
    return {"worker": wid, "started": p.poll() is None, "pid": p.pid,
            "reason": None if p.poll() is None else f"worker process exited ({p.returncode}); see process.log"}


def stop(root: Path, wid: str, *, now: bool = False, wait_seconds: float = 0.0) -> dict:
    _check(root, wid)
    rec = state.ensure(root, wid)
    busy = is_running(rec) and rec.get("active")
    state.update(root, wid, desired="stopped", stop_now=bool(now),
                 **({"status": "stopping", "status_reason": "stop now requested" if now else
                     "stop requested; the current attempt finishes first"} if busy else {}))
    end = time.time() + wait_seconds
    while time.time() < end and is_running(state.load(root, wid)):
        time.sleep(0.2)
    rec = state.load(root, wid)
    return {"worker": wid, "running": is_running(rec), "status": rec.get("status")}


def recover(root: Path, wid: str, *, force: bool = False) -> dict:
    """Settle a worker whose process is gone: its recorded attempt is verified
    and concluded (the request's claim stays with the worker, which resumes it
    on its next start). A running worker is never touched; a still-running
    orphaned session is ended only with ``force``."""
    _check(root, wid)
    rec = state.load(root, wid)
    verdict, why = process_liveness(rec)
    if verdict == plat.ALIVE:
        return {"worker": wid, "changed": False, "detail": f"worker process is running ({why}); nothing to recover"}
    if verdict == plat.UNKNOWN and not force:
        return {"worker": wid, "changed": False,
                "detail": f"worker process cannot be verified ({why}); recover --force once you know it is gone"}
    blocked = runner.recover_active(root, wid, force=force, kill_alive=force)
    if blocked:
        return {"worker": wid, "changed": False, "detail": blocked + " (recover --force ends it)"}
    state.update(root, wid, process=None, status="stopped", stop_now=False,
                 status_reason=f"recovered ({why})" if rec.get("process") else rec.get("status_reason"))
    state.event(root, wid, "worker.recovered", verdict=verdict, reason=why, forced=force)
    return {"worker": wid, "changed": True, "detail": f"settled ({verdict}: {why})"}


def peek(root: Path, wid: str, *, lines: int = 25) -> dict:
    _check(root, wid)
    view = worker_view(root, wid)
    rec = state.load(root, wid)
    aid = (rec.get("active") or {}).get("attempt_id") or (rec.get("last") or {}).get("attempt_id")
    attempt = state.read_json(state.attempt_dir(root, wid, aid) / "attempt.json") if aid else None
    view["attempt"] = None if attempt is None else {
        k: attempt.get(k) for k in ("attempt_id", "request_id", "status", "result", "result_reason", "started_at",
                                    "ended_at", "cwd", "request_state", "report", "validation", "commits")}
    view["transcript_tail"] = agent.transcript_tail(state.attempt_dir(root, wid, aid) / "transcript.jsonl", lines) \
        if aid else []
    view["events"] = state.read_events(root, wid, lines)
    return view
