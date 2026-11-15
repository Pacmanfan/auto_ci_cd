#!/usr/bin/env python3
"""Project configuration and framework constants of the development-control
system (ADR-000199).

Two kinds of settings live here:

* **Framework layout and state machine** (``LAYOUT``, ``REQUEST_STATES``,
  ``WORK_STATES``, ``WORK_TRANSITIONS``, ...): the core structure every
  installation shares. They are defined once, here, and every tool imports
  them instead of repeating literals.
* **Project configuration**: what legitimately varies between projects
  (project name, build system and commands, test registry, source roots,
  invariants, protected files, completion policy, crash artifacts ...). It is
  read from ``devsys.json`` at the repository root and deep-merged over
  ``DEFAULTS``; a missing file means "all defaults", so a fresh copy and the
  synthetic repositories the tests build work without one.

The repository root is ``$AGENT_PROCESS_ROOT`` when set (tests), else the
parent of the ``scripts/`` directory. Every tool resolves it through
``repo_root()``.

Command line (read-only):

    python3 scripts/devsys_config.py                 # effective configuration (JSON)
    python3 scripts/devsys_config.py --get build.system
    python3 scripts/devsys_config.py --validate      # problems, exit 1 when any
"""
from __future__ import annotations

import copy
import json
import re
import os
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
CONFIG_FILE = "devsys.json"
SCHEMA_VERSION = 1

# ------------------------------------------------------------------ framework layout
# Core structure shared by every installation (not project configuration).
LAYOUT = {
    "requests": "requests",
    "work": "work",
    "evidence": "evidence",
    "gates": "evidence/gates",
    "requirements": "docs/requirements",
    "decisions": "docs/decisions",
    "process": "docs/process",
    "history": "docs/history",
    "state": ".agent/state",
    "work_manifest": "work/work.txt",
    "evidence_manifest": "evidence/evidence.txt",
    "decisions_manifest": "docs/decisions/decisions.txt",
    "requirements_manifest": "docs/requirements/requirements.txt",
    "collision_declarations": "docs/process/historical-id-collisions.md",
}

REQUEST_STATES = ("inbox", "processing", "completed", "rejected", "needs-review")
REQUEST_OPEN_STATES = ("inbox", "processing", "needs-review")
REQUEST_CLOSED_STATES = ("completed", "rejected")
# Transitions performed by scripts/claim.py (docs/process/state-model.md §1).
REQUEST_TRANSITIONS = {
    "inbox": ("processing",),
    "processing": ("completed", "rejected", "needs-review", "inbox"),
    "needs-review": ("processing", "rejected", "completed"),
    "completed": (),
    "rejected": (),
}

WORK_STATES = ("queued", "active", "blocked", "needs-review", "verification", "completed", "cancelled")
WORK_OPEN_STATES = ("queued", "active", "blocked", "needs-review")
# docs/process/state-model.md §2 (scripts/work.py enforces it).
WORK_TRANSITIONS = {
    "queued": ("active", "cancelled"),
    "active": ("blocked", "needs-review", "verification", "completed", "cancelled"),
    "blocked": ("active", "needs-review", "cancelled"),
    "needs-review": ("active", "blocked", "cancelled"),
    "verification": ("completed", "active", "blocked"),
    "completed": (),
    "cancelled": (),
}

VERIFICATION_STATES = ("pending", "pass", "fail", "blocked", "pre-existing")

# Exit / error codes shared by the CLIs (scripts/agent_process.py re-exports them).
OK = 0
CONFLICT = 1        # another agent owns it (active)
STALE = 2           # owner looks stale / orphaned: explicit recovery required
NOT_OWNER = 3       # caller does not own the request or path
BAD_STATE = 4       # request/work is not in a state that allows the operation
NOT_FOUND = 5
INVALID = 6
COLLISION = 7       # id / file / duplicate collision at creation time: nothing written
BUSY = 8            # an inter-process lock could not be taken in time (retry)
GATE_REQUIRED = 9   # a transition needs a passing completion gate record
NOT_CONFIGURED = 10  # a project setting the operation needs is not configured

# ------------------------------------------------------------------ project defaults
DEFAULTS: dict = {
    "schema": SCHEMA_VERSION,
    "project": {
        "name": "<PROJECT>",
        # Repository-relative directories holding the project's own sources:
        # the files an architecture rule applies to when it names none, and
        # what the doctor checks exist; empty = not configured.
        "source_roots": [],
    },
    "paths": {
        "scratch": "tmp",               # AGENTS.md "Temporary Work and Staging"
        # The project this development system serves, relative to the system
        # directory. "." when the system is the project root (this layout);
        # ".." when it is installed as <project>/<system-directory>/
        # (docs/process/configuration.md "Project-subdirectory installation").
        "project_root": ".",
        "build_dir": "build/debug",     # default shadow build tree
        "crash_dumps": "log/dump",
    },
    "claims": {
        "stale_hours": 2.0,             # heartbeat age after which an unverified claim is STALE
        "lock_timeout_seconds": 60.0,
        # A claim that records no owner process is verified by heartbeat only.
        "heartbeat_interval_seconds": 300,
        # A verified-gone owner process is DEAD only once the claim has also
        # been silent this long (a session restarting its tool keeps its claim).
        "dead_grace_minutes": 5.0,
    },
    "ids": {
        "request_sources": {"chat": "REQ-CHAT", "inbox": "REQ-IN", "crash": "REQ-CRASH", "automation": "REQ-AUTO"},
        # Also count ids that exist only in other git refs (fetched branches),
        # so two clones that fetched each other never reissue a committed id.
        "scan_git_refs": True,
    },
    "intake": {
        # Refuse a submission whose normalised text equals an open request's.
        "refuse_duplicates": True,
    },
    "protected_artifacts": [
        "AGENTS.md",
        "devsys.json",
        "docs/requirements/development_operations.md",
        "docs/process/state-model.md",
        "docs/process/process-protection.md",
        "docs/process/escalation.md",
        "docs/process/autonomous-operating-model.md",
    ],
    "completion": {
        # scripts/claim.py --complete-request needs a passing, current gate
        # record of every work item linked to the request.
        "enforce_on_transition": True,
        # Request types that may be completed with a recorded reason instead
        # of a work item and a gate record.
        "exempt_types": ["INVESTIGATION"],
        # The lowest tier a linked test run must include when the request
        # changed code (documentation / process records need no run).
        "min_tier": "t2",
        "passing_evidence_statuses": ["pass", "pre-existing"],
        "require_requirement": True,
        "require_request_link": True,
        "require_decision_records": True,
        # A request that changed code needs a linked, passing test run of
        # min_tier on its current code.
        "require_test_run": True,
        "require_drift_clean": True,
        "require_doc_integrity_clean": True,
        # Optional: an audit profile whose run must show no FAIL (None = off).
        "require_audit_profile": None,
    },
    "build": {
        # none | make | cmake | qmake | custom  (scripts/devsys_build.py)
        "system": "none",
        "source_dir": ".",
        "build_dir": None,              # None: paths.build_dir
        "jobs": None,                   # None: CPU count
        "configure": None,              # custom command lists (argv), {build_dir} {source_dir} {jobs} substituted
        "build": None,
        "clean": None,
        "timeout_seconds": 3600,
        # Generated code checked for staleness by scripts/build_health.py:
        # [{"sources": ["proto/*.proto"], "outputs": ["gen/*.pb.h"]}]
        "generated": [],
    },
    "tests": {
        "manifest": "scripts/test_manifest.json",
        "quarantine": "scripts/test_quarantine.json",
        # auto: qmake graph when the root project exists, else suite path globs
        "selection": "auto",
        "root_project": "project.pro",
        "global_build_files": ["project.pro", "warnings.pri", "version.pri", ".qmake.conf"],
        "no_test_patterns": [
            "docs/*", "*.md", "requests/*", "work/*", "evidence/*", ".agent/*", "LICENSE", "readme.md",
            "*.pro.user", "*.pro.user.*", ".gitignore", ".vscode/*",
            "*/__pycache__/*", "*.pyc",
        ],
        "reject_qt5_makefiles": False,
        "scrubbed_env": [],
        # requirement kind -> environment variable that opts in
        "requirement_env": {"testdata": "PROJECT_TEST_DATA", "hardware": "PROJECT_LIVE_HW_ARGS"},
    },
    "doc_integrity": {
        # Finding codes that FAIL (the rest WARN).
        "fail_codes": ["BROKEN_LINK"],
        # Former documentation locations current docs must not cite.
        "stale_paths": ["docs/architecture/desired", "docs/architecture/actual"],
        # Removed technologies current docs must not present as current.
        "obsolete_terms": [],
    },
    # Declarative architecture rules (scripts/invariant_check.py, audit
    # builtin "architecture_rules"): see docs/process/configuration.md.
    "invariants": [],
    "preflight": {"subsystem_keywords": []},
    "requirement_impact": {
        "domains": {"development": ["work", "evidence", "requests", "AGENTS.md", "state-model"]},
    },
    "crash": {
        "core_glob": "*.core",
        "fallback_dir": None,
        "logs": ["log/app.log"],
        "debug_binary": None,
    },
    # Worker Orchestrator (scripts/workers/, docs/process/worker-orchestrator.md):
    # workers execute framework requests; the request lifecycle stays the
    # single source of truth.
    "workers": {
        "max_workers": 2,               # worker slots worker-01 .. worker-NN
        # shared: every worker in the project checkout (claims and owned-path
        # commits keep them apart); worktree: each request in its own git
        # worktree (claim.py --create-worktree; publishing is manual, COR-14).
        "isolation": "shared",
        "poll_seconds": 5.0,            # how often an idle worker looks for a request
        "attempt": {
            "max_runtime_minutes": 180,
            "max_attempts_per_request": 2,
            "kill_grace_seconds": 30,
            "max_consecutive_failures": 3,
            "max_rate_limit_wait_minutes": 720,
        },
        "selection": {"exclude_types": ["PROCESS-CHANGE"]},
        "agent": {
            # argv of the coding-agent CLI a worker runs, reading the prompt on
            # stdin and writing JSON-lines events to stdout. Placeholders:
            # {session_id} {name} {attempt_dir}. Empty: workers are NOT
            # CONFIGURED (docs/process/agent-profiles.md).
            "command": [],
            "env_scrub": [],            # variables removed from the agent's environment
            "rate_limit_patterns": ["usage limit", "rate limit", "limit reached"],
            "prompt": "scripts/workers/prompts/worker.md",
        },
        # Mixed fleets: named profiles override workers.agent key by key
        # ({"<name>": {"command": [...], "env_scrub": [...]}}), and assign
        # maps a worker slot to a profile ({"worker-02": "<name>"}); a slot
        # without an assignment runs workers.agent (profile "default").
        "profiles": {},
        "assign": {},
        "validation": [
            {"name": "git-sanity", "builtin": "git_sanity"},
            {"name": "request-state", "builtin": "request_state"},
            {"name": "commit-binding", "builtin": "commit_binding"},
        ],
    },
    "drift": {
        # Level of a request / work item completed without the tooling's
        # verification (no resolution in the sidecar, no PASS gate record):
        # FAIL, WARN, or OFF (e.g. while adopting a repository with history).
        "unverified_completion": "FAIL",
    },
    "health": {
        # scripts/health_check.py runs the structural check with this budget;
        # a timeout is UNKNOWN, never FAIL.
        "structure_timeout_seconds": 300,
    },
    "structure": {
        # Tracked scaffold scripts/verify_development_system.sh requires.
        "required_paths": [
            "AGENTS.md", "devsys.json",
            "requests/README.md", "requests/request-template.md",
            "docs/README.md", "docs/requirements/README.md", "docs/requirements/requirements.txt",
            "docs/requirements/development_operations.md",
            "docs/verification/tests.md",
            "docs/decisions/README.md", "docs/decisions/decisions.txt",
            "docs/process/state-model.md", "docs/process/autonomous-operating-model.md",
            "docs/process/recovery.md", "docs/process/escalation.md", "docs/process/process-protection.md",
            "docs/process/engineering-rules.md",
            "work/README.md", "work/work.txt", "evidence/README.md", "evidence/evidence.txt",
            ".agent/state/README.md",
            "scripts/claim.py", "scripts/preflight.py", "scripts/completion_gate.py", "scripts/drift_detect.py",
            "scripts/requirement_impact.py", "scripts/invariant_check.py", "scripts/doc_integrity.py",
            "scripts/health_check.py", "scripts/process_next_request.py", "scripts/devsys.py",
            "scripts/agent_process.py", "scripts/commit_request.py", "scripts/allocate_id.py",
            "scripts/process_protection.py", "scripts/work.py", "scripts/evidence.py",
            "scripts/devsys_config.py", "scripts/devsys_platform.py",
        ],
        # Project structural checks run by verify-structure (argv lists; a
        # non-zero exit is a failure), e.g. a module-boundary script.
        "extra_checks": [],
        "required_dirs": [
            "requests/inbox", "requests/processing", "requests/completed", "requests/rejected",
            "requests/needs-review", "docs/requirements", "docs/decisions",
            "work/queued", "work/active", "work/blocked", "work/needs-review", "work/verification",
            "work/completed", "work/cancelled", "evidence",
        ],
    },
}

_CACHE: dict[str, dict] = {}


# ------------------------------------------------------------------ roots
def repo_root() -> Path:
    """The repository root: $AGENT_PROCESS_ROOT (tests) or the scripts' parent."""
    env = os.environ.get("AGENT_PROCESS_ROOT")
    return Path(env).resolve() if env else SCRIPTS_DIR.parent


def project_root(root: Path | None = None) -> Path:
    """The project this development system serves (``paths.project_root``,
    relative to the system directory). Workers run their agents there."""
    root = Path(root or repo_root())
    value = Path(str(get("paths.project_root", root, ".") or "."))
    return (value if value.is_absolute() else root / value).resolve()


def path(root: Path, key: str) -> Path:
    """Absolute path of a framework layout entry (``LAYOUT[key]``)."""
    return root / LAYOUT[key]


# ------------------------------------------------------------------ loading
def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if key.startswith("_"):
            continue  # "_comment" keys document the file
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def load(root: Path | None = None, refresh: bool = False) -> dict:
    """Effective configuration: DEFAULTS deep-merged with <root>/devsys.json."""
    root = Path(root or repo_root()).resolve()
    key = str(root)
    if refresh or key not in _CACHE:
        raw = {}
        f = root / CONFIG_FILE
        if f.is_file():
            try:
                raw = json.loads(f.read_text(encoding="utf-8"))
            except ValueError as e:
                raise SystemExit(f"{f}: invalid JSON ({e}); fix it or run scripts/devsys.py doctor")
        _CACHE[key] = _merge(DEFAULTS, raw)
        _CACHE[key]["_file_present"] = f.is_file()
    return _CACHE[key]


def get(dotted: str, root: Path | None = None, default=None):
    node = load(root)
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def _known_keys(defaults: dict, raw: dict, prefix: str = "") -> list[str]:
    problems = []
    for key, value in raw.items():
        if key.startswith("_"):
            continue
        if key not in defaults:
            if prefix in ("", ) or not isinstance(defaults, dict):
                problems.append(f"unknown setting '{prefix}{key}'")
            continue
        if isinstance(defaults[key], dict) and isinstance(value, dict) and key not in ("requirement_env",
                                                                                          "request_sources",
                                                                                          "domains",
                                                                                          "selection"):
            problems.extend(_known_keys(defaults[key], value, f"{prefix}{key}."))
    return problems


def validate(root: Path | None = None) -> list[str]:
    """Configuration problems (empty when the file is absent or valid)."""
    root = Path(root or repo_root()).resolve()
    f = root / CONFIG_FILE
    if not f.is_file():
        return []
    try:
        raw = json.loads(f.read_text(encoding="utf-8"))
    except ValueError as e:
        return [f"{CONFIG_FILE}: invalid JSON ({e})"]
    problems = _known_keys(DEFAULTS, raw)
    cfg = load(root, refresh=True)
    if cfg.get("schema") != SCHEMA_VERSION:
        problems.append(f"schema {cfg.get('schema')!r} is not {SCHEMA_VERSION}")
    system = cfg["build"]["system"]
    if system not in ("none", "make", "cmake", "qmake", "custom"):
        problems.append(f"build.system {system!r} is not one of none, make, cmake, qmake, custom")
    if system == "custom" and not cfg["build"].get("build"):
        problems.append("build.system is 'custom' but build.build (the command) is not set")
    if cfg["tests"]["selection"] not in ("auto", "qmake", "globs"):
        problems.append(f"tests.selection {cfg['tests']['selection']!r} is not auto, qmake or globs")
    if cfg["completion"]["min_tier"] not in ("t0", "t1", "t2", "t3"):
        problems.append(f"completion.min_tier {cfg['completion']['min_tier']!r} is not t0..t3")
    for i, rule in enumerate(cfg.get("invariants") or []):
        if not isinstance(rule, dict) or not rule.get("id") or rule.get("kind") not in (
                "forbid_pattern", "require_pattern", "forbid_dependency", "layers"):
            problems.append(f"invariants[{i}] needs an 'id' and a kind of forbid_pattern, require_pattern, "
                            "forbid_dependency or layers")
            continue
        for key in ("pattern", "reference"):
            if key in rule:
                try:
                    re.compile(rule[key])
                except (re.error, TypeError) as e:
                    problems.append(f"invariants[{i}].{key} is not a valid regular expression ({e})")
    sources = cfg["ids"].get("request_sources") or {}
    for source in ("chat", "inbox", "crash", "automation"):
        prefix = sources.get(source)
        if not isinstance(prefix, str) or not re.fullmatch(r"REQ-[A-Z]+", prefix):
            problems.append(f"ids.request_sources.{source} {prefix!r} is not REQ-<LETTERS> (request ids are "
                            "REQ-<LETTERS>-YYYYMMDD-NNN)")
    for root_dir in cfg["project"].get("source_roots") or []:
        if not (root / root_dir).is_dir():
            problems.append(f"project.source_roots: {root_dir!r} is not a directory")
    w = cfg.get("workers") or {}
    if w.get("isolation") not in ("shared", "worktree"):
        problems.append(f"workers.isolation {w.get('isolation')!r} is not shared or worktree")
    if isinstance(w.get("max_workers"), bool) or not isinstance(w.get("max_workers"), int) or w["max_workers"] < 0:
        problems.append(f"workers.max_workers {w.get('max_workers')!r} is not a non-negative integer")
    cmd = (w.get("agent") or {}).get("command")
    if not isinstance(cmd, list) or not all(isinstance(a, str) for a in cmd):
        problems.append("workers.agent.command is not a list of strings")
    profiles = w.get("profiles") or {}
    if not isinstance(profiles, dict):
        problems.append("workers.profiles is not an object of named profiles")
        profiles = {}
    for pname, prof in profiles.items():
        pcmd = prof.get("command") if isinstance(prof, dict) else None
        if not isinstance(prof, dict) or (pcmd is not None and (
                not isinstance(pcmd, list) or not all(isinstance(a, str) for a in pcmd))):
            problems.append(f"workers.profiles.{pname} is not an object whose command is a list of strings")
    assign = w.get("assign") or {}
    if not isinstance(assign, dict):
        problems.append("workers.assign is not an object of worker id -> profile name")
        assign = {}
    for slot, pname in assign.items():
        if pname != "default" and pname not in profiles:
            problems.append(f"workers.assign.{slot} names the profile {pname!r}, which workers.profiles does not define")
    if not (root / str(get("paths.project_root", root, "."))).is_dir():
        problems.append(f"paths.project_root {get('paths.project_root', root)!r} is not a directory")
    for key in ("stale_hours", "lock_timeout_seconds", "dead_grace_minutes", "heartbeat_interval_seconds"):
        value = cfg["claims"].get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            problems.append(f"claims.{key} {value!r} is not a non-negative number")
    return problems


# A template placeholder is a bare name in angle brackets (<PROJECT>,
# <PACKAGE_STAGING_DIR>); <stdio.h> or a regex is not.
_PLACEHOLDER = re.compile(r"<[A-Za-z][A-Za-z0-9_]*>")
_REGEX_KEYS = {"pattern", "reference"}


def placeholders(root: Path | None = None) -> list[str]:
    """Settings still holding template placeholders (``<...>``) or left empty
    where the project must decide: reported as NOT CONFIGURED by the doctor."""
    cfg = load(root)
    out = []

    def walk(node, where):
        if isinstance(node, dict):
            for k, v in node.items():
                # Regular expressions (invariant patterns) legitimately hold <...>.
                if not k.startswith("_") and k not in _REGEX_KEYS:
                    walk(v, f"{where}.{k}" if where else k)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{where}[{i}]")
        elif isinstance(node, str) and _PLACEHOLDER.search(node):
            out.append(f"{where} = {node}")
    walk({k: v for k, v in cfg.items() if not k.startswith("_")}, "")
    if not cfg["project"]["source_roots"]:
        out.append("project.source_roots is empty")
    if cfg["build"]["system"] == "none":
        out.append("build.system is 'none' (no build integration)")
    if not cfg.get("invariants"):
        out.append("invariants is empty (no architecture rules)")
    wk = cfg.get("workers") or {}
    if not (wk.get("agent") or {}).get("command") and not any(
            (p or {}).get("command") for p in (wk.get("profiles") or {}).values()):
        out.append("workers.agent.command is empty (workers cannot start an agent)")
    return out


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--get")
    ap.add_argument("--validate", action="store_true")
    a = ap.parse_args(argv)
    if a.validate:
        problems = validate()
        for p in problems:
            print(f"CONFIG {p}")
        print("CONFIG: OK" if not problems else f"CONFIG: {len(problems)} problem(s)")
        return 1 if problems else 0
    if a.get:
        value = get(a.get)
        print(json.dumps(value) if not isinstance(value, str) else value)
        return 0 if value is not None else NOT_FOUND
    cfg = {k: v for k, v in load().items() if not k.startswith("_")}
    print(json.dumps(cfg, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
