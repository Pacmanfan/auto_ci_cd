#!/usr/bin/env python3
"""devsys — operator entry point of the development-control system (ADR-000199).

Every command is a thin, read-mostly front end over the tools it names, so
each automated step keeps a manual equivalent.

    python3 scripts/devsys.py install --target DIR [--force] [--project-name NAME] [--build-system ...] [--git-init]
    python3 scripts/devsys.py init [--project-name NAME] [--build-system make|cmake|qmake|custom|none]
                                   [--git-init] [--force-config]
    python3 scripts/devsys.py doctor [--json]
    python3 scripts/devsys.py status [--json] [--write-mirrors]
    python3 scripts/devsys.py sweep [--apply] [--json]            # scripts/claim.py --sweep
    python3 scripts/devsys.py next [--claim] [--agent-id ID] [--owner-pid PID] [--json]
    python3 scripts/devsys.py verify-structure [--json]           # scripts/verify_development_system.sh
    python3 scripts/devsys.py build configure|build|clean|rebuild|fresh|info|check   # scripts/devsys_build.py
    python3 scripts/devsys.py queue [--json]                     # queued requests, in processing order
    python3 scripts/devsys.py priority <request> CRITICAL|HIGH|NORMAL|LOW
    python3 scripts/devsys.py workers status [--json]
    python3 scripts/devsys.py workers start|stop|enable|disable|recover [ID ...|--all] [--now] [--force]
    python3 scripts/devsys.py workers peek ID [--json]
    python3 scripts/devsys.py workers run ID [--once] [--exit-when-idle]   # the worker process itself
    python3 scripts/devsys.py verify-commit [REV ...] [--json]
    python3 scripts/devsys.py ci [--tier t2] [--base REF] [--no-build] [--audit standard|none]
                                 [--verify-commits HEAD|A..B|none] [--json]

install   copies the framework (FRAMEWORK_PATHS: AGENTS.md, scripts/, tools/, the
          process documents, the framework's ADRs, templates and READMEs) into
          another project's directory — existing files are kept unless --force —
          writes the instance files (devsys.json, the test registry and
          quarantine, the audit manifest, the requirement and decision
          manifests) from scripts/templates/, and runs init there.
init      bootstraps a fresh copy: state directories with their READMEs,
          devsys.json (from the defaults, with the name and build system
          given), the .gitignore entries for runtime state, and optionally
          `git init`. Never overwrites an existing file; with --force-config an
          existing devsys.json gets only the name / build system given.
doctor    environment (Python, git, platform), configuration validity, the
          project settings still NOT CONFIGURED, build tools, test registry.
status    what is happening: workers and their liveness, claims (active,
          stale, dead, recoverable), inbox (and the next eligible request),
          processing, needs-review, work by state, the last successful
          completion and the last failure. --write-mirrors regenerates
          .agent/state/requests.md and work.md from canonical state.
next      the next eligible inbox request (priority, then arrival); --claim
          claims it for the agent (after a report-only sweep).
queue     the unclaimed inbox in the order workers take it (priority, then arrival).
priority  changes an open request's priority (sidecar, header; recorded).
workers   the Worker Orchestrator (scripts/workers/, docs/process/worker-orchestrator.md):
          workers execute requests; the request lifecycle stays authoritative.
verify-commit
          checks from the repository alone that a completed request's commit
          carries exactly the code its gate record verified (the fingerprint
          recomputed from the commit's tree): VERIFIED, WAIVED, NOT_APPLICABLE,
          FAIL, UNKNOWN. Exit 1 on FAIL, 3 on UNKNOWN.
ci        the CI entry point: config, structure, build, the test tier, drift /
          doc integrity / invariants, the architecture audit (--fail-on fail)
          and verify-commit; claims, moves and commits nothing; writes
          <build_dir>/ci-results/<id>.json. Exit 0 PASS, 1 FAIL, 3 UNKNOWN.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_process as proc  # noqa: E402
import devsys_config as cfgmod  # noqa: E402
import devsys_platform as plat  # noqa: E402
import devsys_records as records  # noqa: E402

STATE_READMES = {
    "requests/inbox/README.md": "# Request Inbox\n\nNew requests (scripts/create_request.py). Agents claim them with scripts/claim.py.\n",
    "requests/processing/README.md": "# Requests In Processing\n\nClaimed requests (scripts/claim.py --status shows the owner).\n",
    "requests/completed/README.md": "# Completed Requests\n\nCompleted through scripts/claim.py --complete-request (gate records in evidence/gates/).\n",
    "requests/rejected/README.md": "# Rejected Requests\n\nRejected with a recorded reason.\n",
    "requests/needs-review/README.md": "# Needs Review Requests\n\nRequests waiting for a human decision (docs/process/escalation.md).\n",
    "work/queued/README.md": "# Queued Work\n",
    "work/active/README.md": "# Active Work\n",
    "work/blocked/README.md": "# Blocked Work\n",
    "work/needs-review/README.md": "# Needs Review Work\n",
    "work/verification/README.md": "# Verification Work\n",
    "work/completed/README.md": "# Completed Work\n",
    "work/cancelled/README.md": "# Cancelled Work\n",
}
MANIFEST_HEADERS = {
    "work/work.txt": "# Active Work Manifest\n\nFormat: `WORK-ID | status | title | record | requirements | evidence`\n\n",
    "evidence/evidence.txt": "# Verification Evidence Manifest\n\nFormat: `VER-ID | classification | date | requirements/work | record`\n\n",
    "docs/decisions/decisions.txt": "# Active Decision Manifest\n\nFormat: `ADR-ID | status | title | record | affected requirements/work`\n\n",
}
# What `install` copies into a project: the framework, not this instance's
# records (requests, work, evidence and gate records start empty).
FRAMEWORK_PATHS = [
    "AGENTS.md", "devsys.json", "CI_CD.md", "scripts", "tools", ".audit/manifest.json", ".audit/README.md",
    ".agent/state/README.md", ".agent/state/runs.md",
    "docs/README.md", "docs/process", "docs/verification", "docs/decisions", "docs/architecture/README.md",
    "docs/audits/current-state.md", "docs/history/README.md", "docs/work/README.md",
    "docs/requirements/README.md", "docs/requirements/requirements.txt",
    "docs/requirements/development_operations.md", "docs/requirements/architecture_audit.md",
    "docs/requirements/worker_orchestration.md",
    "requests/README.md", "requests/request-template.md",
    "work/README.md", "work/backlog.md", "work/tasks/README.md", "evidence/README.md",
]
_COPY_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".sandbox")
# Files each instance owns once it exists: written from scripts/templates/,
# never copied from the instance install runs in (scripts/templates/README.md).
INSTANCE_TEMPLATES = {
    "devsys.json": "devsys.json",
    "scripts/test_manifest.json": "test_manifest.json",
    "scripts/test_quarantine.json": "test_quarantine.json",
    ".audit/manifest.json": "audit_manifest.json",
    "docs/requirements/requirements.txt": "requirements.txt",
    "docs/decisions/decisions.txt": "decisions.txt",
}


def _template(source: Path, rel: str) -> Path:
    return source / "scripts" / "templates" / INSTANCE_TEMPLATES[rel]


def _framework_adrs(source: Path) -> set[str]:
    """ADR record files the framework ships: those its decisions template lists."""
    tpl = source / "scripts" / "templates" / "decisions.txt"
    rows = records.manifest_rows(tpl) if tpl.is_file() else []
    return {Path(r[3].strip()).name for r in rows if len(r) > 3 and r[0].strip().startswith("ADR-")}
GITIGNORE_LINES = [".agent/state/claims/", ".agent/state/runtime/", "/.audit/runs/", "/tmp/", "__pycache__/", "*.pyc"]


# ------------------------------------------------------------------ init
def init(root: Path, name: str | None, build_system: str | None, git_init: bool, force_config: bool) -> list[str]:
    done = []
    if git_init and not (root / ".git").exists():
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        done.append("git init")
    for d in cfgmod.DEFAULTS["structure"]["required_dirs"] + ["evidence/gates", ".agent/state/claims",
                                                               ".agent/state/runtime"]:
        p = root / d
        if not p.exists():
            p.mkdir(parents=True)
            done.append(f"created {d}/")
    for rel, text in STATE_READMES.items():
        p = root / rel
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
            done.append(f"created {rel}")
    for rel, text in MANIFEST_HEADERS.items():
        p = root / rel
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
            done.append(f"created {rel}")
    cfg_path = root / cfgmod.CONFIG_FILE
    if cfg_path.exists() and force_config:
        # Keep every other key (and the _examples of the shipped template);
        # set only what was asked for.
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        if name:
            cfg.setdefault("project", {})["name"] = name
        if build_system:
            cfg.setdefault("build", {})["system"] = build_system
        proc.atomic_write_text(cfg_path, json.dumps(cfg, indent=2) + "\n")
        done.append(f"updated {cfgmod.CONFIG_FILE}: " + ", ".join(
            x for x in (name and f"project.name={name}", build_system and f"build.system={build_system}") if x))
    elif not cfg_path.exists() and (Path(__file__).resolve().parent / "templates" / "devsys.json").is_file():
        cfg = json.loads((Path(__file__).resolve().parent / "templates" / "devsys.json").read_text(encoding="utf-8"))
        cfg.setdefault("project", {})["name"] = name or root.name
        if build_system:
            cfg.setdefault("build", {})["system"] = build_system
        cfg_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        done.append(f"created {cfgmod.CONFIG_FILE} (from scripts/templates/devsys.json)")
    elif not cfg_path.exists():
        cfg = {"_comment": "Project configuration of the development-control system; every key not set here "
                           "takes its default (python3 scripts/devsys_config.py prints the effective values; "
                           "docs/process/configuration.md documents them).",
               "schema": cfgmod.SCHEMA_VERSION,
               "project": {"name": name or root.name, "source_roots": []},
               "build": {"system": build_system or "none"},
               "tests": {"selection": "auto"},
               "invariants": []}
        cfg_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        done.append(f"created {cfgmod.CONFIG_FILE}")
    gi = root / ".gitignore"
    existing = gi.read_text(encoding="utf-8").splitlines() if gi.exists() else []
    # Build trees (build results, test run records, caches) and the scratch
    # area are local: evidence copies what must be kept (evidence.py).
    cfg = cfgmod.load(root, refresh=True)
    local = [f"/{Path(cfg['paths'][k]).parts[0]}/" for k in ("build_dir", "scratch")
             if cfg["paths"].get(k) and not Path(cfg["paths"][k]).is_absolute()]
    missing = [l for l in dict.fromkeys(GITIGNORE_LINES + local) if l not in existing]
    if missing:
        with open(gi, "a", encoding="utf-8") as f:
            f.write(("\n" if existing and existing[-1].strip() else "") +
                    "# development-control system runtime state (scripts/devsys.py init)\n" + "\n".join(missing) + "\n")
        done.append(f".gitignore: added {', '.join(missing)}")
    cfgmod.load(root, refresh=True)
    return done


# ------------------------------------------------------------------ install
def install(source: Path, target: Path, force: bool) -> list[str]:
    """Copy the framework from ``source`` into ``target``. Instance files are
    written from their templates, and only the framework's own ADRs are
    copied, so a configured project used as the source does not leak its
    configuration, suites, requirement rows or decisions."""
    target.mkdir(parents=True, exist_ok=True)
    done = []
    adrs = _framework_adrs(source)
    decisions = cfgmod.LAYOUT["decisions"]

    def wanted(rel: str) -> bool:
        if rel in INSTANCE_TEMPLATES:
            return False
        if rel.startswith(decisions + "/ADR-"):
            return Path(rel).name in adrs
        return True

    def put(src: Path, rel: str) -> None:
        dst = target / rel
        if dst.exists() and not force:
            done.append(f"kept existing {rel}")
            return
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        done.append(f"copied {rel}")

    for rel in FRAMEWORK_PATHS:
        src = source / rel
        if not src.exists():
            continue
        files = [src] if src.is_file() else sorted(
            f for f in src.rglob("*") if f.is_file() and "__pycache__" not in f.parts
            and not _COPY_IGNORE(str(f.parent), [f.name]))
        for f in files:
            frel = f.relative_to(source).as_posix()
            if wanted(frel):
                put(f, frel)
    for rel in INSTANCE_TEMPLATES:
        tpl = _template(source, rel)
        if tpl.is_file():
            put(tpl, rel)
    return done


def enclosing_project(target: Path) -> str | None:
    """``paths.project_root`` for a system directory inside an existing git
    work tree it is not the top of (``..`` for ``<project>/<dir>/``), else None."""
    r = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=target, capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    top = Path(r.stdout.strip()).resolve()
    if top == target.resolve():
        return None
    # A directory the enclosing repository ignores (a scratch tree under its
    # tmp/) is not part of that project: its files could never be committed.
    ignored = subprocess.run(["git", "check-ignore", "-q", str(target.resolve())], cwd=top, capture_output=True)
    if ignored.returncode == 0:
        return None
    return Path(os.path.relpath(top, target.resolve())).as_posix()


def set_enclosing_project(target: Path) -> list[str]:
    """Installed into a subdirectory of a project's git checkout: the project
    is the checkout (paths.project_root), unless devsys.json already names one."""
    value = enclosing_project(target)
    cfg_path = target / cfgmod.CONFIG_FILE
    if value is None or not cfg_path.is_file():
        return []
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    current = (cfg.get("paths") or {}).get("project_root")
    if current not in (None, "", "."):
        return [f"kept paths.project_root {current!r}"]
    cfg.setdefault("paths", {})["project_root"] = value
    proc.atomic_write_text(cfg_path, json.dumps(cfg, indent=2) + "\n")
    cfgmod.load(target, refresh=True)
    return [f"paths.project_root = {value!r}: installed as a subdirectory of the project checkout "
            f"{(target / value).resolve()}"]


# ------------------------------------------------------------------ doctor
def _version(cmd) -> str | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        return None


def doctor(root: Path) -> dict:
    report = {"checks": [], "not_configured": []}

    def add(name, level, detail):
        report["checks"].append({"name": name, "level": level, "detail": detail})

    py = sys.version_info
    add("python", "PASS" if py >= (3, 9) else "FAIL", f"{py.major}.{py.minor}.{py.micro} (needs >= 3.9)")
    gv = _version(["git", "--version"])
    m = re.search(r"(\d+)\.(\d+)", gv or "")
    ok_git = bool(m) and (int(m.group(1)), int(m.group(2))) >= (2, 20)
    add("git", "PASS" if ok_git else "FAIL", gv or "git not found (needs >= 2.20)")
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=root, capture_output=True, text=True)
    # The system is the top of its git work tree, or (installed as a project
    # subdirectory, paths.project_root "..") inside the project's work tree.
    checkout = Path(proc.checkout_root(root)).resolve()
    in_repo = top.returncode == 0 and Path(top.stdout.strip()).resolve() == checkout
    where = ("repository root" if checkout == root.resolve() else
             f"project checkout {checkout}, system directory {proc.system_prefix(root)}/")
    add("git repository", "PASS" if in_repo else "FAIL",
        where if in_repo else ("not the top of a git work tree: run `git init` (or devsys.py init --git-init)"
                               if checkout == root.resolve() else
                               f"paths.project_root {cfgmod.get('paths.project_root', root)!r} is not the top "
                               "of the git work tree holding this directory"))
    p = plat.describe()
    add("platform", "PASS" if p["posix"] else "WARN",
        f"{p['platform']}; process verification {'available' if p['process_start_available'] else 'limited'}"
        + ("" if p["posix"] else " (POSIX is the supported platform; see docs/process/portability.md)"))
    if plat.WINDOWS:  # pragma: no cover - exercised only on Windows
        # docs/process/portability.md "Windows and PowerShell" (PORT-004).
        add("utf-8 mode", "PASS" if sys.flags.utf8_mode else "WARN",
            "PYTHONUTF8=1" if sys.flags.utf8_mode else
            "set $env:PYTHONUTF8 = '1': records and git output are UTF-8, the default code page is not")
        crlf = subprocess.run(["git", "config", "--get", "core.autocrlf"], cwd=root, capture_output=True, text=True)
        value = crlf.stdout.strip().lower() or "unset"
        add("git core.autocrlf", "PASS" if value == "false" else "WARN",
            value if value == "false" else
            f"{value}: run `git config core.autocrlf false` (owned-path commits compare blobs byte for byte)")
    problems = cfgmod.validate(root)
    add("devsys.json", "FAIL" if problems else ("PASS" if (root / cfgmod.CONFIG_FILE).exists() else "WARN"),
        "; ".join(problems) if problems else ("valid" if (root / cfgmod.CONFIG_FILE).exists()
                                              else "absent: all defaults (python3 scripts/devsys.py init)"))
    report["not_configured"] = cfgmod.placeholders(root)
    import devsys_build
    s = devsys_build.settings(root)
    if s["system"] == "none":
        add("build", "NOT_CONFIGURED", "build.system is 'none'")
    else:
        missing = devsys_build.check(root, s)
        add("build", "FAIL" if missing else "PASS", "; ".join(missing) if missing else f"{s['system']} tools present")
    try:
        import run_tests
        manifest = run_tests.load_manifest()
        add("test registry", "PASS", f"{len(manifest['suites'])} suite(s), tiers {', '.join(manifest['tiers'])}")
    except Exception as e:  # noqa: BLE001 - reported, not raised
        add("test registry", "FAIL", f"{cfgmod.get('tests.manifest', root)}: {e}")
    structure = verify_structure(root)
    add("structure", "PASS" if not structure["problems"] else "FAIL",
        "scaffold complete" if not structure["problems"] else "; ".join(structure["problems"][:3]))
    levels = {c["level"] for c in report["checks"]}
    report["overall"] = "FAIL" if "FAIL" in levels else ("WARN" if levels & {"WARN", "NOT_CONFIGURED"} or
                                                        report["not_configured"] else "PASS")
    return report


# ------------------------------------------------------------------ structure
def verify_structure(root: Path) -> dict:
    """Tracked scaffold (structure.required_paths / required_dirs), manifest
    targets and a non-empty requirement index. Untracked runtime state is not
    required: it is created on first use (fresh clones pass)."""
    cfg = cfgmod.load(root)
    problems = []
    for rel in cfg["structure"]["required_paths"]:
        if not (root / rel).exists():
            problems.append(f"MISSING {rel}")
    for rel in cfg["structure"]["required_dirs"]:
        if not (root / rel).is_dir():
            problems.append(f"MISSING DIRECTORY {rel}")
    for key, col in (("decisions_manifest", 3), ("work_manifest", 3), ("evidence_manifest", 4)):
        manifest = cfgmod.path(root, key)
        for row in records.manifest_rows(manifest):
            if len(row) <= col or not row[col] or row[col].startswith("pending"):
                continue
            target = row[col].split(",")[0].strip().split()[0]
            if not (root / target).exists() and not (manifest.parent / target).exists():
                problems.append(f"MANIFEST TARGET MISSING {LAYOUT_REL(root, manifest)} -> {target} ({row[0]})")
    req_manifest = cfgmod.path(root, "requirements_manifest")
    if not req_manifest.exists() or not req_manifest.read_text(encoding="utf-8").strip():
        problems.append("EMPTY REQUIREMENTS MANIFEST")
    for argv in cfg["structure"].get("extra_checks", []) or []:
        r = subprocess.run(argv, cwd=root, capture_output=True, text=True)
        if r.returncode != 0:
            problems.append(f"PROJECT CHECK FAILED: {' '.join(argv)}")
    return {"problems": problems}


def LAYOUT_REL(root: Path, p: Path) -> str:  # noqa: N802 - tiny helper
    return p.relative_to(root).as_posix()


# ------------------------------------------------------------------ status
def _requests(root: Path, state: str) -> list[Path]:
    return sorted(p for p in (cfgmod.path(root, "requests") / state).glob("*.md") if p.name != "README.md")


def _priority(md: Path) -> int:
    rank = {"CRITICAL": 0, "HIGH": 1, "NORMAL": 2, "LOW": 3}
    try:
        return rank.get(str(json.loads(md.with_suffix(".json").read_text(encoding="utf-8")).get("priority",
                                                                                                "NORMAL")).upper(), 2)
    except (OSError, ValueError):
        m = re.search(r"\*\*Priority:\*\*\s*(\w+)", md.read_text(encoding="utf-8", errors="ignore"))
        return rank.get(m.group(1).upper(), 2) if m else 2


def _arrival(md: Path) -> tuple:
    """Arrival order: the intake time recorded in the sidecar, then the
    request id's sequence number (the slug in the file name is arbitrary)."""
    try:
        created = str(json.loads(md.with_suffix(".json").read_text(encoding="utf-8")).get("created_utc") or "")
    except (OSError, ValueError):
        created = ""
    m = re.search(r"-(\d{8})-(\d{3,})\.md$", md.name)
    return (created or "~", m.group(1) if m else "~", int(m.group(2)) if m else 0, md.name)


def _queued(root: Path) -> list[Path]:
    inbox = [p for p in _requests(root, "inbox") if proc.read_claim(proc.lock_path(root, p.name)) is None]
    return sorted(inbox, key=lambda p: (_priority(p), _arrival(p)))


def next_eligible(root: Path) -> Path | None:
    """The unclaimed inbox request to process next: highest priority, then
    first to arrive."""
    q = _queued(root)
    return q[0] if q else None


def queue(root: Path) -> list[dict]:
    """The unclaimed inbox in processing order (priority, then arrival): what
    humans see as "queued" and what workers take from."""
    out = []
    for md in _queued(root):
        try:
            side = json.loads(md.with_suffix(".json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            side = {}
        text = md.read_text(encoding="utf-8", errors="ignore")
        first = next((line for line in text.splitlines() if line.strip()), "")
        out.append({"request": md.name, "request_id": side.get("request_id") or md.stem,
                    "priority": str(side.get("priority") or records.header(text, "Priority") or "NORMAL").upper(),
                    "type": str(side.get("type") or records.header(text, "Type") or "").upper(),
                    "title": re.sub(r"^#\s*(Request:\s*)?", "", first).strip()[:160],
                    "created_utc": side.get("created_utc") or ""})
    return out


def _last_events(root: Path) -> dict:
    hist = proc.runtime_dir(root) / "claims-history.jsonl"
    last_ok = last_fail = None
    if hist.exists():
        for line in hist.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("event", "").startswith("released") and e.get("state") == "completed":
                last_ok = {"request": e.get("request_id"), "file": e.get("target"), "at": e.get("at"),
                           "agent_id": e.get("agent_id")}
    for gate in sorted(cfgmod.path(root, "gates").glob("WORK-*.json")):
        try:
            runs = json.loads(gate.read_text(encoding="utf-8")).get("runs", [])
        except (OSError, ValueError):
            continue
        for r in runs:
            if r.get("verdict") == "FAIL" and (last_fail is None or r.get("at", "") > last_fail["at"]):
                last_fail = {"kind": "gate", "work": r.get("work"), "at": r.get("at"), "why": (r.get("fails") or [""])[0]}
    build = root / cfgmod.get("paths.build_dir", root)
    latest = build / "test-results" / "latest.json"
    if latest.exists():
        try:
            rec = json.loads(latest.read_text(encoding="utf-8"))
            if str(rec.get("classification", "")).startswith(("FAIL", "EMPTY", "INTERRUPTED")) and \
                    (last_fail is None or rec.get("started_at", "") > last_fail["at"]):
                last_fail = {"kind": "test run", "run": rec.get("run_id"), "at": rec.get("started_at"),
                             "why": rec.get("classification")}
        except (OSError, ValueError):
            pass
    return {"last_success": last_ok, "last_failure": last_fail}


def status(root: Path) -> dict:
    stale_h = proc.stale_hours(root)
    claims = []
    for c in proc.all_claims(root):
        verdict, why = proc.claim_liveness(c, stale_h)
        claims.append({"kind": c.get("kind"), "target": c.get("target"), "request_id": c.get("request_id"),
                       "agent_id": c.get("agent_id"), "state": c.get("state"), "liveness": verdict, "why": why,
                       "heartbeat_age_min": round(proc.heartbeat_age_hours(c) * 60, 1),
                       "step": (c.get("step") or {}).get("label"), "owned_paths": len(c.get("files") or {})})
    sweep = proc.sweep(root, apply=False)
    work = {s: [p.name[:11] for p in sorted((cfgmod.path(root, "work") / s).glob("WORK-*.md"))]
            for s in cfgmod.WORK_STATES}
    nxt = next_eligible(root)
    return {
        "project": cfgmod.get("project.name", root),
        "workers": sorted({c["agent_id"] for c in claims if c["liveness"] in (proc.ALIVE, proc.ACTIVE)}),
        "claims": claims,
        "stale_or_dead": [c for c in claims if c["liveness"] in (proc.STALE_V, proc.DEAD)],
        "recoverable": [f for f in sweep["findings"] if f.get("recoverable")],
        "inconsistencies": [f for f in sweep["findings"] if not f.get("recoverable")],
        "requests": {s: len(_requests(root, s)) for s in cfgmod.REQUEST_STATES},
        "inbox": [p.name for p in _requests(root, "inbox")],
        "next_eligible": nxt.name if nxt else None,
        "needs_review": [p.name for p in _requests(root, "needs-review")],
        "work": {s: v for s, v in work.items() if v},
        "blocked_work": work.get("blocked", []),
        **_last_events(root),
    }


def write_mirrors(root: Path, st: dict) -> list[Path]:
    now = proc.now_iso()
    req = cfgmod.path(root, "state") / "requests.md"
    wrk = cfgmod.path(root, "state") / "work.md"
    req.write_text(
        "# Request Queue State\n\n"
        "<!-- generated by scripts/devsys.py status --write-mirrors; canonical state: requests/, "
        ".agent/state/claims/ -->\n\n"
        f"updated: {now}\n"
        f"inbox_count: {st['requests']['inbox']}\n"
        f"processing_count: {st['requests']['processing']}\n"
        f"needs_review_count: {st['requests']['needs-review']}\n"
        f"completed_count: {st['requests']['completed']}\n"
        f"rejected_count: {st['requests']['rejected']}\n"
        f"next_eligible: {st['next_eligible'] or 'none'}\n"
        f"workers: {', '.join(st['workers']) or 'none'}\n"
        f"stale_or_dead_claims: {len(st['stale_or_dead'])}\n"
        f"last_success: {json.dumps(st['last_success'])}\n"
        f"last_failure: {json.dumps(st['last_failure'])}\n", encoding="utf-8")
    wrk.write_text(
        "# Work Queue State\n\n"
        "<!-- generated by scripts/devsys.py status --write-mirrors; canonical state: work/, work/work.txt -->\n\n"
        f"updated: {now}\n"
        + "".join(f"{s}: {', '.join(st['work'].get(s, [])) or 'none'}\n" for s in cfgmod.WORK_STATES)
        + "completion_gate: requirements -> implementation -> test -> evidence -> documentation -> audit\n",
        encoding="utf-8")
    return [req, wrk]


def print_status(st: dict) -> None:
    print(f"PROJECT {st['project']}")
    print(f"workers: {', '.join(st['workers']) or 'none'}")
    print("requests: " + ", ".join(f"{k} {v}" for k, v in st["requests"].items()))
    print(f"next eligible: {st['next_eligible'] or 'none'}")
    for c in st["claims"]:
        step = f" step={c['step']}" if c.get("step") else ""
        print(f"  {c['liveness']:7} {c.get('kind')} {c.get('request_id') or c.get('target')} agent={c['agent_id']} "
              f"state={c['state']} heartbeat={c['heartbeat_age_min']}min{step} ({c['why']})")
    if st["recoverable"]:
        print("recoverable:")
        for f in st["recoverable"]:
            print(f"  {f['target']}: {f['issue']}")
    if st["inconsistencies"]:
        print("inconsistencies (python3 scripts/devsys.py sweep):")
        for f in st["inconsistencies"]:
            print(f"  {f['target']}: {f['issue']}")
    if st["needs_review"]:
        print(f"needs review: {', '.join(st['needs_review'])}")
    print("work: " + (", ".join(f"{s} {len(v)}" for s, v in st["work"].items()) or "none"))
    if st["blocked_work"]:
        print(f"blocked work: {', '.join(st['blocked_work'])}")
    ok, bad = st["last_success"], st["last_failure"]
    print("last success: " + (f"{ok['request']} completed and released at {ok['at']} by {ok['agent_id']}"
                              if ok else "none recorded"))
    if not bad:
        print("last failure: none recorded")
    elif bad["kind"] == "gate":
        print(f"last failure: gate {bad['work']} FAIL at {bad['at']}: {bad['why']}")
    else:
        print(f"last failure: test run {bad['run']} {bad['why']} at {bad['at']}")


# ------------------------------------------------------------------ main
# ------------------------------------------------------------------ verify-commit
def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True)


def verify_commit(root: Path, rev: str = "HEAD") -> dict:
    """Does commit ``rev`` carry the code its gate verified? From the
    repository alone: the commit's trailers name the work items and gate
    records; the gate record (as committed) names the fingerprint and the code
    paths; the fingerprint is recomputed from the commit's tree.

    VERIFIED        every linked gate record is PASS on exactly this code
    WAIVED          completion was waived by a human (recorded reason)
    NOT_APPLICABLE  not a completed request's commit (rejection, parking,
                    a commit without request trailers)
    FAIL            a gate record is missing, not PASS, or verified other code
    UNKNOWN         the gate record predates code-path recording"""
    sha = _git(root, "rev-parse", "--verify", f"{rev}^{{commit}}").stdout.strip()
    if not sha:
        return {"commit": rev, "status": "FAIL", "detail": f"no commit {rev}"}
    body = _git(root, "log", "-1", "--format=%B", sha).stdout
    trailers: dict[str, list[str]] = {}
    for line in body.splitlines():
        m = re.match(r"^([A-Z][A-Za-z-]+): (.+)$", line)
        if m:
            trailers.setdefault(m.group(1), []).append(m.group(2).strip())
    out = {"commit": sha, "request_id": (trailers.get("Request-Id") or [None])[0], "works": []}
    name = (trailers.get("Request-File") or [None])[0]
    if not name:
        return dict(out, status="NOT_APPLICABLE", detail="no Request-File trailer")
    req_dir = proc.repo_path(root, cfgmod.LAYOUT["requests"])     # commit paths are repository paths
    state = next((s for s in cfgmod.REQUEST_STATES
                  if _git(root, "cat-file", "-e", f"{sha}:{req_dir}/{s}/{name}").returncode == 0), None)
    out["request_state"] = state
    live = None
    if state in (None, "inbox", "processing") and trailers.get("Gate-Record"):
        # A request-branch commit (workers.isolation "worktree", claim.py
        # --create-worktree): the request moves in the main checkout, never on
        # the branch, so the tree cannot say whether it completed. The
        # lifecycle's own record can: when the request is completed there, the
        # commit is its completion commit and its gate binding is verified.
        locs = proc.locate_request(root, name)
        if locs and locs[0][0] == "completed":
            live = locs[0][1]
            out["request_state"] = "completed"
            out["request_state_source"] = "main checkout (request branch commit)"
    if state != "completed" and live is None:
        return dict(out, status="NOT_APPLICABLE", detail=f"request is {state or 'not in the commit'}")
    if live is not None:
        side = subprocess.CompletedProcess([], 0 if live.with_suffix(".json").is_file() else 1,
                                           live.with_suffix(".json").read_text(encoding="utf-8")
                                           if live.with_suffix(".json").is_file() else "", "")
    else:
        side = _git(root, "show", f"{sha}:{req_dir}/completed/{Path(name).with_suffix('.json')}")
    try:
        resolution = json.loads(side.stdout).get("resolution") or {} if side.returncode == 0 else {}
    except ValueError:
        resolution = {}
    if resolution.get("gate_waiver"):
        return dict(out, status="WAIVED", detail=f"gate waived: {resolution['gate_waiver']}")
    if resolution.get("gate"):
        return dict(out, status="WAIVED", detail=str(resolution["gate"]))
    gates = {g.split()[0] for g in trailers.get("Gate-Record", [])}
    works = trailers.get("Work-Id", [])
    if not works:
        return dict(out, status="FAIL", detail="completed request commit without Work-Id trailers")
    statuses = []
    for wid in works:
        path = next((g for g in gates if Path(g).stem == wid), proc.repo_path(root, f"{cfgmod.LAYOUT['gates']}/{wid}.json"))
        shown = _git(root, "show", f"{sha}:{path}")
        row = {"work": wid, "gate_record": path}
        if shown.returncode != 0:
            row.update(status="FAIL", detail="gate record not in the commit")
        else:
            try:
                entry = (json.loads(shown.stdout).get("runs") or [{}])[-1]
            except ValueError:
                entry = {}
            if entry.get("verdict") != "PASS":
                row.update(status="FAIL", detail=f"latest gate verdict {entry.get('verdict')}")
            elif "request_code_paths" not in entry:
                row.update(status="UNKNOWN", detail="gate record has no code paths (recorded before gate version 3)")
            else:
                actual = proc.tree_fingerprint(root, sha, entry["request_code_paths"])
                ok = actual == entry.get("request_fingerprint")
                row.update(status="VERIFIED" if ok else "FAIL",
                           detail=(f"{len(entry['request_code_paths'])} code path(s) as gated" if ok else
                                   "the commit's code differs from the code the gate verified"))
        statuses.append(row["status"])
        out["works"].append(row)
    worst = "FAIL" if "FAIL" in statuses else ("UNKNOWN" if "UNKNOWN" in statuses else "VERIFIED")
    return dict(out, status=worst, detail="; ".join(f"{r['work']} {r['status']}: {r['detail']}" for r in out["works"]))


# ------------------------------------------------------------------ ci
def ci(root: Path, tier: str, base: str | None, build: bool, audit: str, commits: str | None) -> dict:
    """One non-interactive verification of the checkout, for a CI job or a
    human: configuration, structure, build, the test tier, the consistency
    audits, the architecture audit and the gate binding of the commits.
    Read-only towards records: it claims, moves and commits nothing (CI_CD_ROADMAP
    §9.4). Each step is PASS, FAIL, SKIPPED (not configured / not asked) or
    UNKNOWN (could not be established); the record is written to
    <build_dir>/ci-results/."""
    import time
    steps = []

    def step(name, status, detail="", **extra):
        steps.append(dict({"step": name, "status": status, "detail": detail}, **extra))

    def run(cmd, timeout=None):
        try:
            r = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True, timeout=timeout)
            return r.returncode, (r.stdout + r.stderr).strip()
        except subprocess.TimeoutExpired:
            return None, f"did not finish within {timeout}s"

    started = time.monotonic()
    problems = cfgmod.validate(root)
    step("config", "FAIL" if problems else "PASS", "; ".join(problems[:3]) or "valid")
    rep = verify_structure(root)
    step("structure", "FAIL" if rep["problems"] else "PASS", "; ".join(rep["problems"][:3]) or "scaffold complete")
    if not build:
        step("build", "SKIPPED", "--no-build")
    else:
        import devsys_build
        rec = devsys_build.run(root, "build")
        status = {"PASS": "PASS", "FAIL": "FAIL", "NOT_CONFIGURED": "SKIPPED"}.get(rec["status"], "UNKNOWN")
        step("build", status, rec.get("detail") or rec.get("log") or "", failure_class=rec.get("failure_class"))
    scope = ["--base", base] if base else ["--all"]
    rc, text = run([sys.executable, "scripts/run_tests.py", "--tier", tier, *scope])
    run_id = next((Path(l.split()[1]).stem for l in text.splitlines() if l.startswith("record: ")), None)
    status = {0: "PASS", 1: "FAIL"}.get(rc, "UNKNOWN")
    klass = next((l.split("classification:")[1].split()[0] for l in text.splitlines() if "classification:" in l), "")
    step(f"tests {tier}", status, klass or text.splitlines()[-1] if text else f"exit {rc}", run_id=run_id, exit=rc)
    for tool in ("drift_detect.py", "doc_integrity.py", "invariant_check.py"):
        rc, text = run([sys.executable, f"scripts/{tool}"], timeout=600)
        last = text.splitlines()[-1] if text else ""
        status = "UNKNOWN" if rc is None else ("FAIL" if rc else ("SKIPPED" if "NOT CONFIGURED" in text else "PASS"))
        step(tool[:-3], status, last)
    if audit == "none" or not (root / "tools" / "audit").is_file():
        step("architecture audit", "SKIPPED", "not asked" if audit == "none" else "tools/audit not installed")
    else:
        rc, text = run([sys.executable, "tools/audit", audit, "--fail-on", "fail"], timeout=3600)
        line = next((l.strip() for l in text.splitlines() if l.startswith("AUDIT-")), text.splitlines()[-1] if text else "")
        step("architecture audit", "UNKNOWN" if rc is None else ("FAIL" if rc else "PASS"), line)
    if commits:
        revs = _git(root, "rev-list", "--reverse", commits if ".." in commits else f"{commits}^!").stdout.split()
        for sha in revs:
            v = verify_commit(root, sha)
            status = {"VERIFIED": "PASS", "WAIVED": "PASS", "NOT_APPLICABLE": "SKIPPED"}.get(v["status"], v["status"])
            step(f"commit {sha[:10]}", status, f"{v['status']}: {v.get('detail', '')}")
    statuses = {s["status"] for s in steps}
    overall = "FAIL" if "FAIL" in statuses else ("UNKNOWN" if "UNKNOWN" in statuses else "PASS")
    record = {"id": f"{time.strftime('%Y%m%dT%H%M%S')}-{os.getpid()}", "at": proc.now_iso(), "tier": tier,
              "base": base, "head": _git(root, "rev-parse", "HEAD").stdout.strip() or None,
              "dirty": bool(_git(root, "status", "--porcelain").stdout.strip()), "host": plat.hostname(),
              "seconds": round(time.monotonic() - started, 1), "overall": overall, "steps": steps}
    out = root / cfgmod.get("paths.build_dir", root) / "ci-results"
    out.mkdir(parents=True, exist_ok=True)
    text = json.dumps(record, indent=1)
    (out / f"{record['id']}.json").write_text(text, encoding="utf-8")
    (out / "latest.json").write_text(text, encoding="utf-8")
    record["path"] = str(out / f"{record['id']}.json")
    return record


def workers_cli(root: Path, a) -> int:
    from workers import pool, runner, state as wstate
    if a.wcmd == "run":
        try:
            reason = runner.run(root, a.id, once=a.once, exit_when_idle=a.exit_when_idle)
        except runner.AlreadyRunning as e:
            print(str(e), file=sys.stderr)
            return proc.CONFLICT
        print(f"WORKER {a.id} stopped: {reason}")
        return 0
    if a.wcmd == "status":
        st = pool.status(root)
        if a.json:
            print(json.dumps(st, indent=2, default=str))
            return 0
        print(f"workers: {st['running']} running of max {st['max_workers']} (isolation {st['isolation']}; agent "
              f"{'configured' if st['agent_configured'] else 'NOT CONFIGURED'})")
        for w in st["workers"]:
            act = w["active"]
            print(f"  {w['id']:10} {'enabled ' if w['enabled'] else 'disabled'} {w['status']:8} "
                  f"{w['process']['verdict']:7} " + (f"{act['request_id']} [{act['priority']}] {act['title'][:50]}"
                                                   if act else (w.get("status_reason") or "")[:70]))
        print(f"queued {len(st['queue'])}, working {len(st['working'])}, finished (recent) {len(st['finished'])}")
        for q in st["queue"][:15]:
            print(f"  queued  {q['priority']:8} {q['request_id']:24} {q['title'][:60]}")
        return 0
    if a.wcmd == "peek":
        v = pool.peek(root, a.id, lines=a.lines)
        if a.json:
            print(json.dumps(v, indent=2, default=str))
            return 0
        print(f"{v['id']}: {v['status']} ({v.get('status_reason')}); process {v['process']['verdict']}: "
              f"{v['process']['reason']}")
        if v["active"]:
            act = v["active"]
            print(f"active: {act['request_id']} [{act['priority']}] {act['title']}")
            print(f"  attempt {act['attempt_id']} in {act['cwd']}, session {act['session']['verdict']}: "
                  f"{act['session']['reason']}")
        if v.get("attempt"):
            at = v["attempt"]
            print(f"attempt {at['attempt_id']}: {at['status']} {at.get('result') or ''} {at.get('result_reason') or ''}")
        for line in v["transcript_tail"]:
            print(f"  | {line}")
        return 0
    ids = list(a.ids)
    if a.all or not ids:
        ids = wstate.slot_ids(root)
    rc = 0
    for wid in ids:
        try:
            if a.wcmd == "start":
                out = pool.start(root, wid)
            elif a.wcmd == "stop":
                out = pool.stop(root, wid, now=a.now, wait_seconds=a.wait)
            elif a.wcmd == "enable":
                out = {"worker": wid, "enabled": pool.enable(root, wid)["enabled"]}
            elif a.wcmd == "disable":
                out = {"worker": wid, "enabled": pool.disable(root, wid)["enabled"]}
            else:
                out = pool.recover(root, wid, force=a.force)
        except proc.ProcessError as e:
            out = {"worker": wid, "error": e.message}
            rc = rc or e.code
        print(json.dumps(out) if a.json else f"{a.wcmd.upper()} {wid}: " + ", ".join(
            f"{k}={v}" for k, v in out.items() if k != "worker"))
    return rc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    ins = sub.add_parser("install")
    ins.add_argument("--target", required=True)
    ins.add_argument("--force", action="store_true")
    ins.add_argument("--project-name")
    ins.add_argument("--build-system", choices=("none", "make", "cmake", "qmake", "custom"))
    ins.add_argument("--git-init", action="store_true")
    i = sub.add_parser("init")
    i.add_argument("--project-name")
    i.add_argument("--build-system", choices=("none", "make", "cmake", "qmake", "custom"))
    i.add_argument("--git-init", action="store_true")
    i.add_argument("--force-config", action="store_true")
    for name in ("doctor", "status", "sweep", "next", "verify-structure"):
        sp = sub.add_parser(name)
        sp.add_argument("--json", action="store_true")
        if name == "status":
            sp.add_argument("--write-mirrors", action="store_true")
        if name == "sweep":
            sp.add_argument("--apply", action="store_true")
        if name == "next":
            sp.add_argument("--claim", action="store_true")
            sp.add_argument("--agent-id", default=os.environ.get("AGENT_ID"))
            sp.add_argument("--owner-pid", type=int, default=None)
    b = sub.add_parser("build")
    b.add_argument("rest", nargs=argparse.REMAINDER)
    qp = sub.add_parser("queue")
    qp.add_argument("--json", action="store_true")
    pp = sub.add_parser("priority")
    pp.add_argument("request")
    pp.add_argument("level", type=str.upper, choices=proc.PRIORITIES)
    wp = sub.add_parser("workers")
    wsub = wp.add_subparsers(dest="wcmd", required=True)
    wsub.add_parser("status").add_argument("--json", action="store_true")
    for n in ("start", "stop", "enable", "disable", "recover"):
        sp = wsub.add_parser(n)
        sp.add_argument("ids", nargs="*")
        sp.add_argument("--all", action="store_true")
        sp.add_argument("--json", action="store_true")
        if n == "stop":
            sp.add_argument("--now", action="store_true", help="end the running session at once (INTERRUPTED)")
            sp.add_argument("--wait", type=float, default=0.0, help="seconds to wait for the worker to stop")
        if n == "recover":
            sp.add_argument("--force", action="store_true")
    pk = wsub.add_parser("peek")
    pk.add_argument("id")
    pk.add_argument("--json", action="store_true")
    pk.add_argument("--lines", type=int, default=25)
    rn = wsub.add_parser("run")
    rn.add_argument("id")
    rn.add_argument("--once", action="store_true")
    rn.add_argument("--exit-when-idle", action="store_true")
    v = sub.add_parser("verify-commit")
    v.add_argument("revs", nargs="*", default=["HEAD"])
    v.add_argument("--json", action="store_true")
    c = sub.add_parser("ci")
    c.add_argument("--tier", default="t2")
    c.add_argument("--base", help="test only what changed since this ref (default: every suite of the tier)")
    c.add_argument("--no-build", action="store_true")
    c.add_argument("--audit", default="standard", help="architecture audit profile, or 'none'")
    c.add_argument("--verify-commits", default="HEAD", help="a commit or A..B range whose gate binding is "
                   "checked; 'none' to skip")
    c.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    root = proc.repo_root()
    try:
        if a.cmd == "install":
            target = Path(a.target).resolve()
            copied = install(Path(__file__).resolve().parent.parent, target, a.force)
            print(f"INSTALL {len([c for c in copied if c.startswith('copied')])} file(s) copied into {target}")
            if not a.git_init:
                for line in set_enclosing_project(target):
                    print(f"INSTALL {line}")
            for line in init(target, a.project_name, a.build_system, a.git_init, force_config=bool(
                    a.project_name or a.build_system)) or []:
                print(f"INIT {line}")
            print(f"next: cd {target} && python3 scripts/devsys.py doctor")
            return 0
        if a.cmd == "init":
            for line in init(root, a.project_name, a.build_system, a.git_init, a.force_config) or ["nothing to do"]:
                print(f"INIT {line}")
            print("next: python3 scripts/devsys.py doctor")
            return 0
        if a.cmd == "doctor":
            rep = doctor(root)
            if a.json:
                print(json.dumps(rep, indent=2))
            else:
                for c in rep["checks"]:
                    print(f"  {c['level']:14} {c['name']}: {c['detail']}")
                for n in rep["not_configured"]:
                    print(f"  NOT CONFIGURED  {n}")
                print(f"DOCTOR: {rep['overall']}")
            return 1 if rep["overall"] == "FAIL" else 0
        if a.cmd == "status":
            st = status(root)
            if a.write_mirrors:
                for p in write_mirrors(root, st):
                    print(f"WROTE {p.relative_to(root)}", file=sys.stderr)
            print(json.dumps(st, indent=2) if a.json else "", end="")
            if not a.json:
                print_status(st)
            return 0
        if a.cmd == "sweep":
            rep = proc.sweep(root, apply=a.apply)
            if a.json:
                print(json.dumps(rep, indent=2))
            else:
                for f in rep["findings"]:
                    print(f"FINDING {f['target']}: {f['issue']}")
                for x in rep["actions"]:
                    print(f"APPLIED {x['action']} {x['target']}")
                print(f"{len(rep['findings'])} finding(s), {len(rep['actions'])} repair(s) applied"
                      + ("" if a.apply else " (report only; --apply performs the safe repairs)"))
            return 0
        if a.cmd == "next":
            nxt = next_eligible(root)
            out = {"next_eligible": nxt.name if nxt else None, "claimed": False}
            if nxt and a.claim:
                data, how = proc.claim_request(root, nxt.name, a.agent_id, owner_pid=a.owner_pid)
                out.update(claimed=True, how=how, request_path=data.get("request_path"), request_id=data.get("request_id"))
            print(json.dumps(out) if a.json else (f"NEXT {out['next_eligible'] or 'none'}"
                                                   + (f" CLAIMED {out.get('request_path')}" if out["claimed"] else "")))
            return 0
        if a.cmd == "verify-structure":
            rep = verify_structure(root)
            if a.json:
                print(json.dumps(rep, indent=2))
            else:
                for p in rep["problems"]:
                    print(p, file=sys.stderr)
            return 1 if rep["problems"] else 0
        if a.cmd == "build":
            import devsys_build
            return devsys_build.main(a.rest)
        if a.cmd == "queue":
            items = queue(root)
            if a.json:
                print(json.dumps(items, indent=2))
            else:
                for i in items:
                    print(f"{i['priority']:8} {i['request_id']:24} {i['type']:14} {i['title'][:70]}")
                print(f"{len(items)} queued request(s)")
            return 0
        if a.cmd == "priority":
            r = proc.set_priority(root, a.request, a.level, by=os.environ.get("AGENT_ID", ""))
            print(f"PRIORITY {r['request']}: {r['from']} -> {r['to']} ({r['state']})")
            return 0
        if a.cmd == "workers":
            return workers_cli(root, a)
        if a.cmd == "verify-commit":
            results = [verify_commit(root, r) for r in a.revs]
            if a.json:
                print(json.dumps(results, indent=2))
            else:
                for r in results:
                    print(f"{r['status']:15} {r['commit'][:10]} {r.get('request_id') or '-'}  {r.get('detail', '')}")
            return 1 if any(r["status"] == "FAIL" for r in results) else \
                (3 if any(r["status"] == "UNKNOWN" for r in results) else 0)
        if a.cmd == "ci":
            rec = ci(root, a.tier, a.base, not a.no_build, a.audit,
                     None if a.verify_commits == "none" else a.verify_commits)
            if a.json:
                print(json.dumps(rec, indent=2))
            else:
                for s in rec["steps"]:
                    print(f"  {s['status']:8} {s['step']:22} {s['detail']}")
                print(f"CI: {rec['overall']}  ({rec['seconds']} s, record {rec['path']})")
            return {"PASS": 0, "FAIL": 1}.get(rec["overall"], 3)
    except proc.ProcessError as e:
        print(e.message, file=sys.stderr)
        return e.code
    return proc.INVALID


if __name__ == "__main__":
    sys.exit(main())
