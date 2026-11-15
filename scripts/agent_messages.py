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
import random
import re
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import agent_process as proc
import devsys_config as cfgmod
import devsys_platform as plat

OPERATOR = "operator"
EVERYONE = "all"
# Closed vocabulary: what a message is, so that readers (and panels) can sort
# them without parsing prose.
KINDS = ("note", "question", "answer", "status", "handoff", "path-request", "alert")
RESERVED_NAMES = {OPERATOR, EVERYONE}
MAX_BODY = 8000
# The first agent on an empty roster is always FIRST_NAME; every later agent
# gets a name drawn at random from the free ones of NAME_POOL (a used-up pool
# continues with Name-2, ...). Names are unique whatever their case.
FIRST_NAME = "Cody"
NAME_POOL = ("Amber", "Birch", "Cedar", "Delta", "Ember", "Flint", "Grove", "Heron", "Indigo", "Juniper",
             "Kestrel", "Linden", "Maple", "Nova", "Onyx", "Pine", "Quartz", "Raven", "Sage", "Tern",
             "Umber", "Vale", "Willow", "Yarrow", "Zephyr")
_NAME_RE = re.compile(r"[a-z][a-z0-9-]{1,31}")
AUTOSTART_ENV = "DEVSYS_COORDINATION_AUTOSTART"     # "0" switches the automatic panels off (tests, CI)
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


def _free_name(taken: set[str], first: bool) -> str:
    """``taken``: lower-case names in use. The first agent is FIRST_NAME; the
    others draw at random."""
    if first and FIRST_NAME.lower() not in taken:
        return FIRST_NAME
    n = 1
    while True:
        free = [name for name in ((base if n == 1 else f"{base}-{n}") for base in NAME_POOL)
                if name.lower() not in taken]
        if free:
            return random.choice(free)
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
        name = name.strip()
        if not _NAME_RE.fullmatch(name.lower()) or name.lower() in RESERVED_NAMES:
            raise MessageError(proc.INVALID, f"agent name {name!r} is not usable (letters, digits, '-'; "
                                             f"2-32 characters; not {', '.join(sorted(RESERVED_NAMES))})")
    with proc.exclusive(root, _LOCK):
        others = [e for e in roster(root) if e["agent_id"] != agent_id]
        entry = get(root, agent_id)
        taken = {e["name"].lower() for e in others}
        if name and name.lower() in taken:
            holder = next(e for e in others if e["name"].lower() == name.lower())
            raise MessageError(proc.CONFLICT, f"agent name {name!r} is held by {holder['agent_id']}")
        now = proc.now_iso()
        created = entry is None
        if entry is None:
            entry = {"agent_id": agent_id, "name": name or _free_name(taken | RESERVED_NAMES, first=not others),
                     "profile": None,
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
    # An agent is here: the panels start with the first one (COORD-011), and
    # whoever watches learns of the change at once (COORD-010).
    station = ensure_station(root)
    if created:
        entry = {**entry, "_new": True, "_station": station}
    publish(root, {"event": "roster", "agent": agent_id})
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
        if w == e["agent_id"] or w.lower() == e["name"].lower():
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
    return to == EVERYONE or to == agent_id or bool(entry) and (
        str(to).lower() == str(entry.get("name")).lower() or to in (entry.get("roles") or []))


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
    # The log is the record; the hub only wakes whoever waits (COORD-010).
    publish(root, {"event": "message", "seq": msg["seq"], "from": sender, "to": target, "kind": kind})
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
    sub = subscribe(root)                      # push: the hub wakes us; without a hub, poll
    try:
        while True:
            if unread(root, agent_id):
                return inbox(root, agent_id, mark_read=mark_read)
            left = end - time.monotonic()
            if left <= 0:
                return []
            if sub is None:
                time.sleep(min(poll, left))
                sub = subscribe(root) if int(left) % 5 == 0 else None      # a hub may have started meanwhile
            elif next_event(sub, min(left, 30.0)) is False:
                sub = None                     # the hub went away: fall back to polling
    finally:
        if sub is not None:
            sub.close()


# ------------------------------------------------------- station and hub (push)
#
# The station (tools/panel/station.py) is one background process per clone:
# it runs the hub (a loopback TCP socket, line-delimited JSON, a token) and
# the panels. Senders tell the hub "something changed"; whoever waits on the
# hub (msg.py wait / listen, the web panel's event stream, the dashboard) is
# woken at once and reads the log. The log stays the only record, so a
# missing hub costs promptness, never a message.

def station_file(root: Path) -> Path:
    return coordination_dir(root) / "station.json"


def station_info(root: Path) -> dict | None:
    """The running station's record (hub address and token, panel
    addresses), or None when no station is verified running."""
    try:
        info = json.loads(station_file(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(info, dict) or plat.verify_session(info.get("process"))[0] == plat.DEAD:
        return None
    return info


def autostart_enabled(root: Path) -> bool:
    return os.environ.get(AUTOSTART_ENV, "1") != "0" and bool(cfgmod.get("coordination.autostart", root, True))


def ensure_station(root: Path, wait_seconds: float = 6.0) -> dict | None:
    """Start the station when none runs and autostart is on (the first agent
    starts the panels). Returns its record, with ``started`` True when this
    call started it. Never raises: coordination must not fail a lifecycle command."""
    try:
        info = station_info(root)
        if info is not None or not autostart_enabled(root):
            return info
        script = Path(__file__).resolve().parent.parent / "tools" / "panel" / "station.py"
        if not script.is_file():
            return None
        main = proc.main_root(root)
        with open(coordination_dir(root) / "station.log", "ab") as log:
            subprocess.Popen([sys.executable, str(script), "--root", str(main)], cwd=str(main),
                             stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                             env={**os.environ, "AGENT_PROCESS_ROOT": str(main)}, **plat.group_popen_kwargs())
        end = time.monotonic() + wait_seconds
        while time.monotonic() < end:
            info = station_info(root)
            if info is not None and info.get("ready"):
                return {**info, "started": True}
            time.sleep(0.1)
        return None
    except Exception:                                        # noqa: BLE001
        return None


def panel_lines(info: dict | None) -> list[str]:
    """What to tell a person about where the panels are."""
    if not info:
        return ["PANELS not running (start them: python3 tools/panel/station.py, or python3 tools/panel/web_panel.py)"]
    out = [f"PANEL web        {info.get('web') or 'not started: ' + str(info.get('web_error') or 'disabled')}"]
    qt = info.get("qt") or {}
    out.append("PANEL control    " + (f"window open (pid {qt['pid']})" if qt.get("pid") else
                                     f"not started: {qt.get('reason') or 'disabled'}"))
    out.append("PANEL dashboard  python3 tools/panel/dash.py   (run it in a terminal of your own)")
    hub = info.get("hub") or {}
    out.append(f"HUB              {hub.get('host')}:{hub.get('port')} (push delivery; pid {(info.get('process') or {}).get('pid')})")
    return out


def _connect(root: Path, mode: str, timeout: float = 0.5):
    info = station_info(root)
    hub = (info or {}).get("hub") or {}
    if not hub.get("port"):
        return None
    try:
        s = socket.create_connection((hub.get("host") or "127.0.0.1", int(hub["port"])), timeout=timeout)
        s.sendall((json.dumps({"token": hub.get("token"), "mode": mode}) + "\n").encode("utf-8"))
        return s
    except OSError:
        return None


def publish(root: Path, event: dict) -> bool:
    """Tell the hub that something changed. Best effort, never raises."""
    try:
        s = _connect(root, "pub")
        if s is None:
            return False
        with s:
            s.sendall((json.dumps(event) + "\n").encode("utf-8"))
        return True
    except Exception:                                        # noqa: BLE001
        return False


def subscribe(root: Path):
    """A socket on which the hub announces every change, or None without a hub."""
    return _connect(root, "sub")


def next_event(sub, timeout: float):
    """Wait for the hub's next announcement: the event (a dict), None when
    ``timeout`` passed quietly, False when the connection is gone."""
    sub.settimeout(max(0.05, timeout))
    buf = b""
    try:
        while not buf.endswith(b"\n"):
            chunk = sub.recv(4096)
            if not chunk:
                return False
            buf += chunk
    except socket.timeout:
        return None
    except OSError:
        return False
    try:
        return json.loads(buf.decode("utf-8", "replace").splitlines()[-1])
    except ValueError:
        return {}


def follow(root: Path, on_event, stop) -> None:
    """Call ``on_event(event)`` for every hub announcement until ``stop``
    (a threading.Event) is set; reconnects when the hub restarts. For the
    panels' background threads."""
    while not stop.is_set():
        sub = subscribe(root)
        if sub is None:
            stop.wait(3.0)
            continue
        with sub:
            while not stop.is_set():
                ev = next_event(sub, 5.0)
                if ev is False:
                    break
                if ev is not None:
                    on_event(ev)


def present(root: Path, idle_minutes: float | None = None) -> list[dict]:
    """The agents that are here now: owner process verified ALIVE, or, where
    that cannot be verified, seen within ``coordination.idle_minutes``. The
    station ends when this is empty (COORD-011)."""
    if idle_minutes is None:
        idle_minutes = float(cfgmod.get("coordination.idle_minutes", root, 30))
    out = []
    now = datetime.now(timezone.utc)
    for e in roster(root):
        verdict = liveness(e)[0]
        if verdict == plat.ALIVE:
            out.append(e)
        elif verdict == plat.UNKNOWN:
            try:
                age = (now - datetime.fromisoformat(e.get("seen_at") or "")).total_seconds()
            except ValueError:
                continue
            if age <= idle_minutes * 60:
                out.append(e)
    return out


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
