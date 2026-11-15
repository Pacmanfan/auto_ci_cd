"""The worker prompt and the agent's completion report.

The prompt (``workers.agent.prompt``, default ``prompts/worker.md``) points
the agent at the project's own instructions (AGENTS.md) and at the framework
tools; it adds only what is specific to unattended execution: the claim that
is already held, the time limit, previous attempts and the report format.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path, PurePosixPath
from string import Template

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import agent_process as proc  # noqa: E402
import devsys_config as cfgmod  # noqa: E402

import workers  # noqa: E402

REPORT_STATUSES = ("COMPLETED", "PARTIAL", "BLOCKED", "FAILED", "ESCALATE")
REPORT_FIELDS = ("request_id", "status", "summary", "implementation", "files_changed", "commits",
                 "tests_run", "validation", "known_issues", "remaining_work", "reason_for_status")
REPORT_SCHEMA = {
    "request_id": "REQ-...",
    "status": "COMPLETED | PARTIAL | BLOCKED | FAILED | ESCALATE",
    "summary": "one paragraph: what was asked and what you did",
    "implementation": "what changed and why",
    "files_changed": ["path/one", "path/two"],
    "commits": ["<sha> <subject>"],
    "tests_run": [{"command": "...", "result": "PASS | FAIL | NOT RUN", "notes": "run id, counts"}],
    "validation": "what you verified and how (evidence ids, test run ids, gate record)",
    "known_issues": ["..."],
    "remaining_work": ["..."],
    "reason_for_status": "why this status; for ESCALATE the exact question for the human",
    "work_ids": ["WORK-000123"],
    "request_state": "completed | needs-review | processing | rejected",
}
EXCERPT_CHARS = 6000


def render_previous(prev: list[dict]) -> str:
    if not prev:
        return "None: this is the first attempt at this request."
    lines = ["Earlier sessions worked on this request. Continue from where they stopped; "
             "check the repository and the project's records rather than trusting these notes blindly.", ""]
    for a in prev:
        rep = a.get("report") or {}
        lines.append(f"### {a['attempt_id']} ({a.get('worker_id')}): {a.get('result')}")
        if a.get("result_reason"):
            lines.append(f"- Worker: {a['result_reason']}")
        for key in ("summary", "reason_for_status"):
            if rep.get(key):
                lines.append(f"- {key}: {str(rep[key])[:800]}")
        for key in ("remaining_work", "known_issues"):
            if rep.get(key):
                items = rep[key] if isinstance(rep[key], list) else [rep[key]]
                lines.append(f"- {key}: " + "; ".join(str(i)[:300] for i in items[:8]))
        if a.get("commits"):
            lines.append("- commits: " + "; ".join(f"{c['sha'][:10]} {c['subject'][:80]}" for c in a["commits"][:8]))
        if a.get("owned_uncommitted"):
            lines.append("- left uncommitted (still registered to the claim): " + ", ".join(a["owned_uncommitted"][:20]))
        v = a.get("validation") or {}
        if v.get("overall"):
            failing = [s["name"] for s in v.get("steps", []) if s.get("status") in ("FAIL", "UNKNOWN")]
            lines.append(f"- independent validation: {v['overall']}" + (f" ({', '.join(failing)})" if failing else ""))
        lines.append("")
    return "\n".join(lines).rstrip()


SCRIPTS_DIR = Path(__file__).resolve().parent.parent


def agent_scripts_dir(root: Path, work_dir: Path) -> Path:
    """The development system's scripts *as seen from the agent's working
    directory*: in the project checkout the system's own scripts; in a
    request's worktree that worktree's copy, so every tool the agent runs
    acts on the worktree (its commits land on the request's branch)."""
    try:
        rel = SCRIPTS_DIR.relative_to(cfgmod.project_root(root))
    except ValueError:
        return SCRIPTS_DIR
    candidate = Path(work_dir) / rel
    return candidate if candidate.is_dir() else SCRIPTS_DIR


def scripts_path(root: Path, work_dir: Path) -> str:
    """``agent_scripts_dir`` relative to the working directory when inside it
    (``scripts`` here, ``.devsys/scripts`` in a subdirectory installation)."""
    d = agent_scripts_dir(root, work_dir)
    try:
        return d.relative_to(Path(work_dir).resolve()).as_posix()
    except ValueError:
        return str(d)


def system_path(root: Path, work_dir: Path) -> str:
    """The development system's directory relative to the working directory:
    "." when the system is the project root, e.g. ".devsys" when it is
    installed as a project subdirectory."""
    return proc.system_prefix(root) or "."


def build(root: Path, *, worker_id: str, request: dict, request_path: str, agent_id: str, attempt_no: int,
          max_attempts: int, prev: list[dict], report_path: Path, work_dir: Path, max_runtime_minutes: float) -> str:
    text = request.get("text") or ""
    excerpt = text[:EXCERPT_CHARS]
    if len(text) > EXCERPT_CHARS:
        excerpt += f"\n\n[... {len(text) - EXCERPT_CHARS} more characters: read the file ...]"
    values = {
        "orchestrator_version": workers.VERSION, "worker_id": worker_id, "attempt_no": attempt_no,
        "max_attempts": max_attempts, "work_dir": str(work_dir), "request_id": request["request_id"],
        "request_path": request_path, "request_type": request.get("type") or "unspecified",
        "request_priority": request.get("priority") or "NORMAL", "request_title": request.get("title") or "",
        "request_excerpt": excerpt, "agent_id": agent_id, "report_path": str(report_path),
        "max_runtime_minutes": int(max_runtime_minutes), "wrap_up_minutes": max(1, int(max_runtime_minutes * 0.8)),
        "report_schema": json.dumps(REPORT_SCHEMA, indent=2), "previous_attempts": render_previous(prev),
        "scripts": scripts_path(root, work_dir),
        "python": Path(sys.executable).name if Path(sys.executable).name.lower().startswith("python") else sys.executable,
        "system_dir": system_path(root, work_dir),
        "agents_md": (PurePosixPath(system_path(root, work_dir)) / "AGENTS.md").as_posix(),
    }
    rel = cfgmod.get("workers.agent.prompt", root, "scripts/workers/prompts/worker.md")
    path = Path(rel) if Path(rel).is_absolute() else Path(root) / rel
    return Template(path.read_text(encoding="utf-8")).safe_substitute(values)


_FENCE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)


def extract_json_block(text: str, must_have: str) -> dict | None:
    """The last fenced JSON object in ``text`` that has key ``must_have``."""
    for body in reversed(_FENCE.findall(text or "")):
        try:
            obj = json.loads(body)
        except ValueError:
            continue
        if isinstance(obj, dict) and must_have in obj:
            return obj
    return None


def parse_report(report_path: Path, info: dict) -> tuple[dict | None, str, list[str]]:
    """(report, source, problems). The file the agent wrote wins; the JSON block
    at the end of its final message is the fallback."""
    problems: list[str] = []
    report, source = None, "none"
    if Path(report_path).exists():
        try:
            report = json.loads(Path(report_path).read_text(encoding="utf-8"))
            source = "file"
        except ValueError as e:
            problems.append(f"report file is not valid JSON: {e}")
    if report is None:
        text = ((info.get("result") or {}).get("result") or "") + "\n" + (info.get("last_text") or "")
        report = extract_json_block(text, "status")
        if report is not None:
            source = "final-message"
    if report is None:
        return None, source, problems + ["no completion report (neither report file nor JSON block)"]
    if not isinstance(report, dict):
        return None, source, problems + ["report is not a JSON object"]
    status = str(report.get("status", "")).strip().upper()
    if status not in REPORT_STATUSES:
        return None, source, problems + [f"report status {report.get('status')!r} is not one of {REPORT_STATUSES}"]
    report["status"] = status
    missing = [f for f in REPORT_FIELDS if f not in report]
    if missing:
        problems.append("report is missing fields: " + ", ".join(missing))
    return report, source, problems
