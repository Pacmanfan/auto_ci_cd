#!/usr/bin/env python3
"""
Automatic crash evidence discovery.

Looks for:
  crash dumps in paths.crash_dumps (devsys.json; default log/dump/)
  application logs
  stderr/stdout logs
  recent commits
  changed files
  request/work history related to crash

Usage:
  python3 scripts/crash_evidence.py --request requests/inbox/foo.md
  python3 scripts/crash_evidence.py --request-id REQ-CHAT-20260908-001
  python3 scripts/crash_evidence.py --discover  # just discover latest evidence
"""
import argparse
import subprocess
import json
import sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devsys_config as cfgmod  # noqa: E402

ROOT = cfgmod.repo_root()
_CFG = cfgmod.load(ROOT)

# Crash artifacts of the project's application (devsys.json "crash",
# paths.crash_dumps): where cores are written, how they are named, which logs
# to report and which binary to open them with.
DUMP_DIR = ROOT / _CFG["paths"]["crash_dumps"]
CORE_GLOB = _CFG["crash"]["core_glob"]
FALLBACK_DUMP_DIR = Path(_CFG["crash"]["fallback_dir"]) if _CFG["crash"].get("fallback_dir") else None
APP_LOGS = tuple(_CFG["crash"]["logs"])
BUILD_DIR = _CFG["paths"]["build_dir"]
DEBUG_BINARY = _CFG["crash"].get("debug_binary") or f"{BUILD_DIR}/<APP_BINARY>"


def newest_core():
    dump_dir = DUMP_DIR
    if not dump_dir.exists():
        # check the application's fallback dump directory, if it has one
        alt = FALLBACK_DUMP_DIR
        if alt is not None and alt.exists():
            dump_dir = alt
        else:
            return None
    cores = sorted(dump_dir.glob(CORE_GLOB), key=lambda p: p.stat().st_mtime, reverse=True)
    return cores[0] if cores else None

def recent_commits(n=10):
    try:
        out = subprocess.check_output(["git", "log", "--oneline", f"-{n}"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL)
        return out.strip()
    except:
        return ""

def changed_files():
    try:
        out = subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL)
        return out.strip()[:2000]
    except:
        return ""

def discover():
    ev = {}
    core = newest_core()
    ev["crash_dump"] = (str(core.relative_to(ROOT)) if core.is_relative_to(ROOT) else str(core)) if core else None
    if core:
        try:
            ev["crash_dump_mtime"] = datetime.fromtimestamp(core.stat().st_mtime, tz=timezone.utc).isoformat()
            ev["crash_dump_size"] = core.stat().st_size
        except:
            pass
        # try to get file type
        try:
            out = subprocess.check_output(["file", str(core)], text=True, stderr=subprocess.DEVNULL)
            ev["crash_dump_file"] = out.strip()
        except:
            pass
    # logs
    log_candidates = [ROOT/p for p in APP_LOGS]
    logs = []
    for p in log_candidates:
        if p.exists():
            logs.append(str(p.relative_to(ROOT)))
    # glob logs
    for p in (ROOT/"log").rglob("*.log") if (ROOT/"log").exists() else []:
        if len(logs) < 5 and str(p) not in logs:
            logs.append(str(p.relative_to(ROOT)))
    ev["logs"] = logs
    ev["recent_commits"] = recent_commits(10)
    ev["changed_files"] = changed_files()
    # build dir existence
    ev["build_dir"] = str((ROOT/BUILD_DIR).exists())
    return ev

def attach_to_request(request_path: Path):
    ev = discover()
    ts = datetime.now(timezone.utc).isoformat()
    addition = f"\n\n## Crash Evidence Discovery {ts}\n\n"
    if ev["crash_dump"]:
        addition += f"- Crash dump: `{ev['crash_dump']}`"
        if "crash_dump_mtime" in ev:
            addition += f" (mtime {ev['crash_dump_mtime']}, size {ev['crash_dump_size']})"
        addition += "\n"
        if "crash_dump_file" in ev:
            addition += f"  - file: {ev['crash_dump_file']}\n"
        addition += f"  - GDB: `gdb {DEBUG_BINARY} {ev['crash_dump']}` then `bt`, `thread apply all bt`\n"
    else:
        addition += (f"- Crash dump: _none found in {DUMP_DIR.relative_to(ROOT)}/ (or fallback "
                     f"{FALLBACK_DUMP_DIR or 'none configured'}). Record unavailable._\n")
    if ev["logs"]:
        addition += f"- Logs: {', '.join(f'`{l}`' for l in ev['logs'])}\n"
    else:
        addition += "- Logs: _none found_\n"
    addition += f"- Recent commits:\n```\n{ev['recent_commits'][:1000]}\n```\n"
    if ev["changed_files"]:
        addition += f"- Changed files (git status):\n```\n{ev['changed_files'][:1200]}\n```\n"
    addition += "- If reproduction attempted, record result under ## Reproduction Attempt\n"
    # Read-modify-write under the request's lock (the same lock follow-ups
    # take), written atomically.
    import agent_process as proc
    with proc.exclusive(ROOT, f"request-{request_path.name}"):
        txt = request_path.read_text(encoding="utf-8", errors="ignore")
        if "## Crash Evidence Discovery" not in txt and "## Processing Notes" in txt:
            txt = txt.replace("## Processing Notes", addition + "\n## Processing Notes", 1)
        else:
            txt += addition
        proc.atomic_write_text(request_path, txt)
    print(f"Attached evidence to {request_path.relative_to(ROOT)}")
    for k,v in ev.items():
        print(f"  {k}: {str(v)[:200]}")
    return ev

def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--request", help="request file path")
    g.add_argument("--request-id", help="request ID to find")
    g.add_argument("--discover", action="store_true", help="just discover, don't attach")
    args = ap.parse_args()
    if args.discover:
        ev = discover()
        print(json.dumps(ev, indent=2))
        return
    if args.request:
        p = Path(args.request)
        if not p.is_absolute():
            p = ROOT / p
        attach_to_request(p)
        return
    if args.request_id:
        # find request
        for base in [ROOT/"requests"/"inbox", ROOT/"requests"/"processing", ROOT/"requests"/"needs-review"]:
            for f in base.glob("*.md"):
                if args.request_id.lower() in f.name.lower() or args.request_id in f.read_text(errors="ignore"):
                    attach_to_request(f)
                    return
        print(f"Request {args.request_id} not found", file=sys.stderr)
        raise SystemExit(1)

if __name__ == "__main__":
    main()
