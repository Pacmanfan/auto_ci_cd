#!/usr/bin/env python3
"""
Completion gate — a work item cannot become completed unless its evidence
holds (REQ-DEVOPS-010/019/044/046, ADR-000155, ADR-000185, ADR-000199).

FAIL (each rule configurable under "completion" in devsys.json):
  * the work record is missing, duplicated, or in a state that cannot complete
    (queued, blocked, needs-review, cancelled)
  * the record does not link its request (**Request:**), the request is not
    processing / needs-review / completed, or the caller ($AGENT_ID) is not the
    claim's owner                                    (require_request_link)
  * no requirement is cited, or a cited requirement id is not defined in
    docs/requirements/*.md                            (require_requirement)
  * a cited decision (**Decision:** ADR-...) has no record (require_decision_records)
  * no linked evidence record; a linked evidence record or its evidence.txt
    row has a status outside passing_evidence_statuses; Status / Work missing
  * the request changed code and no linked test run (**Test-Run:**) of at
    least min_tier passed on the current code: a missing run record, a run
    that FAILED, was EMPTY (executed nothing) or INTERRUPTED, or code changed
    after the latest request-scoped run                   (require_test_run)
  * an open checklist item / not-done phase that is not waived
  * a protected artifact registered without process-change + ADR
  * drift_detect / doc_integrity report FAIL findings
                                   (require_drift_clean, require_doc_integrity_clean)
  * ownership conflicts on changed paths
  * require_audit_profile set and no audit run of that profile on HEAD without FAIL

WARN: keyword guidance (regression impact, documentation, traceability,
blockers), no request-owned change, unowned changes in the tree.

--record writes the verdict to evidence/gates/<WORK-ID>.json (appending to its
history) and registers that file in the request's claim when the caller owns
it. scripts/claim.py --complete-request and scripts/commit_request.py require
a current PASS record.

Usage:
  python3 scripts/completion_gate.py --work WORK-000001 [--json] [--record] [--agent-id ID]
"""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devsys_config as cfgmod  # noqa: E402
import devsys_records as records  # noqa: E402
import process_protection  # noqa: E402

# AGENT_PROCESS_ROOT overrides the repository root (tests use synthetic trees).
ROOT = cfgmod.repo_root()
GATE_VERSION = 3   # 3: records the request code paths (devsys.py verify-commit)

# Open items (REQ-DEVOPS-044, ADR-000155): an unchecked or partial checklist
# item, or a phase / item explicitly marked not done, blocks completion unless
# the same line says it was waived. Headings, prose, quoted `code` and fenced
# blocks are narrative, not status.
_OPEN_CHECKBOX = re.compile(r"^\s*(?:[-*+]|\d+\.)\s*\[( |~)\]")
_ITEM = re.compile(r"^\s*(?:(?:[-*+]|\d+\.)\s|\|)")
_NOT_DONE = re.compile(r"\b(not done|not started|not yet done|not implemented yet)\b", re.IGNORECASE)
_WAIVED = re.compile(r"\bwaive[ds]?\b|\bwaiver\b", re.IGNORECASE)
_CODE_SPAN = re.compile(r"`[^`]*`")
_FENCE = re.compile(r"^\s*(```|~~~)")


def open_items(text: str):
    """Lines of a work record that are open and not waived."""
    out = []
    fence = None
    for line in text.splitlines():
        m = _FENCE.match(line)
        if m:
            if fence is None:
                fence = m.group(1)
            elif m.group(1) == fence:
                fence = None
            continue
        if fence:
            continue
        words = _CODE_SPAN.sub("", line)
        if _WAIVED.search(words):
            continue
        if _OPEN_CHECKBOX.match(line) or (_ITEM.match(line) and _NOT_DONE.search(words)):
            out.append(line.strip())
    return out


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


WORK_STATE_DIRS = cfgmod.WORK_STATES
COMPLETABLE_WORK_STATES = ("active", "verification", "completed")
COMPLETABLE_REQUEST_STATES = ("processing", "needs-review", "completed")


def _find_work_files(work_id: str) -> list:
    """Every record named for work_id (WORK-NNNNNN-<slug>.md) in any work state
    directory. A record is identified by its file name only: one that merely
    mentions the id is another record."""
    name = re.compile(rf"{re.escape(work_id)}(?:-.*)?\.md")
    found = []
    for state in WORK_STATE_DIRS:
        found += sorted(f for f in (cfgmod.path(ROOT, "work") / state).glob(f"{work_id}*.md") if name.fullmatch(f.name))
    return found


def _evidence_for_work(work_id: str, work_text: str = ""):
    """Evidence declared for work_id (REQ-DEVOPS-044, ADR-000185): an
    evidence.txt row whose links column names it, a VER record its Evidence
    header names or whose own Work header names it. A VER record that merely
    mentions the id is another work's evidence."""
    candidates, declared = [], set()
    for cols in records.manifest_rows(cfgmod.path(ROOT, "evidence_manifest")):
        # VER-ID | status | date | links | file
        if len(cols) >= 4 and cols[0].startswith("VER-") and work_id in cols[3]:
            candidates.append(" | ".join(cols))
            declared.add(cols[0].split()[0])
            if len(cols) >= 5:
                declared.add(Path(cols[4]).name)
    ev_header = records.header(work_text, "Evidence")
    if ev_header:
        declared.update(re.findall(r"VER-\d{6}", ev_header))
    ev_files = []
    for f in sorted(cfgmod.path(ROOT, "evidence").glob("VER-*.md")):
        w = records.header(_read(f), "Work")
        if f.name in declared or f.name[:10] in declared or (w and work_id in w):
            ev_files.append(f)
    return candidates, ev_files


def _first_word(value):
    return re.sub(r"[^a-z\-]", "", value.split()[0].lower()) if value and value.split() else ""


def _evidence_checks(work_id, ws, policy, passes, warns, fails):
    candidates, ev_files = _evidence_for_work(work_id, ws)
    if not (candidates or ev_files):
        fails.append(f"No evidence linkage for {work_id} in evidence/evidence.txt nor evidence/VER-*.md (need VER record)")
        return []
    passing = [s.lower() for s in policy["passing_evidence_statuses"]]
    passes.append(f"Evidence linkage: manifest lines {candidates[:2]} files "
                  f"{[str(p.relative_to(ROOT)) for p in ev_files[:2]]}")
    ids = []
    for ef in ev_files:
        txt = _read(ef)
        ids.append(ef.name[:10])
        for field in ("Status", "Work"):
            if records.header(txt, field) is None:
                fails.append(f"evidence {ef.name} has no **{field}:** header")
        for field in ("Requirements", "Command", "Result"):
            if field.lower().rstrip("s") not in txt.lower():
                warns.append(f"evidence {ef.name} missing field {field}")
        status = _first_word(records.header(txt, "Status"))
        if status and status not in passing:
            fails.append(f"evidence {ef.name} status is {records.header(txt, 'Status')!r}, not one of "
                         f"{', '.join(passing)}")
        elif status:
            passes.append(f"evidence {ef.name} status {status}")
    for line in candidates:
        cols = [c.strip() for c in line.split("|")]
        status = _first_word(cols[1])
        if status not in passing:
            fails.append(f"evidence.txt row {cols[0]} status is {cols[1][:40]!r}, not one of {', '.join(passing)}")
        ids.append(cols[0].split()[0])
    return sorted(set(ids))


def _test_run_checks(ws, ev_files_text, claim, policy, passes, warns, fails) -> list:
    """Linked **Test-Run:** records: returns the ids of runs that cover the
    request (PASS, tier >= min_tier, something executed)."""
    import agent_process
    import run_tests
    code_paths = agent_process.request_code_paths(ROOT, claim) if claim else None
    lines = [m for t in [ws, *ev_files_text] for m in re.findall(r"^\s*\*\*Test-Run:\*\*\s*(.+)$", t, re.MULTILINE)]
    run_ids = sorted({rid for line in lines for rid in run_tests.RUN_ID.findall(line)})
    needs_run = bool(policy.get("require_test_run", True)) and bool(code_paths)
    if code_paths == []:
        passes.append("no request-owned code changed (documentation / process records only): no test run required")
    if not run_ids:
        (fails if needs_run else warns).append(
            "No recorded test run linked (**Test-Run:** <run id> from scripts/run_tests.py)"
            + (" but the request changed code: " + ", ".join(code_paths[:5]) if needs_run else
               " — evidence is prose only (REQ-DEVOPS-046)"))
        return []
    try:
        manifest = run_tests.load_manifest()
    except (OSError, ValueError):
        # No registry in this tree: the framework's cumulative tier definition.
        manifest = {"tiers": {"t0": {"includes": []}, "t1": {"includes": ["t0"]},
                              "t2": {"includes": ["t0", "t1"]}, "t3": {"includes": ["t0", "t1", "t2"]}}}
    min_tier = policy["min_tier"]
    records_ = []
    covering = []
    for rid in run_ids:
        path = run_tests.find_run_record(rid, ROOT)
        if path is None:
            (fails if needs_run else warns).append(
                f"test run {rid}: no record in the build tree (test-results/runs/) or evidence/*/run-{rid}.json "
                "— cannot verify it (attach it with scripts/evidence.py attach-run)")
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            fails.append(f"test run {rid}: unreadable record {path}")
            continue
        records_.append(record)
        classification = str(record.get("classification", ""))
        tier = record.get("tier")
        summary = f"test run {rid}: {classification} (tier {tier}, {record.get('scope')} scope)"
        jobs = record.get("jobs_detail")
        executed = jobs is None or any(j.get("status") in ("PASS", "CACHED", "FLAKY", "EXPECTED") for j in jobs)
        try:
            tiers = run_tests.tier_set(manifest, tier) if tier else set()
        except SystemExit:
            tiers = {tier}
        head = classification.split()[0] if classification else ""
        if head in ("FAIL", "EMPTY", "INTERRUPTED"):
            fails.append(summary + " — a linked run " + {"FAIL": "failed", "EMPTY": "executed nothing",
                                                        "INTERRUPTED": "was interrupted"}[head])
        elif head == "BLOCKED":
            warns.append(summary + " — some suites could not run; it does not cover the request")
        elif not executed:
            fails.append(summary + " — no job executed")
        elif min_tier not in tiers:
            warns.append(summary + f" — does not include tier {min_tier}")
        else:
            passes.append(summary)
            covering.append(rid)
        if record.get("allow_stale"):
            warns.append(f"test run {rid} ran with --allow-stale (binaries possibly older than their sources)")
    if needs_run and not covering:
        fails.append(f"no linked test run that passed and includes tier {min_tier} "
                     f"(python3 scripts/run_tests.py --tier {min_tier} --request <file>)")
    fingerprinted = sorted((r for r in records_ if r.get("request_fingerprint")), key=lambda r: r.get("started_at", ""))
    if not fingerprinted:
        if records_:
            (fails if needs_run else warns).append(
                "no linked test run was scoped to the request (--request): cannot prove it ran on the "
                "request's current code")
        return covering
    latest = fingerprinted[-1]
    if claim is None:
        warns.append(f"test run {latest['run_id']}: the request's claim is gone — fingerprint not re-checked")
        return covering
    current, _ = agent_process.request_fingerprint(ROOT, claim)
    if current != latest["request_fingerprint"]:
        fails.append(f"the request's code changed after test run {latest['run_id']}: re-run "
                     f"scripts/run_tests.py --request <file> and link the new run (REQ-DEVOPS-046)")
    else:
        passes.append(f"test run {latest['run_id']} exercised the request's current code (fingerprint match)")
    return covering


def _repository_checks(policy, passes, warns, fails):
    if policy.get("require_drift_clean", True):
        import drift_detect
        bad = [f for f in drift_detect.consistent_check() if f[0] == "FAIL"]
        if bad:
            fails.append(f"drift_detect: {len(bad)} FAIL finding(s): " + "; ".join(f"{c}: {m}"[:120] for _, c, m in bad[:3]))
        else:
            passes.append("drift_detect: no FAIL finding")
    if policy.get("require_doc_integrity_clean", True):
        import doc_integrity
        bad = [f for f in doc_integrity.check(ROOT) if f[0] == "FAIL"]
        if bad:
            fails.append(f"doc_integrity: {len(bad)} FAIL finding(s): " + "; ".join(m[:120] for _, _, m in bad[:3]))
        else:
            passes.append("doc_integrity: no FAIL finding")
    profile = policy.get("require_audit_profile")
    if profile:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
        order = {"quick": 0, "standard": 1, "baseline": 2}
        found = None
        for audit in sorted((ROOT / ".audit" / "runs").glob("AUDIT-*/audit.json"), reverse=True):
            try:
                data = json.loads(audit.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if data.get("inventory", {}).get("git", {}).get("commit") == head and \
                    order.get(data.get("profile"), -1) >= order.get(profile, 99):
                found = data
                break
        if found is None:
            fails.append(f"no {profile} (or deeper) audit run on HEAD {head[:10]}: run ./tools/audit {profile}")
        else:
            failed = [c["id"] for c in found.get("checks", []) if c.get("status") == "FAIL"]
            (fails if failed else passes).append(f"audit {found.get('audit_id')} ({found.get('profile')}): "
                                                 + (f"FAIL {', '.join(failed)}" if failed else "no FAIL"))


def check(work_id: str, agent_id=None):
    import agent_process
    policy = cfgmod.load(ROOT)["completion"]
    fails, warns, passes = [], [], []
    info = {"work": work_id, "claim": None, "request": None, "test_runs": [], "evidence": []}

    wfs = _find_work_files(work_id)
    if not wfs:
        fails.append(f"work file not found for {work_id} (expected work/<state>/{work_id}-*.md)")
        return fails, warns, passes, info
    if len(wfs) > 1:
        fails.append(f"{work_id} names {len(wfs)} records ({', '.join(str(f.relative_to(ROOT)) for f in wfs)}) — "
                     "resolve the duplicate before gating")
        return fails, warns, passes, info
    wf = wfs[0]
    ws = _read(wf)
    work_state = wf.parent.name
    passes.append(f"Work record: {wf.relative_to(ROOT)}")
    if work_state not in COMPLETABLE_WORK_STATES:
        fails.append(f"work record is in work/{work_state}/: only {', '.join(COMPLETABLE_WORK_STATES)} work can complete")

    # -- request, state, ownership
    claim = None
    req_header = records.header(ws, "Request") or ""
    req = re.search(r"requests/[A-Za-z\-]+/[^\s`)\]]+\.md", req_header) or re.search(r"[^\s`/]+\.md", req_header)
    if req:
        name = Path(req.group(0)).name
        info["request"] = name
        locs = agent_process.locate_request(ROOT, name)
        if not locs:
            fails.append(f"linked request {name} exists in no request state directory")
        else:
            state = locs[0][0]
            if state not in COMPLETABLE_REQUEST_STATES:
                fails.append(f"linked request {name} is {state}: only {', '.join(COMPLETABLE_REQUEST_STATES)} "
                             "requests can complete")
            else:
                passes.append(f"Request {name} is {state}")
        claim = agent_process.read_claim(agent_process.lock_path(ROOT, name))
        if claim is not None:
            claim.setdefault("target", name)
            info["claim"] = claim.get("agent_id")
            if agent_id and claim.get("agent_id") != agent_id:
                fails.append(f"caller {agent_id} does not own the request (owner {claim.get('agent_id')})")
            else:
                passes.append(f"Request claimed by {claim.get('agent_id')}")
        elif locs and locs[0][0] != "completed":
            warns.append(f"request {name} has no claim: ownership and the code fingerprint cannot be checked")
    elif policy.get("require_request_link", True):
        fails.append("work record does not link its request (**Request:** requests/<state>/<file>.md)")
    else:
        warns.append("work record does not link its request")

    # -- requirements and decisions
    cited = records.cited_requirements(ws)
    if not cited and not re.search(r"Requirement|REQ-", ws):
        fails.append("Requirement not identified in work file (missing **Requirements:** / REQ- link)")
    elif policy.get("require_requirement", True):
        defined = records.defined_requirements(ROOT)
        undefined = [r for r in cited if r not in defined]
        if not cited:
            fails.append("no requirement id cited (**Requirements:** REQ-...)")
        elif undefined:
            fails.append(f"cited requirement(s) not defined in docs/requirements/: {', '.join(undefined[:8])}")
        else:
            passes.append(f"Requirements defined: {', '.join(cited[:6])}" + (" ..." if len(cited) > 6 else ""))
    else:
        passes.append("Requirement identified (work file references requirement)")
    decisions = re.findall(r"ADR-\d{6}", records.header(ws, "Decisions?") or "")
    if decisions and policy.get("require_decision_records", True):
        known = records.known_decisions(ROOT)
        missing = [d for d in decisions if d not in known]
        if missing:
            fails.append(f"decision record(s) missing: {', '.join(missing)}")
        else:
            passes.append(f"Decisions exist: {', '.join(decisions)}")

    # -- implementation (request-owned changes)
    if claim is not None:
        changed = agent_process.uncommitted_owned_paths(ROOT, claim, include_request_files=False)
        commits = claim.get("commits") or []
        if changed or commits:
            passes.append(f"Implementation: {len(changed)} request-owned changed path(s), {len(commits)} commit(s)")
        else:
            warns.append("no request-owned change or commit (acceptable for investigation / documentation-free work)")
        try:
            report = agent_process.classify_worktree(ROOT, claim.get("target"))
        except agent_process.ProcessError:
            report = {}
        if report.get("conflicts"):
            fails.append("ownership conflicts: " + "; ".join(report["conflicts"][:3]))
        unowned = [r["path"] for r in report.get("changes", []) if r["category"] == "unowned"]
        if unowned:
            warns.append(f"{len(unowned)} unowned change(s) in the tree (not this request's; never committed by it)")

    # -- evidence and test runs
    info["evidence"] = _evidence_checks(work_id, ws, policy, passes, warns, fails)
    _, ev_files = _evidence_for_work(work_id, ws)
    info["test_runs"] = _test_run_checks(ws, [_read(f) for f in ev_files], claim, policy, passes, warns, fails)

    # -- open items
    items = open_items(ws)
    if items:
        fails.append(f"{len(items)} open item(s) in the work record (unchecked / partial / not done, not waived): "
                     + "; ".join(i[:80] for i in items[:3]))
    else:
        passes.append("No open checklist items or not-done phases")

    # -- protected artifacts (REQ-DEVOPS-023, ADR-000185)
    touched = process_protection.protected_paths(claim, ROOT)
    if touched and not process_protection.is_process_change_record(ws):
        fails.append(f"request registered protected artifact(s) {', '.join(touched)} but the work record is not "
                     "'Type: process-change' with a Decision ADR (docs/process/process-protection.md)")
    elif touched:
        passes.append(f"Protected artifact(s) {', '.join(touched)} changed as a process change")
    elif claim is None:
        passes.append("Protected artifacts: no claim for the linked request (released or none), nothing registered to check")
    else:
        passes.append("Protected artifacts: none registered by the request")

    # -- repository-wide consistency
    _repository_checks(policy, passes, warns, fails)

    # -- guidance (warnings only)
    low = ws.lower()
    if "regression" not in low and "impact" not in low:
        warns.append("Regression impact not mentioned in work file — explicitly record")
    if "document" not in low:
        warns.append("Documentation update not referenced — verify docs/README or subsystem docs updated")
    if "trace" not in low and not ("REQ-" in ws and "WORK-" in ws):
        warns.append("Traceability not explicitly recorded")
    if "blocker" in low and "no blocker" not in low and "no known blocker" not in low:
        warns.append("Work file mentions blocker — ensure resolved or moved to needs-review")

    if records.status_token(ws) == "completed" and fails:
        fails.append("Work claims COMPLETED but gate fails — must not mark complete")
    return fails, warns, passes, info


def write_record(work_id, verdict, fails, warns, passes, info, agent_id) -> Path:
    import agent_process
    path = agent_process.gate_record_path(ROOT, work_id)
    claim = None
    if info.get("request"):
        claim = agent_process.read_claim(agent_process.lock_path(ROOT, info["request"]))
        if claim is not None:
            claim.setdefault("target", info["request"])
    if claim is not None and agent_id != claim.get("agent_id"):
        raise agent_process.ProcessError(agent_process.NOT_OWNER, f"--record needs the claim's owner "
                                         f"({claim.get('agent_id')}); pass --agent-id or set AGENT_ID")
    fingerprint, code_paths = agent_process.request_fingerprint(ROOT, claim) if claim is not None else (None, [])
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    entry = {"at": datetime.now(timezone.utc).isoformat(), "verdict": verdict, "gate_version": GATE_VERSION,
             "work": work_id, "request": info.get("request"), "request_id": (claim or {}).get("request_id"),
             "request_fingerprint": fingerprint, "request_code_paths": code_paths,
             "test_runs": info.get("test_runs", []),
             "evidence": info.get("evidence", []), "head": head or None, "agent_id": agent_id,
             "fails": fails, "warns": warns, "passes": len(passes)}
    if claim is not None and agent_process.rel(ROOT, path) not in (claim.get("files") or {}):
        agent_process.own_paths(ROOT, info["request"], agent_id, [agent_process.rel(ROOT, path)])
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {"work": work_id, "runs": []}
    data["runs"].append(entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    agent_process.atomic_write_text(path, json.dumps(data, indent=1) + "\n")
    return path


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", required=True, help="WORK-ID")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--record", action="store_true", help="persist the verdict to evidence/gates/<WORK-ID>.json")
    ap.add_argument("--agent-id", default=os.environ.get("AGENT_ID"))
    args = ap.parse_args()
    fails, warns, passes, info = check(args.work, args.agent_id)
    verdict = "FAIL" if fails else "PASS"
    recorded = None
    if args.record:
        import agent_process
        try:
            recorded = write_record(args.work, verdict, fails, warns, passes, info, args.agent_id)
        except agent_process.ProcessError as e:
            print(e.message, file=sys.stderr)
            return e.code
    if args.json:
        print(json.dumps({"work": args.work, "verdict": verdict, "fails": fails, "warns": warns, "passes": passes,
                          "test_runs": info.get("test_runs"), "record": str(recorded) if recorded else None}, indent=2))
    else:
        print(f"Completion gate for {args.work}")
        print(f"  PASS checks: {len(passes)}")
        for p in passes:
            print(f"    + {p}")
        if warns:
            print(f"  WARN ({len(warns)}):")
            for w in warns:
                print(f"    ! {w}")
        if fails:
            print(f"  FAIL ({len(fails)}):")
            for f in fails:
                print(f"    x {f}")
        if recorded:
            print(f"\nrecord: {recorded.relative_to(ROOT)}")
        if not fails:
            print("\nGATE: PASS (warnings are manual review items, not blockers unless policy requires)")
        else:
            print("\nGATE: FAIL — do not mark work completed. Fix fails and re-run.")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
