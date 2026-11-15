#!/usr/bin/env python3
"""
Repository health check — one PASS / WARN / FAIL for autonomous development.

Checks: the development-system structure (scripts/verify_development_system.sh,
unit tests excluded: they belong to the test orchestrator's `scripts` suite),
drift, documentation integrity, architectural invariants, configuration, the
process documents, the claim state (shared across worktrees) and the
needs-review directories.

A check that could not finish (timeout, crash of the check itself) is UNKNOWN:
it makes the overall result WARN, never FAIL — a slow host is not an unhealthy
repository (CI_CD_ROADMAP COR-12). The structure budget is
health.structure_timeout_seconds in devsys.json.

Usage:
  python3 scripts/health_check.py [--json]

Exit: 0 PASS / WARN, 1 FAIL.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devsys_config as cfgmod  # noqa: E402

ROOT = cfgmod.repo_root()


def run_script(cmd, timeout):
    env = dict(os.environ, DEVSYS_SKIP_UNITTESTS="1", AGENT_PROCESS_ROOT=str(ROOT))
    try:
        out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout, env=env)
        return out.returncode, (out.stdout + out.stderr).strip()
    except subprocess.TimeoutExpired:
        return None, f"did not finish within {timeout:.0f}s"
    except Exception as e:  # the check itself could not run
        return None, str(e)


def level_of(rc, out, fail_marker=True):
    if rc is None:
        return "UNKNOWN"
    if rc != 0:
        return "FAIL"
    return "WARN" if "WARN" in out else "PASS"


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    budget = float(cfgmod.get("health.structure_timeout_seconds", ROOT, 300))
    checks = []

    # The structural check itself (what scripts/verify_development_system.sh wraps
    # for POSIX shells), run directly so that no bash is needed.
    rc, out = run_script([sys.executable, "scripts/devsys.py", "verify-structure"], budget)
    checks.append(("verify_development_system", level_of(rc, out), out.splitlines()[-1] if out else ""))
    for name, cmd in (("drift_detect", [sys.executable, "scripts/drift_detect.py"]),
                      ("doc_integrity", [sys.executable, "scripts/doc_integrity.py"]),
                      ("invariant_check", [sys.executable, "scripts/invariant_check.py"])):
        rc, out = run_script(cmd, budget)
        checks.append((name, level_of(rc, out), out.splitlines()[-1] if out else ""))

    problems = cfgmod.validate(ROOT)
    checks.append(("devsys.json", "FAIL" if problems else ("PASS" if (ROOT / cfgmod.CONFIG_FILE).exists() else "WARN"),
                   "; ".join(problems[:3]) if problems else ("valid" if (ROOT / cfgmod.CONFIG_FILE).exists()
                                                            else "absent (all defaults)")))

    for required in ["docs/process/state-model.md", "docs/process/autonomous-operating-model.md",
                     "docs/process/recovery.md", "docs/process/escalation.md", "docs/process/process-protection.md"]:
        exists = (ROOT / required).exists()
        checks.append((required, "PASS" if exists else "FAIL", "exists" if exists else "missing"))

    # Claims live in the MAIN worktree's state directory; it is created on
    # first use, so its absence on a fresh clone is fine.
    import agent_process
    claims = agent_process.state_dir(ROOT) / "claims"
    checks.append(("claim state", "PASS", f"{claims} ({'present' if claims.exists() else 'created on first claim'})"))

    for d in ["requests/needs-review", "work/needs-review"]:
        exists = (ROOT / d).exists()
        checks.append((d, "PASS" if exists else "WARN", "exists" if exists else "missing (optional but recommended)"))

    has_fail = any(lvl == "FAIL" for _, lvl, _ in checks)
    has_warn = any(lvl in ("WARN", "UNKNOWN") for _, lvl, _ in checks)
    overall = "FAIL" if has_fail else ("WARN" if has_warn else "PASS")

    if args.json:
        print(json.dumps({"overall": overall, "checks": [{"name": n, "level": l, "detail": d} for n, l, d in checks]},
                         indent=2))
    else:
        print(f"HEALTH: {overall}")
        for name, lvl, detail in checks:
            print(f"  {lvl:7} {name}: {detail}")
        if overall == "PASS":
            print("\nRepository is internally consistent enough for autonomous development.")
        elif overall == "WARN":
            print("\nWARN: repository has warnings or checks that could not finish (UNKNOWN) — triage them.")
        else:
            print("\nFAIL: repository has failures — do not run autonomous processing until fixed.")
    return 1 if overall == "FAIL" else 0


if __name__ == "__main__":
    sys.exit(main())
