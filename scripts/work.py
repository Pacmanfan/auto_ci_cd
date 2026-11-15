#!/usr/bin/env python3
"""Work-item lifecycle tooling (docs/process/state-model.md §2, ADR-000199).

Work records used to be moved between ``work/<state>/`` directories by hand,
with ``work/work.txt`` and the record's ``**Status:**`` header edited
separately (CI_CD_ROADMAP PRO-01). This tool performs those steps as one
validated, recorded, recoverable transition:

    python3 scripts/work.py create --request <file> --title "..." --requirements REQ-X-001 [...]
                                   [--decision ADR-000123] [--type feature|process-change]
                                   [--test-suites a,b] [--state active|queued]
    python3 scripts/work.py transition WORK-000123 --to verification|completed|blocked|... [--reason TEXT]
    python3 scripts/work.py show WORK-000123 [--json]
    python3 scripts/work.py list [--state active] [--json]

Rules:
  * the caller ($AGENT_ID / --agent-id) must own the claim of the request the
    record links (**Request:**); every file the tool writes is registered in
    that claim first, so scripts/commit_request.py commits it;
  * transitions follow devsys_config.WORK_TRANSITIONS; anything else is refused;
  * -> completed needs the work item's latest gate record to be PASS on the
    request's current code (scripts/completion_gate.py --work W --record);
  * each transition is appended to the record (## Transitions) and to
    .agent/state/runtime/transitions.jsonl; a transition interrupted after the
    move is finished by running the same command again (idempotent).

Exit codes as scripts/claim.py (3 not owner, 4 bad state, 5 not found,
6 invalid, 9 gate required).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_process as proc  # noqa: E402
import devsys_config as cfgmod  # noqa: E402
import devsys_records as records  # noqa: E402

TEMPLATE = """# {work_id}: {title}

**Status:** {state}
**Type:** {wtype}
**Request:** {request_path}
**Requirements:** {requirements}
{decision}**Evidence:** pending
{suites}
## Scope

{title}

## Implementation

- (files changed, and why)

## Tests

- (tests added / updated; the scripts/run_tests.py run linked below as **Test-Run:**)

## Regression impact and documentation

- (what else this can affect; documentation updated)

## Traceability

{request_id} -> {requirements} -> {work_id} -> evidence (pending)

## Transitions

- {now} created in {state} by {agent}
"""


def find_record(root: Path, work_id: str) -> Path:
    found = [f for state in cfgmod.WORK_STATES
             for f in (cfgmod.path(root, "work") / state).glob(f"{work_id}-*.md")]
    found += [f for state in cfgmod.WORK_STATES for f in (cfgmod.path(root, "work") / state).glob(f"{work_id}.md")]
    if not found:
        raise proc.ProcessError(proc.NOT_FOUND, f"no work record named {work_id} in work/<state>/")
    if len(found) > 1:
        raise proc.ProcessError(proc.BAD_STATE, f"{work_id} names {len(found)} records: "
                                + ", ".join(proc.rel(root, f) for f in found))
    return found[0]


def request_of(root: Path, text: str) -> str:
    value = records.header(text, "Request") or ""
    m = re.search(r"[^\s`/]+\.md", value)
    if not m:
        raise proc.ProcessError(proc.INVALID, "the work record has no **Request:** link")
    return m.group(0)


def ensure_owned(root: Path, request: str, agent_id: str, paths: list[str], shared: bool = False) -> None:
    """Register paths in the request's claim before they are written."""
    claim = proc.require_owner(root, request, agent_id)
    todo = [p for p in paths if p not in (claim.get("files") or {})]
    if todo:
        proc.own_paths(root, request, agent_id, todo, shared=shared)


def journal(root: Path, entry: dict) -> None:
    with open(proc.runtime_dir(root) / "transitions.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"at": proc.now_iso(), **entry}) + "\n")


def manifest_row(work_id: str, state: str, title: str, record_rel: str, requirements: str, evidence: str) -> str:
    return f"{work_id} | {state} | {title} | {record_rel} | {requirements} | {evidence}"


def update_manifest(root: Path, request: str, agent_id: str, work_id: str, state: str, record: Path,
                    title: str | None = None) -> None:
    manifest = cfgmod.path(root, "work_manifest")
    ensure_owned(root, request, agent_id, [proc.rel(root, manifest)], shared=True)
    proc.add_tokens(root, request, agent_id, [work_id])
    text = manifest.read_text(encoding="utf-8") if manifest.exists() else \
        "# Active Work Manifest\n\nFormat: `WORK-ID | status | title | record | requirements | evidence`\n\n"
    old = records.manifest_row(manifest, work_id)
    rtext = record.read_text(encoding="utf-8")
    requirements = records.header(rtext, "Requirements?") or (old[4] if old and len(old) > 4 else "")
    evidence = records.header(rtext, "Evidence") or (old[5] if old and len(old) > 5 else "pending")
    title = title or (old[2] if old and len(old) > 2 else record.stem[12:].replace("-", " "))
    record_rel = record.relative_to(cfgmod.path(root, "work")).as_posix()
    row = manifest_row(work_id, state, title, record_rel, requirements, evidence)
    proc.atomic_write_text(manifest, records.upsert_manifest_row(text, work_id, row))


def link_request(root: Path, request: str, work_id: str) -> None:
    """Record the work id in the request (Traceability line + sidecar links)."""
    locs = proc.locate_request(root, request)
    if not locs:
        return
    md = locs[0][1]
    text = md.read_text(encoding="utf-8")
    if work_id not in text:
        if re.search(r"^- Work: pending$", text, re.MULTILINE):
            text = re.sub(r"^- Work: pending$", f"- Work: {work_id}", text, count=1, flags=re.MULTILINE)
        elif re.search(r"^- Work: .*$", text, re.MULTILINE):
            text = re.sub(r"^(- Work: .*)$", lambda m: f"{m.group(1)}, {work_id}", text, count=1, flags=re.MULTILINE)
        else:
            text = text.rstrip("\n") + f"\n\n## Traceability\n\n- Work: {work_id}\n"
        proc.atomic_write_text(md, text)
    js = md.with_suffix(".json")
    if js.exists():
        try:
            data = json.loads(js.read_text(encoding="utf-8"))
        except ValueError:
            return
        links = data.setdefault("links", {})
        if work_id not in links.setdefault("work", []):
            links["work"].append(work_id)
            proc.atomic_write_text(js, json.dumps(data, indent=2) + "\n")


def create(root: Path, request: str, agent_id: str, title: str, requirements: list[str], decision: str | None,
           wtype: str, state: str, suites: list[str]) -> Path:
    if state not in ("queued", "active"):
        raise proc.ProcessError(proc.INVALID, "a new work record starts queued or active")
    claim = proc.require_owner(root, request, agent_id)
    if claim.get("state") not in ("processing",):
        raise proc.ProcessError(proc.BAD_STATE, f"request {request} is {claim.get('state')}; claim it first")
    if not requirements:
        raise proc.ProcessError(proc.INVALID, "--requirements is required (extend an existing requirement; "
                                "docs/requirements/requirements.txt names the owners)")
    defined = records.defined_requirements(root)
    undefined = [r for r in requirements if r not in defined]
    if undefined and cfgmod.get("completion.require_requirement", root, True):
        raise proc.ProcessError(proc.INVALID, f"requirement(s) not defined in docs/requirements/: {', '.join(undefined)} "
                                "(add them to the owning document first)")
    if decision and decision not in records.known_decisions(root):
        raise proc.ProcessError(proc.INVALID, f"{decision} has no record in docs/decisions/")
    # The record, its manifest row and the request link change together under
    # the "work" lock, so a reader (drift_detect, the gate) never sees one
    # without the other while several agents work at once.
    with proc.exclusive(root, "work"):
        work_id = proc.allocate_id(root, "WORK", agent_id)
        path = cfgmod.path(root, "work") / state / f"{work_id}-{records.slug(title)}.md"
        ensure_owned(root, request, agent_id, [proc.rel(root, path)])
        req_path = proc.rel(root, proc.locate_request(root, request)[0][1])
        text = TEMPLATE.format(work_id=work_id, title=title, state=state, wtype=wtype, request_path=req_path,
                               requirements=", ".join(requirements),
                               decision=f"**Decision:** {decision}\n" if decision else "",
                               suites=f"**Test-Suites:** {', '.join(suites)}\n" if suites else "",
                               request_id=claim.get("request_id"), now=proc.now_iso(), agent=agent_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        proc.create_exclusive_text(path, text)
        update_manifest(root, request, agent_id, work_id, state, path, title)
        link_request(root, request, work_id)
        journal(root, {"work": work_id, "event": "created", "state": state, "request": request, "agent_id": agent_id})
    return path


def transition(root: Path, work_id: str, agent_id: str, to_state: str, reason: str | None = None) -> Path:
    if to_state not in cfgmod.WORK_STATES:
        raise proc.ProcessError(proc.INVALID, f"unknown work state {to_state}")
    with proc.exclusive(root, "work"):
        record = find_record(root, work_id)
        text = record.read_text(encoding="utf-8")
        request = request_of(root, text)
        proc.require_owner(root, request, agent_id)
        current = record.parent.name
        header_state = records.status_token(text)
        if current == to_state:
            # Idempotent completion of an interrupted transition: the file
            # moved, header / manifest may lag behind.
            if header_state != to_state or (records.manifest_row(cfgmod.path(root, "work_manifest"), work_id) or
                                            ["", ""])[1].lower() != to_state:
                text = records.set_header(text, "Status", to_state)
                proc.atomic_write_text(record, text)
                update_manifest(root, request, agent_id, work_id, to_state, record)
                journal(root, {"work": work_id, "event": "transition-finished", "state": to_state})
            return record
        if to_state not in cfgmod.WORK_TRANSITIONS.get(current, ()):
            raise proc.ProcessError(proc.BAD_STATE, f"{work_id} is {current}; {current} -> {to_state} is not a "
                                    f"valid transition (valid: {', '.join(cfgmod.WORK_TRANSITIONS.get(current, ())) or 'none'})")
        if to_state in ("blocked", "needs-review", "cancelled") and not (reason and reason.strip()):
            raise proc.ProcessError(proc.INVALID, f"--reason is required for -> {to_state}")
        if to_state == "completed" and cfgmod.get("completion.enforce_on_transition", root, True):
            claim = proc.require_owner(root, request, agent_id)
            gate = proc.read_gate_record(root, work_id)
            if gate is None or gate.get("verdict") != "PASS":
                state = "no gate record" if gate is None else f"a {gate.get('verdict')} gate record " \
                    f"({(gate.get('fails') or ['-'])[0]})"
                raise proc.ProcessError(proc.GATE_REQUIRED, f"{work_id} has {state}: fix it, then run "
                                        f"scripts/completion_gate.py --work {work_id} --record")
            fingerprint, _ = proc.request_fingerprint(root, claim)
            if gate.get("request_fingerprint") != fingerprint:
                raise proc.ProcessError(proc.GATE_REQUIRED, f"{work_id}'s gate record is older than the request's "
                                        "current code: re-run the tests and the gate (--record)")
        target = cfgmod.path(root, "work") / to_state / record.name
        ensure_owned(root, request, agent_id, [proc.rel(root, record), proc.rel(root, target)])
        journal(root, {"work": work_id, "event": "transition-start", "from": current, "to": to_state,
                       "reason": reason, "agent_id": agent_id})
        line = f"- {proc.now_iso()} {current} -> {to_state} by {agent_id}" + (f": {reason}" if reason else "")
        text = records.set_header(text, "Status", to_state)
        text = text.rstrip("\n") + ("\n" if "## Transitions" in text else "\n\n## Transitions\n\n") + line + "\n"
        proc.atomic_write_text(record, text)
        proc.move_no_clobber(record, target)
        update_manifest(root, request, agent_id, work_id, to_state, target)
        journal(root, {"work": work_id, "event": "transition-done", "from": current, "to": to_state})
        return target


def describe(root: Path, work_id: str) -> dict:
    record = find_record(root, work_id)
    text = record.read_text(encoding="utf-8")
    gate = proc.read_gate_record(root, work_id)
    return {"work": work_id, "record": proc.rel(root, record), "state": record.parent.name,
            "status_header": records.status_token(text), "request": records.header(text, "Request"),
            "requirements": records.cited_requirements(text), "evidence": records.header(text, "Evidence"),
            "gate": ({k: gate.get(k) for k in ("at", "verdict", "test_runs", "fails")} if gate else None)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create")
    c.add_argument("--request", required=True)
    c.add_argument("--title", required=True)
    c.add_argument("--requirements", nargs="+", default=[])
    c.add_argument("--decision")
    c.add_argument("--type", default="feature")
    c.add_argument("--state", default="active")
    c.add_argument("--test-suites", default="")
    t = sub.add_parser("transition")
    t.add_argument("work")
    t.add_argument("--to", required=True)
    t.add_argument("--reason")
    s = sub.add_parser("show")
    s.add_argument("work")
    s.add_argument("--json", action="store_true")
    ls = sub.add_parser("list")
    ls.add_argument("--state")
    ls.add_argument("--json", action="store_true")
    for p in (c, t):
        p.add_argument("--agent-id", default=os.environ.get("AGENT_ID"))
    a = ap.parse_args(argv)
    root = proc.repo_root()
    try:
        if a.cmd == "create":
            reqs = [r for item in a.requirements for r in re.split(r"[,\s]+", item) if r]
            path = create(root, Path(a.request).name, a.agent_id, a.title, reqs, a.decision, a.type, a.state,
                          [x for x in re.split(r"[,\s]+", a.test_suites) if x])
            print(f"CREATED {proc.rel(root, path)}")
            print(f"WORK-ID {path.name[:11]}")
            return 0
        if a.cmd == "transition":
            path = transition(root, a.work, a.agent_id, a.to, a.reason)
            print(f"TRANSITIONED {a.work} -> {a.to} ({proc.rel(root, path)})")
            return 0
        if a.cmd == "show":
            info = describe(root, a.work)
            print(json.dumps(info, indent=2) if a.json else "\n".join(f"{k}: {v}" for k, v in info.items()))
            return 0
        if a.cmd == "list":
            rows = []
            for state in cfgmod.WORK_STATES:
                if a.state and state != a.state:
                    continue
                for f in sorted((cfgmod.path(root, "work") / state).glob("WORK-*.md")):
                    rows.append({"work": f.name[:11], "state": state, "record": proc.rel(root, f)})
            if a.json:
                print(json.dumps(rows, indent=2))
            else:
                for r in rows:
                    print(f"{r['work']}  {r['state']:13s} {r['record']}")
                print(f"{len(rows)} work record(s)")
            return 0
    except proc.ProcessError as e:
        print(e.message, file=sys.stderr)
        return e.code
    return proc.INVALID


if __name__ == "__main__":
    sys.exit(main())
