"""Per-worker runtime state (docs/process/worker-orchestrator.md "State").

Everything a worker records lives in its own directory in the main
worktree's untracked runtime area, so workers never share runtime files::

    .agent/state/runtime/workers/
      <worker-id>/
        worker.json          the worker's record (below), rewritten atomically
        worker.lock          flock held by the live worker process
        events.jsonl         the worker's event log (append-only)
        process.log          stdout / stderr of a detached worker process
        attempts/<attempt>/  prompt.md, launch.json, transcript.jsonl,
                             stderr.log, report.json, attempt.json,
                             validation/<step>.log
      attempts.jsonl         one line per concluded attempt, all workers:
                             how often a request was attempted (orchestration
                             memory keyed by request id; the request's state
                             itself is only ever in requests/)

worker.json::

    {"id", "enabled", "desired": "running" | "stopped", "stop_now",
     "agent_id",                         stable per worker slot
     "process": {host, boot_id, pid, start, started_at} | null,
     "status": "stopped" | "idle" | "working" | "stopping",
     "status_reason", "active": {request, request_id, attempt_id, cwd,
     started_at, deadline_epoch, session: {...}} | null,
     "counters": {attempts, completed, escalated, consecutive_failures},
     "last": {request_id, result, outcome, at, reason}, "updated_at"}
"""
from __future__ import annotations

import json
import os
import socket
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import agent_process as proc  # noqa: E402
import devsys_config as cfgmod  # noqa: E402

WORKER_STATUSES = ("stopped", "idle", "working", "stopping")


def workers_dir(root: Path) -> Path:
    return proc.runtime_dir(root) / "workers"


def worker_dir(root: Path, wid: str) -> Path:
    return workers_dir(root) / wid


def slot_ids(root: Path) -> list[str]:
    """The configured worker slots, worker-01 .. worker-NN (workers.max_workers),
    plus any slot that still has a record (a lowered maximum hides no worker)."""
    n = int(cfgmod.get("workers.max_workers", root, 0) or 0)
    ids = [f"worker-{i:02d}" for i in range(1, n + 1)]
    base = workers_dir(root)
    if base.is_dir():
        ids += sorted(p.name for p in base.iterdir() if p.is_dir() and p.name.startswith("worker-")
                      and p.name not in ids and (p / "worker.json").exists())
    return ids


def new_agent_id(wid: str) -> str:
    host = socket.gethostname().split(".")[0][:20]
    return f"agent-{wid}-{host}-{uuid.uuid4().hex[:6]}"


def _default(wid: str) -> dict:
    return {"id": wid, "enabled": True, "desired": "stopped", "stop_now": False,
            "agent_id": None,                 # fixed when the record is first written
            "process": None, "status": "stopped", "status_reason": "never started", "active": None,
            "counters": {"attempts": 0, "completed": 0, "escalated": 0, "consecutive_failures": 0},
            "last": None, "updated_at": proc.now_iso()}


def load(root: Path, wid: str) -> dict:
    """The worker's record; a fresh default (not yet written) for a new slot."""
    f = worker_dir(root, wid) / "worker.json"
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _default(wid)


def update(root: Path, wid: str, **fields) -> dict:
    """Read-modify-write the record under the worker's lock. ``counters`` is
    merged key by key."""
    with proc.exclusive(root, f"worker-{wid}"):
        data = load(root, wid)
        counters = fields.pop("counters", None)
        data.update(fields)
        if not data.get("agent_id"):
            data["agent_id"] = new_agent_id(wid)
        if counters:
            data.setdefault("counters", {}).update(counters)
        data["updated_at"] = proc.now_iso()
        d = worker_dir(root, wid)
        d.mkdir(parents=True, exist_ok=True)
        proc.atomic_write_text(d / "worker.json", json.dumps(data, indent=2) + "\n")
        return data


def ensure(root: Path, wid: str) -> dict:
    """Write the record of a slot that has none yet (its agent id is fixed then)."""
    if not (worker_dir(root, wid) / "worker.json").exists():
        return update(root, wid)
    return load(root, wid)


def event(root: Path, wid: str, kind: str, **data) -> None:
    d = worker_dir(root, wid)
    d.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"at": proc.now_iso(), "event": kind, **data}) + "\n"
    with open(d / "events.jsonl", "a", encoding="utf-8") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())


def read_events(root: Path, wid: str, last: int = 50) -> list[dict]:
    f = worker_dir(root, wid) / "events.jsonl"
    try:
        lines = f.read_text(encoding="utf-8").splitlines()[-last:]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def attempt_dir(root: Path, wid: str, aid: str) -> Path:
    return worker_dir(root, wid) / "attempts" / aid


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    proc.atomic_write_text(path, json.dumps(data, indent=2, default=str) + "\n")


# ------------------------------------------------------------------ attempts journal
def journal(root: Path, entry: dict) -> None:
    """Append one concluded attempt (all workers) under a lock."""
    with proc.exclusive(root, "workers-journal"):
        workers_dir(root).mkdir(parents=True, exist_ok=True)
        with open(workers_dir(root) / "attempts.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": proc.now_iso(), **entry}) + "\n")
            f.flush()
            os.fsync(f.fileno())


def journal_entries(root: Path, request_id: str | None = None) -> list[dict]:
    try:
        lines = (workers_dir(root) / "attempts.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if request_id is None or e.get("request_id") == request_id:
            out.append(e)
    return out


def counted_attempts(root: Path, request_id: str) -> int:
    """Attempts that count against ``workers.attempt.max_attempts_per_request``:
    counted results since the request was last escalated or completed (a
    human who resumes an escalated request gives it fresh attempts)."""
    n = 0
    for e in journal_entries(root, request_id):
        if e.get("event") in ("escalated", "done"):
            n = 0
        elif e.get("counted"):
            n += 1
    return n
