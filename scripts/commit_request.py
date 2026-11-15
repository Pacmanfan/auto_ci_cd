#!/usr/bin/env python3
"""
Commit only the current request's changes (REQ-DEVOPS-038, ADR-000090).

Never use `git add -A`, `git add .` or `git commit -a` in a shared worktree:
they capture other agents' uncommitted work. This helper instead:

  1. loads the request's claim and verifies the caller owns it;
  2. classifies every working-tree change as this-request / other-request /
     unowned (printed);
  3. refuses (exit 3, nothing staged) if a selected path is not owned by this
     request or is owned by another request;
  4. builds the commit in a PRIVATE git index started from HEAD, adding only
     owned paths — and for each of them only THIS REQUEST'S delta: the
     three-way merge of the baseline recorded when the path was registered,
     what is committed now, and the working tree. Another request's changes to
     the same file are left uncommitted, and an overlap is refused with the
     path named rather than resolved by guessing. Claims registered before
     REQ-DEVOPS-042 carry no baseline and fall back to id-token attribution
     for --shared paths;
  5. prints the staged file list/stat (--dry-run stops here);
  6. publishes with `git update-ref HEAD <new> <old>` (fails, committing
     nothing, if HEAD moved concurrently) and records the commit in the claim.

Usage:
  python3 scripts/commit_request.py --request <file> -m "WORK-000123: ..." [--paths P ...] [--dry-run]
  python3 scripts/commit_request.py --report [--request <file>] [--json]
Agent id from --agent-id or $AGENT_ID. Note: commit-tree does not run git hooks.
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_process as proc  # noqa: E402


def print_report(rep):
    print(f"WORKTREE {rep['worktree']}")
    print("Active claims:")
    for c in rep["active_requests"] or []:
        print(f"  {'STALE ' if c['stale'] else ''}{c['request_id']} ({c['request_file']}) agent={c['agent_id']} "
              f"state={c['state']} heartbeat={c['heartbeat_age_min']}min owned={len(c['owned_paths'])}")
    if not rep["active_requests"]:
        print("  (none)")
    print("Working-tree changes:")
    for r in rep["changes"]:
        owners = ", ".join(f"{o['request_id']}/{o['agent_id']}({o['mode']})" for o in r["owners"]) or "-"
        print(f"  {r['status']} {r['category']:18s} {r['path']}  owners: {owners}")
    if not rep["changes"]:
        print("  (clean)")
    for c in rep["conflicts"]:
        print(f"CONFLICT {c}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--request")
    ap.add_argument("--agent-id", default=os.environ.get("AGENT_ID"))
    ap.add_argument("-m", "--message")
    ap.add_argument("-F", "--message-file")
    ap.add_argument("--paths", nargs="+")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    root = proc.repo_root()
    name = Path(a.request).name if a.request else None
    try:
        rep = proc.classify_worktree(root, name)
        if a.report:
            print(json.dumps(rep, indent=2) if a.json else "", end="")
            if not a.json:
                print_report(rep)
            return 3 if rep["conflicts"] else 0
        if not name:
            raise proc.ProcessError(proc.INVALID, "--request is required")
        message = Path(a.message_file).read_text(encoding="utf-8") if a.message_file else a.message
        print_report(rep)
        res = proc.commit_request(root, name, a.agent_id, message, a.paths, dry_run=a.dry_run)
        print("Staged for this request only:")
        for p in res["paths"]:
            print(f"  {p}")
        print(res.get("stat", ""), end="")
        for part in res["partial"]:
            if "left_uncommitted" in part:
                print(f"NOTE {part['path']}: {part['left_uncommitted']} left uncommitted")
            else:
                print(f"NOTE {part['path']}: {part['unattributed_hunks']} hunk(s) not naming this request left uncommitted")
        for p in res.get("index_left_untouched", []):
            print(f"NOTE shared index entry for {p} was staged by someone else and was left as is")
        for p in res.get("index_refresh_failed", []):
            print(f"WARN shared index entry for {p} could not be refreshed (index busy); the commit stands — "
                  f"run `git reset -q -- {p}` so git status does not show a staged revert")
        if a.dry_run:
            print("DRY RUN: nothing committed")
        else:
            print(f"COMMITTED {res['commit']}")
        return 0
    except proc.ProcessError as e:
        print(e.message, file=sys.stderr)
        return e.code


if __name__ == "__main__":
    sys.exit(main())
