"""Read-only git inspection of the checkout a worker's agent works in.

A worker never stages, commits, resets, stashes or checks out anything: the
agent commits through scripts/commit_request.py, and the checkout may hold
other sessions' uncommitted work. These helpers only observe.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


def _git(root: Path, *args: str, timeout: int = 60) -> tuple[int, str]:
    try:
        r = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout
    except (subprocess.TimeoutExpired, OSError) as e:
        return 1, str(e)


def head(root: Path) -> str:
    rc, out = _git(root, "rev-parse", "HEAD")
    return out.strip() if rc == 0 else ""


def branch(root: Path) -> str:
    rc, out = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    return out.strip() if rc == 0 else ""


def dirty(root: Path) -> dict[str, str]:
    """path -> porcelain status code (XY) for every modified/untracked path."""
    rc, out = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    res: dict[str, str] = {}
    if rc != 0:
        return res
    parts = out.split("\0")
    i = 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 4:
            continue
        code, path = entry[:2], entry[3:]
        if code[0] in "RC":  # rename/copy: next field is the source
            i += 1
        res[path] = code
    return res


def staged(root: Path) -> list[str]:
    rc, out = _git(root, "diff", "--cached", "--name-only")
    return [l for l in out.splitlines() if l.strip()] if rc == 0 else []


def staged_index(root: Path) -> dict[str, dict]:
    """path -> {"status": A|M|D|T, "blob": index blob id} for every path staged
    in the shared index, so a later snapshot can tell "still staged as it was"
    from "staged again with different content"."""
    rc, out = _git(root, "diff", "--cached", "--raw", "-z", "--no-renames", "--no-abbrev")
    res: dict[str, dict] = {}
    if rc != 0:
        return res
    parts = out.split("\0")
    i = 0
    while i + 1 < len(parts):
        meta, path = parts[i], parts[i + 1]
        i += 2
        f = meta[1:].split() if meta.startswith(":") else []
        if len(f) >= 5:
            res[path] = {"status": f[4][:1], "blob": f[3]}
    return res


def commits_between(root: Path, start: str, end: str) -> list[dict]:
    if not start or not end or start == end:
        return []
    rc, out = _git(root, "log", "--reverse", "--format=%x1e%H%x1f%an%x1f%aI%x1f%s%x1f%b%x1d",
                   "--name-only", f"{start}..{end}", timeout=120)
    if rc != 0:
        return []
    commits = []
    for chunk in out.split("\x1e"):
        if not chunk.strip():
            continue
        meta, _, files = chunk.partition("\x1d")
        f = meta.split("\x1f")
        if len(f) < 5:
            continue
        commits.append({
            "sha": f[0], "author": f[1], "date": f[2], "subject": f[3], "body": f[4].strip(),
            "files": [l for l in files.splitlines() if l.strip()],
        })
    return commits


def is_ancestor(root: Path, a: str, b: str) -> bool:
    rc, _ = _git(root, "merge-base", "--is-ancestor", a, b)
    return rc == 0


def commit_exists(root: Path, sha: str) -> bool:
    rc, _ = _git(root, "cat-file", "-e", f"{sha}^{{commit}}")
    return rc == 0


def snapshot(root: Path) -> dict:
    d = dirty(root)
    idx = staged_index(root)
    return {"head": head(root), "branch": branch(root), "dirty_count": len(d), "dirty": d,
            "staged": sorted(idx), "staged_index": idx}


DELTA_KEYS = ("staged_before", "staged_before_restaged", "staged_before_cleared", "staged_created",
              "staged_modified", "dirty_before", "dirty_before_changed", "created", "modified")


def attempt_delta(before: dict, after: dict) -> dict:
    """What happened to the shared index and working tree between two
    snapshots, measured against the first one (the attempt's baseline):

    staged_before           staged before the attempt and still staged as it was: pre-existing
    staged_before_restaged  staged before, staged again with different content during the attempt
    staged_before_cleared   staged before, no longer staged after it
    staged_created          newly staged during the attempt: a new file
    staged_modified         newly staged during the attempt: a change to (or removal of) a tracked file
    dirty_before            modified or untracked, not staged, before the attempt: pre-existing
    dirty_before_changed    of those, the ones whose status changed during the attempt
    created                 untracked or added during the attempt, not staged
    modified                tracked, clean before, modified (not staged) during the attempt

    The checkout is shared, so "during the attempt" means in its time window,
    not necessarily by its worker. A snapshot recorded before staged blob ids
    were kept (names only) cannot show a re-staged path; it counts as unchanged."""
    b_idx = before.get("staged_index") or {p: {} for p in before.get("staged") or []}
    a_idx = after.get("staged_index") or {p: {} for p in after.get("staged") or []}
    b_dirty, a_dirty = before.get("dirty") or {}, after.get("dirty") or {}
    out: dict[str, list[str]] = {k: [] for k in DELTA_KEYS}
    for p, e in sorted(b_idx.items()):
        if p not in a_idx:
            out["staged_before_cleared"].append(p)
        elif e.get("blob") and a_idx[p].get("blob") and e["blob"] != a_idx[p]["blob"]:
            out["staged_before_restaged"].append(p)
        else:
            out["staged_before"].append(p)
    for p, e in sorted(a_idx.items()):
        if p not in b_idx:
            out["staged_created" if (e.get("status") == "A" or a_dirty.get(p, "")[:1] == "A")
                else "staged_modified"].append(p)
    for p, code in sorted(b_dirty.items()):
        if p in b_idx:
            continue
        out["dirty_before"].append(p)
        if a_dirty.get(p) != code:
            out["dirty_before_changed"].append(p)
    for p, code in sorted(a_dirty.items()):
        if p in b_dirty or p in a_idx:
            continue
        out["created" if code in ("??", "!!") or code[:1] == "A" else "modified"].append(p)
    return out
