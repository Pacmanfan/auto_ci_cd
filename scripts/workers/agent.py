"""Launching, waiting for and verifying one agent session.

The agent is whatever coding-agent command line the project configures in
``workers.agent.command`` (docs/process/agent-profiles.md). The contract is
generic (per worker slot a named profile may replace it: ``workers.profiles``
and ``workers.assign``):

* the prompt is written to the agent's stdin;
* its stdout is a JSON-lines event stream, written straight to
  ``transcript.jsonl`` on disk (never through the worker), so the record
  survives a worker crash and a restarted worker can re-adopt the session;
* one event of ``"type": "result"`` ends a normal session (fields read:
  ``subtype``, ``is_error``, ``num_turns``, ``duration_ms``,
  ``total_cost_usd``, ``result``, ``stop_reason``); ``"type": "assistant"``
  events carry the conversation (``message.content`` blocks of type ``text``
  / ``tool_use``); an optional ``"type": "system"``, ``"subtype": "init"``
  event reports the session's ``cwd``; ``"type": "rate_limit_event"``
  reports a usage limit;
* the session runs in its own process group, so a timeout or a stop ends it
  and every process it started without touching anything else.

Whether a recorded session still runs is decided only by
``devsys_platform.verify_session`` against its launch identity and the
``DEVSYS_WORKER_SESSION`` marker in its environment, never by a record that
says it is running (ADR-000198).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import agent_process as proc  # noqa: E402
import devsys_config as cfgmod  # noqa: E402
import devsys_platform as plat  # noqa: E402

# Every worker session carries its own session id in this variable; it ties a
# live process to the attempt that launched it.
SESSION_MARKER = "DEVSYS_WORKER_SESSION"


class NotConfigured(RuntimeError):
    pass


DEFAULT_PROFILE = "default"


def profile_name(root: Path, worker: str | None = None) -> str:
    """The agent profile a worker slot runs: ``workers.assign[<worker>]``, else
    ``default`` (the ``workers.agent`` settings)."""
    assigned = (cfgmod.get("workers.assign", root, {}) or {}).get(worker or "")
    return str(assigned) if assigned else DEFAULT_PROFILE


def agent_settings(root: Path, worker: str | None = None) -> dict:
    """The agent settings of a worker slot. ``workers.agent`` is the default
    profile; a named profile (``workers.profiles[<name>]``) overrides it key by
    key, so a fleet can mix agent kinds (docs/process/agent-profiles.md)."""
    base = dict(cfgmod.get("workers.agent", root, {}) or {})
    name = profile_name(root, worker)
    if name != DEFAULT_PROFILE:
        profiles = cfgmod.get("workers.profiles", root, {}) or {}
        if name not in profiles:
            raise NotConfigured(f"workers.assign names the profile {name!r} for {worker}, but workers.profiles "
                                f"has no such profile ({', '.join(sorted(profiles)) or 'none defined'})")
        base.update(profiles[name] or {})
    return base


def build_cmd(root: Path, *, session_id: str, name: str, attempt_dir: Path, worker: str | None = None) -> list[str]:
    template = list(agent_settings(root, worker).get("command") or [])
    if not template:
        raise NotConfigured(f"the agent command of profile {profile_name(root, worker)!r} is empty: configure the "
                            "coding agent the workers run (workers.agent.command / workers.profiles, "
                            "docs/process/agent-profiles.md)")
    # {scripts}: this system's scripts directory, for profiles that wrap the
    # agent in an adapter shipped with the system.
    values = {"session_id": session_id, "name": name, "attempt_dir": str(attempt_dir),
              "scripts": str(Path(__file__).resolve().parent.parent)}
    return [str(a).format(**values) for a in template]


def build_env(root: Path, extra: dict, worker: str | None = None) -> dict:
    scrub = set(agent_settings(root, worker).get("env_scrub") or [])
    env = {k: v for k, v in os.environ.items() if k not in scrub}
    env.update({k: str(v) for k, v in extra.items() if v is not None})
    return env


class AgentSession:
    """One agent process whose files live in ``out_dir``."""

    def __init__(self, root: Path, *, cwd: Path, out_dir: Path, prompt: str, name: str, env_extra: dict,
                 worker: str | None = None):
        self.root = Path(root)
        self.worker = worker
        self.profile = profile_name(root, worker)
        self.cwd = Path(cwd)
        self.out_dir = Path(out_dir)
        self.prompt = prompt
        self.session_id = str(uuid.uuid4())
        self.cmd = build_cmd(root, session_id=self.session_id, name=name, attempt_dir=self.out_dir, worker=worker)
        self.env_extra = {**env_extra, SESSION_MARKER: self.session_id}
        self.env = build_env(root, self.env_extra, worker)
        self.proc: subprocess.Popen | None = None

    @property
    def transcript(self) -> Path:
        return self.out_dir / "transcript.jsonl"

    def start(self) -> dict:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        (self.out_dir / "prompt.md").write_text(self.prompt, encoding="utf-8")
        scrubbed = [k for k in agent_settings(self.root, self.worker).get("env_scrub") or [] if k in os.environ]
        (self.out_dir / "launch.json").write_text(json.dumps({
            "cmd": self.cmd, "profile": self.profile, "cwd": str(self.cwd), "session_id": self.session_id,
            "env_added": self.env_extra, "env_removed": scrubbed, "started_at": proc.now_iso()}, indent=2) + "\n",
            encoding="utf-8")
        out = open(self.transcript, "ab")
        err = open(self.out_dir / "stderr.log", "ab")
        try:
            self.proc = subprocess.Popen(self.cmd, cwd=self.cwd, env=self.env, stdin=subprocess.PIPE,
                                         stdout=out, stderr=err, **plat.group_popen_kwargs())
        finally:
            out.close()
            err.close()
        ident = plat.session_identity(self.proc.pid)
        try:
            self.proc.stdin.write(self.prompt.encode("utf-8"))
            self.proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        return {**ident, "session_id": self.session_id, "started_at": proc.now_iso()}


def verify(session: dict | None) -> tuple[str, str]:
    """ALIVE / DEAD / UNKNOWN for a recorded session (identity + session id)."""
    if not session:
        return plat.UNKNOWN, "no session recorded"
    return plat.verify_session(session, {SESSION_MARKER: session["session_id"]} if session.get("session_id") else None)


def wait(session: dict, *, popen: subprocess.Popen | None, deadline: float, poll_seconds: float,
         grace_seconds: float, tick: Callable[[float], str | None] | None = None) -> dict:
    """Wait for a session to end. ``tick(elapsed)`` runs every poll and may
    return "stop_now". Returns {exit_code, timed_out, interrupted}. With
    ``popen`` None (a re-adopted session) the exit code is unknown and the
    transcript decides."""
    started = time.time()
    pid = session["pid"]
    while True:
        if popen is not None:
            rc = popen.poll()
            if rc is not None:
                return {"exit_code": rc, "timed_out": False, "interrupted": False}
        elif verify(session)[0] != plat.ALIVE:
            return {"exit_code": None, "timed_out": False, "interrupted": False}
        if time.time() >= deadline:
            plat.kill_tree(pid, grace_seconds)
            rc = popen.wait(timeout=30) if popen is not None else None
            return {"exit_code": rc, "timed_out": True, "interrupted": False}
        if tick is not None and tick(time.time() - started) == "stop_now":
            plat.kill_tree(pid, grace_seconds)
            rc = popen.wait(timeout=30) if popen is not None else None
            return {"exit_code": rc, "timed_out": False, "interrupted": True}
        time.sleep(poll_seconds)


def parse_transcript(path: Path) -> dict:
    """Summarise a JSON-lines transcript. Garbage lines are counted, not fatal."""
    info = {"events": 0, "bad_lines": 0, "session_id": None, "init": None, "result": None,
            "rate_limit": None, "tool_uses": 0, "assistant_messages": 0, "last_text": ""}
    try:
        f = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return info
    with f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                info["bad_lines"] += 1
                continue
            if not isinstance(e, dict):
                info["bad_lines"] += 1
                continue
            info["events"] += 1
            t = e.get("type")
            if e.get("session_id"):
                info["session_id"] = e["session_id"]
            if t == "system" and e.get("subtype") == "init":
                info["init"] = {k: e.get(k) for k in ("cwd", "model")}
            elif t == "assistant":
                info["assistant_messages"] += 1
                for block in (e.get("message") or {}).get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        info["tool_uses"] += 1
                    elif isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                        info["last_text"] = block["text"]
            elif t == "rate_limit_event":
                info["rate_limit"] = e.get("rate_limit_info") or {}
            elif t == "result":
                info["result"] = {k: e.get(k) for k in ("subtype", "is_error", "num_turns", "duration_ms",
                                                        "total_cost_usd", "result", "stop_reason")}
    return info


def rate_limited(root: Path, info: dict, worker: str | None = None) -> float | None:
    """Epoch seconds when the agent's usage limit resets, if the session ended
    on one (a rejected rate-limit event, or an error result matching
    ``workers.agent.rate_limit_patterns``); else None."""
    rl = info.get("rate_limit") or {}
    status = str(rl.get("status") or "")
    res = info.get("result") or {}
    text = str(res.get("result") or "").lower()
    patterns = [p.lower() for p in agent_settings(root, worker).get("rate_limit_patterns") or []]
    limited = (status and not status.startswith("allowed")) or (
        res.get("is_error") and any(p in text for p in patterns))
    if not limited:
        return None
    return float(rl.get("resetsAt") or 0) or (time.time() + 3600)


def transcript_tail(path: Path, n: int = 20) -> list[str]:
    """The last ``n`` readable lines of a transcript, summarised for people."""
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()[-400:]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            out.append(f"(unparsed) {line[:160]}")
            continue
        t = e.get("type")
        if t == "assistant":
            for block in (e.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                    out.append("agent: " + block["text"].strip().replace("\n", " ")[:200])
                elif isinstance(block, dict) and block.get("type") == "tool_use":
                    out.append(f"tool: {block.get('name', '?')}")
        elif t == "result":
            out.append(f"result: {e.get('subtype')} ({'error' if e.get('is_error') else 'ok'})")
        elif t == "system":
            out.append(f"system: {e.get('subtype')}")
    return out[-n:]
