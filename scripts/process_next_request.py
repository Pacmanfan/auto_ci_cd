#!/usr/bin/env python3
"""
Safe process_next_request contract.

Implements ~19 steps:
 1. Find highest-priority eligible request (sidecar priority
    CRITICAL > HIGH > NORMAL > LOW, lexicographic filename tiebreak)
 2. Confirm not already claimed (claim.py)
 3. Claim it
 4. Inspect requirements
 5. Inspect architecture
 6. Determine intent
 7. Check for duplicates (idempotency)
 8. Create/update requirement (manual step — report)
 9. Create work
10. Implement (manual — report)
11. Test (manual — report)
12. Verify (completion gate check)
13. Update documentation (report)
14. Run consistency checks
15. Complete work only if completion gate passes
16. Record evidence (report)
17. Archive/complete the request
18. Release the claim
19. Report result

If any stage fails: STOP AT APPROPRIATE STATE.

Usage:
  python3 scripts/process_next_request.py --dry-run   # no mutation
  python3 scripts/process_next_request.py             # process one request
  python3 scripts/process_next_request.py --request requests/inbox/foo.md
"""
import argparse
import json
import re
import subprocess
import os
import sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_process as proc  # noqa: E402

ROOT = proc.repo_root()

def _list_inbox():
    inbox = ROOT / "requests" / "inbox"
    files = sorted([p for p in inbox.glob("*.md") if p.name != "README.md"])
    return files

# Priority order for inbox selection (REQ-DEVOPS-043, ADR-000150).
_PRIORITY_RANK = {"CRITICAL": 0, "HIGH": 1, "NORMAL": 2, "LOW": 3}

def _inbox_priority(p: Path) -> int:
    """Rank of an inbox request's priority (lower is earlier).

    Reads the machine sidecar (<stem>.json); falls back to the
    **Priority:** header in the markdown; unknown/missing is NORMAL.
    Listing or reading a ticket never claims it.
    """
    try:
        sidecar = p.with_suffix(".json")
        if sidecar.exists():
            prio = str(json.loads(sidecar.read_text(encoding="utf-8")).get("priority", "NORMAL")).upper()
            return _PRIORITY_RANK.get(prio, 2)
    except Exception:
        pass
    m = re.search(r"\*\*Priority:\*\*\s*(\w+)", _read(p))
    if m:
        return _PRIORITY_RANK.get(m.group(1).upper(), 2)
    return 2

def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="ignore")
    except:
        return ""

def _find_next():
    """Highest-priority inbox request that nobody holds (an inbox request with
    a claim is an interrupted claim: scripts/claim.py --sweep)."""
    import devsys
    return devsys.next_eligible(ROOT)

def _run(cmd, timeout=120):
    try:
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout + r.stderr
    except subprocess.TimeoutExpired:
        return None, f"UNKNOWN: {' '.join(cmd[1:])} did not finish within {timeout}s"
    except Exception as e:
        return 99, str(e)

def do_dry_run(request_path: Path):
    print(f"DRY RUN for {request_path}")
    print("="*70)
    txt = _read(request_path)
    print(f"Request size: {len(txt)} chars, {len(txt.splitlines())} lines")
    print(f"First 300 chars: {txt[:300]!r}")
    print("\n[Intended requirement changes]")
    # heuristic: suggest domain
    lower = txt.lower()
    # Requirement-domain keywords (devsys.json requirement_impact.domains): each
    # names a docs/requirements/<kw>.md owner document to check.
    import devsys_config as cfgmod
    for kw in sorted(cfgmod.get("requirement_impact.domains", ROOT, {})):
        if kw in lower:
            print(f"  - keyword '{kw}' -> suggest checking docs/requirements/{kw}.md")
    # impact
    rc, out = _run([sys.executable, "scripts/requirement_impact.py", "--requirement", "docs/requirements/development_operations.md"])
    # just show that we would run impact; don't repeat full
    print("\n[Affected architecture]")
    print("  - docs/architecture/*, docs/decisions/ if needed")
    print("\n[Proposed work]")
    print("  - would create WORK-NNNNNN under work/active/ (idempotent check first)")
    # check for duplicate work?
    print("\n[Affected tests]")
    print("  - the project's registered test suites (scripts/test_manifest.json) per docs/verification/tests.md")
    print("\n[Documentation impact]")
    print("  - docs/README.md, relevant subsystem READMEs, docs/architecture/ if needed")
    print("\n[Expected scope]")
    # git diff stat for current changes
    try:
        diff = subprocess.check_output(["git", "diff", "--stat"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL)
        print(f"  Current git diff --stat:\n{diff[:600] if diff else '  (no current diff)'}")
    except:
        pass
    print("\n[Preflight]")
    rc, out = _run([sys.executable, "scripts/preflight.py", "--request", str(request_path)])
    print(out[:1200])
    if rc != 0:
        print("Preflight would FAIL — would escalate to needs-review, not proceed")
    else:
        print("Preflight PASS (with possible WARNs) — would proceed to implementation")
    print("\nDry run complete — no files mutated.")

def do_process(request_path: Path, agent_id: str):
    print(f"Processing {request_path}")
    # Step 2-3: claim
    rc, out = _run([sys.executable, "scripts/claim.py", "--claim-request", request_path.name, "--agent-id", agent_id])
    print(out)
    if rc != 0:
        if rc == proc.CONFLICT:
            print("Aborting: another agent owns this request (see owner above).", file=sys.stderr)
        elif rc == proc.STALE:
            print("Owner looks stale or the request is orphaned — inspect with scripts/claim.py --show, then "
                  "recover explicitly with --recover-request.", file=sys.stderr)
        else:
            print(f"Claim failed rc={rc}", file=sys.stderr)
        sys.exit(rc)

    # claim.py moved the request inbox -> processing atomically while holding the claim.
    request_path = ROOT / "requests" / "processing" / request_path.name

    # Step 4-7: preflight + duplicate check
    rc, out = _run([sys.executable, "scripts/preflight.py", "--request", str(request_path)])
    print(out)
    if rc == 2:
        print("Preflight FAIL — escalating to needs-review", file=sys.stderr)
        # move to needs-review
        rc2, out2 = _run([sys.executable, "scripts/claim.py", "--needs-review-request", request_path.name,
                          "--agent-id", agent_id])
        print(out2)
        sys.exit(2)
    elif rc != 0:
        print(f"Preflight error rc={rc}")

    # Report next steps (human/agent must implement)
    n = request_path.name
    print("\nNext steps for agent (the lifecycle tools record each step):")
    print("  1. Find the owning requirement (docs/requirements/requirements.txt); extend it, do not duplicate.")
    print("  2. Architecture change: create an ADR (python3 scripts/allocate_id.py ADR; docs/decisions/).")
    print(f"  3. python3 scripts/work.py create --request {n} --title '...' --requirements <REQ-IDS>")
    print(f"  4. Register every path BEFORE editing it: python3 scripts/claim.py --own {n} --paths ...")
    print(f"  5. Implement; test: python3 scripts/run_tests.py --tier t2 --request requests/processing/{n}")
    print("  6. python3 scripts/evidence.py create --work WORK-... --status pass --command '...' --result '...' "
          "--test-run <run id>")
    print("  7. Update documentation; python3 scripts/drift_detect.py && python3 scripts/doc_integrity.py")
    print("  8. python3 scripts/completion_gate.py --work WORK-... --record   (must PASS)")
    print("  9. python3 scripts/work.py transition WORK-... --to completed")
    print(f"  10. python3 scripts/claim.py --complete-request {n}")
    print(f"  11. python3 scripts/commit_request.py --request {n} -m '...'  (never git add -A / git commit -a)")
    print(f"  12. python3 scripts/claim.py --release-request {n}")

    print("\nState mirrors: python3 scripts/devsys.py status --write-mirrors")

    print("\nProcess-next-request claimed and staged. Implementation is manual/autonomous next phase.")

def _require_agent(agent_id):
    try:
        proc.validate_agent_id(agent_id)
    except proc.ProcessError as e:
        print(e.message, file=sys.stderr)
        sys.exit(e.code)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report plan without mutating")
    ap.add_argument("--request", help="specific inbox file to process")
    ap.add_argument("--agent-id", default=os.environ.get("AGENT_ID"),
                    help="session-unique agent id (scripts/claim.py --new-agent-id); required unless --dry-run")
    args = ap.parse_args()

    if args.request:
        rp = Path(args.request)
        if not rp.is_absolute():
            rp = ROOT / rp
        if not rp.exists():
            print(f"Request not found: {rp}", file=sys.stderr)
            sys.exit(1)
        if args.dry_run:
            do_dry_run(rp)
        else:
            _require_agent(args.agent_id)
            do_process(rp, args.agent_id)
        return

    nxt = _find_next()
    if not nxt:
        print("No eligible request in requests/inbox/ (next_eligible: none)")
        # also report health
        import devsys_config as cfgmod
        budget = float(cfgmod.get("health.structure_timeout_seconds", ROOT, 300)) + 60
        rc, out = _run([sys.executable, "scripts/health_check.py"], timeout=budget)
        print(out[-800:])
        sys.exit(0)

    print(f"Next eligible: {nxt.relative_to(ROOT)}")
    if args.dry_run:
        do_dry_run(nxt)
    else:
        _require_agent(args.agent_id)
        do_process(nxt, args.agent_id)

if __name__ == "__main__":
    main()
