#!/usr/bin/env python3
"""
Allocate a globally unique id atomically (REQ-DEVOPS-003, ADR-000090).

  python3 scripts/allocate_id.py WORK        -> WORK-000168
  python3 scripts/allocate_id.py VER|ADR
  python3 scripts/allocate_id.py REQ-CHAT-20260914   (request series; intake does this itself)

The id is larger than any id in any store (including completed, rejected,
cancelled and archived records) and than any id ever issued by the allocator,
so ids are never reused and concurrent callers never receive the same one.
"""
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_process as proc  # noqa: E402


def main():
    if len(sys.argv) != 2 or not (sys.argv[1] in proc.SEQ_KINDS or re.fullmatch(r"REQ-[A-Z]+-\d{8}", sys.argv[1])):
        print(__doc__, file=sys.stderr)
        return proc.INVALID
    print(proc.allocate_id(proc.repo_root(), sys.argv[1], os.environ.get("AGENT_ID", "")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
