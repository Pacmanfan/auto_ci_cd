"""Architecture / conformance audit runner (ARCH-AUDIT-*, ADR-000195).

    tools/audit [quick|standard|baseline] [--build-dir DIR] [--run-tests t0|t1|t2]
                [--no-tests] [--check ID ...] [--previous AUDIT-ID] [--ai] [--list]

One audit run = one directory ``.audit/runs/<AUDIT-YYYYMMDD-NNN>/`` holding

    audit.json        machine-readable results (every check, inventory, comparison)
    audit.md          concise human report
    audit-packet.md   compact packet for a development AI (references evidence)
    evidence/<check>.txt   the raw output / data each result is based on
    ai-analysis.md    only with --ai and AUDIT_AI_COMMAND set; interpretation, not evidence

The runner is read-only towards the project: it runs the project's own check
tools and reads files. With a profile (or --run-tests) that executes tests, the
test orchestrator writes its own run record in the build tree, as it always does.
It compares the working tree before and after and reports the result
(check ``audit.read-only``).

Documentation: docs/verification/architecture-audit.md
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import checks as C
import report

TOOL_VERSION = "1.0.0"
AUDIT_ID = re.compile(r"^AUDIT-(\d{8})-(\d{3})$")
TEST_TIERS = ("t0", "t1", "t2")


def repo_root() -> Path:
    env = os.environ.get("AUDIT_ROOT")
    return Path(env).resolve() if env else Path(__file__).resolve().parent.parent.parent


def load_manifest(root: Path) -> dict:
    return json.loads((root / ".audit" / "manifest.json").read_text(encoding="utf-8"))


def runs_dir(root: Path) -> Path:
    return root / ".audit" / "runs"


# ------------------------------------------------------------------ identity
def allocate_run(root: Path, today: str | None = None) -> tuple[str, Path]:
    """AUDIT-<local date>-<NNN>: the next number of the day, reserved by an
    exclusive mkdir so concurrent audits never share an ID."""
    today = today or datetime.now().strftime("%Y%m%d")
    base = runs_dir(root)
    base.mkdir(parents=True, exist_ok=True)
    while True:
        used = [int(m.group(2)) for p in base.iterdir() if (m := AUDIT_ID.match(p.name)) and m.group(1) == today]
        audit_id = f"AUDIT-{today}-{max(used, default=0) + 1:03d}"
        try:
            (base / audit_id).mkdir()
            return audit_id, base / audit_id
        except FileExistsError:
            continue


def completed_runs(root: Path) -> list[dict]:
    """Earlier audits with results, oldest first."""
    found = []
    base = runs_dir(root)
    for path in sorted(base.iterdir()) if base.is_dir() else []:
        if AUDIT_ID.match(path.name) and (path / "audit.json").is_file():
            try:
                found.append(json.loads((path / "audit.json").read_text(encoding="utf-8")))
            except ValueError:
                continue
    return found


# --------------------------------------------------------------- read-only
def tree_snapshot(root: Path) -> dict[str, str]:
    """Every modified or untracked (not ignored) path with a hash of its
    content: what a read-only audit must leave exactly as it found it."""
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=root, capture_output=True, text=True)
    if top.returncode != 0 or Path(top.stdout.strip()).resolve() != Path(root).resolve():
        # Not a git work tree (a fresh copy): hash every file outside runtime,
        # build and scratch trees.
        skip = {".git", "tmp", "build", "__pycache__", ".agent", ".audit", "node_modules"}
        snap = {p.relative_to(root).as_posix(): hashlib.sha1(p.read_bytes()).hexdigest()
                for p in Path(root).rglob("*") if p.is_file() and not (set(p.relative_to(root).parts) & skip)}
        snap["<HEAD>"] = "(no git repository)"
        return snap
    out = subprocess.run(["git", "status", "--porcelain=v1", "-z", "--untracked-files=normal"], cwd=root,
                         capture_output=True, text=True)
    snapshot = {}
    for entry in filter(None, out.stdout.split("\0")):
        path = entry[3:]
        full = root / path
        if full.is_file():
            snapshot[path] = entry[:2] + " " + hashlib.sha1(full.read_bytes()).hexdigest()
        else:
            snapshot[path] = entry[:2]
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True).stdout.strip()
    snapshot["<HEAD>"] = head
    return snapshot


def read_only_result(before: dict, after: dict) -> dict:
    changed = sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))
    evidence = "paths whose status or content changed during the audit:\n" + "\n".join(
        f"{p}: {before.get(p, '(absent)')} -> {after.get(p, '(absent)')}" for p in changed)
    if not changed:
        return C.result(C.PASS, f"working tree identical before and after the audit ({len(before) - 1} modified / "
                                "untracked paths compared, HEAD unchanged)", evidence=evidence,
                        metrics={"compared": len(before) - 1, "changed": 0})
    # Other sessions share this checkout (ADR-000090): a change during the run
    # cannot be attributed, so it is reported, not called a violation.
    return C.result(C.UNKNOWN, f"{len(changed)} path(s) changed while the audit ran (another session may be "
                               "working in this checkout)", changed[:20],
                    {"compared": len(before) - 1, "changed": len(changed)}, evidence)


# -------------------------------------------------------------- comparison
def _flat_metrics(check: dict) -> dict[str, float]:
    flat = {}
    for key, value in (check.get("metrics") or {}).items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            flat[key] = value
    return flat


def compare(root: Path, current: dict, previous: dict | None) -> dict:
    if not previous:
        return {"previous": None}
    prev_checks = {c["id"]: c for c in previous.get("checks", [])}
    status_changes, coverage_changes, metric_changes = [], [], []
    for check in current["checks"]:
        before = prev_checks.get(check["id"])
        if before is None:
            (coverage_changes if check["status"] == C.SKIPPED else status_changes).append(
                {"id": check["id"], "from": None, "to": check["status"]})
            continue
        if before["status"] != check["status"]:
            change = {"id": check["id"], "from": before["status"], "to": check["status"]}
            # Run in one audit and not the other (different profiles): a
            # difference in coverage, not a change in the project.
            (coverage_changes if C.SKIPPED in (before["status"], check["status"]) else status_changes).append(change)
        old, new = _flat_metrics(before), _flat_metrics(check)
        for key in sorted(set(old) & set(new)):
            if old[key] != new[key]:
                metric_changes.append({"id": check["id"], "metric": key, "from": old[key], "to": new[key]})
        if before["status"] == check["status"] == C.DRIFT and before.get("details") != check.get("details"):
            added = [d for d in check.get("details", []) if d not in before.get("details", [])]
            removed = [d for d in before.get("details", []) if d not in check.get("details", [])]
            if added or removed:
                metric_changes.append({"id": check["id"], "metric": "details", "added": added[:10],
                                       "removed": removed[:10]})
    prev_commit = previous.get("git", {}).get("commit", "")
    cur_commit = current["git"]["commit"]
    changed_files: list[str] = []
    commits = 0
    if prev_commit and prev_commit != cur_commit:
        diff = subprocess.run(["git", "diff", "--name-only", prev_commit, cur_commit], cwd=root,
                              capture_output=True, text=True)
        changed_files = [l for l in diff.stdout.splitlines() if l]
        count = subprocess.run(["git", "rev-list", "--count", f"{prev_commit}..{cur_commit}"], cwd=root,
                               capture_output=True, text=True)
        commits = int(count.stdout.strip() or 0) if count.returncode == 0 else 0
    prev_dirty = set(previous.get("inventory", {}).get("git", {}).get("modified_paths", []))
    cur_dirty = set(current["inventory"]["git"]["modified_paths"])
    modules = current["manifest_modules"]
    touched = sorted({C.module_of(p, modules) for p in changed_files + sorted(prev_dirty ^ cur_dirty)})
    return {
        "previous": previous["audit_id"], "previous_profile": previous.get("profile"),
        "previous_commit": prev_commit, "previous_finished_at": previous.get("finished_at"),
        "commits_since": commits, "changed_files": changed_files[:300], "changed_files_count": len(changed_files),
        "working_tree_newly_modified": sorted(cur_dirty - prev_dirty)[:100],
        "working_tree_no_longer_modified": sorted(prev_dirty - cur_dirty)[:100],
        "changed_modules": touched, "status_changes": status_changes, "coverage_changes": coverage_changes,
        "metric_changes": metric_changes,
        "previous_summary": previous.get("summary"),
    }


# ---------------------------------------------------------------- retention
def prune(root: Path, manifest: dict, keep: str, keep_profile: str) -> list[str]:
    """Bounded history: keep every run of a kept profile (baseline) and the
    newest max_runs of the others, the current run included. Only audit run
    directories are removed."""
    policy = manifest.get("retention", {})
    kept_profiles = set(policy.get("keep_profiles", ["baseline"]))
    max_runs = int(policy.get("max_runs", 30)) - (0 if keep_profile in kept_profiles else 1)
    candidates = []
    base = runs_dir(root)
    for path in sorted(base.iterdir()) if base.is_dir() else []:
        if not AUDIT_ID.match(path.name) or path.name == keep:
            continue
        try:
            profile = json.loads((path / "audit.json").read_text(encoding="utf-8")).get("profile")
        except (OSError, ValueError):
            profile = None
        if profile not in kept_profiles:
            candidates.append(path)
    removed = []
    for path in candidates[:max(0, len(candidates) - max_runs)]:
        shutil.rmtree(path, ignore_errors=True)
        removed.append(path.name)
    return removed


# ---------------------------------------------------------------------- run
def selected(check: dict, profile: str, costs: list[str], only: list[str] | None) -> tuple[bool, str]:
    if only and check["id"] not in only:
        return False, "not selected (--check)"
    cost = check.get("cost", "fast")
    if cost not in costs:
        if cost == "expensive":
            return False, "NOT APPLICABLE in Phase 1: " + (check.get("reason") or "expensive check")
        return False, f"cost class '{cost}' is not part of the {profile} profile"
    return True, ""


def run_audit(root: Path, profile: str, build_dir: Path, run_tests: str | None, no_tests: bool,
              only: list[str] | None, previous_id: str | None, ai: bool, quiet: bool = False) -> dict:
    manifest = load_manifest(root)
    profiles = manifest["profiles"]
    if profile not in profiles:
        raise SystemExit(f"unknown profile {profile!r}; one of {', '.join(profiles)}")
    spec = profiles[profile]
    costs = list(spec.get("costs", ["fast"]))
    tier = run_tests or spec.get("run_tests_tier", "t1")
    if run_tests and "tests" not in costs:
        costs.append("tests")
    if no_tests and "tests" in costs:
        costs.remove("tests")
    budget = float(spec.get("budget_seconds", 300))

    earlier = completed_runs(root)
    audit_id, run_dir = allocate_run(root)
    evidence_dir = run_dir / "evidence"
    evidence_dir.mkdir()
    started_at = datetime.now(timezone.utc)
    started = time.monotonic()
    before = tree_snapshot(root)
    ctx = C.Context(root, manifest, profile, build_dir, tier)

    results = []
    for check in manifest["checks"]:
        effective = check
        run, why = selected(effective, profile, costs, only)
        entry = {"id": check["id"], "domain": check["domain"], "title": check["title"],
                 "rule": check.get("rule"), "source": check.get("source"), "kind": check.get("kind"),
                 "cost": check.get("cost", "fast"), "method": _method(effective)}
        if run and time.monotonic() - started > budget:
            run, why = False, f"profile time budget of {budget:.0f} s exhausted"
        if not run:
            entry.update(C.result(C.SKIPPED, why))
            entry.update({"seconds": 0.0, "evidence_file": None,
                          "future_evidence": check.get("future_evidence")})
            del entry["evidence"]
            results.append(entry)
            continue
        if not quiet:
            print(f"[audit] {check['id']} ...", end="", flush=True)
        t0 = time.monotonic()
        res = C.evaluate(effective, ctx)
        entry.update(res)
        entry["seconds"] = round(time.monotonic() - t0, 2)
        entry["future_evidence"] = check.get("future_evidence") if res["status"] == C.UNKNOWN else None
        evidence_file = evidence_dir / f"{check['id']}.txt"
        evidence_file.write_text(f"audit: {audit_id}\ncheck: {check['id']}\nrule: {check.get('rule')}\n"
                                 f"source: {check.get('source')}\nstatus: {res['status']}\n"
                                 f"summary: {res['summary']}\n\n{res.pop('evidence', '') or ''}\n"[:400_000],
                                 encoding="utf-8")
        entry.pop("evidence", None)
        entry["evidence_file"] = str(evidence_file.relative_to(run_dir))
        results.append(entry)
        if not quiet:
            print(f" {res['status']} ({entry['seconds']} s)", flush=True)

    inventory = C.inventory(ctx)
    after = tree_snapshot(root)
    ro = read_only_result(before, after)
    (evidence_dir / "audit.read-only.txt").write_text(f"audit: {audit_id}\n{ro.pop('evidence')}\n", encoding="utf-8")
    results.append({"id": "audit.read-only", "domain": "audit", "title": "The audit left the working tree as it found it",
                    "rule": "ARCH-AUDIT-006", "source": "docs/requirements/architecture_audit.md", "kind": "builtin",
                    "cost": "fast", "method": "git status + content hashes before / after", "seconds": 0.0,
                    "evidence_file": "evidence/audit.read-only.txt", "future_evidence": None, **ro})

    summary: dict[str, dict[str, int]] = {}
    for entry in results:
        summary.setdefault(entry["domain"], {s: 0 for s in C.STATES})
        summary[entry["domain"]][entry["status"]] += 1
    totals = {s: sum(d[s] for d in summary.values()) for s in C.STATES}

    data = {
        "audit_id": audit_id, "tool_version": TOOL_VERSION, "manifest_version": manifest.get("audit_version"),
        "project": manifest.get("project"), "profile": profile, "profile_costs": costs,
        "run_tests_tier": tier if "tests" in costs else None,
        "started_at": started_at.isoformat(), "finished_at": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": round(time.monotonic() - started, 1), "budget_seconds": budget,
        "root": str(root), "build_dir": str(build_dir),
        "git": {k: inventory["git"][k] for k in ("branch", "commit", "describe", "commit_date", "modified", "untracked")},
        "environment": inventory["environment"],
        "canonical_sources": manifest.get("canonical_sources", {}),
        "manifest_modules": manifest.get("modules", []),
        "summary": {"by_domain": summary, "totals": totals},
        "checks": results, "inventory": inventory,
    }
    previous = None
    if previous_id:
        previous = next((r for r in earlier if r["audit_id"] == previous_id), None)
        if previous is None:
            print(f"[audit] previous audit {previous_id} not found; comparing with the latest", file=sys.stderr)
    if previous is None:
        same = [r for r in earlier if r.get("profile") == profile]
        previous = (same or earlier or [None])[-1]
    data["comparison"] = compare(root, data, previous)
    data["retention_removed"] = prune(root, manifest, keep=audit_id, keep_profile=profile)

    (run_dir / "audit.json").write_text(json.dumps(data, indent=1, sort_keys=False), encoding="utf-8")
    (run_dir / "audit.md").write_text(report.human(data), encoding="utf-8")
    (run_dir / "audit-packet.md").write_text(report.packet(data), encoding="utf-8")
    if ai:
        _ai_analysis(run_dir)
    return data


def _method(check: dict) -> str:
    kind = check.get("kind")
    if kind == "command":
        return " ".join(check.get("command", [])) or "(none)"
    if kind == "invariant":
        return "scripts/invariant_check.py --json, messages starting with " + " | ".join(check.get("match", []))
    if kind == "builtin":
        return f"builtin {check.get('function')} (tools/audit_system/checks.py)"
    return "none: documented rule without a verification mechanism"


def _ai_analysis(run_dir: Path) -> None:
    """Optional, vendor-neutral: pipe the packet into AUDIT_AI_COMMAND (any
    command reading a prompt on stdin). Its answer is interpretation and is
    stored apart from the evidence; the deterministic audit never needs it."""
    command = os.environ.get("AUDIT_AI_COMMAND")
    target = run_dir / "ai-analysis.md"
    if not command:
        print("[audit] --ai: AUDIT_AI_COMMAND is not set; the packet is ready for any assistant: "
              f"{run_dir / 'audit-packet.md'}")
        return
    packet = (run_dir / "audit-packet.md").read_text(encoding="utf-8")
    try:
        proc = subprocess.run(command, shell=True, input=packet, capture_output=True, text=True,
                              timeout=float(os.environ.get("AUDIT_AI_TIMEOUT", "900")))
        body = proc.stdout if proc.returncode == 0 else f"AI command failed (exit {proc.returncode}):\n{proc.stderr}"
    except subprocess.TimeoutExpired:
        body = "AI command timed out."
    target.write_text("# AI analysis (interpretation of the audit packet; not evidence)\n\n"
                      f"Command: `{command}`\n\n{body}\n", encoding="utf-8")
    print(f"[audit] AI analysis: {target}")


def list_checks(root: Path) -> None:
    manifest = load_manifest(root)
    profiles = manifest["profiles"]
    print(f"{'check':34s} {'domain':14s} {'cost':9s} " + " ".join(f"{p:9s}" for p in profiles))
    for check in manifest["checks"]:
        marks = []
        for name, spec in profiles.items():
            marks.append(f"{'run' if check.get('cost', 'fast') in spec.get('costs', []) else '-':9s}")
        print(f"{check['id']:34s} {check['domain']:14s} {check.get('cost', 'fast'):9s} " + " ".join(marks))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tools/audit", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("profile", nargs="?", default="standard", help="quick | standard | baseline")
    parser.add_argument("--build-dir", default=None, help="build tree to inspect (never built; default "
                        "paths.build_dir of devsys.json)")
    parser.add_argument("--run-tests", choices=TEST_TIERS,
                        help="also execute this tier through scripts/run_tests.py (baseline: t1 by default)")
    parser.add_argument("--no-tests", action="store_true", help="baseline without test execution")
    parser.add_argument("--check", action="append", help="run only this check (repeatable)")
    parser.add_argument("--previous", help="compare with this audit instead of the latest one")
    parser.add_argument("--ai", action="store_true", help="pipe the packet into $AUDIT_AI_COMMAND")
    parser.add_argument("--list", action="store_true", help="list checks and the profiles that run them")
    parser.add_argument("--json", action="store_true", help="print audit.json instead of the summary")
    parser.add_argument("--fail-on", choices=("fail", "drift", "unknown"),
                        help="exit 1 when a check is FAIL (fail), FAIL or DRIFT (drift), or FAIL / DRIFT / UNKNOWN "
                             "(unknown); default: always 0 (the audit is advisory)")
    args = parser.parse_args(argv)
    root = repo_root()
    if args.list:
        list_checks(root)
        return 0
    if args.build_dir is None:
        try:
            cfg = json.loads((root / "devsys.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cfg = {}
        args.build_dir = (cfg.get("paths") or {}).get("build_dir", "build/debug")
    build_dir = Path(args.build_dir)
    build_dir = build_dir if build_dir.is_absolute() else root / build_dir
    data = run_audit(root, args.profile, build_dir.resolve(), args.run_tests, args.no_tests, args.check,
                     args.previous, args.ai, quiet=args.json)
    run_dir = runs_dir(root) / data["audit_id"]
    if args.json:
        print((run_dir / "audit.json").read_text(encoding="utf-8"))
    else:
        print(report.console(data, run_dir))
    if args.fail_on:
        bad = {"fail": {C.FAIL}, "drift": {C.FAIL, C.DRIFT}, "unknown": {C.FAIL, C.DRIFT, C.UNKNOWN}}[args.fail_on]
        return 1 if any(c["status"] in bad for c in data["checks"]) else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
