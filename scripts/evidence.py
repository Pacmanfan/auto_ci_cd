#!/usr/bin/env python3
"""Evidence records (evidence/VER-NNNNNN-*.md, evidence/evidence.txt; ADR-000199).

    python3 scripts/evidence.py create --work WORK-000123 --status pass|fail|blocked|pre-existing
                                       --command "python3 scripts/run_tests.py --tier t2 --request ..."
                                       --result "t2 PASS, 12 cases" [--test-run RUN_ID ...]
    python3 scripts/evidence.py attach-run VER-000045 RUN_ID
    python3 scripts/evidence.py show VER-000045

``create`` allocates the VER id, writes the record with the fields the
completion gate requires (Status, Requirements, Work, Command, Result,
Test-Run), adds the evidence.txt row, links the id from the work record's
``**Evidence:**`` header and copies each test run's record into
``evidence/VER-NNNNNN/run-<run id>.json`` — build trees are local and
disposable, evidence is committed, so another agent can verify the run
(CI_CD_ROADMAP BLD-02). Every file is registered in the request's claim
before it is written. The caller must own the claim of the request the work
record links.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_process as proc  # noqa: E402
import devsys_config as cfgmod  # noqa: E402
import devsys_records as records  # noqa: E402
import work as worktool  # noqa: E402

TEMPLATE = """# {ver_id}: {title}

**Status:** {status}
**Requirements:** {requirements}
**Work:** {work_id}
**Command:** `{command}`
**Result:** {result}
{runs}
## Environment

- revision: {head}
- recorded: {now} by {agent}

## Notes

{notes}
"""


def _run_record(root: Path, run_id: str) -> Path:
    import run_tests
    path = run_tests.find_run_record(run_id, root)
    if path is None:
        raise proc.ProcessError(proc.NOT_FOUND, f"no run record for {run_id} (build tree test-results/runs/ or evidence/*/run-{run_id}.json)")
    return path


def attach_run(root: Path, request: str, agent_id: str, ver_id: str, run_id: str) -> Path:
    src = _run_record(root, run_id)
    dst = cfgmod.path(root, "evidence") / ver_id / f"run-{run_id}.json"
    worktool.ensure_owned(root, request, agent_id, [proc.rel(root, dst)])
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.resolve() != dst.resolve():
        shutil.copyfile(src, dst)
    return dst


def create(root: Path, work_id: str, agent_id: str, status: str, command: str, result: str, runs: list[str],
           notes: str = "") -> Path:
    if status not in cfgmod.VERIFICATION_STATES:
        raise proc.ProcessError(proc.INVALID, f"status {status!r} is not one of {', '.join(cfgmod.VERIFICATION_STATES)}")
    record = worktool.find_record(root, work_id)
    wtext = record.read_text(encoding="utf-8")
    request = worktool.request_of(root, wtext)
    proc.require_owner(root, request, agent_id)
    for rid in runs:
        _run_record(root, rid)                      # refuse before anything is written
    with proc.exclusive(root, "work"):              # record, manifests and work links change together
        return _create(root, work_id, agent_id, status, command, result, runs, notes, record, wtext, request)


def _create(root, work_id, agent_id, status, command, result, runs, notes, record, wtext, request) -> Path:
    ver_id = proc.allocate_id(root, "VER", agent_id)
    title = f"{work_id} verification"
    path = cfgmod.path(root, "evidence") / f"{ver_id}-{records.slug(work_id + '-' + status)}.md"
    manifest = cfgmod.path(root, "evidence_manifest")
    worktool.ensure_owned(root, request, agent_id, [proc.rel(root, path), proc.rel(root, record)])
    worktool.ensure_owned(root, request, agent_id, [proc.rel(root, manifest)], shared=True)
    proc.add_tokens(root, request, agent_id, [ver_id, work_id])
    requirements = ", ".join(records.cited_requirements(wtext)) or "-"
    import subprocess
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root, capture_output=True, text=True).stdout.strip()
    run_lines = "".join(f"**Test-Run:** {rid}\n" for rid in runs)
    text = TEMPLATE.format(ver_id=ver_id, title=title, status=status.upper(), requirements=requirements,
                           work_id=work_id, command=command, result=result, runs=run_lines, head=head or "-",
                           now=proc.now_iso(), agent=agent_id, notes=notes or "-")
    proc.create_exclusive_text(path, text)
    for rid in runs:
        attach_run(root, request, agent_id, ver_id, rid)
    mtext = manifest.read_text(encoding="utf-8") if manifest.exists() else \
        "# Verification Evidence Manifest\n\nFormat: `VER-ID | classification | date | requirements/work | record`\n\n"
    row = (f"{ver_id} | {status} | {datetime.now().strftime('%Y-%m-%d')} | {requirements} / {work_id} | "
           f"{path.name}")
    proc.atomic_write_text(manifest, records.upsert_manifest_row(mtext, ver_id, row))
    # Link from the work record (Evidence header, Test-Run lines).
    evidence = records.header(wtext, "Evidence") or ""
    ids = [x for x in re.findall(r"VER-\d{6}", evidence)] + [ver_id]
    wtext = records.set_header(wtext, "Evidence", ", ".join(dict.fromkeys(ids)))
    for rid in runs:
        if rid not in wtext:
            wtext = records.set_header(wtext, "Test-Run", rid) if records.header(wtext, "Test-Run") is None else \
                re.sub(r"^(\s*\*\*Test-Run:\*\*.*)$", lambda m: f"{m.group(1)}\n**Test-Run:** {rid}", wtext,
                       count=1, flags=re.MULTILINE)
    proc.atomic_write_text(record, wtext)
    worktool.update_manifest(root, request, agent_id, work_id, record.parent.name, record)
    worktool.journal(root, {"evidence": ver_id, "work": work_id, "event": "evidence-created", "status": status,
                            "runs": runs, "agent_id": agent_id})
    return path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create")
    c.add_argument("--work", required=True)
    c.add_argument("--status", required=True, choices=cfgmod.VERIFICATION_STATES)
    c.add_argument("--command", required=True)
    c.add_argument("--result", required=True)
    c.add_argument("--test-run", nargs="*", default=[])
    c.add_argument("--notes", default="")
    r = sub.add_parser("attach-run")
    r.add_argument("ver")
    r.add_argument("run")
    s = sub.add_parser("show")
    s.add_argument("ver")
    for p in (c, r):
        p.add_argument("--agent-id", default=os.environ.get("AGENT_ID"))
    a = ap.parse_args(argv)
    root = proc.repo_root()
    try:
        if a.cmd == "create":
            path = create(root, a.work, a.agent_id, a.status, a.command, a.result, a.test_run, a.notes)
            print(f"CREATED {proc.rel(root, path)}")
            print(f"VER-ID {path.name[:10]}")
            return 0
        if a.cmd == "attach-run":
            ev = sorted(cfgmod.path(root, "evidence").glob(f"{a.ver}-*.md"))
            if not ev:
                raise proc.ProcessError(proc.NOT_FOUND, f"no evidence record {a.ver}")
            wid = records.header(ev[0].read_text(encoding="utf-8"), "Work") or ""
            wid = (re.findall(r"WORK-\d{6}", wid) or [""])[0]
            request = worktool.request_of(root, worktool.find_record(root, wid).read_text(encoding="utf-8"))
            dst = attach_run(root, request, a.agent_id, a.ver, a.run)
            text = ev[0].read_text(encoding="utf-8")
            if a.run not in text:
                worktool.ensure_owned(root, request, a.agent_id, [proc.rel(root, ev[0])])
                proc.atomic_write_text(ev[0], text.rstrip("\n") + f"\n**Test-Run:** {a.run}\n")
            print(f"ATTACHED {proc.rel(root, dst)}")
            return 0
        if a.cmd == "show":
            ev = sorted(cfgmod.path(root, "evidence").glob(f"{a.ver}-*.md"))
            if not ev:
                raise proc.ProcessError(proc.NOT_FOUND, f"no evidence record {a.ver}")
            print(ev[0].read_text(encoding="utf-8"))
            return 0
    except proc.ProcessError as e:
        print(e.message, file=sys.stderr)
        return e.code
    return proc.INVALID


if __name__ == "__main__":
    sys.exit(main())
