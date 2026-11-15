#!/usr/bin/env python3
"""
Preflight check — runs before implementation begins.

Verifies:
  request exists + interpretable intent
  relevant requirements identified (not duplicate)
  architecture impact considered
  work item exists (or will be created) + dependencies understood
  affected tests identified
  relevant documentation identified
  idempotency: no duplicate artifact

Usage:
  python3 scripts/preflight.py --request requests/inbox/foo.md
  python3 scripts/preflight.py --request requests/processing/foo.md --work WORK-000123
  python3 scripts/preflight.py --dry-run-check   (used by process_next_request --dry-run)

Exit 0 = preflight passed, >0 = blocked with reason.
"""
import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devsys_config as cfgmod  # noqa: E402

ROOT = cfgmod.repo_root()

REQ_TXT = ROOT / "docs" / "requirements" / "requirements.txt"
WORK_TXT = ROOT / "work" / "work.txt"

def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""

def _find_requirement_ids(text: str):
    # find REQ-XXX or REQ-DEVOPS-XXX
    return re.findall(r"REQ-[A-Z0-9\-]+", text)

def check_request(path: Path):
    fails = []
    warns = []
    if not path.exists():
        fails.append(f"request file missing: {path}")
        return fails, warns
    txt = _read(path)
    if len(txt.strip()) < 20:
        fails.append("request too short / not interpretable (<20 chars)")
    # must have some outcome/motivation section or at least a sentence
    if "outcome" not in txt.lower() and "desired" not in txt.lower() and len(txt.split()) < 5:
        warns.append("request may lack clear desired outcome (no 'outcome/desired' keyword and <5 words)")
    # Request type must be canonical vocabulary and, for the symptom types,
    # supported by the request text (REQ-DEVOPS-045). Records that reached the
    # inbox by hand or before the validator are reported, never rewritten:
    # `scripts/create_request.py --file <request> [--reclassify]` reviews/corrects.
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import request_classification as rcls
        import json as _json
        sidecar = None
        js = path.with_suffix(".json")
        if js.exists():
            try:
                sidecar = _json.loads(js.read_text(encoding="utf-8"))
            except ValueError:
                sidecar = None
        review = rcls.review_record(txt, sidecar)
        if review["verdict"] == "INVALID-VOCABULARY":
            warns.append(f"request Type {review['stored_type']!r} is not in the canonical vocabulary "
                         f"({', '.join(rcls.CANONICAL_TYPES)}); review with scripts/create_request.py --file {path.name}")
        elif review["verdict"] == "UNSUPPORTED":
            warns.append(f"request Type {review['stored_type']} has no supporting evidence in the request text; "
                         f"the text supports {review['heuristic_type']} ({review['confidence']}); "
                         f"review with scripts/create_request.py --file {path.name} [--reclassify]")
        elif review["verdict"] == "NON-CANONICAL":
            warns.append(f"request Type {review['stored_type']!r} is a non-canonical spelling of "
                         f"{review['normalized_type']}; review with scripts/create_request.py --file {path.name}")
    except Exception as e:  # never block preflight on the advisory check
        warns.append(f"request type review unavailable: {e}")
    return fails, warns

def check_claim(path: Path, agent_id: str | None):
    """docs/process/state-model.md §4: preflight refuses to advance a request
    another agent holds, and an orphaned processing request (CI_CD_ROADMAP PRO-04)."""
    import agent_process as proc
    fails, warns = [], []
    state = path.parent.name
    claim = proc.read_claim(proc.lock_path(ROOT, path.name))
    if claim is None:
        if state == "processing":
            fails.append(f"{path.name} is processing with no claim (orphaned): scripts/claim.py --recover-request")
        elif state in ("inbox", "needs-review"):
            warns.append("request not claimed yet: claim it before implementation (scripts/claim.py --claim-request)")
        return fails, warns
    verdict, why = proc.claim_liveness(claim, proc.stale_hours(ROOT))
    if agent_id and claim.get("agent_id") != agent_id:
        fails.append(f"{path.name} is claimed by {claim.get('agent_id')} [{verdict}: {why}], not by {agent_id}")
    elif not agent_id:
        warns.append(f"no agent id given ($AGENT_ID): claimed by {claim.get('agent_id')} [{verdict}] — ownership unchecked")
    if verdict in (proc.STALE_V, proc.DEAD):
        warns.append(f"the claim's owner is {verdict}: {why}")
    return fails, warns


def check_requirements(request_text: str):
    fails = []
    warns = []
    manifest = _read(REQ_TXT)
    # naive duplicate check: if request text contains a requirement ID that already in manifest title?
    # Better: warn if request essentially duplicates existing requirement doc title
    # The intake-generated Classification section cites the framework's own
    # classification requirement in every request; only the request's own
    # content counts.
    request_text = re.sub(r"^## Classification\b.*?(?=^## |\Z)", "", request_text, flags=re.MULTILINE | re.DOTALL)
    ids_in_req = _find_requirement_ids(request_text)
    for rid in ids_in_req:
        if rid in manifest:
            warns.append(f"request references existing requirement {rid} — verify not duplicate creation")
    # must have at least some requirement docs existing
    if not (ROOT / "docs" / "requirements").exists():
        fails.append("docs/requirements directory missing")
    return fails, warns

def check_work(work_id: str):
    fails = []
    warns = []
    if not work_id:
        warns.append("no WORK-ID supplied — preflight will check for existing work before creating new one (idempotency)")
        return fails, warns
    if not re.match(r"WORK-\d{6}$", work_id):
        fails.append(f"work id format invalid: {work_id} (expected WORK-NNNNNN)")
        return fails, warns
    # idempotency: does work already exist?
    found = False
    for base in [ROOT / "work" / "queued", ROOT / "work" / "active", ROOT / "work" / "blocked", ROOT / "work" / "needs-review", ROOT / "work" / "verification", ROOT / "work" / "completed", ROOT / "work" / "work.txt"]:
        if base.is_file():
            if work_id in _read(base):
                found = True
        elif base.is_dir():
            for f in base.glob("*.md"):
                if work_id in f.name or work_id in _read(f):
                    found = True
    if found:
        warns.append(f"work {work_id} already exists — prefer update/link over duplicate creation")
    # check work record file exists if active
    work_file = ROOT / f"work/active/{work_id}*.md"
    candidates = list(ROOT.glob(f"work/**/{work_id}*.md"))
    if work_id and not candidates:
        warns.append(f"work record file for {work_id} not found — will need creation")
    return fails, warns

def check_tests_and_docs(request_text: str):
    warns = []
    # Advisory: the request should name a subsystem so affected tests / docs can
    # be identified (keywords: devsys.json preflight.subsystem_keywords).
    known_subsystems = list(cfgmod.get("preflight.subsystem_keywords", ROOT, []))
    if not known_subsystems:
        return warns   # NOT CONFIGURED: no advisory keyword check (devsys.json preflight.subsystem_keywords)
    lower = request_text.lower()
    if not any(k in lower for k in known_subsystems):
        warns.append("no subsystem keyword detected — ensure affected tests/docs are identified manually")
    return warns

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--request", required=True, help="path to request file")
    ap.add_argument("--work", help="WORK-ID if known")
    ap.add_argument("--json", action="store_true", help="output json")
    ap.add_argument("--agent-id", default=os.environ.get("AGENT_ID"))
    args = ap.parse_args()

    req_path = Path(args.request)
    if not req_path.is_absolute():
        # Relative to the system directory or to the caller's directory (an
        # agent in the project root names `.devsys/requests/...` when the
        # system is installed as a project subdirectory); a bare file name is
        # found in its request state directory.
        import agent_process
        candidates = [ROOT / args.request, Path.cwd() / args.request,
                      *(p for _state, p in agent_process.locate_request(ROOT, req_path.name))]
        req_path = next((c for c in candidates if c.is_file()), ROOT / args.request)

    fails, warns = [], []

    f1, w1 = check_request(req_path)
    fails += f1; warns += w1

    fc, wc = check_claim(req_path, args.agent_id) if req_path.exists() else ([], [])
    fails += fc; warns += wc

    txt = _read(req_path)
    f2, w2 = check_requirements(txt)
    fails += f2; warns += w2

    f3, w3 = check_work(args.work) if args.work else ([], ["no WORK-ID context"])
    fails += f3; warns += w3

    warns += check_tests_and_docs(txt)

    # Architecture impact: check if docs/decisions exists
    if not (ROOT / "docs" / "decisions").exists():
        fails.append("docs/decisions directory missing — cannot record architecture impact")

    if args.json:
        import json
        print(json.dumps({"fails": fails, "warns": warns, "request": str(req_path), "work": args.work}, indent=2))
    else:
        print(f"Preflight for {req_path} (work={args.work or 'none'})")
        if fails:
            print("FAIL:")
            for f in fails:
                print(f"  - {f}")
        if warns:
            print("WARN:")
            for w in warns:
                print(f"  - {w}")
        if not fails and not warns:
            print("PASS: no issues")
        elif not fails:
            print("PASS with warnings")

    if fails:
        print("\nPreflight FAILED — do not proceed with implementation. Record reason in work file or request needs-review.", file=sys.stderr)
        sys.exit(2)
    if warns:
        sys.exit(0)
    sys.exit(0)

if __name__ == "__main__":
    main()
