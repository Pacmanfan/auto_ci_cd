"""Reports of an architecture audit run (ARCH-AUDIT-*, ADR-000195).

human()   audit.md: concise, for a developer.
packet()  audit-packet.md: compact, for a development AI. It summarises the
          deterministic results and points at the evidence files instead of
          copying the repository.
console() the few lines printed at the end of a run.

Everything here is derived from audit.json alone.
"""
from __future__ import annotations

from pathlib import Path

DOMAINS = ("architecture", "requirements", "documentation", "build", "tests", "static", "process", "audit")
STATES = ("PASS", "FAIL", "DRIFT", "UNKNOWN", "SKIPPED")


def _counts_table(data: dict) -> list[str]:
    by_domain = data["summary"]["by_domain"]
    rows = ["| Domain | PASS | FAIL | DRIFT | UNKNOWN | SKIPPED |", "|---|---:|---:|---:|---:|---:|"]
    for domain in DOMAINS + tuple(sorted(set(by_domain) - set(DOMAINS))):
        if domain in by_domain:
            c = by_domain[domain]
            rows.append(f"| {domain.capitalize()} | {c.get('PASS', 0)} | {c.get('FAIL', 0)} | {c.get('DRIFT', 0)} | "
                        f"{c.get('UNKNOWN', 0)} | {c.get('SKIPPED', 0)} |")
    t = data["summary"]["totals"]
    rows.append(f"| **All** | **{t.get('PASS', 0)}** | **{t.get('FAIL', 0)}** | **{t.get('DRIFT', 0)}** | "
                f"**{t.get('UNKNOWN', 0)}** | **{t.get('SKIPPED', 0)}** |")
    return rows


def _checks(data: dict, status: str) -> list[dict]:
    return [c for c in data["checks"] if c["status"] == status]


def _header(data: dict) -> list[str]:
    git = data["git"]
    return [
        f"**Audit ID:** {data['audit_id']}  ",
        f"**Profile:** {data['profile']} (cost classes: {', '.join(data['profile_costs'])})  ",
        f"**Commit:** {git['commit'][:12]} ({git['describe']}, branch {git['branch']}); working tree: "
        f"{git['modified']} modified, {git['untracked']} untracked  ",
        f"**Started:** {data['started_at']} · **Duration:** {data['duration_seconds']} s "
        f"(budget {data['budget_seconds']:.0f} s)  ",
        f"**Tool:** tools/audit {data['tool_version']}, manifest {data['manifest_version']} · "
        f"**Build tree:** {data['build_dir']}",
    ]


def _changes(data: dict, limit: int = 12) -> list[str]:
    cmp = data.get("comparison") or {}
    if not cmp.get("previous"):
        return ["No previous audit: this run is the first reference point."]
    lines = [f"Compared with **{cmp['previous']}** ({cmp.get('previous_profile')}, commit "
             f"{(cmp.get('previous_commit') or '')[:12]}, finished {cmp.get('previous_finished_at')}).", ""]
    lines.append(f"* Commits since: {cmp['commits_since']}; files changed in those commits: "
                 f"{cmp['changed_files_count']}; modules touched (commits + working tree): "
                 f"{', '.join(cmp['changed_modules']) or 'none'}")
    if cmp["working_tree_newly_modified"] or cmp["working_tree_no_longer_modified"]:
        lines.append(f"* Working tree: {len(cmp['working_tree_newly_modified'])} newly modified, "
                     f"{len(cmp['working_tree_no_longer_modified'])} no longer modified")
    changes = cmp["status_changes"]
    if changes:
        for change in changes[:limit]:
            lines.append(f"* `{change['id']}`: {change['from'] or '(new check)'} → {change['to']}")
    else:
        lines.append("* No check changed status.")
    if cmp.get("coverage_changes"):
        lines.append(f"* {len(cmp['coverage_changes'])} check(s) ran in only one of the two audits "
                     f"({cmp.get('previous_profile')} vs {data['profile']}): "
                     + ", ".join(f"`{c['id']}`" for c in cmp["coverage_changes"][:limit]))
    metric_lines = []
    for m in cmp["metric_changes"]:
        if m["metric"] == "details":
            metric_lines.append(f"* `{m['id']}` findings: +{len(m['added'])} / -{len(m['removed'])}"
                                + (f" (new: {m['added'][0][:120]})" if m["added"] else ""))
        else:
            metric_lines.append(f"* `{m['id']}` {m['metric']}: {m['from']} → {m['to']}")
    lines += metric_lines[:limit]
    if len(metric_lines) > limit:
        lines.append(f"* … {len(metric_lines) - limit} more metric changes in audit.json")
    return lines


def _tests_section(data: dict) -> list[str]:
    lines = []
    recorded = next((c for c in data["checks"] if c["id"] == "test.recorded-results"), None)
    if recorded and recorded["status"] != "SKIPPED":
        m = recorded.get("metrics", {})
        by = ", ".join(f"{n} {s}" for s, n in (m.get("latest_by_status") or {}).items())
        lines.append(f"* Recorded results ({Path(data['build_dir']).name}): {m.get('recorded_groups')} of "
                     f"{m.get('registered_groups')} registered groups have a result ({by or 'none'}); "
                     f"{m.get('on_current_head')} on the current HEAD; {m.get('unrecorded_tier_groups')} tier "
                     f"groups never recorded; {m.get('lane_groups')} lane groups (perf / stress / soak / live / "
                     "package) are outside every audit profile.")
    run = next((c for c in data["checks"] if c["id"] == "test.unit-tier"), None)
    if run and run["status"] != "SKIPPED":
        lines.append(f"* Executed this audit: {run['summary']}")
    elif run:
        lines.append(f"* No tests executed by this audit ({run['summary']}).")
    plan = next((c for c in data["checks"] if c["id"] == "build.freshness"), None)
    if plan and plan["status"] != "SKIPPED":
        lines.append(f"* Test-binary freshness ({Path(data['build_dir']).name}): {plan['summary']}")
    return lines or ["* No test evidence collected in this profile."]


def _build_section(data: dict) -> list[str]:
    b = data["inventory"]["build"]
    lines = [f"* Tree `{b['build_dir']}`: {'present' if b['exists'] else 'absent'}"
             + (f", configuration {b.get('configuration')}" if b.get("configuration") else "")
             + (f", compiler `{b.get('compiler')}` ({b.get('compiler_version')})" if b.get("compiler") else
                f", compiler {b.get('compiler_version')}"),
             f"* Build system: {b.get('system', 'unknown')}"
             + (f"; qmake6 reports Qt {b.get('qmake6_qt_version')}; Makefiles by generating Qt version: "
                f"{b.get('qmake_qt_versions', {})}" if b.get("system") == "qmake" else ""),
             f"* Warnings as errors: {'-Werror in ' + b['warning_policy'] if b.get('werror') else 'NOT set'}; "
             "the audit does not build, so the warning count of the current sources is known only when their "
             "binaries are current (a -Werror build that succeeded has zero first-party warnings)."]
    for check_id in ("build.toolchain", "build.freshness"):
        c = next((x for x in data["checks"] if x["id"] == check_id), None)
        if c:
            lines.append(f"* `{check_id}` {c['status']}: {c['summary']}")
    return lines


def _next_actions(data: dict) -> list[str]:
    actions = []
    drift = _checks(data, "DRIFT")
    if drift:
        actions.append("For each DRIFT, the owner of the rule decides whether the implementation or the document "
                       "is out of date and records it (requirement / ADR change, or a fix request); the audit "
                       "changes neither: " + "; ".join(f"`{c['id']}` ({c.get('rule')})" for c in drift) + ".")
    fresh = next((c for c in data["checks"] if c["id"] == "build.freshness"), None)
    if fresh and fresh["status"] == "UNKNOWN":
        actions.append("Build the current sources (a private tree avoids disturbing other sessions) before "
                       "treating build and test state as known.")
    unknown = [c for c in _checks(data, "UNKNOWN") if c["domain"] != "audit"]
    if unknown:
        actions.append(f"{len(unknown)} rules are UNKNOWN; the cheapest next evidence for each is in "
                       "'Unverified areas' (future evidence).")
    return actions or ["Nothing to act on: no DRIFT and no UNKNOWN."]


def human(data: dict) -> str:
    lines = [f"# Audit Summary — {data['audit_id']}", ""] + _header(data) + ["", "## Results", ""]
    lines += _counts_table(data)
    lines += ["", "PASS: rule evaluated and holds · FAIL: a gate rule evaluated and does not hold · "
              "DRIFT: evaluated and deviates · UNKNOWN: could not be "
                  "evaluated, or the only check cannot show it holds (never counted as PASS) · SKIPPED: not "
                  "part of this profile.", "", "## Important findings (DRIFT)", ""]
    drift = _checks(data, "DRIFT")
    for c in drift:
        lines.append(f"### `{c['id']}` — {c['title']}")
        lines.append(f"Rule: {c.get('rule')} ({c.get('source')}). {c['summary']}  ")
        for d in [d for d in c.get("details", []) if d != c["summary"]][:6]:
            lines.append(f"* {d}")
        if len(c.get("details", [])) > 6:
            lines.append(f"* … {len(c['details']) - 6} more in `{c['evidence_file']}`")
        lines.append(f"Evidence: `{c['evidence_file']}`")
        lines.append("")
    if not drift:
        lines += ["None.", ""]
    lines += ["## Changes since previous audit", ""] + _changes(data) + [""]
    lines += ["## Build", ""] + _build_section(data) + [""]
    lines += ["## Tests", ""] + _tests_section(data) + [""]
    lines += ["## Unverified areas (UNKNOWN)", "", "| Check | Why | Future evidence |", "|---|---|---|"]
    for c in _checks(data, "UNKNOWN"):
        why = next((d[len("why unknown: "):] for d in c.get("details", []) if d.startswith("why unknown: ")),
                   c["summary"])
        lines.append(f"| `{c['id']}` | {why[:220]} | {c.get('future_evidence') or '—'} |")
    skipped = _checks(data, "SKIPPED")
    if skipped:
        lines += ["", f"Not run in the {data['profile']} profile (SKIPPED / NOT APPLICABLE): "
                  + ", ".join(f"`{c['id']}`" for c in skipped) + "."]
    lines += ["", "## Next actions", ""] + [f"{i}. {a}" for i, a in enumerate(_next_actions(data), 1)]
    lines += ["", f"Machine-readable: `audit.json` · AI packet: `audit-packet.md` · evidence: `evidence/`", ""]
    return "\n".join(lines)


def packet(data: dict) -> str:
    inv = data["inventory"]
    git = data["git"]
    lines = [
        f"# Audit packet — {data['audit_id']}",
        "",
        "You are reading the evidence packet of a deterministic architecture / conformance audit of the "
        f"{data['project'].get('name')} repository ({data['project'].get('product')}). Every result below was "
        "produced by a script, not by a model. When you reason over it:",
        "",
        "* cite check IDs and evidence paths (relative to this run directory) for every claim;",
        "* UNKNOWN means not established — never treat it as PASS;",
        "* a DRIFT says the implementation and a canonical document disagree; which one is wrong is a decision "
        "for the requirement / ADR owners, so do not rewrite a document just to match code;",
        "* ask for more evidence (a file, a command) rather than guessing.",
        "",
        "## Identity",
        "",
        f"* Profile {data['profile']}; commit {git['commit']} ({git['describe']}, {git['branch']}, "
        f"committed {git['commit_date']}); working tree {git['modified']} modified / {git['untracked']} untracked",
        f"* Audit {data['started_at']}, {data['duration_seconds']} s; tools/audit {data['tool_version']}; "
        f"host {inv['environment']['os']} {inv['environment']['machine']}, Python {inv['environment']['python']}",
        "",
        "## Canonical sources",
        "",
    ]
    lines += [f"* {k}: `{v}`" for k, v in data["canonical_sources"].items()]
    lines += ["", "## Architecture", "", "Modules (tracked files / source / test / docs):", ""]
    for name, counts in inv["modules"].items():
        lines.append(f"* {name}: {counts.get('files', 0)} / {counts.get('source', 0)} / {counts.get('test', 0)} / "
                     f"{counts.get('docs', 0)}")
    lines += ["", "Declared qmake dependencies (root project graph; nested subdirs children inherit their "
                  "parent's `.depends`):", ""]
    for pro, deps in inv["projects"].items():
        lines.append(f"* `{pro}` → {', '.join(f'`{d}`' for d in deps) or '—'}")
    not_current = inv["documentation"]["architecture_not_listed_as_current"]
    lines += ["", f"Architecture documents: {inv['documentation']['architecture_documents']}, of which "
                  f"{inv['documentation']['architecture_current_references']} are listed as current references in "
                  f"`docs/architecture/README.md`; not listed: {', '.join(Path(p).name for p in not_current)}."]
    req = next((c for c in data["checks"] if c["id"] == "req.verification-coverage"), None)
    lines += ["", "## Requirements", ""]
    lines.append(f"* {inv['requirements']['ids']} requirement IDs in {inv['requirements']['documents']} documents "
                 f"({inv['requirements']['namespaces']} ID namespaces)")
    if req and req["status"] != "SKIPPED":
        lines.append(f"* Coverage by citation: {req['summary']}")
        lines += [f"  * {d}" for d in req.get("details", [])[:8] if not d.startswith("why unknown")]
    lines += ["", "## Deterministic check results", "", "| Check | Domain | Status | Summary | Evidence |",
              "|---|---|---|---|---|"]
    for c in data["checks"]:
        lines.append(f"| `{c['id']}` | {c['domain']} | {c['status']} | {c['summary'][:160].replace('|', '/')} | "
                     f"{c.get('evidence_file') or '—'} |")
    lines += ["", "## DRIFT details", ""]
    for c in _checks(data, "DRIFT"):
        lines.append(f"### `{c['id']}` ({c.get('rule')}; {c.get('source')})")
        lines += [f"* {d[:300]}" for d in c.get("details", [])[:15]]
        lines.append("")
    lines += ["## Unknowns", ""]
    for c in _checks(data, "UNKNOWN"):
        why = next((d[len("why unknown: "):] for d in c.get("details", []) if d.startswith("why unknown: ")),
                   c["summary"])
        lines.append(f"* `{c['id']}` ({c.get('rule')}): {why[:240]}"
                     + (f" — future evidence: {c['future_evidence']}" if c.get("future_evidence") else ""))
    lines += ["", "## Build", ""] + _build_section(data)
    lines += ["", "## Tests", ""] + _tests_section(data)
    lines += ["", "## Changes", ""] + _changes(data, limit=25)
    cmp = data.get("comparison") or {}
    if cmp.get("previous_summary"):
        lines += ["", f"Previous audit totals: {cmp['previous_summary'].get('totals')}"]
    lines += ["", "## Suggested analysis", "",
              "1. For each DRIFT: is the requirement / architecture text or the implementation out of date? What "
              "evidence decides it?",
              "2. Which UNKNOWN rules matter most for this commit's changed modules, and what is the cheapest "
              "evidence that would settle them?",
              "3. Read the current architecture documents listed in the inventory against the declared module "
              "graph: which statements are no longer true (prose the path check cannot judge)?",
              "4. Which requirement documents have the largest uncited share, and are those areas covered by tests "
              "that do not cite IDs?", ""]
    return "\n".join(lines)


def console(data: dict, run_dir: Path) -> str:
    t = data["summary"]["totals"]
    lines = [f"\n{data['audit_id']} ({data['profile']}, {data['duration_seconds']} s): "
             f"{t.get('PASS', 0)} PASS, {t.get('FAIL', 0)} FAIL, {t.get('DRIFT', 0)} DRIFT, "
             f"{t.get('UNKNOWN', 0)} UNKNOWN, {t.get('SKIPPED', 0)} SKIPPED"]
    for c in _checks(data, "FAIL"):
        lines.append(f"  FAIL    {c['id']}: {c['summary'][:150]}")
    for c in _checks(data, "DRIFT"):
        lines.append(f"  DRIFT   {c['id']}: {c['summary'][:150]}")
    cmp = data.get("comparison") or {}
    if cmp.get("previous"):
        changed = cmp["status_changes"]
        lines.append(f"  vs {cmp['previous']}: {len(changed)} status change(s)"
                     + (": " + ", ".join(f"{c['id']} {c['from']}→{c['to']}" for c in changed[:6]) if changed else ""))
    lines.append(f"  report: {run_dir / 'audit.md'}")
    lines.append(f"  packet: {run_dir / 'audit-packet.md'}")
    return "\n".join(lines)
