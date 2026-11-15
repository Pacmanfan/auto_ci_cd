"""Agent coordination: the roster of agents and the message bus between them
(docs/requirements/agent_coordination.md COORD-001..006, ADR-000201).

Claims keep concurrent agents *apart*; this module lets them *talk*. It adds
no authority: a message never moves a request, grants a path or replaces a
claim. Requests, claims and the gate stay the single source of truth.

State (untracked runtime, in the MAIN worktree so that every linked git
worktree of the clone shares one roster and one bus)::

    .agent/state/runtime/coordination/
      messages.jsonl        append-only, one JSON object per line, ``seq`` strictly increasing
      agents/<agent id>.json   one roster entry per agent

A roster entry::

    {"agent_id", "name",            friendly, unique among the entries
     "profile",                     which agent kind runs it (free text, e.g. the worker's profile name)
     "roles": ["lead", ...],        addressable groups; a message to a role reaches every holder
     "worker",                      worker slot, when a worker's agent
     "owner": {host, boot_id, pid, start} | null,   the session's long-lived process (verified liveness)
     "cwd", "worktree", "branch",   where the agent works (a request's git worktree and its branch)
     "request",                     the request it last reported working on
     "read_seq",                    every message up to this seq has been delivered to the agent
     "registered_at", "seen_at"}

A message::

    {"seq", "id", "at", "from" (agent id | "operator"), "from_name", "to", "kind",
     "request" (request id or file, optional), "subject", "body", "reply_to" (message id, optional)}

``to`` is an agent's name or id, a role, ``operator`` (the human at a panel),
or ``all``. A message is *for* an agent when ``to`` is ``all``, its id, its
name or one of its roles, and the agent did not send it.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path

import agent_process as proc
import devsys_platform as plat

OPERATOR = "operator"
EVERYONE = "all"
# Closed vocabulary: what a message is, so that readers (and panels) can sort
# them without parsing prose.
KINDS = ("note", "question", "answer", "status", "handoff", "path-request", "alert")
RESERVED_NAMES = {OPERATOR, EVERYONE}
MAX_BODY = 8000
# Friendly names handed out in order; a full pool continues with name-2, ...
NAME_POOL = ("amber", "birch", "cedar", "delta", "ember", "flint", "grove", "heron", "indigo", "juniper",
             "kestrel", "linden", "maple", "nova", "onyx", "pine", "quartz", "raven", "sage", "tern",
             "umber", "vale", "willow", "yarrow", "zephyr")
_NAME_RE = re.compile(r"[a-z][a-z0-9-]{1,31}")
_LOCK = "coordination"


class MessageError(proc.ProcessError):
    pass


# ----------------------------------------------------------------- locations

def coordination_dir(root: Path) -> Path:
    d = proc.runtime_dir(root) / "coordination"
    d.mkdir(parents=True, exist_ok=True)
    return d


def agents_dir(root: Path) -> Path:
    d = coordination_dir(root) / "agents"
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_path(root: Path) -> Path:
    return coordination_dir(root) / "messages.jsonl"


def _entry_path(root: Path, agent_id: str) -> Path:
    return agents_dir(root) / (re.sub(r"[^A-Za-z0-9._-]", "_", agent_id) + ".json")


# -------------------------------------------------------------------- roster

def _read_entry(path: Path) -> dict | None:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) and d.get("agent_id") else None
    except (OSError, ValueError):
        return None


def roster(root: Path) -> list[dict]:
    """Every registered agent, oldest registration first."""
    out = [e for e in (_read_entry(p) for p in sorted(agents_dir(root).glob("*.json"))) if e]
    return sorted(out, key=lambda e: (e.get("registered_at") or "", e["agent_id"]))


def get(root: Path, agent_id: str) -> dict | None:
    return _read_entry(_entry_path(root, agent_id))


def liveness(entry: dict) -> tuple[str, str]:
    """ALIVE / DEAD / UNKNOWN from the recorded owner process; an entry
    without one is UNKNOWN (a record alone is never proof, ADR-000198)."""
    if not entry.get("owner"):
        return plat.UNKNOWN, "no owner process recorded"
    return plat.verify_session(entry["owner"])


def _free_name(taken: set[str]) -> str:
    n = 1
    while True:
        for base in NAME_POOL:
            name = base if n == 1 else f"{base}-{n}"
            if name not in taken:
                return name
        n += 1


def checkout_info(cwd: Path | str | None) -> dict:
    """Where an agent works: its checkout's top directory and branch. In a
    request's git worktree these differ from the main checkout's."""
    info = {"cwd": str(cwd) if cwd else None, "worktree": None, "branch": None}
    if not cwd or not Path(cwd).is_dir():
        return info
    r = subprocess.run(["git", "rev-parse", "--show-toplevel", "--abbrev-ref", "HEAD"], cwd=str(cwd),
                       capture_output=True, text=True)
    lines = r.stdout.strip().splitlines()
    if r.returncode == 0 and len(lines) >= 2:
        info["worktree"], info["branch"] = lines[0], lines[1]
    elif lines:
        info["worktree"] = lines[0]             # a repository without a commit has no HEAD yet
    return info


def register(root: Path, agent_id: str, *, name: str | None = None, profile: str | None = None,
             roles: list[str] | None = None, worker: str | None = None, owner_pid: int | None = None,
             cwd: Path | str | None = None, request: str | None = None) -> dict:
    """Create or refresh an agent's roster entry (idempotent). A name is
    allocated on first registration; an explicit ``name`` held by another
    agent is refused. Only the given fields change on a refresh."""
    agent_id = proc.validate_agent_id(agent_id)
    if name is not None:
        name = name.strip().lower()
        if not _NAME_RE.fullmatch(name) or name in RESERVED_NAMES:
            raise MessageError(proc.INVALID, f"agent name {name!r} is not usable (lower-case letters, digits, '-'; "
                                             f"2-32 characters; not {', '.join(sorted(RESERVED_NAMES))})")
    with proc.exclusive(root, _LOCK):
        others = [e for e in roster(root) if e["agent_id"] != agent_id]
        entry = get(root, agent_id)
        taken = {e["name"] for e in others}
        if name and name in taken:
            holder = next(e for e in others if e["name"] == name)
            raise MessageError(proc.CONFLICT, f"agent name {name!r} is held by {holder['agent_id']}")
        now = proc.now_iso()
        if entry is None:
            entry = {"agent_id": agent_id, "name": name or _free_name(taken | RESERVED_NAMES), "profile": None,
                     "roles": [], "worker": None, "owner": None, "cwd": None, "worktree": None, "branch": None,
                     "request": None, "read_seq": last_seq(root), "registered_at": now}
        elif name:
            entry["name"] = name
        if profile is not None:
            entry["profile"] = profile
        if roles is not None:
            entry["roles"] = sorted({r.strip().lower() for r in roles if r.strip()})
        if worker is not None:
            entry["worker"] = worker
        if owner_pid:
            entry["owner"] = plat.session_identity(int(owner_pid))
        if cwd is not None:
            entry.update(checkout_info(cwd))
        if request is not None:
            entry["request"] = request or None
        entry["seen_at"] = now
        proc.atomic_write_text(_entry_path(root, agent_id), json.dumps(entry, indent=2) + "\n")
        return entry


def ensure(root: Path, agent_id: str, owner_pid: int | None = None) -> dict:
    """The agent's entry, registered with defaults when it has none yet."""
    return get(root, agent_id) or register(root, agent_id, owner_pid=owner_pid or proc.owner_pid_from_env(),
                                           cwd=os.getcwd())


def leave(root: Path, agent_id: str) -> bool:
    """Remove an agent's roster entry (its messages stay in the log)."""
    with proc.exclusive(root, _LOCK):
        try:
            _entry_path(root, agent_id).unlink()
            return True
        except FileNotFoundError:
            return False


def prune(root: Path) -> list[str]:
    """Remove the entries whose owner process is verified DEAD. Entries that
    cannot be verified are kept."""
    gone = []
    for e in roster(root):
        if liveness(e)[0] == plat.DEAD and leave(root, e["agent_id"]):
            gone.append(e["agent_id"])
    return gone


def resolve(root: Path, who: str) -> str:
    """Canonical recipient for ``who``: ``all``, ``operator``, a role, or the
    agent id behind a name or id. Unknown recipients are refused, so a typo
    never becomes a message nobody reads."""
    w = (who or "").strip()
    if w.lower() in (EVERYONE, OPERATOR):
        return w.lower()
    entries = roster(root)
    for e in entries:
        if w == e["agent_id"] or w.lower() == e["name"]:
            return e["agent_id"]
    if any(w.lower() in (e.get("roles") or []) for e in entries):
        return w.lower()
    known = ", ".join(e["name"] for e in entries) or "none registered"
    raise MessageError(proc.NOT_FOUND, f"no agent, role or group {who!r} (agents: {known}; also: all, operator)")


def display(root: Path, ident: str, entries: list[dict] | None = None) -> str:
    """The friendly name for an agent id (the id itself when unknown)."""
    for e in entries if entries is not None else roster(root):
        if e["agent_id"] == ident:
            return e["name"]
    return ident


# ------------------------------------------------------------------ messages

def _iter(root: Path, after: int = 0):
    try:
        f = open(log_path(root), encoding="utf-8", errors="replace")
    except OSError:
        return
    with f:
        for line in f:
            try:
                m = json.loads(line)
            except ValueError:
                continue                         # a torn line is skipped, never fatal
            if isinstance(m, dict) and int(m.get("seq", 0)) > after:
                yield m


def last_seq(root: Path) -> int:
    """The highest sequence number written. Read from the tail of the log."""
    p = log_path(root)
    try:
        size = p.stat().st_size
    except OSError:
        return 0
    with open(p, "rb") as f:
        f.seek(max(0, size - 65536))
        for line in reversed(f.read().splitlines()):
            try:
                return int(json.loads(line)["seq"])
            except (ValueError, KeyError, TypeError):
                continue
    return 0


def read(root: Path, *, after: int = 0, limit: int | None = None, request: str | None = None,
         involving: str | None = None) -> list[dict]:
    """Messages with ``seq > after`` in order; ``limit`` keeps the newest.
    ``request`` filters by request id / file; ``involving`` (an agent id)
    keeps what that agent sent or was addressed by."""
    entry = get(root, involving) if involving else None
    out = []
    for m in _iter(root, after):
        if request and request not in (m.get("request") or ""):
            continue
        if involving and m.get("from") != involving and not _addressed(m, involving, entry):
            continue
        out.append(m)
    return out[-limit:] if limit else out


def _addressed(m: dict, agent_id: str, entry: dict | None) -> bool:
    to = m.get("to")
    return to == EVERYONE or to == agent_id or bool(entry) and (to == entry.get("name")
                                                                 or to in (entry.get("roles") or []))


def send(root: Path, sender: str, to: str, body: str, *, kind: str = "note", request: str | None = None,
         subject: str | None = None, reply_to: str | None = None) -> dict:
    """Append one message. ``sender`` is an agent id (registered on demand) or
    ``operator``. A reply inherits the request and, with no ``to``, goes back
    to the sender of the message it answers."""
    body = (body or "").strip()
    if not body:
        raise MessageError(proc.INVALID, "empty message")
    if len(body) > MAX_BODY:
        raise MessageError(proc.INVALID, f"message longer than {MAX_BODY} characters: put the detail in the work "
                                         "record or a file and send the reference")
    if kind not in KINDS:
        raise MessageError(proc.INVALID, f"message kind {kind!r} is not one of {', '.join(KINDS)}")
    if sender != OPERATOR:
        ensure(root, sender)
    if reply_to:
        original = next((m for m in _iter(root) if m.get("id") == reply_to), None)
        if original is None:
            raise MessageError(proc.NOT_FOUND, f"no message {reply_to} to reply to")
        to = to or original["from"]
        request = request or original.get("request")
        subject = subject or original.get("subject")
    target = resolve(root, to)
    if target == sender:
        raise MessageError(proc.INVALID, "a message to yourself is not delivered")
    with proc.exclusive(root, _LOCK):
        msg = {"seq": last_seq(root) + 1, "id": "MSG-" + uuid.uuid4().hex[:10], "at": proc.now_iso(),
               "from": sender, "from_name": OPERATOR if sender == OPERATOR else display(root, sender),
               "to": target, "to_name": display(root, target), "kind": kind, "request": request or None,
               "subject": (subject or "").strip()[:120] or None, "body": body, "reply_to": reply_to or None}
        with open(log_path(root), "a", encoding="utf-8") as f:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
    return msg


def unread(root: Path, agent_id: str) -> list[dict]:
    """Messages for the agent that it has not been shown yet (no side effect)."""
    entry = get(root, agent_id)
    if entry is None:
        return []
    return [m for m in _iter(root, int(entry.get("read_seq") or 0))
            if m.get("from") != agent_id and _addressed(m, agent_id, entry)]


def unread_count(root: Path, agent_id: str | None) -> int:
    """Cheap enough for every tool invocation; never raises."""
    try:
        return len(unread(root, agent_id)) if agent_id else 0
    except Exception:
        return 0


def inbox(root: Path, agent_id: str, *, mark_read: bool = True) -> list[dict]:
    """The agent's unread messages; delivered ones are marked read."""
    ensure(root, agent_id)
    msgs = unread(root, agent_id)
    with proc.exclusive(root, _LOCK):
        entry = get(root, agent_id)
        if entry is not None:
            if mark_read and msgs:
                entry["read_seq"] = max(int(entry.get("read_seq") or 0), msgs[-1]["seq"])
            entry["seen_at"] = proc.now_iso()
            proc.atomic_write_text(_entry_path(root, agent_id), json.dumps(entry, indent=2) + "\n")
    return msgs


def wait(root: Path, agent_id: str, *, timeout: float = 300.0, poll: float = 1.0, mark_read: bool = True) -> list[dict]:
    """Block until the agent has unread messages or ``timeout`` passes (then
    an empty list): how an agent waits for the answer to a question."""
    ensure(root, agent_id)
    end = time.monotonic() + max(0.0, timeout)
    while True:
        if unread(root, agent_id):
            return inbox(root, agent_id, mark_read=mark_read)
        if time.monotonic() >= end:
            return []
        time.sleep(poll)


def notice(root: Path, agent_id: str | None) -> str | None:
    """One line telling an agent it has mail, or None. The lifecycle tools
    print it, so an agent that never polls still learns of a message at its
    next claim / own / commit command."""
    n = unread_count(root, agent_id)
    if not n:
        return None
    return f"MESSAGES: {n} unread for {display(root, agent_id)}: read them with scripts/msg.py inbox"


def format_message(m: dict) -> str:
    head = f"#{m['seq']} {m['at']} {m.get('from_name') or m['from']} -> {m.get('to_name') or m['to']} [{m['kind']}]"
    if m.get("request"):
        head += f" ({m['request']})"
    if m.get("subject"):
        head += f" {m['subject']}"
    tail = f"\n    (id {m['id']}" + (f", reply to {m['reply_to']}" if m.get("reply_to") else "") + ")"
    return head + "\n    " + m["body"].replace("\n", "\n    ") + tail
