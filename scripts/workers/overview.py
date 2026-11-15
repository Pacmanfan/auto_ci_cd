"""One picture of the whole system, and the operator's actions on it
(COORD-007, docs/process/agent-coordination.md).

The web control panel (tools/panel/web_panel.py) and the terminal dashboard
(tools/panel/dash.py) are clients of this module only, as the Qt panel is a
client of ``workers.pool``: nothing here decides request state.

    snapshot(root)            workers, requests, agents, worktrees, messages
    act(root, action, ...)    start / stop / stop_now / recover / enable /
                              disable (one worker or ``all``), priority,
                              message (as the operator), submit (a request)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import agent_messages as bus  # noqa: E402
import agent_process as proc  # noqa: E402
import devsys_config as cfgmod  # noqa: E402

from workers import pool, state  # noqa: E402

WORKER_ACTIONS = ("start", "stop", "stop_now", "recover", "recover_force", "enable", "disable")
SCRIPTS_DIR = Path(__file__).resolve().parent.parent


def worktrees(root: Path) -> list[dict]:
    """Every git worktree of the clone with its branch, and the request whose
    claim works there (the main checkout: every claim without a worktree)."""
    r = subprocess.run(["git", "worktree", "list", "--porcelain"], cwd=str(proc.main_checkout(root)),
                       capture_output=True, text=True)
    out, cur = [], None
    for line in r.stdout.splitlines() if r.returncode == 0 else []:
        key, _, value = line.partition(" ")
        if key == "worktree":
            cur = {"path": value, "branch": None, "head": None, "main": not out, "requests": []}
            out.append(cur)
        elif cur is not None and key == "branch":
            cur["branch"] = value.replace("refs/heads/", "", 1)
        elif cur is not None and key == "HEAD":
            cur["head"] = value[:10]
        elif cur is not None and key == "detached":
            cur["branch"] = "(detached)"
    by_path = {str(Path(w["path"]).resolve()): w for w in out}
    for c in proc.all_claims(root):
        if c.get("kind", "request") != "request":
            continue
        wt = c.get("worktree")
        home = by_path.get(str(Path(wt).resolve())) if wt else (out[0] if out else None)
        if home is not None:
            home["requests"].append(c.get("request_id") or c.get("target"))
    return out


def agents(root: Path) -> list[dict]:
    """The roster joined with the claims: who is who, verified liveness, what
    each agent holds and where it works. An agent that holds a claim but never
    registered appears under its id."""
    claims: dict[str, list[dict]] = {}
    for c in proc.all_claims(root):
        if c.get("kind", "request") == "request":
            claims.setdefault(c.get("agent_id"), []).append(c)
    stale = proc.stale_hours(root)
    out = []
    entries = bus.roster(root)
    for agent_id in [e["agent_id"] for e in entries] + [a for a in claims if a and not bus.get(root, a)]:
        e = bus.get(root, agent_id) or {"agent_id": agent_id, "name": agent_id, "roles": [], "registered": False}
        verdict, why = bus.liveness(e)
        held = []
        for c in claims.get(agent_id, []):
            live = proc.claim_liveness(c, stale)[0]
            held.append({"request": c.get("target"), "request_id": c.get("request_id"), "state": c.get("state"),
                         "liveness": live, "step": (c.get("step") or {}).get("label"),
                         "worktree": c.get("worktree"), "heartbeat_at": c.get("heartbeat_at"),
                         "paths": sum(1 for f in (c.get("files") or {}).values() if not f.get("request_file"))})
            if verdict == "UNKNOWN" and live in ("ALIVE", "ACTIVE", "DEAD"):
                verdict, why = live, "from its claim"
        out.append({**{k: e.get(k) for k in ("agent_id", "name", "profile", "roles", "worker", "cwd", "worktree",
                                              "branch", "request", "registered_at", "seen_at")},
                    "registered": e.get("registered", True), "liveness": verdict, "liveness_reason": why,
                    "unread": bus.unread_count(root, agent_id), "claims": held})
    return out


def snapshot(root: Path, *, messages: int = 80, after: int = 0) -> dict:
    """Everything the panels show. ``after``: only messages newer than this
    sequence number (a client that already has the earlier ones)."""
    st = pool.status(root)
    msgs = bus.read(root, after=after, limit=None if after else messages)
    return {"at": proc.now_iso(), "project": cfgmod.get("project.name", root, ""),
            "system_root": st["system_root"], "pool": st, "agents": agents(root), "worktrees": worktrees(root),
            "messages": msgs, "last_seq": bus.last_seq(root), "kinds": list(bus.KINDS),
            "priorities": list(proc.PRIORITIES)}


def act(root: Path, action: str, *, worker: str | None = None, request: str | None = None,
        priority: str | None = None, to: str | None = None, body: str | None = None, kind: str = "note",
        subject: str | None = None, reply_to: str | None = None, text: str | None = None) -> dict:
    """Perform one operator action through the same APIs the CLI uses.
    Raises ``proc.ProcessError`` (with the tooling's exit code) on refusal."""
    if action in WORKER_ACTIONS:
        ids = state.slot_ids(root) if worker in (None, "", "all") else [worker]
        results = []
        for wid in ids:
            try:
                if action == "start":
                    if worker in (None, "", "all") and not state.load(root, wid).get("enabled", True):
                        continue                      # "start all" means all enabled workers
                    r = pool.start(root, wid)
                elif action in ("stop", "stop_now"):
                    r = pool.stop(root, wid, now=action == "stop_now")
                elif action in ("recover", "recover_force"):
                    r = pool.recover(root, wid, force=action == "recover_force")
                elif action == "enable":
                    r = {"worker": wid, "enabled": pool.enable(root, wid)["enabled"]}
                else:
                    r = {"worker": wid, "enabled": pool.disable(root, wid)["enabled"]}
            except proc.ProcessError as e:
                if len(ids) == 1:
                    raise
                r = {"worker": wid, "error": e.message}
            results.append(r)
        return {"action": action, "results": results}
    if action == "priority":
        d = proc.set_priority(root, Path(request or "").name, str(priority or "").upper(), by=bus.OPERATOR)
        return {"action": action, **d}
    if action == "message":
        m = bus.send(root, bus.OPERATOR, to or "", body or "", kind=kind, request=request, subject=subject,
                     reply_to=reply_to)
        return {"action": action, "message": m}
    if action == "submit":
        # Intake stays the one way a request is created (docs/process/chat-intake.md).
        if not (text or "").strip():
            raise proc.ProcessError(proc.INVALID, "empty request text")
        cmd = [sys.executable, str(SCRIPTS_DIR / "create_request.py"), "--source", "chat", "--json", "--text", text]
        if priority:
            cmd += ["--priority", str(priority).upper()]
        r = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True,
                           env={**os.environ, "AGENT_PROCESS_ROOT": str(Path(root).resolve())})
        if r.returncode != 0:
            raise proc.ProcessError(r.returncode, (r.stderr or r.stdout).strip()[-600:] or "intake refused the request")
        return {"action": action, "created": json.loads(r.stdout.strip().splitlines()[-1])}
    raise proc.ProcessError(proc.INVALID, f"unknown action {action!r}")
