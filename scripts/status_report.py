#!/usr/bin/env python3
"""
Automatic status reporting — answers from persistent artifacts.

Covers:
  What is being worked on?  What is blocked?  Why hasn't my request been processed?
  What changed because of my request?  What remains to be verified?
  What caused last crash?  What requirements changed?  What tests prove fix?

Usage:
  python3 scripts/status_report.py
  python3 scripts/status_report.py --request REQ-CHAT-20260908-001
  python3 scripts/status_report.py --json
"""
import argparse
import re
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devsys_config as cfgmod  # noqa: E402

ROOT = cfgmod.repo_root()

def _read(p: Path):
    try:
        return p.read_text(encoding="utf-8", errors="ignore")
    except:
        return ""

def _list_requests():
    out = []
    for base in [ROOT/"requests"/"inbox", ROOT/"requests"/"processing", ROOT/"requests"/"needs-review", ROOT/"requests"/"completed", ROOT/"requests"/"rejected"]:
        if not base.exists():
            continue
        for f in sorted(base.glob("*.md")):
            if f.name=="README.md":
                continue
            out.append((str(base.relative_to(ROOT)), f))
    return out

def _list_work():
    work_txt = _read(ROOT/"work"/"work.txt")
    rows = []
    for line in work_txt.splitlines():
        if not line.strip() or line.strip().startswith("#") or "Format" in line:
            continue
        parts=[p.strip() for p in line.split("|")]
        if len(parts)>=4:
            rows.append(parts)
    return rows

def status_for_request(req_id: str):
    # find request file
    for _, f in _list_requests():
        if req_id.lower() in f.name.lower() or req_id in _read(f):
            txt=_read(f)
            # trace work
            m=re.search(r"WORK-\d{6}", txt)
            work_id=m.group(0) if m else None
            work_info=""
            if work_id:
                for row in _list_work():
                    if work_id in row[0]:
                        work_info = f" | ".join(row)
                # find evidence
                ev_txt=_read(ROOT/"evidence"/"evidence.txt")
                ev_line=[l for l in ev_txt.splitlines() if work_id in l]
                work_info+= f"\n  Evidence manifest: {ev_line[:1]}"
            return f"{f.relative_to(ROOT)}:\n{txt[:1500]}\n---\nWork: {work_info}"
    return f"Request {req_id} not found"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--request", help="request ID to detail")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.request:
        print(status_for_request(args.request))
        return

    requests = _list_requests()
    work_rows = _list_work()
    # build summary
    by_state = {}
    for loc, f in requests:
        by_state.setdefault(loc, 0)
        by_state[loc]+=1
    work_by_status={}
    for row in work_rows:
        st=row[1].lower() if len(row)>1 else "?"
        work_by_status[st]=work_by_status.get(st,0)+1

    # last crash
    dump_dir=ROOT/cfgmod.get("paths.crash_dumps", ROOT, "log/dump")
    last_core=None
    if dump_dir.exists():
        cores=sorted(dump_dir.glob(cfgmod.get("crash.core_glob", ROOT, "*.core")), key=lambda p: p.stat().st_mtime, reverse=True)
        last_core=str(cores[0].relative_to(ROOT)) if cores else None

    summary={
        "requests_by_location": by_state,
        "work_by_status": work_by_status,
        "last_crash": last_core,
        "total_requests": len(requests),
        "total_work": len(work_rows),
    }
    if args.json:
        print(json.dumps(summary, indent=2))
        return
    print("=== STATUS REPORT (derived from persistent artifacts) ===")
    print(f"\nRequests: {summary['total_requests']} files")
    for k,v in sorted(by_state.items()):
        print(f"  {k}: {v}")
    print(f"\nWork: {summary['total_work']} entries")
    for k,v in sorted(work_by_status.items()):
        print(f"  {k}: {v}")
    print(f"\nLast crash dump: {last_core or 'none'}")
    print("\nWhat is being worked on?")
    active=[r for r in work_rows if "active" in r[1].lower()]
    for r in active[:5]:
        print(f"  - {' | '.join(r[:4])}")
    if not active:
        print("  (no active work)")
    print("\nWhat is blocked / needs-review?")
    blocked=[r for r in work_rows if "blocked" in r[1].lower() or "needs-review" in r[1].lower()]
    for r in blocked[:5]:
        print(f"  - {' | '.join(r[:4])}")
    if not blocked:
        print("  (none)")
    print("\nWhat remains to be verified?")
    pending=[r for r in work_rows if "pending" in r[5].lower() if len(r)>5]
    for r in pending[:5]:
        print(f"  - {r[0]} pending evidence")
    if not pending:
        print("  (check work/work.txt evidence column)")
    print("\nTip: python3 scripts/status_report.py --request REQ-XXXX  for detail")
    print("      python3 scripts/health_check.py  and python3 scripts/build_health.py")
    print("      python3 scripts/devsys.py status  (claims, liveness, recoverable work, last success / failure)")

if __name__=="__main__":
    main()
