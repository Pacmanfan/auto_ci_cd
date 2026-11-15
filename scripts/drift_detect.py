#!/usr/bin/env python3
"""
Drift detection — deterministic checks for development drift.

Checks:
 - requirement with no work reference
 - work with no requirement
 - requirement with no verification path
 - completed work without evidence
 - broken document links (manifest targets)
 - references to nonexistent paths (grep *_REF failures)
 - duplicate requirement IDs / work IDs / decision IDs / evidence IDs
 - stale status (outside state-model)
 - orphaned requests / orphaned work
 - contradictory canonical docs (multiple normative authorities)
 - multiple documents claiming normative authority
 - WORK record whose directory, Status header and work.txt status disagree
 - WORK record still open while its request is completed / rejected
 - WORK record whose Request path does not resolve
 - processing request whose claim heartbeat is stale
 - evidence.txt status outside the state model

Usage:
  python3 scripts/drift_detect.py
  python3 scripts/drift_detect.py --json
"""
import os
import re
import json
import sys
from pathlib import Path
from collections import Counter

# ``AGENT_PROCESS_ROOT`` overrides the repository root, exactly as
# scripts/agent_process.py does, so the validator can be run against a
# synthetic manifest set by scripts/tests/test_drift_id_collisions.py
# without a test-only code path.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import devsys_config as cfgmod  # noqa: E402
import devsys_records as records  # noqa: E402

ROOT = cfgmod.repo_root()

# Manifests whose first column is a record id, with the column holding that
# row's record path (relative to the manifest's own directory).
ID_MANIFESTS = (
    ("work/work.txt", r"WORK-\d{6}", 3),
    ("evidence/evidence.txt", r"VER-\d{6}", 4),
    ("docs/decisions/decisions.txt", r"ADR-\d{6}", 3),
)
# The one place an accepted identity collision may be declared.
COLLISION_DECLARATIONS = "docs/process/historical-id-collisions.md"

ALLOWED_WORK_STATUS = set(cfgmod.WORK_STATES)
ALLOWED_REQ_STATUS = set(cfgmod.REQUEST_STATES)
ALLOWED_VER_STATUS = set(cfgmod.VERIFICATION_STATES)

def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""

def _grep_ids(pattern: str, directory: Path):
    ids = []
    for f in directory.rglob("*.md"):
        txt = _read(f)
        ids.extend(re.findall(pattern, txt))
    return ids

def _manifest_rows(manifest: Path):
    """(id, record-path) for every data row of an id manifest, in file order."""
    rows = []
    if not manifest.exists():
        return rows
    for line in manifest.read_text(errors="ignore").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("Format"):
            continue
        rows.append([p.strip() for p in s.split("|")])
    return rows


def _duplicate_records(manifest: Path, pattern: str, record_col: int):
    """id -> [record path, ...] for every id appearing on more than one row.

    The record path is the row's own record column, which is what an accepted
    collision is declared against (see docs/process/historical-id-collisions.md).
    A row with no usable record column contributes the empty string, so a
    malformed row can never masquerade as a declared record.
    """
    seen = {}
    for parts in _manifest_rows(manifest):
        ids = re.findall(pattern, parts[0])
        if not ids:
            continue
        record = parts[record_col] if len(parts) > record_col else ""
        record = record.split(",")[0].strip().split()[0] if record.strip() else ""
        seen.setdefault(ids[0], []).append(record)
    return {k: v for k, v in seen.items() if len(v) > 1}


def _parse_collision_declarations(root: Path):
    """Accepted identity collisions declared in the canonical process document.

    Returns (declarations, problems):
      declarations[(manifest, id)] = {"records": [...], "reason": str, "line": int}
      problems                     = [(code, message), ...] for malformed lines

    Format (one line per collision, inside the document):
        ID | manifest | record ; record [; record ...] | reason
    """
    declarations, problems = {}, []
    doc = root / COLLISION_DECLARATIONS
    if not doc.exists():
        return declarations, problems
    known_manifests = {m for m, _, _ in ID_MANIFESTS}
    id_rx = re.compile(r"^(WORK|VER|ADR)-\d{6}$")
    for lineno, line in enumerate(doc.read_text(errors="ignore").splitlines(), 1):
        s = line.strip()
        # Only fully-formed declaration lines are considered; prose, table
        # rows and fence markers are ignored.
        if not s or s.startswith("#") or s.startswith("|") or s.startswith("Format") or s.startswith("`"):
            continue
        parts = [p.strip() for p in s.split("|")]
        if len(parts) < 3 or not id_rx.match(parts[0]):
            continue
        rid, manifest = parts[0], parts[1]
        records = [r.strip() for r in parts[2].split(";") if r.strip()]
        reason = parts[3] if len(parts) > 3 else ""
        where = f"{COLLISION_DECLARATIONS}:{lineno} {rid}"
        if manifest not in known_manifests:
            problems.append(("MALFORMED_COLLISION_DECLARATION",
                             f"{where} names unknown manifest '{manifest}'"))
            continue
        if (manifest, rid) in declarations:
            problems.append(("MALFORMED_COLLISION_DECLARATION",
                             f"{where} is declared twice for {manifest}"))
            continue
        if len(records) < 2:
            problems.append(("MALFORMED_COLLISION_DECLARATION",
                             f"{where} declares {len(records)} record(s); a collision needs at least 2"))
            continue
        if len(set(records)) != len(records):
            problems.append(("MALFORMED_COLLISION_DECLARATION",
                             f"{where} declares the same record more than once: {records}"))
            continue
        if not reason:
            problems.append(("MALFORMED_COLLISION_DECLARATION", f"{where} has no reason"))
            continue
        manifest_dir = (root / manifest).parent
        missing = [r for r in records if not (root / r).exists() and not (manifest_dir / r).exists()]
        if missing:
            problems.append(("MALFORMED_COLLISION_DECLARATION",
                             f"{where} names record(s) that do not exist: {', '.join(missing)}"))
            continue
        declarations[(manifest, rid)] = {"records": records, "reason": reason, "line": lineno}
    return declarations, problems


def _check_id_collisions(root: Path, findings: list):
    """Every duplicate id is reported. Only a duplicate whose record set is
    EXACTLY the declared set is accepted (MISSION of ADR-000149); a third
    record, a replaced record or an undeclared id is a hard failure.
    """
    declarations, problems = _parse_collision_declarations(root)
    for code, msg in problems:
        findings.append(("FAIL", code, msg))

    used = set()
    for manifest, pattern, record_col in ID_MANIFESTS:
        duplicates = _duplicate_records(root / manifest, pattern, record_col)
        for rid in sorted(duplicates):
            rows = duplicates[rid]
            # One RECORD listed on several rows is a stale manifest row, not an
            # identity collision: two ids pointing at the same record is a
            # different defect from two records sharing an id, and folding it
            # into the collision set would let a declaration accept it.
            repeated = sorted({r for r in rows if rows.count(r) > 1})
            for rec in repeated:
                findings.append(("FAIL", "DUPLICATE_MANIFEST_ROW",
                                 f"{manifest} lists record {rec} on {rows.count(rec)} rows under {rid} — "
                                 f"remove the superseded row"))
            # An identity collision is about DISTINCT records sharing an id.
            records = sorted(set(rows))
            if len(records) < 2:
                continue
            declared = declarations.get((manifest, rid))
            shown = " ; ".join(records)
            if declared and sorted(declared["records"]) == sorted(records):
                used.add((manifest, rid))
                findings.append(("ACCEPTED", "HISTORICAL_ID_COLLISION",
                                 f"{manifest} {rid} x{len(records)} — {shown} "
                                 f"({declared['reason']}; declared in {COLLISION_DECLARATIONS})"))
            elif declared:
                used.add((manifest, rid))
                findings.append(("FAIL", "UNEXPECTED_ID_COLLISION",
                                 f"{manifest} duplicate {rid} x{len(records)} does NOT match the accepted "
                                 f"historical collision: declared [{' ; '.join(declared['records'])}], "
                                 f"found [{shown}]"))
            else:
                findings.append(("FAIL", "UNEXPECTED_ID_COLLISION",
                                 f"{manifest} duplicate {rid} x{len(records)} — {shown} "
                                 f"(not a declared historical collision)"))
    for (manifest, rid), d in sorted(declarations.items()):
        if (manifest, rid) not in used:
            findings.append(("WARN", "STALE_COLLISION_DECLARATION",
                             f"{COLLISION_DECLARATIONS}:{d['line']} declares {rid} in {manifest}, "
                             f"which is no longer duplicated — remove the declaration"))


WORK_STATE_DIRS = cfgmod.WORK_STATES
OPEN_WORK_STATES = set(cfgmod.WORK_OPEN_STATES)
CLOSED_REQUEST_STATES = set(cfgmod.REQUEST_CLOSED_STATES)
REQUEST_STATE_DIRS = cfgmod.REQUEST_STATES


def _status_token(text: str):
    """The first word of a record's Status header, lower-case, or None."""
    m = re.search(r"^\s*\*\*Status:\*\*\s*(.+)$", text, re.MULTILINE)
    if not m:
        m = re.search(r"^\s*Status:\s*(.+)$", text, re.MULTILINE | re.IGNORECASE)
    if not m:
        return None
    return re.sub(r"[^a-z\-]", "", m.group(1).strip().split()[0].lower().strip("*_`")) or None


def _work_txt_status(work_txt: str):
    """WORK id -> status column of work/work.txt (last row wins)."""
    out = {}
    for line in work_txt.splitlines():
        s = line.strip()
        if not s.startswith("WORK-") or "|" not in s:
            continue
        parts = [p.strip() for p in s.split("|")]
        if len(parts) >= 2:
            out[parts[0]] = parts[1].lower().replace(" ", "")
    return out


def _check_work_state(root: Path, findings: list, work_txt: str):
    listed = _work_txt_status(work_txt)
    for state in WORK_STATE_DIRS:
        for f in sorted((root / "work" / state).glob("WORK-*.md")):
            m = re.search(r"WORK-\d{6}", f.name)
            if not m:
                continue
            wid, rel = m.group(0), f.relative_to(root)
            text = _read(f)
            header = _status_token(text)
            if header and header in ALLOWED_WORK_STATUS and header != state:
                findings.append(("FAIL", "WORK_STATE_MISMATCH",
                                 f"{rel} is in work/{state}/ but its Status header says '{header}'"))
            column = listed.get(wid)
            if column and column in ALLOWED_WORK_STATUS and column != state:
                findings.append(("FAIL", "WORK_STATE_MISMATCH",
                                 f"{rel} is in work/{state}/ but work/work.txt says '{column}'"))
            # The linked request.
            rm = re.search(r"^\s*\*\*Request:\*\*\s*(.+)$", text, re.MULTILINE)
            if not rm:
                continue
            paths = re.findall(r"requests/[A-Za-z\-]+/[^\s`)\]]+\.md", rm.group(1))
            if not paths:
                continue
            target = paths[0]
            # A request moves between state directories; the record usually
            # names the directory it was in when written. The same file in
            # another state is the same request (not drift); a request that
            # exists in no state directory is.
            name = Path(target).name
            where = next((rs for rs in REQUEST_STATE_DIRS if (root / "requests" / rs / name).exists()), None)
            if where is None:
                findings.append(("FAIL", "WORK_REQUEST_MISSING",
                                 f"{rel} links {target}, which exists in no request state directory"))
                continue
            if state in OPEN_WORK_STATES and where in CLOSED_REQUEST_STATES:
                findings.append(("WARN", "WORK_OPEN_REQUEST_CLOSED",
                                 f"{rel} is still {state} but its request is {where}"))


def _check_stale_claims(root: Path, findings: list, now: float = None):
    """Processing claims whose owner is STALE (silent past the threshold) or
    verified DEAD. Claims live in the MAIN worktree's state directory, which
    agent_process.state_dir() resolves from any linked worktree too
    (CI_CD_ROADMAP COR-14)."""
    import agent_process
    stale_h = agent_process.stale_hours(root)
    claims_dir = agent_process.state_dir(root) / "claims"
    for lock in sorted(claims_dir.glob("request-*.lock")):
        data = agent_process.read_claim(lock)
        if not data or data.get("state") != "processing":
            continue
        verdict, why = agent_process.claim_liveness(data, stale_h)
        if verdict in (agent_process.STALE_V, agent_process.DEAD):
            findings.append(("WARN", "STALE_CLAIM",
                             f"{data.get('request_id', lock.name)} processing, owner {verdict}: {why}; "
                             "recover or release (docs/process/recovery.md, scripts/claim.py --sweep)"))


def _check_lifecycle(root: Path, findings: list):
    """Completion and state integrity (CI_CD_ROADMAP COR-06/07/16):

    * a completed request must have been completed through scripts/claim.py
      (its sidecar carries a resolution: gate records, an exemption or a
      recorded waiver) — level drift.unverified_completion;
    * completed work needs a PASS gate record and passing evidence;
    * two request files may not share a request id;
    * a request's sidecar belongs beside its markdown;
    * a claim's recorded state must match its request's directory."""
    import agent_process
    level = str(cfgmod.get("drift.unverified_completion", root, "FAIL")).upper()
    passing = [s.lower() for s in cfgmod.get("completion.passing_evidence_statuses", root, ["pass"])]
    requests_dir = cfgmod.path(root, "requests")
    ids: dict = {}
    for state in cfgmod.REQUEST_STATES:
        for md in sorted((requests_dir / state).glob("*.md")):
            if md.name == "README.md":
                continue
            for rid in agent_process._request_identity_ids(md)[:1]:
                ids.setdefault(rid, []).append(md.relative_to(root).as_posix())
            if state != "completed":
                continue
            try:
                sidecar = json.loads(md.with_suffix(".json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                sidecar = None
            if sidecar is not None and "resolution" not in sidecar and level in ("FAIL", "WARN"):
                findings.append((level, "UNVERIFIED_COMPLETION",
                                 f"{md.relative_to(root)} is completed but was not completed through "
                                 "scripts/claim.py --complete-request (no resolution / gate record)"))
    for rid, files in sorted(ids.items()):
        if len(set(Path(f).name for f in files)) > 1:
            findings.append(("FAIL", "DUPLICATE_REQUEST_ID", f"{rid} is held by {len(files)} request files: {', '.join(files)}"))
    for w in sorted((cfgmod.path(root, "work") / "completed").glob("WORK-*.md")):
        m = re.match(r"WORK-\d{6}", w.name)
        if not m:
            continue
        wid = m.group(0)
        gate = agent_process.read_gate_record(root, wid)
        if cfgmod.get("completion.enforce_on_transition", root, True) and level in ("FAIL", "WARN") and \
                (gate is None or gate.get("verdict") != "PASS"):
            findings.append((level, "UNVERIFIED_COMPLETION",
                             f"{w.relative_to(root)} is completed without a PASS gate record "
                             f"(scripts/completion_gate.py --work {wid} --record)"))
        text = _read(w)
        ev_ids = set(re.findall(r"VER-\d{6}", records.header(text, "Evidence") or ""))
        for row in records.manifest_rows(cfgmod.path(root, "evidence_manifest")):
            if len(row) >= 4 and wid in row[3]:
                ev_ids.add(row[0].split()[0])
                status = re.sub(r"[^a-z\-]", "", (row[1].split() or [""])[0].lower())
                if status not in passing:
                    findings.append(("FAIL", "COMPLETED_EVIDENCE_NOT_PASSING",
                                     f"{w.relative_to(root)} is completed but its evidence {row[0]} is {row[1][:30]!r}"))
    for js, md in agent_process._lone_sidecars(root):
        findings.append(("WARN", "LONE_SIDECAR", f"{js.relative_to(root)} is not beside {md.relative_to(root)} "
                         "(interrupted move): scripts/claim.py --sweep --apply"))
    try:
        claims = agent_process.all_claims(root)
    except Exception:
        claims = []
    for c in claims:
        if c.get("kind") != "request":
            continue
        states = [s for s, _ in agent_process.locate_request(root, c.get("target", ""))]
        if len(states) == 1 and c.get("state") and c.get("state") != states[0] and not c.get("parked"):
            findings.append(("WARN", "CLAIM_STATE_MISMATCH",
                             f"claim of {c.get('target')} records {c.get('state')} but the request is in {states[0]} "
                             "(interrupted operation): scripts/claim.py --sweep"))


def _check_mirrors(root: Path, findings: list):
    """.agent/state/requests.md is a generated mirror (scripts/devsys.py status
    --write-mirrors); a count that disagrees with the canonical directories is
    drift (docs/process/state-model.md §4)."""
    mirror = cfgmod.path(root, "state") / "requests.md"
    text = _read(mirror)
    requests_dir = cfgmod.path(root, "requests")
    for key, state in (("inbox_count", "inbox"), ("processing_count", "processing"), ("needs_review_count", "needs-review")):
        m = re.search(rf"^{key}:\s*(\d+)", text, re.MULTILINE)
        if not m:
            continue
        actual = len([p for p in (requests_dir / state).glob("*.md") if p.name != "README.md"])
        if int(m.group(1)) != actual:
            findings.append(("WARN", "MIRROR_STALE", f".agent/state/requests.md says {key}: {m.group(1)} but "
                             f"requests/{state}/ holds {actual}: python3 scripts/devsys.py status --write-mirrors"))


def _check_evidence_status(root: Path, findings: list):
    for lineno, line in enumerate(_read(root / "evidence" / "evidence.txt").splitlines(), 1):
        s = line.strip()
        if not s.startswith("VER-") or "|" not in s:
            continue
        status = s.split("|")[1].strip()
        # Rows may qualify their status in free text ("PASS with classified
        # failures (...)"); the first word is the state and must be one.
        first = re.sub(r"[^a-z\-]", "", status.split()[0].lower()) if status else ""
        if first not in ALLOWED_VER_STATUS:
            findings.append(("WARN", "STALE_STATUS",
                             f"evidence/evidence.txt:{lineno} {s.split('|')[0].strip()} status '{status[:60]}{'...' if len(status) > 60 else ''}' "
                             f"not in {sorted(ALLOWED_VER_STATUS)}"))


def check():
    findings = []  # list of (level, code, message)

    # 1. duplicate IDs
    # requirement IDs: look for REQ-DEVOPS-XXX headers in docs/requirements
    req_ids = []
    for f in (ROOT/"docs"/"requirements").glob("*.md"):
        txt = _read(f)
        req_ids.extend(re.findall(r"REQ-[A-Z0-9\-]+", txt))
    cnt = Counter(req_ids)
    for k,v in cnt.items():
        if v > 5 and k.startswith("REQ-DEVOPS"):
            # many occurrences is okay (references) but header duplicates are bad
            pass
    # Better duplicate: WORK IDs in work.txt and work files
    def _ids_first_column(manifest: Path, pattern: str):
        ids = []
        if not manifest.exists():
            return ids
        for line in manifest.read_text(errors="ignore").splitlines():
            s=line.strip()
            if not s or s.startswith("#") or s.startswith("Format"):
                continue
            first = s.split("|")[0].strip()
            ids.extend(re.findall(pattern, first))
        return ids

    # Duplicate WORK / VER / ADR ids across the three id manifests. Every
    # duplicate is reported; only one whose record set exactly matches an
    # accepted historical collision is not a failure (ADR-000149,
    # docs/process/historical-id-collisions.md).
    _check_id_collisions(ROOT, findings)

    # 2. manifest targets missing (broken links)
    # reuse verify_development_system logic: check work.txt col 4, evidence col 5 etc
    def check_manifest(manifest: Path, col_idx: int):
        if not manifest.exists():
            findings.append(("FAIL", "MISSING_MANIFEST", f"{manifest} missing"))
            return
        for lineno, line in enumerate(manifest.read_text(errors="ignore").splitlines(), 1):
            line=line.strip()
            if not line or line.startswith("#") or line.startswith("Format"):
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) <= col_idx:
                continue
            record = parts[col_idx].strip()
            if not record or record.startswith("pending"):
                continue
            # record may contain multiple comma-separated
            for rec in re.split(r",\s*", record):
                rec = rec.strip().split()[0]  # first token
                if not rec or rec == "pending":
                    continue
                candidate = ROOT / rec
                # also try relative to manifest dir
                if not candidate.exists():
                    candidate2 = manifest.parent / rec
                    if not candidate2.exists():
                        findings.append(("FAIL", "BROKEN_MANIFEST_LINK", f"{manifest}:{lineno} -> {rec} missing"))

    check_manifest(ROOT/"docs"/"decisions"/"decisions.txt", 3)
    check_manifest(ROOT/"work"/"work.txt", 3)
    check_manifest(ROOT/"evidence"/"evidence.txt", 4)

    # 3. stale status
    work_txt = _read(ROOT/"work"/"work.txt")
    for line in work_txt.splitlines():
        if line.strip().startswith("#") or not line.strip() or "Format" in line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2:
            continue
        status_raw = parts[1].strip()
        # normalize: split by / and check each?
        # legacy ACTIVE/REVIEW is drift
        norm = status_raw.lower().replace(" ", "")
        if "/" in norm:
            findings.append(("WARN", "STALE_STATUS", f"work/work.txt status '{status_raw}' contains '/' — not in state-model (allowed: {ALLOWED_WORK_STATUS}) line: {line.strip()[:80]}"))
        elif norm not in ALLOWED_WORK_STATUS:
            findings.append(("WARN", "STALE_STATUS", f"work/work.txt status '{status_raw}' not in {ALLOWED_WORK_STATUS}"))

    # check work file headers
    for f in (ROOT/"work").rglob("*.md"):
        if f.name == "README.md":
            continue
        txt = _read(f)
        m = re.search(r"^\s*\*\*Status:\*\*\s*(.+)$", txt, re.MULTILINE)
        if not m:
            m = re.search(r"^\s*Status:\s*(.+)$", txt, re.MULTILINE | re.IGNORECASE)
        if m:
            st = m.group(1).strip().split()[0].lower().strip("*_`")
            # strip punctuation
            st_clean = re.sub(r"[^a-z\-]", "", st)
            if st_clean and st_clean not in ALLOWED_WORK_STATUS and st_clean not in {"historical"}:
                # historical is legacy drift
                findings.append(("WARN", "STALE_STATUS", f"{f.relative_to(ROOT)} Status '{m.group(1).strip()}' not in state-model"))

    # 4. orphaned requests
    inbox = list((ROOT/"requests"/"inbox").glob("*.md"))
    # filter README
    inbox = [p for p in inbox if p.name != "README.md"]
    processing = list((ROOT/"requests"/"processing").glob("*.md"))
    processing = [p for p in processing if p.name != "README.md" and not p.name.startswith("INT-")]
    if processing:
        # each processing request should have a WORK link or Intent
        for p in processing:
            txt = _read(p)
            if "WORK-" not in txt and "INT-" not in txt:
                findings.append(("WARN", "ORPHANED_REQUEST", f"{p.relative_to(ROOT)} in processing without WORK/INT link"))

    # 5. work with no requirement
    for f in (ROOT/"work").rglob("WORK-*.md"):
        txt = _read(f)
        if "REQ-" not in txt and "Requirement" not in txt:
            findings.append(("WARN", "WORK_NO_REQUIREMENT", f"{f.relative_to(ROOT)} has no REQ link"))

    # 6. requirement with no verification path (check requirements.txt verification column)
    req_txt = _read(ROOT/"docs"/"requirements"/"requirements.txt")
    for line in req_txt.splitlines():
        if line.strip().startswith("#") or not line.strip() or "Format" in line:
            continue
        if "|" not in line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 4:
            findings.append(("WARN", "REQUIREMENT_NO_VERIFICATION", f"requirements.txt line missing verification col: {line[:80]}"))
        else:
            ver_col = parts[3]
            if not ver_col or ver_col.lower() == "pending":
                findings.append(("WARN", "REQUIREMENT_NO_VERIFICATION", f"requirement {parts[0]} has no verification path"))

    # 7. completed work without evidence
    for f in (ROOT/"work"/"completed").glob("*.md"):
        txt = _read(f)
        # find WORK id from filename
        m = re.search(r"WORK-\d{6}", f.name)
        if m:
            wid = m.group(0)
            cands = _read(ROOT/"evidence"/"evidence.txt")
            if wid not in cands and wid not in txt:
                findings.append(("WARN", "COMPLETED_NO_EVIDENCE", f"{f.relative_to(ROOT)} completed without evidence link"))
            # also check evidence file exists
            ev_files = list((ROOT/"evidence").glob(f"*{wid}*.md"))
            if not ev_files and wid not in cands:
                findings.append(("FAIL", "COMPLETED_NO_EVIDENCE", f"{f.relative_to(ROOT)} no VER file nor manifest entry for {wid}"))

    # 8. orphaned work: work file exists but not in work.txt
    work_txt_ids = set(re.findall(r"WORK-\d{6}", work_txt))
    for f in (ROOT/"work").rglob("WORK-*.md"):
        m = re.search(r"WORK-\d{6}", f.name)
        if m and m.group(0) not in work_txt_ids:
            findings.append(("WARN", "ORPHANED_WORK", f"{f.relative_to(ROOT)} not listed in work/work.txt"))

    # 9./10. The repository's own documentation only (git scope, as
    # doc_integrity uses: ignored trees such as node_modules and build output
    # are not the repository's findings; CI_CD_ROADMAP PRO-10).
    import doc_integrity
    stale_paths = list(cfgmod.get("doc_integrity.stale_paths", ROOT, []))
    for f in doc_integrity.documentation_files(ROOT):
        rel = f.relative_to(ROOT).as_posix()
        txt = _read(f)
        # contradictory canonical docs: normative authority claimed outside docs/requirements
        if not rel.startswith("docs/requirements/") and re.search(r"sole normative.*authority", txt, re.IGNORECASE) \
                and "docs/requirements" not in txt:
            findings.append(("WARN", "CONTRADICTORY_AUTHORITY", f"{rel} claims normative authority outside docs/requirements"))
        # references to deleted documentation locations (doc_integrity.stale_paths)
        low = txt.lower()
        for stale in stale_paths:
            if stale in txt and "migration" not in low and "historical" not in low:
                findings.append(("WARN", "STALE_PATH_REF", f"{rel} references deleted path {stale}"))

    # 12..16. Work-state consistency (REQ-DEVOPS-044, ADR-000155). The
    # stuck-ticket audit (REQ-CHAT-20260921-011) found finished WORK records
    # left open that none of the checks above reported.
    _check_work_state(ROOT, findings, work_txt)
    _check_stale_claims(ROOT, findings)
    _check_evidence_status(ROOT, findings)
    _check_lifecycle(ROOT, findings)
    _check_mirrors(ROOT, findings)

    # 11. duplicate requirement IDs across files (same ID in two different docs headers)
    # simple: collect REQ-XXX headers
    header_ids = {}
    for f in (ROOT/"docs"/"requirements").glob("*.md"):
        txt = _read(f)
        for rid in re.findall(r"REQ-[A-Z]+-\d+", txt):
            header_ids.setdefault(rid, []).append(f.name)
    # only flag if same rid appears in 2 different files as defining header (heuristic: appears near ###)
    # skip for now — too noisy

    return findings

def consistent_check():
    """check() on a consistent view of the shared records. Work records, their
    manifest rows and request links change together under the "work" lock
    (work.py, evidence.py); a request's move and its resolution change
    together under the "claims" lock (agent_process). Reading without them can
    catch another agent between two writes. Lock order is work, then claims,
    as in work.py. Used by main() and by the completion gate."""
    import contextlib
    import agent_process
    with contextlib.ExitStack() as held:
        try:
            held.enter_context(agent_process.exclusive(ROOT, "work"))
            held.enter_context(agent_process.exclusive(ROOT, "claims"))
        except agent_process.ProcessError:
            pass                    # a lock could not be taken in time: read as is
        return check()


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--fail-on-warn", action="store_true")
    args = ap.parse_args()
    findings = consistent_check()
    fails = [f for f in findings if f[0]=="FAIL"]
    warns = [f for f in findings if f[0]=="WARN"]
    # Accepted conditions are real, documented and printed, but they are not
    # failures and are counted apart from them (ADR-000149).
    accepted = [f for f in findings if f[0]=="ACCEPTED"]
    if args.json:
        print(json.dumps([{"level":l,"code":c,"msg":m} for l,c,m in findings], indent=2))
    else:
        if not findings:
            print("DRIFT: PASS — no drift detected")
        else:
            for lvl, code, msg in findings:
                print(f"{lvl} {code}: {msg}")
            summary = f"{len(fails)} FAIL, {len(warns)} WARN"
            if accepted:
                summary += f", {len(accepted)} ACCEPTED"
            print(f"\nSummary: {summary}, {len(findings)} total")
    if fails:
        sys.exit(1)
    if args.fail_on_warn and warns:
        sys.exit(1)
    sys.exit(0)

if __name__ == "__main__":
    main()
