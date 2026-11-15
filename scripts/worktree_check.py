#!/usr/bin/env python3
"""
Safe clean worktree check — before autonomous implementation.

Inspects:
  uncommitted changes, unrelated user changes, changes belonging to another work item,
  generated files, build artifacts

Usage:
  python3 scripts/worktree_check.py
  python3 scripts/worktree_check.py --json
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_process as proc  # noqa: E402

ROOT = proc.repo_root()

def _run(cmd):
    try:
        out = subprocess.check_output(cmd, cwd=ROOT, text=True, stderr=subprocess.DEVNULL)
        return out.strip()
    except:
        return ""

def main():
    import argparse, json
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--request", help="request file: classify changes relative to this request's ownership")
    ap.add_argument("--strict", action="store_true", help="exit 3 when ownership conflicts exist")
    args = ap.parse_args()
    status = _run(["git","status","--porcelain"])
    diff_stat = _run(["git","diff","--stat"])
    diff_cached = _run(["git","diff","--cached","--stat"])
    untracked = _run(["git","ls-files","--others","--exclude-standard"])
    # classify
    lines = status.splitlines() if status else []
    build_artifacts = [l for l in lines if "build/" in l or ".o " in l or ".a " in l]
    generated = [l for l in lines if "v2_gen" in l or ".pb." in l]
    work_changes = [l for l in lines if "requests/" in l or "work/" in l or "evidence/" in l or "docs/" in l]
    source_changes = [l for l in lines if l not in build_artifacts and l not in generated and "requests/" not in l and "work/" not in l]

    report = {
        "porcelain": status[:3000] if status else "(clean)",
        "diff_stat": (diff_stat + "\n" + diff_cached)[:2000],
        "untracked": untracked[:1500],
        "counts": {"total": len(lines), "build_artifacts": len(build_artifacts), "generated": len(generated), "work": len(work_changes), "source": len(source_changes)},
    }
    # verdict
    if not status:
        verdict = "CLEAN"
        msg = "Worktree clean — safe to mutate."
    elif source_changes and work_changes:
        verdict = "WARN"
        msg = "Mixed source + work changes — inspect ownership; do not overwrite unrelated human work. Escalate if ambiguous."
    elif source_changes:
        verdict = "WARN"
        msg = f"Uncommitted source changes ({len(source_changes)} files) — verify they belong to current WORK; otherwise stash or escalate."
    elif build_artifacts or generated:
        verdict = "PASS_WITH_ARTIFACTS"
        msg = "Only build/generated artifacts dirty — safe for autonomous loop."
    else:
        verdict = "WARN"
        msg = "Uncommitted changes — review before proceeding."

    # Concurrent-agent ownership (ADR-000090): which request owns each change.
    try:
        own = proc.classify_worktree(ROOT, Path(args.request).name if args.request else None)
    except proc.ProcessError as e:
        own = {"error": e.message, "conflicts": [], "changes": [], "active_requests": []}
    report["ownership"] = own
    if own.get("conflicts"):
        verdict = "CONFLICT"
        msg = "Several active requests claim the same changed path(s) — resolve ownership before committing."
    elif any(r["category"] == "other-request" for r in own.get("changes", [])):
        msg += " Changes owned by other active requests are present: never stage or commit them."
    report["verdict"] = verdict
    report["message"] = msg

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"WORKTREE: {verdict}")
        print(msg)
        print(f"\nCounts: {report['counts']}")
        if status:
            print(f"\ngit status --porcelain:\n{status[:1500]}")
        if diff_stat or diff_cached:
            print(f"\ngit diff --stat:\n{(diff_stat + diff_cached)[:1500]}")
        if untracked:
            print(f"\nUntracked (first 500 chars):\n{untracked[:500]}")
        print("\nOwnership:")
        for r in own.get("changes", []):
            owners = ", ".join(f"{o['request_id']}/{o['agent_id']}" for o in r["owners"]) or "-"
            print(f"  {r['category']:18s} {r['path']}  owners: {owners}")
        for c in own.get("conflicts", []):
            print(f"  CONFLICT {c}")
    # warn by default; --strict makes ownership conflicts fatal
    sys.exit(3 if (args.strict and own.get("conflicts")) else 0)

if __name__ == "__main__":
    main()
