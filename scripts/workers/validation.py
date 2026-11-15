"""Independent validation after each attempt.

The agent's report says what the agent believes; this records what the worker
observed with the development system's own tools. Steps come from
``workers.validation``; each is a builtin or a shell command run in the
attempt's working directory::

    {"name": "affected-tests",
     "cmd": "python3 scripts/run_tests.py --tier t1 --paths {changed_files}",
     "when": "changed:*",                 # always | completed | changed:<glob,glob>
     "timeout_minutes": 60,
     "exit_codes": {"0": "PASS", "4": "UNKNOWN"}}   # anything unlisted -> FAIL

Builtins:

    git_sanity      branch and history intact; the shared index and working
                    tree compared with the attempt's own baseline
    request_state   the request's directory agrees with the reported outcome
    commit_binding  every commit attributed to the attempt carries the code
                    its gate record verified (scripts/devsys.py verify-commit)

``{changed_files}`` is the shell-quoted list of files the attempt's commits
touched. Step status: PASS, WARN, FAIL, UNKNOWN (could not be established) or
SKIPPED (condition not met). The overall result is the worst.
"""
from __future__ import annotations

import fnmatch
import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import agent_process as proc  # noqa: E402
import devsys_config as cfgmod  # noqa: E402

from workers import gitops, state  # noqa: E402

ORDER = {"SKIPPED": 0, "PASS": 1, "WARN": 2, "UNKNOWN": 3, "FAIL": 4}


def worst(statuses) -> str:
    s = [x for x in statuses if x != "SKIPPED"]
    return max(s, key=lambda x: ORDER[x]) if s else "SKIPPED"


def changed_files(attempt: dict) -> list[str]:
    seen = []
    for c in attempt.get("commits") or []:
        for f in c.get("files") or []:
            if f not in seen:
                seen.append(f)
    return seen


def _applies(when: str, attempt: dict, files: list[str]) -> tuple[bool, str]:
    when = (when or "always").strip()
    if when == "always":
        return True, ""
    if when == "completed":
        return attempt.get("result") == "COMPLETED", "attempt did not report COMPLETED"
    if when.startswith("changed:"):
        pats = [p.strip() for p in when[len("changed:"):].split(",") if p.strip()]
        hit = [f for f in files if any(fnmatch.fnmatch(f, p) or fnmatch.fnmatch(f.rsplit("/", 1)[-1], p)
                                        for p in pats)]
        return bool(hit), f"no committed file matches {','.join(pats)}"
    return False, f"unknown condition {when!r}"


def _names(paths: list[str], n: int = 10) -> str:
    return ", ".join(paths[:n]) + (f" (+{len(paths) - n} more)" if len(paths) > n else "")


# ---------------------------------------------------------------------- builtins
def b_git_sanity(root: Path, attempt: dict, step: dict) -> tuple[str, str]:
    """What was already staged or modified before the agent started is
    reported as pre-existing and never counted against the attempt. The
    worker only looks; it never stages, unstages or reverts anything."""
    cwd = Path(attempt["cwd"])
    before, after = attempt["git_before"], attempt.get("git_after") or {}
    notes, status = [], "PASS"
    if before.get("branch") != after.get("branch"):
        status = "FAIL"
        notes.append(f"branch changed: {before.get('branch')} -> {after.get('branch')}")
    if before.get("head") and after.get("head") and not gitops.is_ancestor(cwd, before["head"], after["head"]):
        status = "FAIL"
        notes.append(f"HEAD {after['head'][:12]} does not descend from the starting commit {before['head'][:12]} "
                     "(history rewritten or reset)")
    d = attempt.get("git_delta") or gitops.attempt_delta(before, gitops.snapshot(cwd))
    d = {k: d.get(k) or [] for k in gitops.DELTA_KEYS}
    newly_staged = d["staged_created"] + d["staged_modified"]
    # Another agent's commit moves HEAD before it refreshes the shared index:
    # in that moment its committed paths look staged. They are not this
    # attempt's.
    others = {f for c in attempt.get("other_commits") or [] for f in c.get("files") or []}
    transient = [x for x in newly_staged if x in others]
    newly_staged = [x for x in newly_staged if x not in others]
    if transient:
        notes.append(f"{len(transient)} path(s) of other requests' commits in the same window appeared staged "
                     f"while those commits refreshed the index (not this attempt's): {_names(transient)}")
    if newly_staged:
        status = worst([status, "WARN"])
        notes.append(f"{len(newly_staged)} path(s) staged in the shared index during the attempt and left staged "
                     f"(commit_request.py never uses the shared index): {_names(newly_staged)}")
    if d["staged_before_restaged"]:
        status = worst([status, "WARN"])
        notes.append("path(s) staged before the attempt were staged again with different content: "
                     + _names(d["staged_before_restaged"]))
    if d["staged_before_cleared"]:
        status = worst([status, "WARN"])
        notes.append("path(s) staged before the attempt are no longer staged: " + _names(d["staged_before_cleared"]))
    if d["staged_before"]:
        notes.append(f"{len(d['staged_before'])} path(s) already staged before the attempt (pre-existing): "
                     + _names(d["staged_before"]))
    if d["dirty_before"]:
        notes.append(f"{len(d['dirty_before'])} path(s) already modified or untracked before the attempt "
                     "(pre-existing, not attributed)")
    if attempt.get("owned_uncommitted"):
        status = worst([status, "WARN"])
        notes.append("uncommitted work still registered to the request's claim (preserved): "
                     + _names(attempt["owned_uncommitted"], 20))
    if d["created"] or d["modified"]:
        notes.append(f"during the attempt {len(d['created'])} path(s) were created and {len(d['modified'])} "
                     "modified without being committed (the checkout may be shared): "
                     + _names(d["created"] + d["modified"], 15))
    if attempt.get("result") == "COMPLETED" and not attempt.get("commits"):
        status = worst([status, "WARN"])
        notes.append("COMPLETED with no commit attributed to this request")
    notes.append(f"commits attributed: {len(attempt.get('commits') or [])}; other commits in the same window: "
                 f"{len(attempt.get('other_commits') or [])}")
    return status, "\n".join(notes)


def b_request_state(root: Path, attempt: dict, step: dict) -> tuple[str, str]:
    st, res = attempt.get("request_state"), attempt.get("result")
    expected = {"COMPLETED": {"completed"}, "ESCALATE": {"needs-review"}, "BLOCKED": {"needs-review", "processing"},
                "PARTIAL": {"processing"}, "FAILED": {"processing", "needs-review"}}.get(res)
    if st is None:
        return "FAIL", f"request file {attempt['request']} is in no state directory"
    msg = f"request is in {st}/; attempt result {res}"
    if expected is None or st in expected:
        return "PASS", msg
    return "WARN", msg + f"; expected {' or '.join(sorted(expected))}/"


def b_commit_binding(root: Path, attempt: dict, step: dict) -> tuple[str, str]:
    """The framework's own check (verify-commit) on every attributed commit:
    a completed request's commit must carry the code its gate verified."""
    import devsys
    commits = attempt.get("commits") or []
    completed = attempt.get("request_state") == "completed"
    if completed and attempt.get("owned_uncommitted"):
        # The request moved to completed/ but its work is not (all) committed:
        # the commit was refused or never made. Never a pass.
        return "FAIL", ("the request is completed but its work is not committed: "
                        + _names(attempt["owned_uncommitted"], 20))
    if not commits:
        return ("FAIL" if completed else "SKIPPED"), "no commit attributed" + (
            " to a completed request" if completed else "")
    status, lines = "PASS", []
    for c in commits:
        v = devsys.verify_commit(proc.system_dir_in(root, Path(attempt["cwd"])), c["sha"])
        s = {"VERIFIED": "PASS", "WAIVED": "PASS", "NOT_APPLICABLE": "PASS", "FAIL": "FAIL"}.get(v["status"], "UNKNOWN")
        status = worst([status, s])
        lines.append(f"{c['sha'][:10]} {v['status']}: {v.get('detail', '')}")
    return status, "\n".join(lines)


BUILTINS = {"git_sanity": b_git_sanity, "request_state": b_request_state, "commit_binding": b_commit_binding}


# ---------------------------------------------------------------------- runner
def validate(root: Path, wid: str, attempt: dict) -> dict:
    steps_cfg = cfgmod.get("workers.validation", root, []) or []
    vdir = state.attempt_dir(root, wid, attempt["attempt_id"]) / "validation"
    vdir.mkdir(parents=True, exist_ok=True)
    files = changed_files(attempt)
    results = []
    for step in steps_cfg:
        name = step.get("name") or step.get("builtin") or "step"
        ok, why = _applies(step.get("when"), attempt, files)
        if not ok:
            results.append({"name": name, "status": "SKIPPED", "detail": why})
            continue
        if step.get("builtin"):
            fn = BUILTINS.get(step["builtin"])
            if fn is None:
                status, detail = "UNKNOWN", f"unknown builtin {step['builtin']!r}"
            else:
                try:
                    status, detail = fn(root, attempt, step)
                except Exception as e:  # a broken check is UNKNOWN, never PASS
                    status, detail = "UNKNOWN", f"{type(e).__name__}: {e}"
        else:
            cmd = str(step.get("cmd") or "").replace("{changed_files}", " ".join(shlex.quote(f) for f in files))
            timeout = float(step.get("timeout_minutes") or 30) * 60
            try:
                r = subprocess.run(cmd, shell=True, cwd=attempt["cwd"], capture_output=True, text=True,
                                   timeout=timeout)
                codes = {str(k): v for k, v in (step.get("exit_codes") or {"0": "PASS"}).items()}
                status = codes.get(str(r.returncode), "FAIL")
                detail = f"$ {cmd}\nexit {r.returncode}\n{(r.stdout + r.stderr)[-4000:]}"
            except subprocess.TimeoutExpired:
                status, detail = "UNKNOWN", f"$ {cmd}\ndid not finish within {timeout:.0f}s"
        (vdir / f"{name}.log").write_text(detail + "\n", encoding="utf-8")
        results.append({"name": name, "status": status, "detail": detail.splitlines()[0] if detail else ""})
    return {"overall": worst([r["status"] for r in results]) if results else "SKIPPED", "steps": results}
