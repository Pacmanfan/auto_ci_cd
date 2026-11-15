#!/usr/bin/env python3
"""
Build health check — can the autonomous system currently build and verify?

Checks, for the build system configured in devsys.json ("build"):
  * the adapter's tools are present (scripts/devsys_build.py check)
  * the build tree exists and is configured
  * generated code is not older than its sources (build.generated: a list of
    {"sources": [globs], "outputs": [globs]}; optional)
  * the crash-dump directory is writable
Distinguishes infrastructure failure (missing tool: FAIL) from state that
the next build fixes (unconfigured tree: WARN). With build.system "none" the
build part is NOT CONFIGURED (reported, not failed).

Usage:
  python3 scripts/build_health.py [--probe-build] [--json]
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devsys_build  # noqa: E402
import devsys_config as cfgmod  # noqa: E402

ROOT = cfgmod.repo_root()


def _shown(path: Path) -> str:
    """Repository-relative when inside the repository; a configured path
    (crash dumps, build tree) may be absolute and elsewhere."""
    return str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)


def _newest(patterns):
    best = 0.0
    for pattern in patterns:
        for p in ROOT.glob(pattern):
            if p.is_file():
                best = max(best, p.stat().st_mtime)
    return best


def _oldest(patterns):
    times = [p.stat().st_mtime for pattern in patterns for p in ROOT.glob(pattern) if p.is_file()]
    return min(times) if times else None


def check():
    fails, warns, passes = [], [], []
    s = devsys_build.settings(ROOT)
    if s["system"] == "none":
        warns.append("build: NOT CONFIGURED (build.system is 'none' in devsys.json)")
    else:
        missing = devsys_build.check(ROOT, s)
        for m in missing:
            fails.append(f"tool missing: {m}")
        if not missing:
            passes.append(f"{s['system']} tools present")
        bd = ROOT / s["build_dir"]
        if not bd.exists():
            warns.append(f"{s['build_dir']} missing — python3 scripts/devsys.py build build")
        elif not devsys_build.configured(ROOT, s):
            warns.append(f"{s['build_dir']} is not configured — python3 scripts/devsys.py build configure")
        else:
            passes.append(f"{s['build_dir']} configured")
    for i, rule in enumerate(cfgmod.get("build.generated", ROOT, []) or []):
        newest_src, oldest_out = _newest(rule.get("sources", [])), _oldest(rule.get("outputs", []))
        if newest_src and oldest_out is None:
            warns.append(f"generated code {rule.get('outputs')} missing — run the generator")
        elif newest_src and oldest_out is not None and newest_src > oldest_out:
            warns.append(f"generated code {rule.get('outputs')} older than {rule.get('sources')} — stale")
        elif newest_src:
            passes.append(f"generated code {rule.get('outputs')} up to date")
    # Read-only (the audit runs this check): test the directory, or the
    # nearest existing parent it would be created in, without creating it.
    dump = ROOT / cfgmod.get("paths.crash_dumps", ROOT, "log/dump")
    existing = next((d for d in [dump, *dump.parents] if d.exists()), None)
    if existing is not None and existing.is_dir() and os.access(existing, os.W_OK | os.X_OK):
        passes.append(f"{_shown(dump)} writable" if existing == dump else
                      f"{_shown(dump)} can be created (in {_shown(existing)})")
    else:
        warns.append(f"{_shown(dump)} not writable")
    return fails, warns, passes


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--probe-build", action="store_true", help="run the configured build once")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    fails, warns, passes = check()
    if args.probe_build:
        record = devsys_build.run(ROOT, "build")
        if record["status"] == "PASS":
            passes.append("probe build succeeded")
        elif record["status"] == "FAIL":
            fails.append(f"probe build failed ({record['failure_class']}): {record.get('detail')}")
    if args.json:
        print(json.dumps({"fails": fails, "warns": warns, "passes": passes}, indent=2))
    else:
        for p in passes:
            print(f"PASS {p}")
        for w in warns:
            print(f"WARN {w}")
        for f in fails:
            print(f"FAIL {f}")
        lvl = "FAIL" if fails else ("WARN" if warns else "PASS")
        print(f"\nBUILD HEALTH: {lvl}  ({len(fails)} fail, {len(warns)} warn, {len(passes)} pass)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
