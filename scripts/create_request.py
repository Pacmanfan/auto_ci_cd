#!/usr/bin/env python3
"""
Canonical Request Intake — unified for chat, inbox, crash, automation sources.

All sources converge to the same persistent artifact: requests/inbox/*.md

SUBMIT-ONLY CONTRACT (REQ-DEVOPS-043, ADR-000150): this tool submits a
request into the inbox for later review. It does not process or execute
the request — it never claims, assigns, worktrees, or starts work.
Listing or reading a ticket does not claim it; claiming (scripts/claim.py
--claim-request) is the separate, explicit step that begins processing.
SUBMIT != CLAIM != PROCESS.

CLASSIFICATION AND SYSTEM METADATA (REQ-DEVOPS-045, ADR-000173):
  * The request type is a closed vocabulary (scripts/request_classification.py):
      FEATURE       new capability / integration / enhancement
      BUG           existing behaviour is incorrect or fails (a test failure is a BUG)
      CRASH         an actual fatal runtime failure only: crash, segfault, abort,
                    fatal exception, unexpected termination — never inferred from
                    generic failure language ("fails", "broken", "error", "issue")
      REGRESSION    previously working behaviour became broken (needs that evidence)
      PERFORMANCE   correct but too slow / too much CPU, GPU, memory, drops
      REFACTOR      internal restructuring, no material behaviour change
      BUILD         the tree does not build / compile / link
      DOCUMENTATION documentation is the deliverable
      INVESTIGATION a question or analysis
      PROCESS-CHANGE a change to the development system itself
  * `--type` is a *proposal*. The deterministic validator is authoritative: an
    unknown value is rejected; CRASH / REGRESSION / PERFORMANCE / BUILD without
    supporting evidence in the text is corrected to what the text supports or,
    when that is ambiguous, rejected so the caller classifies again. The
    proposed type, the final type, the verdict, the reason and the evidence are
    recorded in the ticket (## Classification) and its JSON sidecar.
  * Request id, sequence number and date are SYSTEM METADATA. The date is the
    local system date at creation (never UTC-shifted, never taken from the
    text or the caller); the number comes from the global monotonic allocator.
    Do not generate or guess them.

Usage:
  # Chat
  python3 scripts/create_request.py --source chat --text "I loaded a file and the application crashed." --priority normal
  python3 scripts/create_request.py --source chat --text "Add support for HDF5 data files." --type feature

  # Review an existing record's stored type (never modifies); --reclassify corrects it in place
  python3 scripts/create_request.py --file requests/inbox/foo.md
  python3 scripts/create_request.py --file requests/inbox/foo.md --reclassify [--type FEATURE]
  python3 scripts/create_request.py --review-all          # every record under requests/

  # Follow-up: append context to existing request
  python3 scripts/create_request.py --follow-up REQ-CHAT-20260908-001 --text "It only happens with this particular file."

  # Crash report
  python3 scripts/create_request.py --source crash --text "I loaded a file and the application crashed." --attach log/dump/<APP>_*.core

Design:
  Source != Workflow. After intake, every request follows docs/process/state-model.md lifecycle.
  Preserves original user statement verbatim; interpretation is separate.
  Generates stable ID: REQ-<SRC>-YYYYMMDD-NNN  or REQ-IN-YYYYMMDD-NNN for inbox compatibility.
"""
import argparse
import re
import json
from pathlib import Path
from datetime import datetime, timezone
import sys
import os

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_process as proc  # noqa: E402  (ADR-000090 concurrency-safe primitives)
import request_classification as rcls  # noqa: E402  (REQ-DEVOPS-045 vocabulary + validation)

ROOT = proc.repo_root()
INBOX = ROOT / "requests" / "inbox"
PROCESSING = ROOT / "requests" / "processing"

import devsys_config as _cfg  # noqa: E402
# The sources are fixed (their workflows differ); the id prefix of each is
# configuration (devsys.json ids.request_sources, validated as REQ-<LETTERS>).
SOURCE_PREFIX = dict(_cfg.get("ids.request_sources", ROOT))


def classify(text: str) -> str:
    """Heuristic type of ``text`` (kept for callers of the old name)."""
    return rcls.classify(text).final_type


def next_id(source: str, date: dict) -> str:
    """Allocate a request id that is unique for the request's whole lifetime
    (REQ-DEVOPS-003/014, ADR-000090).

    The series is named after the LOCAL system date passed in ``date``
    (REQ-DEVOPS-045): the former implementation used the UTC date, so a
    request filed in the evening (America/New_York) carried tomorrow's date.
    The allocator takes the maximum over every request store and a persistent
    monotonic counter, under an inter-process lock.
    """
    prefix = SOURCE_PREFIX.get(source, "REQ-IN")
    return proc.allocate_id(ROOT, f"{prefix}-{date['yyyymmdd']}", agent_id=os.environ.get("AGENT_ID", ""))


def diagnostics(c: rcls.Classification) -> dict:
    """Structured classification diagnostics for the sidecar: the whole verdict
    plus the evidence that decided it (symptom-type evidence and the final
    type's evidence, capped so a 40 KB specification stays reviewable)."""
    d = c.to_dict()
    keep = set(rcls.EVIDENCE_REQUIRED) | {c.final_type or "", c.heuristic_type}
    ev = [e for e in d["evidence"] if e["category"] in keep]
    d["evidence"] = ev[:40]
    d["evidence_total"] = len(c.evidence)
    d["validator"] = "scripts/request_classification.py"
    return d


def classification_markdown(c: rcls.Classification, priority: str, source: str) -> str:
    lines = [
        f"- Type: {c.final_type} (final; validated by `scripts/request_classification.py`, REQ-DEVOPS-045)",
        f"- Priority: {priority}",
        f"- Source: {source}",
        "- classification:",
        f"  - proposed_type: {c.proposed_type or '-'} ({c.proposed_source})",
        f"  - final_type: {c.final_type}",
        f"  - heuristic_type: {c.heuristic_type} (confidence {c.confidence})",
        f"  - validation: {c.validation}",
        f"  - classifier_reason: {c.classifier_reason}",
    ]
    if c.correction_reason:
        lines.append(f"  - correction_reason: {c.correction_reason}")
    if c.normalization_note:
        lines.append(f"  - normalization: {c.normalization_note}")
    sup = rcls.supporting_evidence(c.evidence, c.final_type) if c.final_type else []
    if sup:
        lines.append("  - evidence: " + "; ".join(f"{e.phrase!r} [{e.where}]" for e in sup[:5]))
    return "\n".join(lines)


def render_request(req_id: str, source: str, text: str, classification: rcls.Classification, priority: str,
                   attachments: list, context: str = "", date: dict = None):
    date = date or proc.request_date()
    req_type = classification.final_type
    attachments_md = "\n".join(f"- `{a}`" for a in attachments) if attachments else "- none"
    # inferred intent — lightweight, preserve observed vs interpretation
    intent = ""
    if req_type == "CRASH":
        intent = ("Observed: application crashed (user report). Unknown: file/format, crash location, "
                  "reproducibility, dump availability, recent changes. Intent: investigate crash, identify root cause, "
                  "implement minimal fix, add regression test, verify without regression.")
    elif req_type == "FEATURE":
        intent = "Intent: implement requested capability per classification; derive requirement, implement, test, verify."
    elif req_type == "BUG":
        intent = "Intent: investigate bug, identify root cause, implement correct behavior per requirement, add regression test."
    elif req_type == "REGRESSION":
        intent = ("Intent: confirm the previously working behaviour and the change that broke it, restore it, "
                  "add a regression test, verify.")
    elif req_type == "PERFORMANCE":
        intent = "Intent: measure the reported performance problem, identify the bottleneck, optimise with evidence, verify."
    else:
        intent = f"Intent: investigate {req_type.lower()} per classification; derive requirement and verify."

    if context:
        intent += f"\nConversational context: {context[:500]}"

    body = f"""# Request: {text[:80]}

**Request ID:** {req_id}
**Source:** {source}
**Type:** {req_type}
**Priority:** {priority}
**Created:** {date['iso']}
**Status:** inbox
**Attachments:** {attachments_md}

## Original User Statement

> {text}

## Classification

{classification_markdown(classification, priority, source)}

## Inferred Intent (agent interpretation — not user statement)

{intent}

## Observed / Unknown

- Observed: see original statement above.
- Unknown: {('file, format, crash dump, reproducibility, logs' if req_type=='CRASH' else 'exact scope — agent to derive')}
- Follow-ups will be appended under ## Follow-up Context

## Follow-up Context

""" + (f"{context}\n" if context else "_No follow-up yet._\n") + f"""
## Processing Notes

- This is the canonical persistent request artifact. Conversation history is contextual evidence; this file is authoritative.
- After intake, workflow is `requests/inbox -> processing -> intent -> requirement -> work -> implementation -> test -> verification -> documentation -> complete` per `docs/process/state-model.md` and `docs/process/autonomous-operating-model.md`.
- Next: agent claims via `scripts/claim.py`, then `scripts/preflight.py`, then requirement/work linkage.

## Traceability

- Intent: pending (created from this request)
- Requirement: pending
- Work: pending
- Evidence: pending
"""
    return body


def print_classification(c: rcls.Classification, stream=None):
    stream = stream or sys.stdout
    print(f"Classification: {c.summary()}", file=stream)
    print(f"  reason: {c.classifier_reason}", file=stream)
    if c.normalization_note:
        print(f"  normalization: {c.normalization_note}", file=stream)
    sup = rcls.supporting_evidence(c.evidence, c.final_type) if c.final_type else []
    for e in sup[:3]:
        print(f"  evidence [{e.where}]: {e.phrase!r} — {e.sentence[:120]!r}", file=stream)


def create_request(source: str, text: str, req_type: str, priority: str, attachments: list, context: str = "",
                   allow_duplicate: bool = False, as_json: bool = False):
    if not text or len(text.strip()) < 3:
        print("Text too short", file=sys.stderr)
        sys.exit(1)
    # --- classification: proposal -> deterministic validation (authoritative)
    classification = rcls.validate(text, req_type)
    if classification.validation == "REJECTED":
        print(f"REJECTED: {classification.correction_reason}", file=sys.stderr)
        print(f"  text alone supports: {classification.heuristic_type} (confidence {classification.confidence}) — "
              f"{classification.classifier_reason}", file=sys.stderr)
        print("  nothing was written; classify again with a supported --type (or omit --type)", file=sys.stderr)
        sys.exit(proc.INVALID)
    prio = rcls.normalize_priority(priority)
    if prio is None:
        print(f"REJECTED: unknown priority {priority!r}; valid: {', '.join(rcls.VALID_PRIORITIES)}", file=sys.stderr)
        sys.exit(proc.INVALID)
    priority = prio
    # --- duplicate submissions (identical normalised text of an open request)
    import hashlib
    import devsys_config as cfgmod
    # The intake lock spans the duplicate check and the creation below, so two
    # identical concurrent submissions cannot both pass the check.
    intake_lock = proc.exclusive(ROOT, "intake")
    intake_lock.__enter__()
    if cfgmod.get("intake.refuse_duplicates", ROOT, True) and not allow_duplicate:
        dup = open_duplicate(text)
        if dup:
            intake_lock.__exit__(None, None, None)
            print(f"DUPLICATE: {dup[0]} ({dup[1].relative_to(ROOT)}) is open with the same statement; nothing was "
                  "written. Add context with --follow-up, or pass --allow-duplicate if it is a separate request.",
                  file=sys.stderr)
            sys.exit(proc.COLLISION)
    # --- system metadata: local date + allocator; never from the text or the caller
    date = proc.request_date()
    req_id = next_id(source, date)
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower().strip()[:40]).strip("-")
    filename = f"{date['date']}-{slug[:30]}-{req_id.lower()}.md" if slug else f"{date['date']}-{req_id.lower()}.md"
    body = render_request(req_id, source, text, classification, priority, attachments, context, date)
    sidecar = {
        "request_id": req_id,
        "source": source,
        "type": classification.final_type,
        "priority": priority,
        "created": date["iso"],
        "created_utc": date["utc"],
        "id_date": date["date"],
        "timezone": date["timezone"],
        "status": "inbox",
        "original_text": text,
        "file": f"requests/inbox/{filename}",
        "attachments": attachments,
        "context": context,
        "text_sha256": hashlib.sha256(normalized_text(text).encode()).hexdigest(),
        "classification": diagnostics(classification),
    }
    # Created with O_EXCL: an existing request file (or an id already held by
    # any request) is never overwritten, renamed around or renumbered.
    try:
        path = proc.create_request_files(ROOT, req_id, filename, body, sidecar)
    except proc.ProcessError as e:
        print(f"COLLISION: {e.message}", file=sys.stderr)
        sys.exit(e.code)
    finally:
        intake_lock.__exit__(None, None, None)
    if as_json:
        print(json.dumps({"created": str(path.relative_to(ROOT)), "request_id": req_id, "file": path.name,
                          "type": classification.final_type, "priority": priority, "status": "inbox"}))
        return path, req_id
    print(f"Created {path.relative_to(ROOT)}")
    print(f"Request ID: {req_id}  (date {date['date']} {date['timezone']}, system-generated)")
    print(f"Type: {classification.final_type}  Priority: {priority}  Source: {source}")
    print_classification(classification)
    print("Status: inbox (SUBMITTED)  Processing: NOT STARTED — submit-only; "
          "claim explicitly with scripts/claim.py --claim-request to begin work.")
    return path, req_id


def follow_up(request_id: str, text: str, attach: list = None):
    """Append follow-up context to the request whose id is exactly
    ``request_id`` (its file name or its Request ID header; a request that
    merely mentions the id is another request). Serialised under a per-request
    lock with atomic writes, so concurrent follow-ups are never lost; a closed
    (completed / rejected) request is refused — file a new request that
    references it (CI_CD_ROADMAP COR-08)."""
    matches = [p for p in proc.find_request_files_by_id(ROOT, request_id) if p.suffix == ".md"]
    if not matches:
        print(f"Request {request_id} not found", file=sys.stderr)
        sys.exit(proc.NOT_FOUND)
    if len({p.name for p in matches}) > 1:
        print(f"Request id {request_id} is held by several files: {', '.join(str(p) for p in matches)}", file=sys.stderr)
        sys.exit(proc.BAD_STATE)
    name = matches[0].name
    with proc.exclusive(ROOT, f"request-{name}"):
        locs = proc.locate_request(ROOT, name)
        if not locs:
            print(f"Request {request_id} moved away while waiting", file=sys.stderr)
            sys.exit(proc.NOT_FOUND)
        state, target = locs[0]
        if state in ("completed", "rejected"):
            print(f"REFUSED: {request_id} is {state}; file a new request that references it "
                  f"(scripts/create_request.py --text '... (follows {request_id})')", file=sys.stderr)
            sys.exit(proc.BAD_STATE)
        ts = datetime.now(timezone.utc).isoformat()
        existing = target.read_text(encoding="utf-8")
        addition = f"\n\n### Follow-up {ts}\n\n> {text}\n"
        if attach:
            addition += "\nAttachments: " + ", ".join(f"`{a}`" for a in attach) + "\n"
        if "## Follow-up Context\n" in existing:
            existing = existing.replace("## Follow-up Context\n", f"## Follow-up Context\n{addition}\n", 1)
            existing = existing.replace("_No follow-up yet._\n", "")
        else:
            existing += addition
        proc.atomic_write_text(target, existing)
        sidecar = target.with_suffix(".json")
        if sidecar.exists():
            try:
                data = json.loads(sidecar.read_text(encoding="utf-8"))
            except ValueError:
                data = None
            if data is not None:
                data.setdefault("follow_ups", []).append({"timestamp": ts, "text": text, "attachments": attach or []})
                proc.atomic_write_text(sidecar, json.dumps(data, indent=2) + "\n")
    print(f"Appended follow-up to {target.relative_to(ROOT)}")
    return target


def normalized_text(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


def open_duplicate(text: str):
    """An open request (inbox / processing / needs-review) whose original
    statement normalises to the same text, or None (CI_CD_ROADMAP PRO-07)."""
    import hashlib
    want = hashlib.sha256(normalized_text(text).encode()).hexdigest()
    for state in ("inbox", "processing", "needs-review"):
        for js in sorted((ROOT / "requests" / state).glob("*.json")):
            try:
                data = json.loads(js.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            h = data.get("text_sha256") or hashlib.sha256(
                normalized_text(str(data.get("original_text") or "")).encode()).hexdigest()
            if h == want:
                return data.get("request_id"), js.with_suffix(".md")
    return None


# ------------------------------------------------------------------ review / reclassify

def _load_record(p: Path):
    md = p.read_text(encoding="utf-8", errors="ignore")
    js = p.with_suffix(".json")
    sidecar = None
    if js.exists():
        try:
            sidecar = json.loads(js.read_text(encoding="utf-8"))
        except ValueError:
            sidecar = None
    return md, js, sidecar


def review_file(p: Path, quiet: bool = False) -> dict:
    md, js, sidecar = _load_record(p)
    r = rcls.review_record(md, sidecar)
    if not quiet:
        rel = p.relative_to(ROOT) if str(p).startswith(str(ROOT)) else p
        print(f"{rel}")
        print(f"  stored_type: {r['stored_type']}   normalized: {r['normalized_type']}   "
              f"heuristic: {r['heuristic_type']} ({r['confidence']})")
        print(f"  verdict: {r['verdict']}{(' — ' + r['detail']) if r['detail'] else ''}")
        print(f"  reason: {r['classifier_reason']}")
        for e in r["evidence"][:3]:
            print(f"  evidence [{e['category']}/{e['where']}/{e['strength']}]: {e['phrase']!r} — {e['sentence'][:110]!r}")
    return r


def reclassify_file(p: Path, proposed: str = None) -> dict:
    """Explicit, recorded correction of an existing record's type: the stored
    type is replaced by the validated type, the previous value is kept in the
    sidecar under classification.history, and the markdown ## Classification
    block is refreshed. The original statement is never touched."""
    md, js, sidecar = _load_record(p)
    text = (sidecar or {}).get("original_text") or rcls.original_statement_of(md)
    stored = (sidecar or {}).get("type") or rcls.stored_type_of(md)
    c = rcls.validate(text, proposed)
    if c.validation == "REJECTED":
        print(f"REJECTED: {c.correction_reason}", file=sys.stderr)
        sys.exit(proc.INVALID)
    new_type = c.final_type
    ts = proc.request_date()["iso"]
    md2, n = re.subn(r"^\*\*Type:\*\*.*$", f"**Type:** {new_type}", md, count=1, flags=re.M)
    if n == 0:
        print("no **Type:** line in the record; nothing changed", file=sys.stderr)
        sys.exit(proc.INVALID)
    m = re.search(r"^- Type: .*$", md2, re.M)
    if m:
        md2 = md2[:m.start()] + f"- Type: {new_type} (reclassified {ts} from {stored}: {c.validation}" \
              + (f", {c.correction_reason}" if c.correction_reason else f", {c.classifier_reason}") + ")" + md2[m.end():]
    p.write_text(md2, encoding="utf-8")
    if js.exists() and sidecar is not None:
        hist = sidecar.setdefault("classification", {}).setdefault("history", [])
        hist.append({"at": ts, "from": stored, "to": new_type, "validation": c.validation,
                     "reason": c.correction_reason or c.classifier_reason, "proposed": proposed})
        sidecar["type"] = new_type
        d = diagnostics(c)
        d["history"] = hist
        sidecar["classification"] = d
        js.write_text(json.dumps(sidecar, indent=2) + "\n", encoding="utf-8")
    rel = p.relative_to(ROOT) if str(p).startswith(str(ROOT)) else p
    print(f"Reclassified {rel}: {stored} -> {new_type} [{c.validation}]")
    print_classification(c)
    return c.to_dict()


def review_all() -> int:
    bad = 0
    rows = []
    for p in sorted((ROOT / "requests").glob("*/*.md")):
        if p.name == "README.md":
            continue
        r = review_file(p, quiet=True)
        rows.append((p.relative_to(ROOT), r))
        if r["verdict"] in ("INVALID-VOCABULARY", "UNSUPPORTED", "NON-CANONICAL"):
            bad += 1
    print(f"{'verdict':18} {'stored':14} {'heuristic':14} record")
    for rel, r in rows:
        if r["verdict"] != "OK":
            print(f"{r['verdict']:18} {str(r['stored_type'])[:14]:14} {r['heuristic_type']:14} {rel}")
    print(f"\n{len(rows)} records reviewed; {bad} need attention (INVALID-VOCABULARY / UNSUPPORTED / NON-CANONICAL); "
          f"DIFFERS is informational.")
    return bad


def main():
    ap = argparse.ArgumentParser(
        description="Submit a request into requests/inbox/ for later review. "
                    "Submit-only: creates the persistent ticket and stops — "
                    "does not claim, assign, or process it. "
                    "The request type is validated deterministically against the closed vocabulary "
                    "(" + ", ".join(rcls.CANONICAL_TYPES) + "); CRASH is reserved for actual fatal runtime "
                    "failures and is never inferred from generic failure language. Request id, date and "
                    "sequence number are system-generated metadata (local system date + global allocator).",
        epilog="SUBMIT != CLAIM != PROCESS. After intake, a later agent lists "
               "the inbox (scripts/process_next_request.py --dry-run) and "
               "claims explicitly (scripts/claim.py --claim-request).")
    ap.add_argument("--source", choices=["chat","inbox","crash","automation"], default="chat")
    ap.add_argument("--text", help="original user statement")
    ap.add_argument("--file", help="existing request record to review (with --reclassify: correct its type in place)")
    ap.add_argument("--reclassify", action="store_true", help="with --file: rewrite the stored type with the validated type, recording the history")
    ap.add_argument("--review-all", action="store_true", help="review the stored type of every record under requests/")
    ap.add_argument("--strict", action="store_true", help="with --file/--review-all: exit non-zero when a record needs attention")
    ap.add_argument("--type", dest="req_type", help="proposed type (validated; unknown values are rejected, unsupported CRASH/REGRESSION/PERFORMANCE/BUILD corrected or rejected)")
    ap.add_argument("--priority", default="NORMAL", help="CRITICAL|HIGH|NORMAL|LOW")
    ap.add_argument("--attach", action="append", default=[], help="attachment path (repeatable)")
    ap.add_argument("--context", default="", help="conversational context")
    ap.add_argument("--follow-up", help="append to existing request ID instead of creating new")
    ap.add_argument("--allow-duplicate", action="store_true", help="submit although an open request has the same "
                    "statement")
    ap.add_argument("--json", action="store_true", help="print the created request as JSON")
    args = ap.parse_args()

    if args.follow_up:
        if not args.text:
            print("--text required for follow-up", file=sys.stderr)
            sys.exit(1)
        follow_up(args.follow_up, args.text, args.attach)
        return

    if args.review_all:
        bad = review_all()
        sys.exit(proc.INVALID if (args.strict and bad) else 0)

    if args.file:
        p = Path(args.file)
        if not p.is_absolute():
            p = ROOT / p
        if not p.exists():
            print(f"File not found: {p}", file=sys.stderr)
            sys.exit(1)
        if args.reclassify:
            reclassify_file(p, args.req_type)
            return
        r = review_file(p)
        sys.exit(proc.INVALID if (args.strict and r["verdict"] in ("INVALID-VOCABULARY", "UNSUPPORTED", "NON-CANONICAL")) else 0)

    if not args.text:
        print("--text required (or --file / --follow-up)", file=sys.stderr)
        sys.exit(1)
    create_request(args.source, args.text, args.req_type, args.priority, args.attach, args.context,
                   allow_duplicate=args.allow_duplicate, as_json=args.json)

if __name__ == "__main__":
    main()
