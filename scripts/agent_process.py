#!/usr/bin/env python3
"""Concurrency-safe primitives for the request/work process tooling.

ADR-000090, REQ-DEVOPS-003/014/016/025/038 (WORK-000167).

Several AI agents may run the process scripts at the same time — in the same
working tree, or in separate git worktrees of one clone. Every mutation of
shared process state goes through the primitives in this module:

* ``exclusive(root, name)`` — inter-process critical section (``fcntl.flock``
  on a lock file in the shared runtime directory). Held only for short
  read-check-write sequences, never while an agent "works".
* ``allocate_id(...)`` — monotonic, globally unique REQ/WORK/VER/ADR ids: the
  maximum of a persistent counter and a scan of *every* request/work/evidence/
  decision store, incremented under the allocator lock.
* claims — one JSON ownership record per request
  (``.agent/state/claims/request-<file>.lock``): agent, token, claim time,
  heartbeat, state, owned paths, id tokens, commits, recovery history.
* ``move_request(...)`` — no-clobber state transitions (link + unlink).
* ``commit_request(...)`` — commits exactly the paths a request owns, built in
  a private git index and published with a compare-and-swap on HEAD, so the
  shared index and other agents' changes can never be swept in.

Shared state lives under ``<main worktree>/.agent/state`` (resolved through
``git rev-parse --git-common-dir``), so every worktree of a clone sees the same
claims, counters and locks. ``AGENT_PROCESS_ROOT`` overrides the repository
root (used by the tests).

Two roots (docs/process/worker-orchestrator.md "Installation as a project
subdirectory"): ``root`` is the development system's directory (its records,
configuration and state); ``checkout_root(root)`` is the top of the git work
tree it lives in. *Repository paths* -- claim registrations, commit paths,
fingerprints, everything handed to git -- are relative to the checkout. When
the system is the project root (``paths.project_root`` "."), both are the same
directory; installed as ``<project>/<dir>/`` they differ by ``system_prefix``.
"""
from __future__ import annotations

import contextlib
from typing import Optional
import json
import os
import re
import socket
import stat
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path, PurePosixPath

SCRIPTS_DIR = Path(__file__).resolve().parent
import sys as _sys  # noqa: E402
_sys.path.insert(0, str(SCRIPTS_DIR))
import devsys_config as cfgmod  # noqa: E402  (framework constants + project configuration, ADR-000199)
import devsys_platform as plat  # noqa: E402  (locks, links, process identity)

REQUEST_STATES = cfgmod.REQUEST_STATES
TRANSITION_TARGETS = ("completed", "rejected", "needs-review")
STALE_HOURS_DEFAULT = float(cfgmod.DEFAULTS["claims"]["stale_hours"])

# Exit / error codes shared by the CLIs (defined once in devsys_config).
OK = cfgmod.OK
CONFLICT = cfgmod.CONFLICT            # another agent owns it (active)
STALE = cfgmod.STALE                  # owner looks stale / orphaned: explicit recovery required
NOT_OWNER = cfgmod.NOT_OWNER          # caller does not own the request or path
BAD_STATE = cfgmod.BAD_STATE          # request is not in a state that allows the operation
NOT_FOUND = cfgmod.NOT_FOUND
INVALID = cfgmod.INVALID
COLLISION = cfgmod.COLLISION          # id / file collision at creation time: nothing overwritten
BUSY = cfgmod.BUSY                    # an inter-process lock was not obtained in time: retry
GATE_REQUIRED = cfgmod.GATE_REQUIRED  # completion needs a passing, current gate record

# Liveness verdicts of a claim (claim_liveness()).
ACTIVE, ALIVE, DEAD, STALE_V, UNKNOWN_V = "ACTIVE", "ALIVE", "DEAD", "STALE", "UNKNOWN"


class ProcessError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- roots

def repo_root() -> Path:
    return cfgmod.repo_root()


def stale_hours(root: Path | None = None) -> float:
    return float(cfgmod.get("claims.stale_hours", root, STALE_HOURS_DEFAULT))


def git(root: Path, *args: str, env: dict | None = None, check: bool = True,
        input: str | None = None) -> str:
    """Run git in the checkout ``root`` belongs to, so pathspecs are
    repository paths (as git itself prints them)."""
    r = subprocess.run(["git", *args], cwd=str(checkout_root(root)), capture_output=True, text=True,
                       env=env, input=input)
    if check and r.returncode != 0:
        raise ProcessError(INVALID, f"git {' '.join(args)} failed ({r.returncode}): {r.stderr.strip()}")
    return r.stdout


_TOPLEVEL: dict[str, Path] = {}


def _toplevel(path: Path) -> Path | None:
    """``git rev-parse --show-toplevel`` of ``path`` (cached; None outside git)."""
    key = str(path)
    if key not in _TOPLEVEL:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=key, capture_output=True, text=True)
        if r.returncode != 0 or not r.stdout.strip():
            return None
        _TOPLEVEL[key] = Path(r.stdout.strip()).resolve()
    return _TOPLEVEL[key]


def checkout_root(root: Path) -> Path:
    """The top of the git work tree whose paths the system's records use.

    ``root`` itself when the system *is* the project (``paths.project_root``
    "."; also every synthetic tree the tests nest under an ignored directory).
    Installed as a project subdirectory (``paths.project_root`` ".."), the top
    of the work tree holding ``root``: the project checkout, or the request's
    worktree of it. Outside a git work tree, ``root``."""
    resolved = Path(root).resolve()
    if cfgmod.project_root(root) == resolved:
        return Path(root)
    top = _toplevel(resolved)
    if top is not None and (top == resolved or top in resolved.parents):
        return top
    return Path(root)


def system_prefix(root: Path) -> str:
    """The system directory as a repository path: "" when it is the checkout
    top, e.g. ".devsys" when installed as ``<project>/.devsys/``."""
    top, resolved = Path(checkout_root(root)).resolve(), Path(root).resolve()
    return "" if top == resolved else resolved.relative_to(top).as_posix()


def repo_path(root: Path, system_rel: str) -> str:
    """Repository path of a path given relative to the system directory
    (``evidence/gates/W.json`` -> ``.devsys/evidence/gates/W.json``)."""
    prefix = system_prefix(root)
    return f"{prefix}/{system_rel}" if prefix else system_rel


def system_relative(root: Path, repository_path: str) -> str:
    """The inverse of ``repo_path``: how a repository path is spelled relative
    to the system directory, the form the configuration's globs use
    (``src/a.c`` -> ``../src/a.c`` in a ``.devsys`` installation). Identity
    when the system is the checkout top."""
    prefix = system_prefix(root)
    if not prefix:
        return repository_path
    if repository_path == prefix or repository_path.startswith(prefix + "/"):
        return repository_path[len(prefix) + 1:]
    return PurePosixPath(os.path.relpath(repository_path, prefix)).as_posix()


def main_checkout(root: Path) -> Path:
    """Top of the main worktree of the checkout ``root`` belongs to (shared by
    all linked worktrees of a clone).

    ``git rev-parse --git-common-dir`` is resolved against the checkout so it
    works without ``--path-format`` (git < 2.31): silently falling back to a
    linked worktree's own root would give it a private ``.agent/state`` and
    switch the shared claims, counters and locks off (CI_CD_ROADMAP COR-21).
    Outside a git work tree the checkout itself is used."""
    top = checkout_root(root)
    r = subprocess.run(["git", "rev-parse", "--show-toplevel", "--git-common-dir"], cwd=str(top),
                       capture_output=True, text=True)
    lines = r.stdout.strip().splitlines()
    if r.returncode != 0 or len(lines) < 2:
        return top
    # A root nested inside another work tree (a synthetic tree under an
    # ignored directory) is a tree of its own, not that repository's worktree.
    if Path(lines[0]).resolve() != Path(top).resolve():
        return top
    common = Path(lines[1])
    if not common.is_absolute():
        common = (Path(top) / common)
    common = common.resolve()
    return common.parent if common.name == ".git" else Path(top)


def system_dir_in(root: Path, checkout: Path) -> Path:
    """This system's directory inside ``checkout`` (the project checkout or a
    request's worktree of it)."""
    prefix = system_prefix(root)
    return Path(checkout) / prefix if prefix else Path(checkout)


def main_root(root: Path) -> Path:
    """The system directory in the main worktree: where the requests, the
    claims, the counters and the locks live for every worktree of a clone."""
    prefix = system_prefix(root)
    return main_checkout(root) / prefix if prefix else main_checkout(root)


def state_dir(root: Path) -> Path:
    return main_root(root) / ".agent" / "state"


def runtime_dir(root: Path) -> Path:
    d = state_dir(root) / "runtime"
    d.mkdir(parents=True, exist_ok=True)
    return d


def claims_dir(root: Path) -> Path:
    d = state_dir(root) / "claims"
    d.mkdir(parents=True, exist_ok=True)
    return d


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def local_now() -> datetime:
    """The authoritative wall-clock time of the machine running the tooling,
    in its configured local timezone (``TZ`` / /etc/localtime)."""
    return datetime.now().astimezone()


def request_date(now: Optional[datetime] = None) -> dict:
    """System metadata for a request created now (REQ-DEVOPS-045): the date
    that names the request series (``REQ-CHAT-YYYYMMDD``) and the file, taken
    from the local system clock, never from the request text or its author.

    Returns ``{"date": "2026-09-22", "yyyymmdd": "20260922",
    "iso": "2026-09-22T21:48:21-04:00", "utc": "2026-09-23T01:48:21+00:00",
    "timezone": "EDT (UTC-04:00)"}``.
    """
    now = (now or local_now())
    if now.tzinfo is None:
        now = now.astimezone()
    off = now.utcoffset() or timedelta(0)
    sign = "+" if off >= timedelta(0) else "-"
    off = abs(off)
    hh, rem = divmod(int(off.total_seconds()), 3600)
    mm = rem // 60
    return {
        "date": now.strftime("%Y-%m-%d"),
        "yyyymmdd": now.strftime("%Y%m%d"),
        "iso": now.isoformat(timespec="seconds"),
        "utc": now.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "timezone": f"{now.tzname() or 'local'} (UTC{sign}{hh:02d}:{mm:02d})",
    }


# ------------------------------------------------------------------ atomic primitives

@contextlib.contextmanager
def exclusive(root: Path, name: str, timeout: float | None = None):
    """Blocking inter-process critical section keyed by ``name``.

    A timeout is BUSY (retryable), never CONFLICT: CONFLICT means "owned by
    another active agent" (CI_CD_ROADMAP COR-17)."""
    if timeout is None:
        timeout = float(cfgmod.get("claims.lock_timeout_seconds", root, 60.0))
    lock_dir = runtime_dir(root) / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_dir / f"{name}.lock"), os.O_CREAT | os.O_RDWR, 0o644)
    deadline = time.monotonic() + timeout
    locked = False
    try:
        while True:
            try:
                plat.lock(fd)
                locked = True
                break
            except plat.WouldBlock:
                if time.monotonic() > deadline:
                    raise ProcessError(BUSY, f"timed out after {timeout:.0f}s waiting for process lock '{name}' "
                                             "(another process holds it); retry")
                time.sleep(0.01)
        yield
    finally:
        try:
            if locked:
                plat.unlock(fd)
        finally:
            os.close(fd)


def atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def create_exclusive_text(path: Path, text: str) -> None:
    """Create ``path`` with ``text``; raises FileExistsError, never overwrites."""
    fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())


def move_no_clobber(src: Path, dst: Path) -> None:
    """Atomic-per-file move that fails instead of overwriting ``dst``."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    plat.link_no_clobber(src, dst)  # FileExistsError if dst exists
    with contextlib.suppress(FileNotFoundError):  # the rename fallback already removed src
        os.unlink(src)


# ------------------------------------------------------------------------- ids

REQ_ID_RE = re.compile(r"\b(REQ-[A-Z]+-\d{8})-(\d{3,})\b", re.IGNORECASE)
SEQ_KINDS = {  # kind -> (directories to scan, manifest files)
    "WORK": (("work",), ("work/work.txt",)),
    "VER": (("evidence",), ("evidence/evidence.txt",)),
    "ADR": (("docs/decisions",), ("docs/decisions/decisions.txt",)),
}


def _roots(root: Path) -> list[Path]:
    out = []
    for r in (root, main_root(root)):
        if r not in out:
            out.append(r)
    return out


def _request_identity_ids(path: Path) -> list[str]:
    """Request ids that identify this file (its name and its own id header)."""
    ids = [f"{a.upper()}-{b}" for a, b in REQ_ID_RE.findall(path.name)]
    try:
        head = path.read_text(encoding="utf-8", errors="ignore")[:4000]
    except OSError:
        head = ""
    for line in head.splitlines():
        if "Request ID" in line or '"request_id"' in line:
            ids.extend(f"{a.upper()}-{b}" for a, b in REQ_ID_RE.findall(line))
    return ids


def request_files(root: Path) -> list[Path]:
    out = []
    for base in _roots(root):
        req = base / "requests"
        if req.is_dir():
            out.extend(p for p in req.rglob("*") if p.is_file() and p.suffix in (".md", ".json"))
    return out


def scan_request_series_max(root: Path, series: str) -> int:
    best = 0
    for p in request_files(root):
        for rid in _request_identity_ids(p):
            s, _, n = rid.rpartition("-")
            if s == series.upper():
                best = max(best, int(n))
    return best


def find_request_files_by_id(root: Path, request_id: str) -> list[Path]:
    rid = request_id.upper()
    return [p for p in request_files(root) if rid in _request_identity_ids(p)]


def scan_seq_max(root: Path, kind: str) -> int:
    dirs, manifests = SEQ_KINDS[kind]
    rx = re.compile(rf"^{kind}-(\d{{6}})")
    best = 0
    for base in _roots(root):
        for d in dirs:
            if (base / d).is_dir():
                for p in (base / d).rglob(f"{kind}-*"):
                    m = rx.match(p.name)
                    if m:
                        best = max(best, int(m.group(1)))
        for mf in manifests:
            f = base / mf
            if f.is_file():
                for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
                    m = rx.match(line.strip())
                    if m:
                        best = max(best, int(m.group(1)))
    return best


_REF_SCAN_VERSION = 2


def _commit_id_summary(root: Path, commit: str) -> dict:
    """Highest WORK/VER/ADR number and per-series request number recorded in
    one commit's record stores and id manifests."""
    summary: dict = {"REQ": {}}
    stores = [cfgmod.LAYOUT[k] for k in ("requests", "work", "evidence", "decisions")]
    ls = subprocess.run(["git", "ls-tree", "-r", "--name-only", commit, "--", *stores], cwd=str(root),
                        capture_output=True, text=True)
    lines = [PurePosixPath(n).name for n in ls.stdout.splitlines()]
    # A request id is issued by the request store only: ids a manifest row or
    # a record merely cites (history, another project) are not this
    # repository's series.
    request_names = [PurePosixPath(n).name for n in ls.stdout.splitlines()
                     if PurePosixPath(n).parts[:1] == (cfgmod.LAYOUT["requests"],)]
    for mf in (cfgmod.LAYOUT["work_manifest"], cfgmod.LAYOUT["evidence_manifest"], cfgmod.LAYOUT["decisions_manifest"]):
        # "./": relative to the system directory (the cwd), wherever it sits in the checkout.
        show = subprocess.run(["git", "show", f"{commit}:./{mf}"], cwd=str(root), capture_output=True, text=True)
        if show.returncode == 0:
            lines.extend(l.strip() for l in show.stdout.splitlines())
    for kind in SEQ_KINDS:
        rx = re.compile(rf"^{kind}-(\d{{6}})")
        summary[kind] = max((int(m.group(1)) for m in map(rx.match, lines) if m), default=0)
    for name in request_names:
        for a, b in REQ_ID_RE.findall(name):
            series = a.upper()
            summary["REQ"][series] = max(summary["REQ"].get(series, 0), int(b))
    return summary


def _ref_max(root: Path, series: str) -> int:
    """Highest number of ``series`` in any local or remote-tracking ref, so an
    id committed on a fetched but unmerged branch is never reissued (clones
    that fetch each other; CI_CD_ROADMAP CON-03). Commits are immutable, so
    each commit's summary is cached in the runtime directory. 0 outside git
    or when ids.scan_git_refs is off. Called under the id-allocator lock."""
    if not cfgmod.get("ids.scan_git_refs", root, True):
        return 0
    r = subprocess.run(["git", "for-each-ref", "--format=%(objectname)", "refs/heads", "refs/remotes"],
                       cwd=str(root), capture_output=True, text=True)
    if r.returncode != 0:
        return 0
    cache_path = runtime_dir(root) / "ref-id-cache.json"
    try:
        stored = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stored = {}
    # Summaries of another scan version are recomputed.
    cache = stored.get("commits", {}) if stored.get("version") == _REF_SCAN_VERSION else {}
    changed = not cache and bool(stored)
    best = 0
    for commit in sorted(set(r.stdout.split())):
        if commit not in cache:
            cache[commit] = _commit_id_summary(root, commit)
            changed = True
        s = cache[commit]
        best = max(best, int(s.get(series, 0)) if series in SEQ_KINDS else int(s["REQ"].get(series.upper(), 0)))
    if changed:
        atomic_write_text(cache_path, json.dumps({"version": _REF_SCAN_VERSION, "commits": cache}) + "\n")
    return best


def allocate_id(root: Path, series: str, agent_id: str = "") -> str:
    """Allocate the next id atomically.

    ``series`` is ``WORK``/``VER``/``ADR`` (``WORK-000167``) or a request series
    such as ``REQ-CHAT-20260914`` (``REQ-CHAT-20260914-004``). The result is
    greater than every id found in any store *and* than every id this
    allocator has ever issued (persistent counter), so archived or deleted
    records are never reused.
    """
    with exclusive(root, "id-allocator"):
        if series in SEQ_KINDS:
            scanned, width = scan_seq_max(root, series), 6
        else:
            scanned, width = scan_request_series_max(root, series), 3
        scanned = max(scanned, _ref_max(root, series))
        counters_path = runtime_dir(root) / "id-counters.json"
        try:
            counters = json.loads(counters_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            counters = {}
        value = max(int(counters.get(series, 0)), scanned) + 1
        counters[series] = value
        atomic_write_text(counters_path, json.dumps(counters, indent=2, sort_keys=True) + "\n")
        new_id = f"{series}-{value:0{width}d}"
        with open(runtime_dir(root) / "id-allocations.log", "a", encoding="utf-8") as log:
            log.write(json.dumps({"id": new_id, "agent_id": agent_id, "at": now_iso(), "pid": os.getpid()}) + "\n")
        return new_id


def create_request_files(root: Path, request_id: str, md_name: str, md_text: str, sidecar: dict) -> Path:
    """Create a new inbox request (markdown + JSON sidecar) without ever
    overwriting or reusing anything; raises ProcessError(COLLISION)."""
    existing = find_request_files_by_id(root, request_id)
    if existing:
        raise ProcessError(COLLISION, f"request id {request_id} is already held by "
                           + ", ".join(str(p.relative_to(main_root(root))) for p in existing)
                           + "; nothing was written (no renumbering, no overwrite)")
    inbox = root / "requests" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    md = inbox / md_name
    js = md.with_suffix(".json")
    created: list[Path] = []
    try:
        create_exclusive_text(md, md_text)
        created.append(md)
        create_exclusive_text(js, json.dumps(sidecar, indent=2) + "\n")
        created.append(js)
    except FileExistsError as e:
        for c in created:
            c.unlink()
        raise ProcessError(COLLISION, f"refusing to overwrite existing file {e.filename}; "
                           f"request {request_id} was not created")
    return md


# ---------------------------------------------------------------------- requests

def locate_request(root: Path, name: str) -> list[tuple[str, Path]]:
    base = main_root(root) / "requests"
    return [(s, base / s / name) for s in REQUEST_STATES if (base / s / name).is_file()]


def request_id_of(path: Path) -> str:
    ids = _request_identity_ids(path)
    return ids[0] if ids else path.stem


def rel(root: Path, path: Path) -> str:
    """Repository path of ``path``: relative to the checkout that holds it,
    this one (a work or evidence record written in a request's worktree) or
    the main one (request files, which always move in the main worktree)."""
    out = ""
    for base in (checkout_root(root), main_checkout(root)):
        out = PurePosixPath(os.path.relpath(path, base)).as_posix()
        if not out.startswith("../"):
            return out
    return out


def _set_request_status(md: Path, state: str, extra: dict | None = None) -> None:
    if md.is_file():
        text = md.read_text(encoding="utf-8")
        text = re.sub(r"^\*\*Status:\*\* .*$", f"**Status:** {state}", text, count=1, flags=re.M)
        atomic_write_text(md, text)
    js = md.with_suffix(".json")
    if js.is_file():
        try:
            data = json.loads(js.read_text(encoding="utf-8"))
        except ValueError:
            return
        data["status"] = state
        data.update(extra or {})
        atomic_write_text(js, json.dumps(data, indent=2) + "\n")


def move_request(root: Path, name: str, from_state: str, to_state: str) -> Path:
    locs = locate_request(root, name)
    if [s for s, _ in locs] != [from_state]:
        raise ProcessError(BAD_STATE, f"request {name} is in {[s for s, _ in locs] or 'no state'}; "
                           f"expected exactly '{from_state}' for {from_state} -> {to_state}")
    src = locs[0][1]
    dst = src.parent.parent / to_state / name
    try:
        move_no_clobber(src, dst)
    except FileExistsError:
        raise ProcessError(BAD_STATE, f"{rel(root, dst)} already exists; refusing to overwrite")
    s_js, d_js = src.with_suffix(".json"), dst.with_suffix(".json")
    if s_js.is_file():
        try:
            move_no_clobber(s_js, d_js)
        except FileExistsError:
            raise ProcessError(BAD_STATE, f"{rel(root, d_js)} already exists; refusing to overwrite")
    return dst


# ------------------------------------------------------------------------ claims

def lock_path(root: Path, name: str, kind: str = "request") -> Path:
    return claims_dir(root) / f"{kind}-{name.replace('/', '_')}.lock"


def read_claim(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def all_claims(root: Path) -> list[dict]:
    out = []
    for p in sorted(claims_dir(root).glob("*.lock")):
        d = read_claim(p)
        if d is not None:
            d.setdefault("target", p.name.split("-", 1)[1][:-5])
            d.setdefault("kind", p.name.split("-", 1)[0])
            d["_lock"] = str(p)
            out.append(d)
    return out


def heartbeat_age_hours(data: dict) -> float:
    ts = data.get("heartbeat_at") or data.get("claimed_at")
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds() / 3600.0
    except Exception:
        return float("inf")


DEAD_GRACE_MINUTES = float(cfgmod.DEFAULTS["claims"]["dead_grace_minutes"])


def dead_grace_minutes(root: Path | None = None) -> float:
    return float(cfgmod.get("claims.dead_grace_minutes", root or repo_root(), DEAD_GRACE_MINUTES))


def claim_liveness(data: dict, stale_h: float | None = None) -> tuple[str, str]:
    """(verdict, reason) for a claim's owner (CI_CD_ROADMAP CON-02, COR-10):

    * ALIVE   the recorded owner process (``session``, from --owner-pid /
              $AGENT_OWNER_PID) or a running step (``step``, scripts/claim.py --run)
              is verified running on this host and boot;
    * DEAD    the recorded owner process is verified gone (exited, pid reused,
              or an earlier boot) and nothing touched the claim for
              claims.dead_grace_minutes (default 5);
    * ACTIVE  no verifiable process, heartbeat within the stale threshold;
    * STALE   no verifiable process, heartbeat older than the threshold.

    Only DEAD is a *verified* absence; STALE is an inference from silence."""
    stale_h = STALE_HOURS_DEFAULT if stale_h is None else stale_h
    age = heartbeat_age_hours(data)
    step = data.get("step")
    if step:
        v, why = plat.verify_session(step)
        if v == plat.ALIVE:
            return ALIVE, f"step '{step.get('label', '?')}' running ({why})"
    session = data.get("session")
    if session:
        v, why = plat.verify_session(session)
        if v == plat.ALIVE:
            return ALIVE, f"owner {why}"
        if v == plat.DEAD:
            if age * 60.0 >= dead_grace_minutes():
                return DEAD, f"owner {why}"
            return ACTIVE, f"registered owner process gone ({why}) but the claim was touched {age * 60:.0f} min ago"
    if age > stale_h:
        age_txt = "never" if age == float("inf") else f"{age * 60:.0f} min ago"
        return STALE_V, f"no heartbeat since {age_txt} (> {stale_h:g} h) and no verifiable owner process"
    return ACTIVE, f"heartbeat {age * 60:.0f} min ago (owner process not verified)"


def is_stale(data: dict, stale_hours: float = STALE_HOURS_DEFAULT) -> bool:
    """True when the owner is verified DEAD or its heartbeat is STALE."""
    return claim_liveness(data, stale_hours)[0] in (DEAD, STALE_V)


def new_agent_id() -> str:
    return f"agent-{socket.gethostname()}-{time.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"


def validate_agent_id(agent_id: str | None) -> str:
    if not agent_id or not agent_id.strip():
        raise ProcessError(INVALID, "an explicit, session-unique agent id is required "
                           "(--agent-id or AGENT_ID); create one with: scripts/claim.py --new-agent-id")
    agent_id = agent_id.strip()
    if re.fullmatch(r"agent-\d+", agent_id) or any(c.isspace() for c in agent_id) or len(agent_id) > 128:
        raise ProcessError(INVALID, f"agent id '{agent_id}' is not session-unique (uid/pid defaults are shared "
                           "between sessions); use scripts/claim.py --new-agent-id")
    return agent_id


def owner_pid_from_env() -> int | None:
    """$AGENT_OWNER_PID: the long-lived process of the agent session (a
    worker, an engine, an interactive shell) whose death means the session
    is gone. Short-lived tool processes are never recorded as the owner."""
    raw = os.environ.get("AGENT_OWNER_PID", "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else None


def describe_owner(data: dict, stale_hours: float = STALE_HOURS_DEFAULT) -> str:
    verdict, why = claim_liveness(data, stale_hours)
    kind = data.get("kind", "request")
    what = (f"request {data.get('request_id', '?')} ({data.get('target')})" if kind == "request"
            else f"{kind} {data.get('target')}")
    return (f"{what} is owned by agent '{data.get('agent_id')}' — claimed {data.get('claimed_at')}, "
            f"state {data.get('state', '?')}, host {data.get('host', data.get('hostname', '?'))}, "
            f"{len(data.get('files', {}))} owned path(s) [{verdict}: {why}]")


def _save_claim(root: Path, data: dict) -> None:
    clean = {k: v for k, v in data.items() if not k.startswith("_")}
    atomic_write_text(lock_path(root, data["target"], data.get("kind", "request")), json.dumps(clean, indent=2) + "\n")


def _touch(data: dict, owner_pid: int | None = None) -> None:
    data["heartbeat_at"] = now_iso()
    data["host"] = socket.gethostname()
    owner_pid = owner_pid or owner_pid_from_env()
    if owner_pid:
        data["session"] = plat.session_identity(owner_pid)
        data["session_at"] = now_iso()


def _worktree_key(data: dict, root: Path) -> str:
    """The checkout a claim's paths live in: its request worktree, or the main one."""
    return str(Path(data.get("worktree") or main_checkout(root)).resolve())


def _register_request_files(root: Path, data: dict, *paths: Path) -> None:
    for p in paths:
        for q in (p, p.with_suffix(".json")):
            data["files"].setdefault(rel(root, q), {"mode": "exclusive", "adopted": False,
                                                    "registered_at": now_iso(), "request_file": True})


def _history(data: dict, event: str, **fields) -> None:
    data.setdefault("history", []).append({"event": event, "at": now_iso(), **fields})


def _enter_processing(root: Path, data: dict, name: str, state: str, path: Path) -> Path:
    dst = move_request(root, name, state, "processing")
    _set_request_status(dst, "processing")
    _register_request_files(root, data, path, dst)
    data["state"] = "processing"
    data["request_path"] = rel(root, dst)
    data.pop("parked", None)
    return dst


def claim_request(root: Path, name: str, agent_id: str, owner_pid: int | None = None) -> tuple[dict, str]:
    """Claim ``name`` (inbox or needs-review -> processing), idempotently.

    * The owner claiming again completes an interrupted claim (the lock was
      written but the request never moved: CI_CD_ROADMAP COR-06) and resumes
      a request it escalated to needs-review (COR-05).
    * A needs-review request is parked: any agent may take it over; the
      takeover is recorded in the claim history with the previous owner."""
    agent_id = validate_agent_id(agent_id)
    with exclusive(root, "claims"):
        locs = locate_request(root, name)
        if not locs:
            raise ProcessError(NOT_FOUND, f"request {name} not found in any request state")
        if len(locs) > 1:
            raise ProcessError(BAD_STATE, f"request {name} exists in several states {[s for s, _ in locs]}; "
                               "run scripts/claim.py --sweep to diagnose, then resolve manually")
        state, path = locs[0]
        lp = lock_path(root, name)
        existing = read_claim(lp)
        if existing is not None:
            for key, default in (("files", {}), ("tokens", []), ("commits", []), ("history", [])):
                existing.setdefault(key, default)
            existing.setdefault("target", name)
            existing.setdefault("kind", "request")
            existing.setdefault("request_id", request_id_of(path))
            existing.setdefault("claim_token", uuid.uuid4().hex)
            if existing.get("agent_id") == agent_id:
                how = "already-owned"
                if state in ("inbox", "needs-review"):
                    _enter_processing(root, existing, name, state, path)
                    how = "resumed" if state == "needs-review" else "claim-completed"
                    _history(existing, how, agent_id=agent_id, from_state=state)
                else:
                    existing.setdefault("state", state)
                    existing.setdefault("request_path", rel(root, path))
                _touch(existing, owner_pid)
                _save_claim(root, existing)
                return existing, how
            if state == "needs-review" and existing.get("state") == "needs-review":
                _history(existing, "takeover-from-needs-review", previous_agent_id=existing.get("agent_id"),
                         previous_claim_token=existing.get("claim_token"), recovered_by=agent_id)
                existing["agent_id"] = agent_id
                existing["claim_token"] = uuid.uuid4().hex
                existing["claimed_at"] = now_iso()
                existing.pop("session", None)
                existing.pop("step", None)
                _enter_processing(root, existing, name, state, path)
                _touch(existing, owner_pid)
                _save_claim(root, existing)
                _archive_claim(root, existing, "taken-over")
                return existing, "taken-over"
            code = STALE if is_stale(existing, stale_hours(root)) else CONFLICT
            raise ProcessError(code, "ALREADY CLAIMED: " + describe_owner(existing, stale_hours(root)))
        if state == "processing":
            raise ProcessError(STALE, f"request {name} is in processing with no claim (orphaned); "
                               "inspect it, then use scripts/claim.py --recover-request")
        if state not in ("inbox", "needs-review"):
            raise ProcessError(BAD_STATE, f"request {name} is {state}; it cannot be claimed "
                               "(reopen by creating a new request referencing it)")
        data = {
            "kind": "request", "target": name, "request_id": request_id_of(path),
            "agent_id": agent_id, "claim_token": uuid.uuid4().hex, "pid": os.getpid(),
            "host": socket.gethostname(), "claimed_at": now_iso(), "heartbeat_at": now_iso(),
            # The state this claim establishes: if the process dies before the
            # move below, the owner's retry or --sweep completes it.
            "state": "processing", "worktree": None, "files": {}, "tokens": [], "commits": [], "history": [],
        }
        _touch(data, owner_pid)
        create_exclusive_text(lp, json.dumps(data, indent=2) + "\n")
        try:
            _enter_processing(root, data, name, state, path)
        except Exception:
            lp.unlink()
            raise
        _save_claim(root, data)
        return data, "claimed"


def require_owner(root: Path, name: str, agent_id: str, kind: str = "request") -> dict:
    agent_id = validate_agent_id(agent_id)
    data = read_claim(lock_path(root, name, kind))
    if data is None:
        raise ProcessError(NOT_FOUND, f"{kind} {name} has no claim")
    if data.get("agent_id") != agent_id:
        raise ProcessError(NOT_OWNER, f"agent '{agent_id}' does not own it: " + describe_owner(data))
    data.setdefault("target", name)
    data.setdefault("kind", kind)
    for key, default in (("files", {}), ("tokens", []), ("commits", []), ("history", [])):
        data.setdefault(key, default)
    return data


def heartbeat(root: Path, name: str, agent_id: str, owner_pid: int | None = None) -> dict:
    with exclusive(root, "claims"):
        data = require_owner(root, name, agent_id)
        _touch(data, owner_pid)
        _save_claim(root, data)
        return data


class Heartbeat:
    """Refresh a claim's heartbeat from a background thread while a long
    operation runs (CI_CD_ROADMAP CON-01). Failures are ignored: losing the
    claim is detected by the next owner operation, not by the heartbeat."""

    def __init__(self, root: Path, name: str, agent_id: str, interval: float | None = None):
        import threading
        self.root, self.name, self.agent_id = root, name, agent_id
        self.interval = float(interval or min(60.0, float(cfgmod.get("claims.heartbeat_interval_seconds", root, 300))))
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            with contextlib.suppress(Exception):
                heartbeat(self.root, self.name, self.agent_id)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=5)
        return False


def set_step(root: Path, name: str, agent_id: str, label: str | None, pid: int | None = None) -> dict:
    """Record (or clear, label=None) a long-running step of the owner: a
    process whose verified liveness keeps the claim ALIVE (scripts/claim.py --run)."""
    with exclusive(root, "claims"):
        data = require_owner(root, name, agent_id)
        if label is None:
            data.pop("step", None)
        else:
            data["step"] = {**plat.session_identity(pid or os.getpid()), "label": label, "started_at": now_iso()}
        _touch(data)
        _save_claim(root, data)
        return data


def normalize_path(root: Path, p: str) -> str:
    """A repository path: relative paths are taken as repository paths (the
    checkout top, i.e. the project root), absolute ones are made relative to
    the checkout."""
    path = Path(p)
    if path.is_absolute():
        try:
            path = path.resolve().relative_to(Path(checkout_root(root)).resolve())
        except ValueError:
            raise ProcessError(INVALID, f"{p} is outside the repository")
    posix = PurePosixPath(path.as_posix())
    if ".." in posix.parts:
        raise ProcessError(INVALID, f"{p} escapes the repository")
    return str(posix).lstrip("./") if str(posix).startswith("./") else str(posix)


def is_directory_entry(root: Path, relpath: str) -> bool:
    """True for a real directory in the working tree (a symlink is a file to
    git, whatever it points at)."""
    p = checkout_root(root) / relpath
    return p.is_dir() and not p.is_symlink()


def directory_refusal(relpath: str) -> str:
    return (f"{relpath} is a directory. A registration owns exactly one file: its baseline is that file's "
            "content (REQ-DEVOPS-016, REQ-DEVOPS-042, ADR-000197), a directory has none, and nothing under it "
            "would be owned. Register each file before creating or editing it: scripts/claim.py --own <request> "
            f"--paths {relpath}/<file> [...]. A file this request has already written there is adopted "
            "explicitly with --adopt-existing-changes, after checking that it is this request's.")


def path_is_modified(root: Path, relpath: str) -> bool:
    return bool(git(root, "status", "--porcelain=v1", "--untracked-files=all", "--", relpath).strip())


def _write_blob(root: Path, data: bytes) -> str:
    """Store `data` as a git blob and return its id (kept alive by the claim
    record, which names it; `git gc` prunes only unreferenced loose objects
    older than its grace period, and a claim is short-lived by comparison)."""
    r = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=str(checkout_root(root)), input=data,
                       capture_output=True, check=True)
    return r.stdout.decode().strip()


def head_blob(root: Path, relpath: str) -> str:
    """The blob id of `relpath` in HEAD (empty string when HEAD does not have
    it)."""
    r = subprocess.run(["git", "rev-parse", "--verify", "-q", f"HEAD:{relpath}"], cwd=str(checkout_root(root)),
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def baseline_blob(root: Path, relpath: str) -> str:
    """The blob id of `relpath` as it is RIGHT NOW in the working tree (empty
    string for a path that does not exist). This is what a request's changes
    are measured against: everything that file gains from here on is this
    request's delta, and everything already in it is someone else's business
    (REQ-DEVOPS-042, REQ-CHAT-20260920-011 sections 8-13)."""
    path = checkout_root(root) / relpath
    try:
        return _write_blob(root, path.read_bytes())
    except (FileNotFoundError, IsADirectoryError):
        return ""


def snapshot_paths(root: Path, name: str, agent_id: str, paths: list[str] | None = None,
                   rebaseline: bool = False, from_head: bool = False, from_file: str | None = None) -> dict:
    """Record this request's own version of the files it holds.

    Default: "the content of these paths right now is MY work" -- stored as a
    blob, so anything another agent writes into the same file afterwards is
    not swept into this request's commit. Call it when an edit round is
    finished, especially before a long-running build or test.

    ``rebaseline=True`` instead moves the *baseline* to the current content:
    "everything in this file up to here is somebody else's; only what I change
    from now on is mine". Use it after another request commits into a file
    this one also holds.

    ``from_head=True`` moves the baseline to HEAD: "everything this file has
    gained since the last commit is mine". Use it when a path was registered
    after it was already edited.

    ``from_file`` records this request's version from ANOTHER file rather than
    from the working tree, for exactly one path. Use it when the working copy
    of a shared file also holds work that is not this request's and must not
    be touched: prepare your version elsewhere and name it here."""
    with exclusive(root, "claims"):
        data = require_owner(root, name, agent_id)
        selected = [normalize_path(root, p) for p in paths] if paths else sorted(data["files"])
        if from_file is not None and len(selected) != 1:
            raise ProcessError(INVALID, "--from-file records the version of exactly one path; name it with --paths")
        for rp in selected:
            f = data["files"].get(rp)
            if f is None:
                raise ProcessError(NOT_OWNER, f"{rp} is not registered by this request")
            if from_head:
                f["baseline_blob"] = head_blob(root, rp)
                f["baseline_at"] = now_iso()
                f.pop("mine_blob", None)
                f.pop("mine_at", None)
            elif rebaseline:
                f["baseline_blob"] = baseline_blob(root, rp)
                f["baseline_at"] = now_iso()
                f.pop("mine_blob", None)
                f.pop("mine_at", None)
            elif from_file is not None:
                source = Path(from_file)
                if not source.is_absolute():
                    source = checkout_root(root) / source
                if not source.is_file():
                    raise ProcessError(NOT_FOUND, f"{from_file} is not a file")
                f["mine_blob"] = _write_blob(root, source.read_bytes())
                f["mine_at"] = now_iso()
                f["mine_from"] = str(source)
            else:
                f["mine_blob"] = baseline_blob(root, rp)
                f["mine_at"] = now_iso()
        _touch(data)
        _save_claim(root, data)
        return data


def own_paths(root: Path, name: str, agent_id: str, paths: list[str], shared: bool = False,
              adopt: bool = False, concurrent: bool = False) -> dict:
    with exclusive(root, "claims"):
        data = require_owner(root, name, agent_id)
        mine = _worktree_key(data, root)
        others = [c for c in all_claims(root) if c.get("target") != name and _worktree_key(c, root) == mine]
        for raw in paths:
            rp = normalize_path(root, raw)
            # One registration, one file. A directory (or a path spelled as
            # one) would record no baseline and own nothing beneath it; it is
            # refused before anything is recorded, so a call naming several
            # paths registers none of them (ADR-000197).
            if rp in ("", ".") or raw.rstrip().endswith(("/", os.sep)) or is_directory_entry(root, rp):
                raise ProcessError(INVALID, directory_refusal(rp or "."))
            mode = "concurrent" if concurrent else ("shared" if shared else "exclusive")
            for o in others:
                f = (o.get("files") or {}).get(rp)
                if f and not (mode != "exclusive" and f.get("mode") == mode):
                    raise ProcessError(CONFLICT, f"{rp} is already owned ({f.get('mode')}) by " + describe_owner(o))
            if rp not in data["files"]:
                dirty = path_is_modified(root, rp)
                if dirty and mode == "exclusive" and not adopt:
                    raise ProcessError(NOT_OWNER, f"{rp} already has uncommitted changes that no request owns. "
                                       "Register paths before editing them; pass --adopt-existing-changes only "
                                       "if you know those changes are this request's")
                data["files"][rp] = {"mode": mode,
                                     "adopted": bool(dirty and adopt), "registered_at": now_iso(),
                                     # What the file looked like before this
                                     # request touched it. Attribution is the
                                     # delta from here, not a search for this
                                     # request's id inside the text. When the
                                     # request explicitly adopts changes that
                                     # are already in the file, those changes
                                     # are its own, so the baseline is HEAD.
                                     "baseline_blob": (head_blob(root, rp) if (dirty and adopt)
                                                       else baseline_blob(root, rp)),
                                     "baseline_at": now_iso()}
            elif mode != data["files"][rp].get("mode"):
                # Correcting one's own registration is legal while no other
                # request holds the path; changing it under a co-owner is not.
                if any(rp in (o.get("files") or {}) for o in others):
                    raise ProcessError(INVALID, f"{rp} is registered as {data['files'][rp].get('mode')} and another "
                                       "request holds it too; its mode cannot be changed now")
                data["files"][rp]["mode"] = mode
                data["files"][rp]["mode_changed_at"] = now_iso()
                if adopt:
                    data["files"][rp]["baseline_blob"] = head_blob(root, rp)
                    data["files"][rp]["baseline_at"] = now_iso()
        _touch(data)
        _save_claim(root, data)
        return data


def add_tokens(root: Path, name: str, agent_id: str, tokens: list[str]) -> dict:
    with exclusive(root, "claims"):
        data = require_owner(root, name, agent_id)
        for t in tokens:
            if t and t not in data["tokens"]:
                data["tokens"].append(t)
        _touch(data)
        _save_claim(root, data)
        return data


# ------------------------------------------------------------ completion evidence
_REQUEST_HEADER = re.compile(r"^\s*\*\*Request:\*\*\s*(.+)$", re.MULTILINE)


def request_type(root: Path, name: str) -> str:
    for _state, md in locate_request(root, name):
        try:
            return str(json.loads(md.with_suffix(".json").read_text(encoding="utf-8")).get("type") or "").upper()
        except (OSError, ValueError):
            text = md.read_text(encoding="utf-8", errors="ignore")
            m = re.search(r"^\*\*Type:\*\*\s*(\S+)", text, re.MULTILINE)
            return m.group(1).upper() if m else ""
    return ""


def linked_work_records(root: Path, request_file: str) -> list[Path]:
    """Work records whose ``**Request:**`` header names this request file
    (in any request state directory; a request moves, its name does not)."""
    name = Path(request_file).name
    out = []
    work = cfgmod.path(root, "work")
    for state in cfgmod.WORK_STATES:
        for f in sorted((work / state).glob("WORK-*.md")):
            m = _REQUEST_HEADER.search(f.read_text(encoding="utf-8", errors="ignore"))
            if m and name in m.group(1):
                out.append(f)
    return out


def gate_record_path(root: Path, work_id: str) -> Path:
    return cfgmod.path(root, "gates") / f"{work_id}.json"


def read_gate_record(root: Path, work_id: str) -> dict | None:
    """The latest persisted completion-gate result of ``work_id``."""
    try:
        data = json.loads(gate_record_path(root, work_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    runs = data.get("runs") or []
    return runs[-1] if runs else None


def _no_test_path(relpath: str, patterns) -> bool:
    import fnmatch
    for pattern in patterns:
        if fnmatch.fnmatchcase(relpath, pattern):
            return True
        if pattern.endswith("/**") and (relpath == pattern[:-3] or relpath.startswith(pattern[:-2])):
            return True
    return False


def request_code_paths(root: Path, claim: dict) -> list[str]:
    """The request's registered paths that carry code (documentation and
    process records excluded, tests.no_test_patterns)."""
    patterns = cfgmod.get("tests.no_test_patterns", root, [])
    # A pattern matches the repository path or, for the system's own records
    # in a subdirectory installation, the path relative to the system directory.
    return sorted(p for p in (claim.get("files") or {})
                  if not (_no_test_path(p, patterns) or _no_test_path(system_relative(root, p), patterns)))


def request_fingerprint(root: Path, claim: dict) -> tuple[str, list[str]]:
    """sha256 over the content of the request's registered code paths: which
    code a test run or a gate exercised (REQ-DEVOPS-046)."""
    import hashlib
    paths = request_code_paths(root, claim)
    top = checkout_root(root)
    digest = hashlib.sha256()
    for relpath in paths:
        digest.update(relpath.encode() + b"\0")
        target = top / relpath
        if target.is_file():
            digest.update(hashlib.sha256(target.read_bytes()).hexdigest().encode())
        elif target.is_dir():
            for child in sorted(p for p in target.rglob("*") if p.is_file()):
                digest.update(child.relative_to(top).as_posix().encode())
                digest.update(hashlib.sha256(child.read_bytes()).hexdigest().encode())
        else:
            digest.update(b"<missing>")
        digest.update(b"\n")
    return digest.hexdigest(), paths


def tree_fingerprint(root: Path, commit: str, paths: list[str]) -> str:
    """request_fingerprint() of ``paths`` as they are in ``commit`` (not the
    working tree): lets anyone check, from the repository alone, that a commit
    carries the code its gate record verified (scripts/devsys.py verify-commit)."""
    import hashlib
    digest = hashlib.sha256()
    for relpath in paths:
        digest.update(relpath.encode() + b"\0")
        blob = subprocess.run(["git", "cat-file", "blob", f"{commit}:{relpath}"], cwd=str(checkout_root(root)),
                              capture_output=True)
        if blob.returncode == 0:
            digest.update(hashlib.sha256(blob.stdout).hexdigest().encode())
        else:
            digest.update(b"<missing>")
        digest.update(b"\n")
    return digest.hexdigest()


def completion_status(root: Path, claim: dict) -> tuple[bool, str, list[dict]]:
    """Is the request ready to be completed? Every work record linked to it
    needs a persisted gate record (scripts/completion_gate.py --record) whose
    verdict is PASS and whose request fingerprint equals the current one
    (CI_CD_ROADMAP COR-01, COR-09, AUD-01). Returns (ready, reason, gates)."""
    works = linked_work_records(root, claim.get("target", ""))
    if not works:
        return False, ("no work record links this request (a work record's **Request:** header must name "
                       f"{claim.get('target')}; create one with scripts/work.py create)"), []
    fingerprint, _ = request_fingerprint(root, claim)
    gates = []
    for w in works:
        wid = re.match(r"WORK-\d{6}", w.name).group(0)
        g = read_gate_record(root, wid)
        if g is None:
            return False, f"{wid} has no gate record: run scripts/completion_gate.py --work {wid} --record", gates
        if g.get("verdict") != "PASS":
            return False, f"{wid}'s latest gate record is {g.get('verdict')} ({'; '.join(g.get('fails', [])[:2])})", gates
        if g.get("request_fingerprint") != fingerprint:
            return False, (f"{wid}'s gate record was made on different code (fingerprint changed since "
                           f"{g.get('at')}): re-run the tests and scripts/completion_gate.py --work {wid} --record"), gates
        gates.append({"work": wid, **{k: g.get(k) for k in ("at", "verdict", "request_fingerprint", "test_runs")}})
    return True, f"{len(gates)} linked work record(s) with a current PASS gate record", gates


def uncommitted_owned_paths(root: Path, claim: dict, include_request_files: bool = True) -> list[str]:
    """Owned paths holding work of this request that is not committed.

    For an ordinary path that is any difference from HEAD. A ``shared``
    manifest (``work.txt``, ``evidence.txt``) is attributed line by line, so
    only this request's own lines count: other requests' uncommitted rows in
    the same file are theirs, and must not keep this request from releasing
    its claim once its own rows are committed."""
    tokens = [t for t in [claim.get("request_id"), claim.get("target"), *(claim.get("tokens") or [])] if t]
    out = []
    for p, f in sorted((claim.get("files") or {}).items()):
        if f.get("request_file") and not include_request_files:
            continue
        if is_directory_entry(root, p):
            continue
        try:
            if not path_is_modified(root, p):
                continue
            if f.get("mode") == "shared" and tokens:
                content, _left = _attributed_content(root, p, tokens)
                if content is None or content == _blob_bytes(root, head_blob(root, p)):
                    continue            # nothing of this request's is left uncommitted here
            out.append(p)
        except ProcessError:
            continue
    return out


# ------------------------------------------------------------ transitions
PRIORITIES = ("CRITICAL", "HIGH", "NORMAL", "LOW")


def set_priority(root: Path, name: str, priority: str, by: str = "") -> dict:
    """Change an open request's priority (inbox, processing, needs-review): the
    sidecar's ``priority``, the ``**Priority:**`` header and the
    Classification line, with the change appended to ``priority_history``.
    It is request metadata, not a state transition, so anyone may set it; the
    per-request lock (shared with follow-ups) keeps concurrent edits whole."""
    priority = str(priority or "").strip().upper()
    if priority not in PRIORITIES:
        raise ProcessError(INVALID, f"priority {priority!r} is not one of {', '.join(PRIORITIES)}")
    with exclusive(root, f"request-{Path(name).name}"):
        locs = [(s, pth) for s, pth in locate_request(root, Path(name).name) if s in ("inbox", "processing", "needs-review")]
        if not locs:
            raise ProcessError(NOT_FOUND if not locate_request(root, Path(name).name) else BAD_STATE,
                               f"{name} is not an open request (inbox, processing or needs-review)")
        _state, md = locs[0]
        js = md.with_suffix(".json")
        try:
            side = json.loads(js.read_text(encoding="utf-8")) if js.exists() else {}
        except ValueError:
            side = {}
        old = str(side.get("priority") or "NORMAL").upper()
        side["priority"] = priority
        side.setdefault("priority_history", []).append({"from": old, "to": priority, "at": now_iso(), "by": by})
        text = md.read_text(encoding="utf-8")
        text = re.sub(r"^(\*\*Priority:\*\*)\s*\S+", rf"\1 {priority}", text, count=1, flags=re.MULTILINE)
        text = re.sub(r"^(- Priority:)\s*\S+", rf"\1 {priority}", text, count=1, flags=re.MULTILINE)
        atomic_write_text(md, text)
        atomic_write_text(js, json.dumps(side, indent=2) + "\n")
        return {"request": md.name, "state": _state, "from": old, "to": priority}


def create_worktree(root: Path, name: str, agent_id: str, directory: str | Path | None = None) -> dict:
    """Give the owner's claim its own git worktree and branch
    (``request/<stem>``); the claim's paths are then owned in that worktree
    (ADR-000090). Returns ``{"path", "branch"}``."""
    with exclusive(root, "claims"):
        data = require_owner(root, name, agent_id)
        if data.get("worktree"):
            raise ProcessError(INVALID, f"request already has worktree {data['worktree']}")
        main = main_checkout(root)          # the project checkout, not the system directory in it
        stem = Path(name).stem
        path = Path(directory).resolve() if directory else (main.parent / f"{main.name}-worktrees" / stem)
        branch = f"request/{stem}"
        git(main, "worktree", "add", "-b", branch, str(path), "HEAD")
        data["worktree"] = str(path.resolve())
        data["branch"] = branch
        _touch(data)
        _save_claim(root, data)
        return {"path": str(path.resolve()), "branch": branch}


def transition_request(root: Path, name: str, agent_id: str, to_state: str, reason: str | None = None,
                       waiver: str | None = None) -> dict:
    """Owner-only, no-clobber transition processing|needs-review -> completed|
    rejected|needs-review (docs/process/state-model.md).

    * rejected needs a reason;
    * completed needs a current PASS gate record for every linked work item
      (completion.enforce_on_transition), except for request types listed in
      completion.exempt_types (a reason is then required) or with an explicit,
      recorded human waiver."""
    if to_state not in TRANSITION_TARGETS:
        raise ProcessError(INVALID, f"unsupported transition target {to_state}")
    with exclusive(root, "claims"):
        data = require_owner(root, name, agent_id)
        locs = [s for s, _ in locate_request(root, name)]
        if len(locs) != 1:
            raise ProcessError(BAD_STATE, f"request {name} is in {locs or 'no state'}; run scripts/claim.py --sweep")
        current = locs[0]
        if to_state not in cfgmod.REQUEST_TRANSITIONS.get(current, ()):
            raise ProcessError(BAD_STATE, f"request {name} is in {[current]}; expected exactly 'processing' "
                               f"(or 'needs-review') for {current} -> {to_state}")
        if to_state == "rejected" and not (reason and reason.strip()):
            raise ProcessError(INVALID, "--reason is required to reject a request")
        resolution: dict = {"from": current, "to": to_state, "at": now_iso(), "by": agent_id}
        if reason:
            resolution["reason"] = reason
        if to_state == "completed":
            rtype = request_type(root, name)
            exempt = rtype in [t.upper() for t in cfgmod.get("completion.exempt_types", root, [])]
            if waiver:
                resolution["gate_waiver"] = waiver
            elif not cfgmod.get("completion.enforce_on_transition", root, True):
                resolution["gate"] = "not enforced (completion.enforce_on_transition = false)"
            elif exempt and not linked_work_records(root, name):
                if not (reason and reason.strip()):
                    raise ProcessError(INVALID, f"{rtype} requests are exempt from the gate but need --reason")
                resolution["gate"] = f"exempt request type {rtype}"
            else:
                ready, why, gates = completion_status(root, data)
                if not ready:
                    raise ProcessError(GATE_REQUIRED, f"cannot complete {name}: {why} "
                                       "(a human override is --waive-gate '<reason>', recorded)")
                resolution["gates"] = gates
        path = locate_request(root, name)[0][1]
        dst = move_request(root, name, current, to_state)
        _set_request_status(dst, to_state, {"commits": data.get("commits", []), "resolution": resolution})
        _register_request_files(root, data, path, dst)
        data["state"] = to_state
        data["request_path"] = rel(root, dst)
        if to_state == "needs-review":
            data["parked"] = True   # any agent may take it over (claim_request)
        _history(data, f"transition:{current}->{to_state}", **{k: v for k, v in resolution.items()
                                                               if k in ("reason", "gate_waiver", "gate")})
        _touch(data)
        _save_claim(root, data)
        return data


def _archive_claim(root: Path, data: dict, event: str) -> None:
    entry = {k: v for k, v in data.items() if not k.startswith("_")}
    entry["event"] = event
    entry["at"] = now_iso()
    with open(runtime_dir(root) / "claims-history.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def release_request(root: Path, name: str, agent_id: str, requeue: bool = False,
                    abandon: str | None = None) -> dict:
    """Release the claim after the final commit.

    Refused while an owned path still differs from HEAD (it would be left
    uncommitted and unowned: CI_CD_ROADMAP COR-04) unless ``abandon`` gives a
    reason; the abandoned paths are recorded in the claim history. With
    ``requeue`` a processing request returns to the inbox (its own request
    files are then not counted)."""
    with exclusive(root, "claims"):
        data = require_owner(root, name, agent_id)
        locs = [s for s, _ in locate_request(root, name)]
        if locs == ["processing"] and not requeue:
            raise ProcessError(BAD_STATE, f"request {name} is still processing; complete/reject/needs-review it "
                               "first, or pass --requeue to return it to the inbox")
        pending = uncommitted_owned_paths(root, data, include_request_files=not (requeue and locs == ["processing"]))
        if pending and not (abandon and abandon.strip()):
            raise ProcessError(BAD_STATE, f"request {name} still has {len(pending)} uncommitted owned path(s): "
                               + ", ".join(pending[:10]) + ("…" if len(pending) > 10 else "")
                               + ". Commit them (scripts/commit_request.py) first, or release with "
                               "--abandon '<reason>' to leave them uncommitted and unowned (recorded).")
        if locs == ["processing"]:
            dst = move_request(root, name, "processing", "inbox")
            _set_request_status(dst, "inbox")
        event = "requeued" if (requeue and locs == ["processing"]) else "released"
        if pending:
            _history(data, "abandoned", paths=pending, reason=abandon, agent_id=agent_id)
            event += "+abandoned"
        _archive_claim(root, data, event)
        lock_path(root, name).unlink()
        return data


def recover_request(root: Path, name: str, new_agent_id: str, reason: str,
                    stale_hours: float = STALE_HOURS_DEFAULT, force_active: bool = False,
                    expect_owner: str | None = None, owner_pid: int | None = None) -> dict:
    """Explicit ownership transfer of a DEAD or STALE claim (or an orphaned
    processing request). ``force_active`` overrides an unverified ACTIVE
    owner; an owner whose process is verified ALIVE is never taken over."""
    new_agent_id = validate_agent_id(new_agent_id)
    if not reason or not reason.strip():
        raise ProcessError(INVALID, "--reason is required for recovery")
    with exclusive(root, "claims"):
        locs = locate_request(root, name)
        states = [s for s, _ in locs]
        lp = lock_path(root, name)
        data = read_claim(lp)
        if data is None:
            if states != ["processing"]:
                raise ProcessError(BAD_STATE, f"nothing to recover: {name} has no claim and is in {states or 'no state'}")
            path = locs[0][1]
            data = {"kind": "request", "target": name, "request_id": request_id_of(path), "files": {}, "tokens": [],
                    "commits": [], "history": [{"event": "recovered-orphan", "orphan": True, "recovered_at": now_iso(),
                                                "recovered_by": new_agent_id, "reason": reason}],
                    "state": "processing", "worktree": None, "request_path": rel(root, path)}
            _register_request_files(root, data, path)
        else:
            if expect_owner and data.get("agent_id") != expect_owner:
                raise ProcessError(CONFLICT, f"expected owner '{expect_owner}' but " + describe_owner(data, stale_hours))
            if data.get("agent_id") == new_agent_id:
                raise ProcessError(INVALID, "the request is already owned by this agent id")
            verdict, why = claim_liveness(data, stale_hours)
            if verdict == ALIVE:
                raise ProcessError(CONFLICT, "recovery refused, the owner process is verified running: "
                                   + describe_owner(data, stale_hours))
            if verdict == ACTIVE and not force_active:
                raise ProcessError(CONFLICT, "recovery refused, owner is still active: " + describe_owner(data, stale_hours))
            data.setdefault("history", []).append({
                "event": "recovered", "previous_agent_id": data.get("agent_id"),
                "previous_claim_token": data.get("claim_token"),
                "previous_claimed_at": data.get("claimed_at"), "previous_heartbeat_at": data.get("heartbeat_at"),
                "previous_liveness": f"{verdict}: {why}",
                "recovered_at": now_iso(), "recovered_by": new_agent_id, "reason": reason,
                "forced_while_active": bool(force_active and verdict == ACTIVE)})
            for key, default in (("files", {}), ("tokens", []), ("commits", [])):
                data.setdefault(key, default)
            data.setdefault("target", name)
            data.setdefault("kind", "request")
            if states:
                data["state"] = states[0]
        data["agent_id"] = new_agent_id
        data["claim_token"] = uuid.uuid4().hex
        data["claimed_at"] = now_iso()
        data["pid"] = os.getpid()
        data.pop("session", None)
        data.pop("step", None)
        _touch(data, owner_pid)
        _save_claim(root, data)
        _archive_claim(root, data, "recovered")
        return data


# ------------------------------------------------------------ sweep (safe recovery)
def _lone_sidecars(root: Path) -> list[tuple[Path, Path]]:
    """(json, md) where a request's sidecar sits in another state directory
    than its markdown (an interrupted paired move: CI_CD_ROADMAP COR-07)."""
    base = cfgmod.path(root, "requests")
    out = []
    for state in REQUEST_STATES:
        for js in sorted((base / state).glob("*.json")):
            md = js.with_suffix(".md")
            if md.exists():
                continue
            for other in REQUEST_STATES:
                cand = base / other / md.name
                if cand.exists():
                    out.append((js, cand))
    return out


def sweep(root: Path, apply: bool = False, stale_h: float | None = None) -> dict:
    """Diagnose claims and request state; with ``apply`` perform only the
    repairs that cannot lose or steal work (docs/process/recovery.md):

    * finish an interrupted claim of a live owner (the lock exists, the
      request never moved);
    * drop an interrupted claim of a DEAD / STALE owner that owns no
      uncommitted change (the request is still in the inbox);
    * resynchronise a claim's recorded state with the request's directory;
    * move a request sidecar next to its markdown;
    * requeue a request whose owner is verified DEAD and which has no
      uncommitted owned change (nothing to lose; history kept);
    * remove work locks whose owner is DEAD / STALE.

    Everything else — a DEAD owner with uncommitted work, a STALE owner
    (silence is not proof), an orphaned processing request — is reported as
    needing an explicit --recover-request / --release-request --abandon."""
    stale_h = stale_hours(root) if stale_h is None else stale_h
    actions: list[dict] = []
    findings: list[dict] = []
    with exclusive(root, "claims"):
        claims = all_claims(root)
        claimed = {c.get("target") for c in claims if c.get("kind") == "request"}
        for c in claims:
            verdict, why = claim_liveness(c, stale_h)
            target = c.get("target")
            if c.get("kind") == "work":
                exists = any(cfgmod.path(root, "work").rglob(f"{target}*.md"))
                if verdict in (DEAD, STALE_V) or not exists:
                    findings.append({"target": target, "kind": "work", "verdict": verdict,
                                     "issue": "work lock of a gone owner" if exists else "work lock for a missing record"})
                    if apply and verdict in (DEAD, STALE_V):
                        _archive_claim(root, c, "swept")
                        Path(c["_lock"]).unlink()
                        actions.append({"action": "removed-work-lock", "target": target, "reason": why})
                continue
            locs = locate_request(root, target)
            states = [s for s, _ in locs]
            row = {"target": target, "request_id": c.get("request_id"), "agent_id": c.get("agent_id"),
                   "verdict": verdict, "why": why, "claim_state": c.get("state"), "request_state": states}
            if not states:
                row["issue"] = "claim for a request that exists in no state directory"
                findings.append(row)
                continue
            if len(states) > 1:
                row["issue"] = f"request exists in several states {states}"
                findings.append(row)
                continue
            state, path = locs[0]
            pending = uncommitted_owned_paths(root, c, include_request_files=False)
            if c.get("state") == "processing" and state in ("inbox", "needs-review") and not c.get("parked"):
                if verdict in (ACTIVE, ALIVE):
                    row["issue"] = "interrupted claim (request never moved)"
                    findings.append(row)
                    if apply:
                        _enter_processing(root, c, target, state, path)
                        _history(c, "claim-completed-by-sweep")
                        _save_claim(root, c)
                        actions.append({"action": "completed-interrupted-claim", "target": target})
                    continue
                row["issue"] = "interrupted claim of a gone owner"
                if pending:
                    row["issue"] += f" with {len(pending)} uncommitted owned path(s): recover explicitly"
                    findings.append(row)
                    continue
                findings.append(row)
                if apply:
                    _archive_claim(root, c, "dropped-interrupted-claim")
                    Path(c["_lock"]).unlink()
                    actions.append({"action": "dropped-interrupted-claim", "target": target, "reason": why})
                continue
            if c.get("state") != state:
                row["issue"] = f"claim records state {c.get('state')} but the request is in {state}"
                findings.append(row)
                if apply:
                    c["state"] = state
                    c["request_path"] = rel(root, path)
                    _history(c, "state-synced-by-sweep", request_state=state)
                    _save_claim(root, c)
                    actions.append({"action": "synced-claim-state", "target": target, "state": state})
            if state == "processing" and verdict == DEAD:
                if pending:
                    row["issue"] = (f"owner DEAD ({why}) with {len(pending)} uncommitted owned path(s): "
                                    "recover it explicitly (--recover-request --reason), then finish the work or return it "
                                    "(--release-request --requeue --abandon REASON)")
                    row["recoverable"] = True
                    findings.append(row)
                else:
                    row["issue"] = f"owner DEAD ({why}); nothing uncommitted: safe to requeue"
                    findings.append(row)
                    if apply:
                        dst = move_request(root, target, "processing", "inbox")
                        _set_request_status(dst, "inbox")
                        _history(c, "requeued-by-sweep", reason=why)
                        _archive_claim(root, c, "requeued-dead-owner")
                        Path(c["_lock"]).unlink()
                        actions.append({"action": "requeued-dead-owner", "target": target, "reason": why})
            elif state == "processing" and verdict == STALE_V:
                row["issue"] = f"owner STALE ({why}): recover explicitly after checking the owner is gone"
                row["recoverable"] = True
                findings.append(row)
        for p in sorted((cfgmod.path(root, "requests") / "processing").glob("*.md")):
            if p.name != "README.md" and p.name not in claimed:
                findings.append({"target": p.name, "issue": "processing request with no claim (orphan): "
                                 "scripts/claim.py --recover-request", "recoverable": True})
        for js, md in _lone_sidecars(root):
            findings.append({"target": md.name, "issue": f"sidecar {rel(root, js)} is not beside {rel(root, md)}"})
            if apply:
                dst = md.with_suffix(".json")
                if not dst.exists():
                    move_no_clobber(js, dst)
                    actions.append({"action": "moved-sidecar", "target": md.name, "to": rel(root, dst)})
    report = {"at": now_iso(), "applied": apply, "actions": actions, "findings": findings}
    if apply and actions:
        with open(runtime_dir(root) / "sweeps.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(report) + "\n")
    return report


# ------------------------------------------------------------------ git ownership

def changed_paths(root: Path) -> dict[str, str]:
    out = git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    items = out.split("\0")
    result: dict[str, str] = {}
    i = 0
    while i < len(items):
        entry = items[i]
        i += 1
        if len(entry) < 4:
            continue
        xy, path = entry[:2], entry[3:]
        result[path] = xy
        if xy[0] in "RC" and i < len(items):
            result[items[i]] = xy  # rename/copy source
            i += 1
    return result


def classify_worktree(root: Path, request_name: str | None = None) -> dict:
    changed = changed_paths(root)
    claims = [c for c in all_claims(root) if c.get("kind") == "request"]
    top = Path(checkout_root(root)).resolve()
    here = str(Path(main_checkout(root)).resolve()) if top == Path(main_checkout(root)).resolve() else str(top)
    local = [c for c in claims if _worktree_key(c, root) == here]
    rows, conflicts = [], []
    for path, xy in sorted(changed.items()):
        owners = [(c, c["files"][path]) for c in local if path in (c.get("files") or {})]
        exclusive_owners = [c for c, f in owners if f.get("mode") not in ("shared", "concurrent")]
        if len(exclusive_owners) > 1 or (exclusive_owners and len(owners) > 1):
            conflicts.append(f"{path} is claimed by several requests: " + ", ".join(
                f"{c.get('request_id')}/{c.get('agent_id')}" for c, _ in owners))
        if not owners:
            category = "unowned"
        elif not request_name:
            category = "owned"
        elif any(c.get("target") == request_name for c, _ in owners):
            category = "this-request" if len(owners) == 1 else "shared-with-others"
        else:
            category = "other-request"
        rows.append({"path": path, "status": xy, "category": category,
                     "owners": [{"request_id": c.get("request_id"), "request_file": c.get("target"),
                                 "agent_id": c.get("agent_id"), "mode": f.get("mode"),
                                 "stale": is_stale(c)} for c, f in owners]})
    return {"worktree": here, "active_requests": [{"request_id": c.get("request_id"), "request_file": c.get("target"),
                                                   "agent_id": c.get("agent_id"), "state": c.get("state"),
                                                   "heartbeat_age_min": round(heartbeat_age_hours(c) * 60, 1),
                                                   "stale": is_stale(c), "owned_paths": sorted((c.get("files") or {}).keys()),
                                                   "worktree": c.get("worktree")} for c in claims],
            "changes": rows, "conflicts": conflicts}


def _index_mode(root: Path, relpath: str, env: dict) -> str:
    entry = git(root, "ls-files", "-s", "--", relpath, env=env).split()
    return entry[0] if entry else "100644"


def _commit_mode(root: Path, relpath: str, env: dict) -> str:
    """The mode a plain `git add` would record for `relpath`: 100755 when the
    working-tree file has its owner-exec bit, 100644 otherwise. Where git
    does not trust the executable bit (core.fileMode=false) or the path is
    not a regular file in the working tree, the indexed mode is kept
    (REQ-DEVOPS-042, ADR-000193)."""
    indexed = _index_mode(root, relpath, env)
    path = checkout_root(root) / relpath
    if indexed not in ("100644", "100755") or path.is_symlink() or not path.is_file():
        return indexed
    trust = subprocess.run(["git", "config", "--type=bool", "core.fileMode"], cwd=str(checkout_root(root)),
                           capture_output=True, text=True).stdout.strip()
    if trust == "false":
        return indexed
    return "100755" if path.stat().st_mode & stat.S_IXUSR else "100644"


def _blob_bytes(root: Path, blob: str) -> bytes:
    if not blob:
        return b""
    r = subprocess.run(["git", "cat-file", "blob", blob], cwd=str(checkout_root(root)), capture_output=True)
    if r.returncode != 0:
        raise ProcessError(INVALID, f"the recorded baseline blob {blob[:10]} is gone; "
                           "re-register the path (scripts/claim.py --snapshot) and commit again")
    return r.stdout


def _delta_content(root: Path, relpath: str, baseline: str, mine: str = "") -> tuple[bytes | None, str]:
    """This request's delta applied to the committed content.

    base   = the file as it was when this request registered it
    ours   = HEAD (which may already contain another request's commits)
    theirs = the working tree now

    Returns (content, "") on success, or (None, reason) when the merge
    conflicts -- which is reported, never resolved by guessing. Attribution
    comes from repository state, not from ids embedded in the text
    (REQ-CHAT-20260920-011 sections 8-13)."""
    base = _blob_bytes(root, baseline)
    ours = _blob_bytes(root, head_blob(root, relpath))
    if mine:
        # The request declared its own version with --snapshot; anything
        # written into the file afterwards is not its work.
        theirs = _blob_bytes(root, mine)
    else:
        try:
            theirs = (checkout_root(root) / relpath).read_bytes()
        except (FileNotFoundError, IsADirectoryError):
            theirs = b""
    if theirs == base:
        return None, "unchanged since this request registered the path"
    if ours == base:
        return theirs, ""          # nobody else committed into it: the file is the delta
    with tempfile.TemporaryDirectory() as tmp:
        names = {}
        for label, blob in (("base", base), ("ours", ours), ("theirs", theirs)):
            names[label] = Path(tmp) / label
            names[label].write_bytes(blob)
        merged = subprocess.run(["git", "merge-file", "-p", "-L", "HEAD", "-L", "baseline", "-L", "this request",
                                 str(names["ours"]), str(names["base"]), str(names["theirs"])],
                                cwd=str(checkout_root(root)), capture_output=True)
        if merged.returncode != 0:
            return None, (f"this request's changes and what has been committed since it registered the path "
                          f"overlap ({merged.returncode} conflict(s)); re-baseline with "
                          f"'scripts/claim.py --snapshot <request> --paths {relpath}' after reconciling them")
        return merged.stdout, ""


def _attributed_content(root: Path, relpath: str, tokens: list[str]) -> tuple[bytes | None, int]:
    """HEAD content of a shared path plus ONLY the changed lines that name one
    of this request's tokens (line-level, so adjacent lines appended by
    different requests are separated). Returns (content or None if nothing is
    attributable, number of unattributed change blocks left out)."""
    import difflib
    base = subprocess.run(["git", "show", f"HEAD:{relpath}"], cwd=str(checkout_root(root)), capture_output=True)
    old = base.stdout.decode("utf-8", errors="surrogateescape") if base.returncode == 0 else ""
    try:
        new = (checkout_root(root) / relpath).read_text(encoding="utf-8", errors="surrogateescape")
    except FileNotFoundError:
        new = ""
    a, b = old.splitlines(keepends=True), new.splitlines(keepends=True)
    named = lambda line: any(t in line for t in tokens)  # noqa: E731
    out, kept, left_out = [], 0, 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            out.extend(a[i1:i2])
        elif tag == "insert":
            mine = [l for l in b[j1:j2] if named(l)]
            out.extend(mine)
            kept += bool(mine)
            left_out += len(mine) != j2 - j1
        elif tag == "delete":
            if all(named(l) for l in a[i1:i2]):
                kept += 1
            else:
                out.extend(a[i1:i2])
                left_out += 1
        else:  # replace
            if all(named(l) for l in a[i1:i2]) and all(named(l) for l in b[j1:j2]):
                out.extend(b[j1:j2])
                kept += 1
            else:
                # Mixed block (e.g. another request edited an adjacent line
                # while this one appended): keep the old lines, add only
                # this request's new lines.
                mine = [l for l in b[j1:j2] if named(l)]
                out.extend(a[i1:i2])
                out.extend(mine)
                kept += bool(mine)
                left_out += 1
    if not kept:
        return None, left_out
    return "".join(out).encode("utf-8", errors="surrogateescape"), left_out


def commit_request(root: Path, name: str, agent_id: str, message: str, paths: list[str] | None = None,
                   dry_run: bool = False, _before_update_ref=None) -> dict:
    """Commit exactly this request's owned changes (see module docstring)."""
    if not message or not message.strip():
        raise ProcessError(INVALID, "a commit message is required")
    top = checkout_root(root)          # repository paths are relative to it
    with exclusive(root, "claims"):
        data = require_owner(root, name, agent_id)
        mine = _worktree_key(data, root)
        others = [c for c in all_claims(root) if c.get("target") != name and _worktree_key(c, root) == mine]
    changed = changed_paths(root)
    selected = [normalize_path(root, p) for p in paths] if paths else sorted(p for p in data["files"] if p in changed)
    problems = []
    for p in selected:
        f = data["files"].get(p)
        if f is None:
            owner = next((o for o in others if p in (o.get("files") or {})), None)
            problems.append(f"{p}: " + ("owned by " + describe_owner(owner) if owner else
                                       "not owned by this request (register it with scripts/claim.py --own)"))
            continue
        for o in others:
            of = (o.get("files") or {}).get(p)
            # A path both requests registered in the same non-exclusive mode
            # is legal: "shared" manifests are attributed line by line, and
            # "concurrent" content by each request's own delta.
            if of and not (f.get("mode") == of.get("mode") and f.get("mode") in ("shared", "concurrent")):
                problems.append(f"{p}: also owned ({of.get('mode')}) by " + describe_owner(o))
    # A path registered before it existed can turn out to be a directory (the
    # WORK-000317 sequence: register evidence/VER-NNNNNN, then write files into
    # it), and claims recorded before ADR-000197 may hold directory entries.
    # Such an entry owns nothing, so files written under it that nobody
    # registered would be left out of this commit without a word. Refuse
    # loudly instead; files another request registered there stay its own.
    for d in sorted(p for p in data["files"] if is_directory_entry(root, p)):
        loose = sorted(p for p in changed if p.startswith(d + "/") and p not in data["files"]
                       and not any(p in (o.get("files") or {}) for o in others))
        if loose:
            problems.append(f"{d} is registered but is a directory, and a registration owns exactly one file "
                            f"(ADR-000197). Changed files under it that no request has registered: "
                            + ", ".join(loose[:20]) + (f" (+{len(loose) - 20} more)" if len(loose) > 20 else "")
                            + ". Register each file this request wrote (scripts/claim.py --own <request> --paths "
                            "<file> --adopt-existing-changes, after checking it is this request's) and commit "
                            "again; files that are not this request's need a human (--needs-review-request).")
    if problems:
        raise ProcessError(NOT_OWNER, "commit refused, nothing staged:\n  " + "\n  ".join(problems))
    selected = [p for p in selected if p in changed]
    if not selected:
        raise ProcessError(INVALID, "nothing to commit: none of this request's owned paths has changes")
    # A completed request's commit carries the code its gate verified: the
    # current request fingerprint must equal every linked gate record's
    # (CI_CD_ROADMAP COR-09), unless completion was waived or exempt.
    works = linked_work_records(root, name)
    gates: list[dict] = []
    if data.get("state") == "completed":
        done = [h for h in data.get("history", []) if str(h.get("event", "")).endswith("->completed")]
        unchecked = done and (done[-1].get("gate_waiver") or done[-1].get("gate"))
        if not unchecked:
            ready, why, gates = completion_status(root, data)
            if not ready:
                raise ProcessError(GATE_REQUIRED, f"commit refused, nothing staged: {why}")
    trailers = [f"Request-Id: {data.get('request_id')}", f"Request-File: {name}", f"Agent-Id: {agent_id}"]
    for w in works:
        wid = re.match(r"WORK-\d{6}", w.name).group(0)
        trailers.append(f"Work-Id: {wid}")
        g = read_gate_record(root, wid)
        if g is not None:
            trailers.append(f"Gate-Record: {rel(root, gate_record_path(root, wid))} ({g.get('verdict')} {g.get('at')})")
            for run in g.get("test_runs") or []:
                trailers.append(f"Test-Run: {run}")
    tokens = [t for t in [data.get("request_id"), name, *data.get("tokens", [])] if t]
    result = {"request_id": data.get("request_id"), "agent_id": agent_id, "paths": [], "partial": [], "commit": None,
              "trailers": trailers}
    conflicts: list[str] = []
    with exclusive(root, "git-commit"):
        head = git(root, "rev-parse", "--verify", "HEAD").strip()
        tmp_index = runtime_dir(root) / f"index-{uuid.uuid4().hex}"
        env = dict(os.environ, GIT_INDEX_FILE=str(tmp_index))
        try:
            git(root, "read-tree", "HEAD", env=env)
            for p in selected:
                entry_mode = data["files"][p].get("mode")
                baseline = data["files"][p].get("baseline_blob")
                if entry_mode == "shared" and baseline is not None:
                    # "shared" means an append-only manifest whose lines carry
                    # ids; "concurrent" means ordinary content, which goes
                    # straight to the delta below.
                    # An append-only manifest whose lines carry ids is
                    # attributable exactly, line by line; prefer that when it
                    # finds anything, and fall back to the delta below when the
                    # content does not name this request (prose, code,
                    # configuration -- the case REQ-CHAT-20260920-011 names).
                    content, left_out = _attributed_content(root, p, tokens)
                    if content is not None:
                        git(root, "update-index", "--add", "--cacheinfo",
                            f"{_commit_mode(root, p, env)},{_write_blob(root, content)},{p}", env=env)
                        if left_out:
                            result["partial"].append({"path": p, "unattributed_hunks": left_out})
                        continue
                if baseline is not None and not (top / p).is_symlink():
                    # Attribution from repository state: this request's delta
                    # since it registered the path, applied to what is
                    # committed now. Works for source, prose, configuration
                    # and generated files alike, because it never looks for an
                    # id inside the content (REQ-DEVOPS-042).
                    content, reason = _delta_content(root, p, baseline, data["files"][p].get("mine_blob", ""))
                    if content is None and reason.startswith("this request's changes"):
                        conflicts.append(f"{p}: {reason}")
                        continue
                    if content is not None:
                        if content == b"" and not (top / p).exists():
                            git(root, "update-index", "--force-remove", "--", p, env=env)
                        else:
                            git(root, "update-index", "--add", "--cacheinfo",
                                f"{_commit_mode(root, p, env)},{_write_blob(root, content)},{p}", env=env)
                        try:
                            worktree_now = (top / p).read_bytes()
                        except (FileNotFoundError, IsADirectoryError):
                            worktree_now = b""
                        if content != worktree_now:
                            result["partial"].append({"path": p, "left_uncommitted":
                                                      "changes in this file that are not this request's"})
                    elif (top / p).is_file():
                        # Content unchanged since registration: a mode-only
                        # change (chmod +x) keeps HEAD's content and takes the
                        # working tree's mode (ADR-000193).
                        blob = head_blob(root, p)
                        indexed, wanted = _index_mode(root, p, env), _commit_mode(root, p, env)
                        if blob and indexed != wanted:
                            git(root, "update-index", "--cacheinfo", f"{wanted},{blob},{p}", env=env)
                    continue
                if entry_mode == "shared":
                    # Claims registered before REQ-DEVOPS-042 carry no
                    # baseline: the id-token attribution they were created
                    # under is all there is (migration, request section 19).
                    content, left_out = _attributed_content(root, p, tokens)
                    if content is not None:
                        git(root, "update-index", "--add", "--cacheinfo",
                            f"{_commit_mode(root, p, env)},{_write_blob(root, content)},{p}", env=env)
                    if left_out:
                        result["partial"].append({"path": p, "unattributed_hunks": left_out})
                elif (top / p).exists() or (top / p).is_symlink():
                    git(root, "update-index", "--add", "--", p, env=env)
                else:
                    git(root, "update-index", "--force-remove", "--", p, env=env)
            if conflicts:
                raise ProcessError(CONFLICT, "commit refused, nothing staged:\n  " + "\n  ".join(conflicts))
            staged = [line.split("\t")[-1] for line in
                      git(root, "diff", "--cached", "--name-status", "--no-renames", "HEAD", env=env).splitlines() if line]
            outside = sorted(set(staged) - set(selected))
            if outside:
                raise ProcessError(INVALID, f"internal safety check failed, staged paths outside ownership: {outside}")
            if not staged:
                raise ProcessError(INVALID, "nothing to commit: owned changes contain no attributable hunks")
            result["paths"] = staged
            result["stat"] = git(root, "diff", "--cached", "--stat", "HEAD", env=env)
            if dry_run:
                return result
            tree = git(root, "write-tree", env=env).strip()
            body = message.rstrip() + "\n\n" + "\n".join(trailers) + "\n"
            commit = git(root, "commit-tree", tree, "-p", head, "-F", "-", input=body).strip()
            if _before_update_ref:
                _before_update_ref()
            r = subprocess.run(["git", "update-ref", "-m", f"commit_request: {data.get('request_id')}", "HEAD", commit, head],
                               cwd=str(top), capture_output=True, text=True)
            if r.returncode != 0:
                raise ProcessError(CONFLICT, "HEAD moved while committing (another commit landed); nothing was "
                                   f"committed, re-run the command: {r.stderr.strip()}")
            result["commit"] = commit
        finally:
            for f in (tmp_index, Path(str(tmp_index) + ".lock")):
                with contextlib.suppress(FileNotFoundError):
                    f.unlink()
        # Keep the shared index consistent for committed paths nobody had staged.
        for p in staged:
            idx = git(root, "ls-files", "-s", "--", p).split()
            base = subprocess.run(["git", "rev-parse", "--verify", "-q", f"{head}:{p}"], cwd=str(top),
                                  capture_output=True, text=True).stdout.strip()
            if (idx and idx[1] == base) or (not idx and not base):
                for _ in range(50):
                    if subprocess.run(["git", "reset", "-q", commit, "--", p], cwd=str(top),
                                      capture_output=True).returncode == 0:
                        break
                    time.sleep(0.1)
                else:
                    # The commit stands; the shared index still shows the old
                    # version of p (git status: a staged revert). Reported, so
                    # the caller can run `git reset -q -- <path>`.
                    result.setdefault("index_refresh_failed", []).append(p)
            else:
                result.setdefault("index_left_untouched", []).append(p)
    with exclusive(root, "claims"):
        data = require_owner(root, name, agent_id)
        data["commits"].append({"sha": commit, "at": now_iso(), "paths": staged})
        # What this request committed is its new starting point: a later
        # commit measures only what it changes after this one (a PARTIAL
        # progress commit followed by the completion commit of the same file
        # must not look like someone else's change and conflict).
        for p in staged:
            f = (data.get("files") or {}).get(p)
            if f is not None and "baseline_blob" in f:
                r = subprocess.run(["git", "rev-parse", "--verify", "-q", f"{commit}:{p}"], cwd=str(top),
                                   capture_output=True, text=True)
                f["baseline_blob"] = r.stdout.strip() if r.returncode == 0 else ""
                f["baseline_at"] = now_iso()
                f.pop("mine_blob", None)
        _touch(data)
        _save_claim(root, data)
    return result
