"""Checks and inventory of the architecture audit (ARCH-AUDIT-*, ADR-000195).

A check evaluates one documented rule and returns a result:

    {"status": PASS | DRIFT | UNKNOWN, "summary": str, "details": [str],
     "metrics": {name: number | dict}, "evidence": str}

PASS     the rule was evaluated and holds.
DRIFT    the rule was evaluated and the implementation (or the documentation it
         names) deviates from it.
UNKNOWN  the rule could not be evaluated, or the only available check cannot
         show that it holds. Never counted as PASS.

Check kinds (manifest "kind"):

  command     an existing project command; "parse" turns its output into a result
  invariant   a slice of scripts/invariant_check.py --json (run once per audit)
  builtin     a small named function below, tied to a documented rule
  unverified  a documented rule nothing can check yet: always UNKNOWN

The module reads the repository and runs the project's own tools; it writes
nothing. Only the Python standard library is used.
"""
from __future__ import annotations

import functools
import json
import os
import platform
import re
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

# PASS: rule evaluated and holds · FAIL: a gate-severity rule evaluated and does
# not hold · DRIFT: an advisory rule evaluated and the implementation deviates
# · UNKNOWN: could not be evaluated (never PASS) · SKIPPED: not part of this
# profile, or the rule is not configured for this project (never PASS).
PASS, FAIL, DRIFT, UNKNOWN, SKIPPED = "PASS", "FAIL", "DRIFT", "UNKNOWN", "SKIPPED"
STATES = (PASS, FAIL, DRIFT, UNKNOWN, SKIPPED)

# A requirement / check identifier as the requirement documents write them:
# upper-case dash-separated namespace ending in a number (CORE-QT-001, UI-GUI-013).
REQ_ID = r"[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*-\d{2,4}[a-z]?"
_REQ_TOKEN = re.compile(r"\b(" + REQ_ID + r")(?:\.\.(\d{2,4}))?\b")
_DEFINITION = (
    re.compile(r"^#{2,6}\s+\**`?(" + REQ_ID + r")(?:\.\.(\d{2,4}))?"),      # ### CORE-QT-001 — ...
    re.compile(r"^\|\s*\**`?(" + REQ_ID + r")(?:\.\.(\d{2,4}))?"),          # | FTMARK-001 | ...
    re.compile(r"^(?:[*-]\s+)?\*\*(" + REQ_ID + r")(?:\.\.(\d{2,4}))?"),    # **RADAR-FP-001** ...
)
_NUMBER_TAIL = re.compile(r"^(.*-)(\d{2,4})([a-z]?)$")


def result(status: str, summary: str, details=None, metrics=None, evidence: str = "") -> dict:
    return {"status": status, "summary": summary, "details": list(details or []),
            "metrics": dict(metrics or {}), "evidence": evidence}


def prefix_of(req_id: str) -> str:
    match = _NUMBER_TAIL.match(req_id)
    return match.group(1)[:-1] if match else req_id


def expand(start: str, end: str | None) -> list[str]:
    """CORE-QT-001..004 -> the four IDs; a single ID -> itself."""
    match = _NUMBER_TAIL.match(start)
    if not end or not match or match.group(3):
        return [start]
    first, last = int(match.group(2)), int(end)
    if last < first or last - first > 200:
        return [start]
    width = len(match.group(2))
    return [f"{match.group(1)}{n:0{width}d}" for n in range(first, last + 1)]


@functools.lru_cache(maxsize=None)
def _glob_regex(pattern: str) -> re.Pattern:
    """Repository glob: ``*`` stays inside one path segment, ``**`` crosses
    segments (``dir/**`` is everything under dir, ``a/**/b`` includes a/b)."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out + r"\Z")


def glob_match(path: str, patterns) -> bool:
    return any(_glob_regex(p).match(path) for p in patterns)


class Context:
    """What every check may read: the repository root, the manifest, the
    profile, the audited build tree, and memoised command / file results."""

    def __init__(self, root: Path, manifest: dict, profile: str, build_dir: Path,
                 run_tests_tier: str = "t1") -> None:
        self.root = root
        self.manifest = manifest
        self.profile = profile
        self.build_dir = build_dir
        self.run_tests_tier = run_tests_tier
        self._memo: dict = {}

    # -- process execution (read-only tools only) -----------------------
    def run(self, command: list[str], env: dict | None = None, timeout: float = 120) -> dict:
        key = (tuple(command), tuple(sorted((env or {}).items())))
        if key in self._memo:
            return self._memo[key]
        full_env = dict(os.environ)
        full_env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
        full_env.update(env or {})
        started = time.monotonic()
        try:
            proc = subprocess.run(command, cwd=self.root, env=full_env, capture_output=True, text=True,
                                  timeout=timeout, errors="replace")
            outcome = {"rc": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr, "error": None}
        except subprocess.TimeoutExpired as exc:
            outcome = {"rc": None, "stdout": exc.stdout or "", "stderr": exc.stderr or "",
                       "error": f"timed out after {timeout:.0f} s"}
        except OSError as exc:
            outcome = {"rc": None, "stdout": "", "stderr": "", "error": f"could not run: {exc}"}
        if isinstance(outcome["stdout"], bytes):
            outcome["stdout"] = outcome["stdout"].decode(errors="replace")
        if isinstance(outcome["stderr"], bytes):
            outcome["stderr"] = outcome["stderr"].decode(errors="replace")
        outcome["seconds"] = round(time.monotonic() - started, 2)
        self._memo[key] = outcome
        return outcome

    def git(self, *args: str) -> str:
        out = self.run(["git", *args], timeout=60)
        return out["stdout"] if out["rc"] == 0 else ""

    # -- repository files ----------------------------------------------
    def is_git(self) -> bool:
        if "is_git" not in self._memo:
            top = self.run(["git", "rev-parse", "--show-toplevel"], timeout=30)
            self._memo["is_git"] = top["rc"] == 0 and Path(top["stdout"].strip()).resolve() == self.root.resolve()
        return self._memo["is_git"]

    def _walk(self) -> list[str]:
        """Without git: every file outside runtime, build and scratch trees."""
        skip = {".git", "node_modules", "tmp", "build", "__pycache__", ".agent", ".audit", "dist"}
        return sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*")
                      if p.is_file() and not (set(p.relative_to(self.root).parts) & skip))

    def tracked(self) -> list[str]:
        if "tracked" not in self._memo:
            self._memo["tracked"] = ([p for p in self.git("ls-files", "-z").split("\0") if p] if self.is_git()
                                     else self._walk())
        return self._memo["tracked"]

    def working_files(self) -> list[str]:
        """Tracked plus untracked-but-not-ignored files: the working tree the
        audit measures (another session's new, uncommitted test is part of it)."""
        if "working" not in self._memo:
            self._memo["working"] = ([p for p in self.git("ls-files", "-z", "-co", "--exclude-standard").split("\0")
                                      if p and (self.root / p).is_file()] if self.is_git() else self._walk())
        return self._memo["working"]

    def read(self, rel: str) -> str:
        try:
            return (self.root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def invariants(self) -> dict | None:
        out = self.run([sys.executable, "scripts/invariant_check.py", "--json"], timeout=180)
        try:
            data = json.loads(out["stdout"])
            return {"fails": list(data.get("fails", [])), "warns": list(data.get("warns", [])),
                    "rules": data.get("rules"), "configured": data.get("configured", True), "text": out["stdout"]}
        except ValueError:
            return None

    def project_graph(self):
        if "graph" not in self._memo:
            try:
                sys.path.insert(0, str(self.root / "scripts"))
                import test_selection  # noqa: E402  (the qmake graph run_tests.py uses)
                self._memo["graph"] = test_selection.ProjectGraph(self.root)
            except Exception as exc:  # the audit reports it, it does not crash on it
                self._memo["graph"] = exc
        return self._memo["graph"]

    def requirements(self) -> dict:
        if "requirements" not in self._memo:
            self._memo["requirements"] = requirement_model(self)
        return self._memo["requirements"]


# ====================================================================== parsers
def _tail(text: str, lines: int = 1) -> str:
    rows = [l.strip() for l in text.splitlines() if l.strip()]
    return " / ".join(rows[-lines:]) if rows else ""


def _command_evidence(command: list[str], out: dict) -> str:
    return (f"$ {' '.join(command)}\nexit: {out['rc']}  seconds: {out.get('seconds')}"
            + (f"  error: {out['error']}" if out.get("error") else "")
            + f"\n--- stdout ---\n{out['stdout']}\n--- stderr ---\n{out['stderr']}")


def parse_exit(command, out) -> dict:
    evidence = _command_evidence(command, out)
    if out["rc"] is None:
        return result(UNKNOWN, out["error"] or "did not complete", evidence=evidence)
    text = out["stdout"] + "\n" + out["stderr"]
    flagged = [l.strip() for l in text.splitlines()
               if re.search(r"\b(FAIL|FAILED|VIOLATION|ERROR|MISSING)\b", l)][:12]
    if out["rc"] == 0 or out["rc"] == 10:
        if "NOT CONFIGURED" in text or out["rc"] == 10:
            line = next((l.strip() for l in text.splitlines() if "NOT CONFIGURED" in l), "not configured")
            return result(SKIPPED, f"not configured: {line}", evidence=evidence)
        warnings = [l.strip() for l in text.splitlines() if re.match(r"\s*WARN\b", l)][:12]
        summary = _tail(out["stdout"]) or "exit 0"
        if warnings:
            # The tool passed with warnings: PASS, and the warnings are shown
            # (they were hidden behind the last output line before).
            summary = f"passed with {len(warnings)} warning(s): {warnings[0][:120]}"
        return result(PASS, summary, details=warnings, evidence=evidence)
    return result(DRIFT, (flagged[0] if flagged else _tail(text)) or f"exit {out['rc']}", details=flagged,
                  evidence=evidence)


def parse_findings_json(command, out) -> dict:
    """[{level, code, msg}] (drift_detect, doc_integrity): the tool's FAIL is
    DRIFT; WARN / ACCEPTED are its own advisory classes, counted and kept."""
    evidence = _command_evidence(command, out)
    try:
        findings = json.loads(out["stdout"])
        assert isinstance(findings, list)
    except (ValueError, AssertionError):
        return result(UNKNOWN, out["error"] or f"no JSON findings (exit {out['rc']})", evidence=evidence)
    levels = Counter(f.get("level", "?") for f in findings)
    codes = Counter(f"{f.get('level', '?')} {f.get('code', '?')}" for f in findings)
    fails = [f"{f.get('code')}: {f.get('msg')}" for f in findings if f.get("level") == "FAIL"]
    metrics = {"levels": dict(sorted(levels.items())), "codes": dict(sorted(codes.items())),
               "fail": levels.get("FAIL", 0), "warn": levels.get("WARN", 0)}
    summary = ", ".join(f"{n} {level}" for level, n in sorted(levels.items())) or "no findings"
    details = fails[:20] + [f"{n} x {code}" for code, n in codes.most_common() if not code.startswith("FAIL")][:8]
    return result(DRIFT if fails else PASS, summary, details, metrics, evidence)


_PLAN_LINE = re.compile(r"^(PENDING|CACHED|STALE|MISSING|BLOCKED|SKIPPED|NO_TESTS)\s+(\S+)\s+~\s*(\d+)s\s+(.*)$")


def parse_test_plan(command, out) -> dict:
    """scripts/run_tests.py --plan: which registered test binaries are built
    from the current sources (PENDING / CACHED) and which are STALE / MISSING."""
    evidence = _command_evidence(command, out)
    if out["rc"] != 0:
        return result(UNKNOWN, out["error"] or f"run_tests.py --plan exit {out['rc']}", evidence=evidence)
    groups: dict[str, str] = {}
    detail: dict[str, str] = {}
    for line in out["stdout"].splitlines():
        m = _PLAN_LINE.match(line.strip())
        if not m:
            continue
        label = re.sub(r"\[\d+/\d+\]$", "", m.group(2))
        groups[label] = m.group(1)
        if m.group(1) in ("STALE", "MISSING", "BLOCKED"):
            detail[label] = m.group(4).split("[", 1)[-1].rstrip("]") if "[" in m.group(4) else m.group(4)
    warnings = [l.split("WARN", 1)[1].strip() for l in out["stdout"].splitlines() if "[run_tests] WARN" in l]
    counts = Counter(groups.values())
    not_built = {g: s for g, s in groups.items() if s in ("STALE", "MISSING", "BLOCKED")}
    metrics = {"groups": len(groups), "by_status": dict(sorted(counts.items())),
               "not_current": len(not_built)}
    details = [f"{s} {g}: {detail.get(g, '')}"[:220] for g, s in sorted(not_built.items())] + \
              [f"run_tests warning: {w}" for w in warnings]
    if not groups:
        return result(UNKNOWN, "no plan lines parsed", details, metrics, evidence)
    if not_built:
        return result(UNKNOWN, f"{len(not_built)} of {len(groups)} test groups need a build of the current "
                               f"sources ({', '.join(sorted(set(not_built.values())))})", details, metrics, evidence)
    return result(PASS, f"all {len(groups)} registered test groups are built from the current sources",
                  details, metrics, evidence)


def parse_test_run(command, out) -> dict:
    """scripts/run_tests.py (execution): its classification and run record."""
    evidence = _command_evidence(command, out)
    record_path = None
    for line in out["stdout"].splitlines():
        if line.startswith("record: "):
            record_path = line[len("record: "):].strip()
    record = None
    if record_path:
        try:
            record = json.loads(Path(record_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            record = None
    if record is None:
        return result(UNKNOWN, out["error"] or f"no run record (exit {out['rc']})", evidence=evidence)
    rows = record.get("groups", [])
    by_status = Counter(r.get("status") for r in rows)
    failed = [f"{r['suite']}/{r['group']}: {r.get('status')} {', '.join(r.get('failed', [])[:5])}"
              for r in rows if r.get("status") in ("FAIL", "TIMEOUT", "CRASH")]
    not_run = [f"{r['suite']}/{r['group']}: {r.get('status')} {'; '.join(r.get('detail', []))[:160]}"
               for r in rows if r.get("status") in ("STALE", "MISSING", "BLOCKED", "ENVIRONMENTAL")]
    metrics = {"run_id": record.get("run_id"), "classification": record.get("classification"),
               "tier": record.get("tier"), "wall_seconds": record.get("wall_seconds"),
               "groups": len(rows), "by_status": dict(sorted(by_status.items())),
               "cases": sum(int(r.get("cases") or 0) for r in rows)}
    summary = (f"run {record.get('run_id')} tier {record.get('tier')}: {record.get('classification')} "
               f"({', '.join(f'{n} {s}' for s, n in sorted(by_status.items()))})")
    status = DRIFT if failed else (UNKNOWN if not_run else PASS)
    return result(status, summary, failed + not_run, metrics, evidence)


PARSERS = {"exit": parse_exit, "findings-json": parse_findings_json,
           "test-plan": parse_test_plan, "test-run": parse_test_run}


# ==================================================================== evaluate
def evaluate(check: dict, ctx: Context) -> dict:
    kind = check.get("kind")
    if kind == "command":
        command = [c.format(build_dir=str(ctx.build_dir), run_tests_tier=ctx.run_tests_tier)
                   for c in check.get("command", [])]
        if not command:
            return result(UNKNOWN, check.get("reason", "no command"))
        if command[0] == "python3":
            command[0] = sys.executable
        out = ctx.run(command, env=check.get("env"), timeout=float(check.get("timeout_seconds", 120)))
        res = PARSERS[check.get("parse", "exit")](command, out)
    elif kind == "invariant":
        res = invariant_slice(ctx, check)
    elif kind == "builtin":
        function = BUILTINS.get(check.get("function", ""))
        if function is None:
            return result(UNKNOWN, f"unknown builtin {check.get('function')!r}")
        try:
            res = function(ctx, check.get("params", {}), check)
        except Exception as exc:  # a broken check is UNKNOWN, never PASS
            res = result(UNKNOWN, f"check raised {type(exc).__name__}: {exc}")
    elif kind == "unverified":
        res = result(UNKNOWN, check.get("reason", "no verification mechanism exists"))
    else:
        return result(UNKNOWN, f"unknown check kind {kind!r}")
    if res["status"] == UNKNOWN and check.get("reason") and check["reason"] not in res["details"]:
        res["details"].insert(0, "why unknown: " + check["reason"])
    # A gate-severity rule that does not hold is a FAIL; an advisory one DRIFT.
    if res["status"] == DRIFT and check.get("severity") == "gate":
        res["status"] = FAIL
    return res


def architecture_rules(ctx: Context, params: dict, check: dict) -> dict:
    """The project's declarative architecture rules (devsys.json "invariants",
    scripts/invariant_check.py): PASS / DRIFT (FAIL with severity gate);
    SKIPPED when the project has configured none — never a vacuous PASS."""
    data = ctx.invariants()
    if data is None:
        return result(UNKNOWN, "scripts/invariant_check.py produced no JSON")
    if not data.get("configured", True):
        return result(SKIPPED, "not configured: no architecture rule in devsys.json \"invariants\"",
                      evidence=data["text"])
    violated = [f for f in data["fails"] if f.startswith("INVARIANT ")]
    advisory = [w for w in data["warns"] if w.startswith("INVARIANT ")]    # rules declared severity "warn"
    metrics = {"rules": data.get("rules"), "violations": len(violated), "warnings": len(advisory)}
    if violated:
        return result(DRIFT, violated[0][:200], violated + advisory, metrics, data["text"])
    summary = f"{data.get('rules')} architecture rule(s) hold"
    if advisory:
        summary += f"; {len(advisory)} advisory (severity warn) violation(s): {advisory[0][:120]}"
    return result(PASS, summary, advisory, metrics, data["text"])


def invariant_slice(ctx: Context, check: dict) -> dict:
    data = ctx.invariants()
    if data is None:
        return result(UNKNOWN, "scripts/invariant_check.py produced no JSON")
    prefixes = check.get("match", [])
    fails = [f for f in data["fails"] if any(f.startswith(p) for p in prefixes)]
    warns = [w for w in data["warns"] if any(w.startswith(p) for p in prefixes)]
    evidence = (f"$ {sys.executable} scripts/invariant_check.py --json\nmatch: {prefixes}\n"
                f"matching fails: {json.dumps(fails, indent=1)}\nmatching warns: {json.dumps(warns, indent=1)}\n"
                f"--- full output ---\n{data['text']}")
    if fails:
        return result(DRIFT, fails[0][:200], fails + warns, {"fail": len(fails), "warn": len(warns)}, evidence)
    if check.get("strength") == "enforced":
        return result(PASS, "scripts/invariant_check.py reports no violation", warns,
                      {"fail": 0, "warn": len(warns)}, evidence)
    summary = (f"heuristic only; {len(warns)} warning(s)" if warns
               else "heuristic only; it reports nothing, which does not show the rule holds")
    return result(UNKNOWN, summary, warns, {"fail": 0, "warn": len(warns)}, evidence)


# ==================================================================== builtins
def _qmake_text(text: str) -> str:
    """Comments removed and backslash continuations joined."""
    text = re.sub(r"#[^\n]*", "", text)
    return re.sub(r"\\\s*\n", " ", text)


def root_declarations(text: str) -> tuple[list[str], dict[str, set[str]]]:
    text = _qmake_text(text)
    names: list[str] = []
    for m in re.finditer(r"^\s*SUBDIRS\s*\+?=\s*(.*)$", text, re.MULTILINE):
        names += m.group(1).split()
    depends: dict[str, set[str]] = {}
    for m in re.finditer(r"^\s*([A-Za-z0-9_]+)\.depends\s*\+?=\s*(.*)$", text, re.MULTILINE):
        depends.setdefault(m.group(1), set()).update(m.group(2).split())
    return names, depends


def declared_dependencies(ctx: Context, params: dict, check: dict) -> dict:
    project_file = params.get("project_file", "project.pro")
    text = ctx.read(project_file)
    if not text:
        return result(UNKNOWN, f"{project_file} not readable")
    names, depends = root_declarations(text)
    deviations = []
    for product, rule in params.get("expect", {}).items():
        declared = depends.get(product, set())
        if product not in names:
            deviations.append(f"{product}: not a SUBDIRS entry of {project_file}")
            continue
        missing = sorted(set(rule.get("includes", [])) - declared)
        if missing:
            deviations.append(f"{product}.depends lacks {', '.join(missing)} (declared: {' '.join(sorted(declared)) or 'nothing'})")
        if "only" in rule:
            extra = sorted(declared - set(rule["only"]))
            absent = sorted(set(rule["only"]) - declared)
            if extra or absent:
                deviations.append(f"{product}.depends = {' '.join(sorted(declared))}; {check.get('rule')} prescribes "
                                  f"only {' '.join(rule['only'])}" + (f" (extra: {', '.join(extra)})" if extra else "")
                                  + (f" (missing: {', '.join(absent)})" if absent else ""))
    evidence = f"{project_file} SUBDIRS: {' '.join(names)}\n" + "\n".join(
        f"{n}.depends = {' '.join(sorted(d))}" for n, d in sorted(depends.items()))
    metrics = {"subdirs": len(names), "declared_edges": sum(len(d) for d in depends.values()),
               "deviations": len(deviations)}
    if deviations:
        return result(DRIFT, deviations[0], deviations, metrics, evidence)
    return result(PASS, f"{len(params.get('expect', {}))} products declare the prescribed dependencies",
                  [], metrics, evidence)


def dependency_direction(ctx: Context, params: dict, check: dict) -> dict:
    graph = ctx.project_graph()
    if isinstance(graph, Exception):
        return result(UNKNOWN, f"qmake project graph unavailable: {graph}")
    closure: dict[str, set[str]] = {}

    def reach(pro: str, seen: set[str]) -> set[str]:
        for dep in graph.projects.get(pro).depends if pro in graph.projects else ():
            if dep not in seen:
                seen.add(dep)
                reach(dep, seen)
        return seen

    violations = []
    for source, forbidden in params.get("forbidden", {}).items():
        if source not in graph.projects:
            violations.append(f"{source}: not in the root qmake project graph")
            continue
        closure[source] = reach(source, set())
        for target in forbidden:
            if target in closure[source]:
                violations.append(f"{source} depends (transitively) on {target}")
    evidence = "declared dependencies (scripts/test_selection.py ProjectGraph; nested subdirs children inherit " \
               "their parent's .depends):\n" + "\n".join(
                   f"{p}: {' '.join(sorted(pr.depends))}" for p, pr in sorted(graph.projects.items())) + \
               "\n\ntransitive closure of the checked projects:\n" + "\n".join(
                   f"{p}: {' '.join(sorted(c))}" for p, c in sorted(closure.items()))
    metrics = {"projects": len(graph.projects), "violations": len(violations)}
    if violations:
        return result(DRIFT, violations[0], violations, metrics, evidence)
    return result(PASS, f"no declared dependency of {len(closure)} checked projects points against the "
                        "documented direction", [], metrics, evidence)


# -- requirements -----------------------------------------------------------
def requirement_model(ctx: Context) -> dict:
    spec = ctx.manifest.get("requirements", {})
    exclude = set(spec.get("exclude", []))
    documents = sorted(p for p in ctx.working_files()
                       if glob_match(p, [spec.get("documents", "docs/requirements/*.md")]) and p not in exclude)
    defined: dict[str, str] = {}
    duplicates: dict[str, list[str]] = {}
    per_document: dict[str, int] = {}
    for doc in documents:
        count = 0
        for line in ctx.read(doc).splitlines():
            for pattern in _DEFINITION:
                m = pattern.match(line)
                if not m:
                    continue
                for req in expand(m.group(1), m.group(2)):
                    if req in defined and defined[req] != doc:
                        duplicates.setdefault(req, [defined[req]]).append(doc)
                    elif req not in defined:
                        count += 1
                    defined.setdefault(req, doc)
                break
        per_document[doc] = count
    prefixes = {prefix_of(r) for r in defined}
    suffixes = tuple(spec.get("test_suffixes", [".cpp", ".h", ".py"]))
    tests = [p for p in ctx.working_files() if p.endswith(suffixes) and glob_match(p, spec.get("test_sources", []))]
    evidence_files = [p for p in ctx.working_files() if glob_match(p, spec.get("evidence", ["evidence/**/*.md"]))]

    def citations(paths: list[str]) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
        """(IDs written out, IDs only implied by a range such as X-001..005)."""
        explicit: dict[str, set[str]] = {}
        implied: dict[str, set[str]] = {}
        for path in paths:
            for m in _REQ_TOKEN.finditer(ctx.read(path)):
                explicit.setdefault(m.group(1), set()).add(path)
                for req in expand(m.group(1), m.group(2))[1:]:
                    implied.setdefault(req, set()).add(path)
        return explicit, implied

    tests_explicit, tests_implied = citations(tests)
    evidence_explicit, evidence_implied = citations(evidence_files)

    def union(a: dict, b: dict) -> dict:
        merged = {k: set(v) for k, v in a.items()}
        for k, v in b.items():
            merged.setdefault(k, set()).update(v)
        return merged

    return {"documents": documents, "defined": defined, "duplicates": duplicates, "per_document": per_document,
            "prefixes": prefixes, "tests": tests, "evidence_files": evidence_files,
            "in_tests": union(tests_explicit, tests_implied), "in_evidence": union(evidence_explicit, evidence_implied),
            "tests_explicit": tests_explicit, "tests_implied": tests_implied, "evidence_explicit": evidence_explicit}


def requirement_index(ctx: Context, params: dict, check: dict) -> dict:
    spec = ctx.manifest.get("requirements", {})
    index = spec.get("index", "docs/requirements/requirements.txt")
    text = ctx.read(index)
    if not text:
        return result(UNKNOWN, f"{index} not readable")
    owners: set[str] = set()
    for line in text.splitlines():
        cols = line.split("|")
        if len(cols) >= 4 and not line.startswith("Format"):
            owners.update(o.strip().strip("`") for o in cols[1].split(",") if o.strip())
    base = str(Path(index).parent)
    documents = {Path(d).name for d in ctx.requirements()["documents"]}
    unlisted = sorted(documents - owners)
    missing = sorted(o for o in owners if not (ctx.root / base / o).exists())
    details = [f"{base}/{d}: no owner row in {index}" for d in unlisted] + \
              [f"{index} names {o}, which does not exist" for o in missing]
    metrics = {"documents": len(documents), "owner_files": len(owners), "unlisted": len(unlisted),
               "missing": len(missing)}
    if details:
        return result(DRIFT, details[0], details, metrics, "\n".join(details))
    return result(PASS, f"{len(documents)} requirement documents, all indexed", [], metrics, "")


def requirement_references(ctx: Context, params: dict, check: dict) -> dict:
    model = ctx.requirements()
    defined, prefixes = model["defined"], model["prefixes"]

    def undefined(cited: dict[str, set[str]]) -> dict[str, set[str]]:
        # Only namespaces some requirement document uses: REQ-CHAT-*, WORK-*,
        # CVE numbers and similar tokens are not requirement references.
        return {r: p for r, p in cited.items() if r not in defined and prefix_of(r) in prefixes}

    # DRIFT only for an ID a test writes out. A range (X-001..005) whose end
    # overshoots and an old evidence record are reported, not counted: both are
    # weaker signals, and a false DRIFT costs more than a missed one.
    in_tests = undefined(model["tests_explicit"])
    by_range = {r: p for r, p in undefined(model["tests_implied"]).items() if r not in in_tests}
    in_evidence = undefined(model["evidence_explicit"])
    details = [f"{r} cited by {', '.join(sorted(p)[:3])}" for r, p in sorted(in_tests.items())]
    evidence = "requirement IDs written in test sources but defined by no requirement document:\n" + \
               "\n".join(details) + \
               "\n\nfor information — implied only by a range in a test source (X-001..NNN past the last defined ID):\n" + \
               "\n".join(f"{r} implied by {', '.join(sorted(p)[:3])}" for r, p in sorted(by_range.items())) + \
               "\n\nfor information — cited only by evidence records (historical, not counted as drift):\n" + \
               "\n".join(f"{r} cited by {', '.join(sorted(p)[:3])}" for r, p in sorted(in_evidence.items()))
    metrics = {"undefined_in_tests": len(in_tests), "undefined_via_range": len(by_range),
               "undefined_in_evidence": len(in_evidence),
               "test_files_scanned": len(model["tests"]), "evidence_files_scanned": len(model["evidence_files"])}
    if in_tests:
        return result(DRIFT, f"{len(in_tests)} requirement ID(s) cited by tests are defined nowhere: "
                             f"{', '.join(sorted(in_tests)[:6])}", details, metrics, evidence)
    return result(PASS, f"every requirement ID cited by {len(model['tests'])} test sources is defined",
                  [], metrics, evidence)


def requirement_coverage(ctx: Context, params: dict, check: dict) -> dict:
    model = ctx.requirements()
    defined = model["defined"]
    tested = {r for r in defined if r in model["in_tests"]}
    evidence_only = {r for r in defined if r in model["in_evidence"]} - tested
    uncited = set(defined) - tested - evidence_only
    per_doc = Counter(defined[r] for r in uncited)
    totals = Counter(defined.values())
    rows = [f"{doc}: {n} of {totals[doc]} uncited" for doc, n in per_doc.most_common()]
    metrics = {"requirements": len(defined), "documents": len(model["documents"]),
               "cited_by_tests": len(tested), "cited_by_evidence_only": len(evidence_only),
               "uncited": len(uncited), "duplicate_definitions": len(model["duplicates"])}
    summary = (f"{len(defined)} requirement IDs in {len(model['documents'])} documents: {len(tested)} cited by "
               f"tests, {len(evidence_only)} only by evidence, {len(uncited)} with no identifiable verification")
    evidence = summary + "\n\nuncited per document:\n" + "\n".join(rows) + \
        "\n\nduplicate definitions:\n" + "\n".join(f"{r}: {', '.join(d)}" for r, d in sorted(model["duplicates"].items())) + \
        "\n\nuncited IDs:\n" + "\n".join(sorted(uncited))
    return result(UNKNOWN, summary, rows[:10], metrics, evidence)


# -- documentation ------------------------------------------------------------
def current_architecture_documents(ctx: Context, index: str) -> list[str]:
    """The "Current references" list of docs/architecture/README.md."""
    listed, in_section = [], False
    for line in ctx.read(index).splitlines():
        if line.startswith("## "):
            in_section = line.strip().lower().startswith("## current references")
            continue
        m = re.match(r"^\*\s+`([^`]+\.md)`", line)
        if in_section and m:
            listed.append(str(Path(index).parent / m.group(1)))
    return listed


def documented_paths(ctx: Context, params: dict, check: dict) -> dict:
    spec = ctx.manifest.get("documentation_paths", {})
    documents = list(spec.get("documents", []))
    index = spec.get("architecture_index", "docs/architecture/README.md")
    architecture = current_architecture_documents(ctx, index)
    # Architecture documents the documentation index presents as entry points,
    # whether or not the architecture README lists them as current references.
    for doc_index in spec.get("indexes", []):
        for prefix in spec.get("indexed_prefixes", []):
            pattern = re.compile(r"`(" + re.escape(prefix) + r"[^`\s]+\.md)`")
            architecture += [d for d in pattern.findall(ctx.read(doc_index)) if d not in architecture]
    documents += [d for d in architecture if d not in documents]
    tracked = ctx.working_files()
    tops = {p.split("/", 1)[0] for p in tracked if "/" in p}
    by_name: dict[str, list[str]] = {}
    for p in tracked:
        by_name.setdefault(p.rsplit("/", 1)[-1], []).append(p)
    extensions = tuple(spec.get("extensions", []))
    skip = tuple(spec.get("skip_prefixes", []))
    context_words = [w.lower() for w in spec.get("context_words", [])]
    token = re.compile(r"`([^`\s]+)`")
    candidates, missing = 0, []
    for doc in documents:
        text = ctx.read(doc)
        if not text:
            missing.append((doc, 0, doc, "listed as a current reference but does not exist"))
            continue
        for number, line in enumerate(text.splitlines(), 1):
            lowered = line.lower()
            for raw in token.findall(line):
                path = re.sub(r":\d+$", "", raw.rstrip(".,;:)"))
                if "/" not in path or path.startswith(skip) or not path.endswith(extensions):
                    continue
                if any(c in path for c in "*<>{}$|?[]()=\"'") or ".." in path or "/." in path:
                    continue
                if path.split("/", 1)[0] not in tops:
                    continue
                candidates += 1
                if (ctx.root / path).exists():
                    continue
                if any(w in lowered for w in context_words):
                    continue  # the sentence itself says the path is gone / moved / historical
                if any(m[:3] == (doc, number, path) for m in missing):
                    continue
                moved = [m for m in by_name.get(path.rsplit("/", 1)[-1], []) if not m.startswith(".")]
                hint = f"possibly moved to {moved[0]}" if len(moved) == 1 else (
                    f"{len(moved)} files share the name" if moved else "no file with that name exists")
                missing.append((doc, number, path, hint))
    details = [f"{d}:{n} `{p}` — {h}" for d, n, p, h in missing]
    metrics = {"documents": len(documents), "architecture_current": len(architecture),
               "paths_checked": candidates, "missing": len(missing)}
    evidence = "documents checked:\n" + "\n".join(documents) + "\n\nmissing:\n" + "\n".join(details)
    if missing:
        return result(DRIFT, f"{len(missing)} documented path(s) do not exist ({candidates} checked in "
                             f"{len(documents)} documents)", details, metrics, evidence)
    return result(PASS, f"all {candidates} documented paths exist ({len(documents)} documents)", [], metrics, evidence)


# -- build ------------------------------------------------------------------
def _makefiles(build_dir: Path, depth: int = 3) -> list[Path]:
    found = []
    for current, dirs, files in os.walk(build_dir):
        rel_depth = len(Path(current).relative_to(build_dir).parts)
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("test-results",)] if rel_depth < depth else []
        if "Makefile" in files:
            found.append(Path(current) / "Makefile")
    return sorted(found)


def makefile_generators(build_dir: Path) -> dict[str, str]:
    versions = {}
    for makefile in _makefiles(build_dir):
        try:
            head = makefile.read_text(encoding="utf-8", errors="replace")[:400]
        except OSError:
            continue
        m = re.search(r"Generated by qmake \([^)]*\) \(Qt ([0-9.]+)\)", head)
        if m:
            versions[str(makefile.relative_to(build_dir))] = m.group(1)
    return versions


def build_tree_qt(ctx: Context, params: dict, check: dict) -> dict:
    if not ctx.build_dir.is_dir():
        return result(UNKNOWN, f"build tree {ctx.build_dir} does not exist")
    versions = makefile_generators(ctx.build_dir)
    if not versions:
        return result(UNKNOWN, f"no qmake-generated Makefile under {ctx.build_dir}")
    by_major = Counter(v.split(".")[0] for v in versions.values())
    qt5 = sorted(m for m, v in versions.items() if v.startswith("5."))
    evidence = "\n".join(f"{m}: Qt {v}" for m, v in sorted(versions.items()))
    metrics = {"makefiles": len(versions), "by_qt_version": dict(sorted(Counter(versions.values()).items()))}
    if qt5:
        return result(DRIFT, f"{len(qt5)} of {len(versions)} Makefiles in {ctx.build_dir.name} were generated by "
                             f"Qt 5 qmake", [f"Qt 5: {m}" for m in qt5[:12]], metrics, evidence)
    return result(PASS, f"all {len(versions)} Makefiles generated by Qt {', '.join(sorted(by_major))}",
                  [], metrics, evidence)


# -- tests ------------------------------------------------------------------
NOT_A_RESULT = {"STALE", "MISSING", "BLOCKED", "PENDING", "SKIPPED"}
FAILING = {"FAIL", "TIMEOUT", "CRASH"}
LANES = {"perf", "stress", "soak", "live", "package"}


def test_registry(ctx: Context) -> list[dict]:
    try:
        data = json.loads(ctx.read("scripts/test_manifest.json"))
    except ValueError:
        return []
    groups = []
    for suite in data.get("suites", []):
        for name, group in suite.get("groups", {}).items():
            tiers = group.get("tiers", [])
            groups.append({"label": f"{suite['id']}/{name}", "tiers": tiers,
                           "lane": bool(tiers) and set(tiers) <= LANES,
                           "expected_seconds": group.get("expected_seconds")})
    return groups


def recorded_test_results(ctx: Context, params: dict, check: dict) -> dict:
    registry = test_registry(ctx)
    if not registry:
        return result(UNKNOWN, "scripts/test_manifest.json not readable")
    runs_dir = ctx.build_dir / "test-results" / "runs"
    records = []
    for path in sorted(runs_dir.glob("*.json")) if runs_dir.is_dir() else []:
        try:
            records.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    records.sort(key=lambda r: r.get("started_at") or r.get("run_id") or "")
    head = ctx.git("rev-parse", "HEAD").strip()
    latest: dict[str, dict] = {}
    for record in records:
        for row in record.get("groups", []):
            if row.get("status") in NOT_A_RESULT:
                continue
            latest[f"{row.get('suite')}/{row.get('group')}"] = {
                "status": row.get("status"), "run_id": record.get("run_id"), "at": record.get("started_at"),
                "head": (record.get("head") or "")[:10], "current_head": record.get("head") == head,
                "cases": row.get("cases"), "seconds": round(float(row.get("seconds") or 0), 1),
                "failed": row.get("failed", [])[:5]}
    table, failing, unrecorded = [], [], []
    for group in registry:
        entry = latest.get(group["label"])
        kind = "lane" if group["lane"] else "tier"
        if entry is None:
            table.append(f"{group['label']} [{','.join(group['tiers'])}]: no recorded result")
            if kind == "tier":
                unrecorded.append(group["label"])
            continue
        table.append(f"{group['label']} [{','.join(group['tiers'])}]: {entry['status']} run {entry['run_id']} "
                     f"at {entry['head']}{'' if entry['current_head'] else ' (older HEAD)'} "
                     f"{entry['cases']} cases {entry['seconds']} s"
                     + (f" failed: {', '.join(entry['failed'])}" if entry["failed"] else ""))
        if entry["status"] in FAILING:
            failing.append(f"{group['label']}: {entry['status']} (run {entry['run_id']})")
    statuses = Counter(latest[g["label"]]["status"] for g in registry if g["label"] in latest)
    metrics = {"registered_groups": len(registry), "lane_groups": sum(g["lane"] for g in registry),
               "records": len(records), "recorded_groups": len([g for g in registry if g["label"] in latest]),
               "unrecorded_tier_groups": len(unrecorded), "failing": len(failing),
               "on_current_head": sum(1 for g in registry if latest.get(g["label"], {}).get("current_head")),
               "latest_by_status": dict(sorted(statuses.items()))}
    evidence = f"run records: {runs_dir} ({len(records)})\ncurrent HEAD: {head[:10]}\n\n" + "\n".join(table)
    if failing:
        return result(DRIFT, f"{len(failing)} test group(s) failed in their latest recorded run",
                      failing + [f"no record: {u}" for u in unrecorded], metrics, evidence)
    if not records or unrecorded:
        return result(UNKNOWN, f"{len(unrecorded)} of {len(registry) - metrics['lane_groups']} tier groups have no "
                               f"recorded result in {ctx.build_dir.name}", [f"no record: {u}" for u in unrecorded],
                      metrics, evidence)
    return result(PASS, f"every tier group's latest recorded result passed "
                        f"({metrics['on_current_head']} recorded on the current HEAD)", [], metrics, evidence)


BUILTINS = {
    "declared_dependencies": declared_dependencies,
    "dependency_direction": dependency_direction,
    "requirement_index": requirement_index,
    "requirement_references": requirement_references,
    "requirement_coverage": requirement_coverage,
    "documented_paths": documented_paths,
    "build_tree_qt": build_tree_qt,
    "recorded_test_results": recorded_test_results,
    "architecture_rules": architecture_rules,
}


# =================================================================== inventory
SOURCE_SUFFIXES = (".cpp", ".cc", ".c", ".h", ".hpp", ".py", ".ts", ".js", ".sh", ".proto", ".ui", ".qml")


def module_of(path: str, modules: list[dict]) -> str:
    for module in modules:
        for prefix in module["paths"]:
            if path.startswith(prefix) if prefix.endswith("/") else path == prefix:
                return module["name"]
    return "other"


def _build_system(ctx: Context) -> str:
    try:
        return json.loads(ctx.read("devsys.json") or "{}").get("build", {}).get("system", "none")
    except ValueError:
        return "unknown"


def inventory(ctx: Context) -> dict:
    """Facts, not rule evaluations: evidence for the report and the packet."""
    tracked = ctx.tracked()
    modules = ctx.manifest.get("modules", [])
    spec = ctx.manifest.get("requirements", {})
    per_module: dict[str, Counter] = {}
    for path in tracked:
        counter = per_module.setdefault(module_of(path, modules), Counter())
        counter["files"] += 1
        if path.endswith(SOURCE_SUFFIXES):
            counter["source"] += 1
        if glob_match(path, spec.get("test_sources", [])) and path.endswith(tuple(spec.get("test_suffixes", []))):
            counter["test"] += 1
        if path.endswith(".md"):
            counter["docs"] += 1

    status = ctx.git("status", "--porcelain=v1", "--untracked-files=normal")
    dirty = [l for l in status.splitlines() if l and not l.startswith("??")]
    untracked = [l for l in status.splitlines() if l.startswith("??")]
    git = {"branch": ctx.git("rev-parse", "--abbrev-ref", "HEAD").strip(),
           "commit": ctx.git("rev-parse", "HEAD").strip(),
           "describe": ctx.git("describe", "--always", "--tags").strip(),
           "commit_date": ctx.git("log", "-1", "--format=%cI").strip(),
           "tracked_files": len(tracked), "modified": len(dirty), "untracked": len(untracked),
           "modified_paths": sorted(l[3:] for l in dirty)[:200],
           "untracked_paths": sorted(l[3:] for l in untracked)[:100]}

    graph = ctx.project_graph()
    projects = {} if isinstance(graph, Exception) else {
        p: sorted(pr.depends) for p, pr in sorted(graph.projects.items())}

    architecture_index = ctx.manifest.get("documentation_paths", {}).get("architecture_index",
                                                                          "docs/architecture/README.md")
    current = set(current_architecture_documents(ctx, architecture_index))
    arch_docs = sorted(p for p in tracked if p.startswith("docs/architecture/") and p.endswith(".md")
                       and not p.endswith("README.md"))
    docs_by_dir = Counter(p.split("/")[1] if p.count("/") > 1 else "(root)" for p in tracked if p.startswith("docs/"))

    model = ctx.requirements()
    req_per_doc = {Path(d).name: n for d, n in sorted(model["per_document"].items())}

    build = {"build_dir": str(ctx.build_dir), "exists": ctx.build_dir.is_dir()}
    if build["exists"]:
        versions = makefile_generators(ctx.build_dir)
        build["qmake_qt_versions"] = dict(sorted(Counter(versions.values()).items()))
        # PROJECT-SPECIFIC: which Makefile to sample for the compiler and its
        # flags (the source project sampled its core library's Makefile).
        core_makefile = ctx.build_dir / ctx.manifest.get("project", {}).get("sample_makefile", "Makefile")
        head = core_makefile.read_text(encoding="utf-8", errors="replace")[:6000] if core_makefile.is_file() else ""
        cxx = re.search(r"^CXX\s*=\s*(.+)$", head, re.MULTILINE)
        flags = re.search(r"^CXXFLAGS\s*=\s*(.+)$", head, re.MULTILINE)
        build["compiler"] = cxx.group(1).strip() if cxx else None
        build["cxxflags_sample"] = flags.group(1).strip()[:300] if flags else None
        build["configuration"] = ("debug" if flags and re.search(r"(^|\s)-g(\s|$)", flags.group(1)) else
                                  "release" if flags and "-O2" in flags.group(1) else None)
        runs = ctx.build_dir / "test-results" / "runs"
        build["test_run_records"] = len(list(runs.glob("*.json"))) if runs.is_dir() else 0
    compiler = (build.get("compiler") or "g++").split()[0]
    build["compiler_version"] = _first_line(ctx.run([compiler, "--version"], timeout=20))
    build["system"] = _build_system(ctx)
    if build["system"] == "qmake":
        qt = ctx.run(["qmake6", "-query", "QT_VERSION"], timeout=20)
        build["qmake6_qt_version"] = qt["stdout"].strip() if qt["rc"] == 0 else None
    policy = ctx.manifest.get("canonical_sources", {}).get("warning_policy")
    build["warning_policy"] = policy if policy and (ctx.root / policy).is_file() else None
    build["werror"] = bool(policy) and "-Werror" in ctx.read(policy)

    registry = test_registry(ctx)
    tiers = Counter(t for g in registry for t in g["tiers"])

    first_party = [m["paths"] for m in modules if m["name"] not in ("Vendored", "Documentation", "Development system")]
    first_party = [p.rstrip("/") for paths in first_party for p in paths if p.endswith("/")]
    todo = ctx.run(["git", "grep", "-c", "-E", r"\b(TODO|FIXME)\b", "--", *first_party], timeout=60)
    todo_total = sum(int(l.rsplit(":", 1)[1]) for l in todo["stdout"].splitlines() if ":" in l) \
        if todo["rc"] in (0, 1) else None
    analyzers = [n for n in (".clang-tidy", ".cppcheck", "cppcheck.cfg", ".clang-format", ".pre-commit-config.yaml",
                             ".github", ".gitlab-ci.yml", "Jenkinsfile") if (ctx.root / n).exists()]

    return {
        "git": git,
        "environment": {"os": f"{platform.system()} {platform.release()}", "machine": platform.machine(),
                        "host": platform.node(), "python": platform.python_version(), "cpus": os.cpu_count()},
        "modules": {m: dict(c) for m, c in sorted(per_module.items())},
        "projects": projects,
        "documentation": {"docs_by_directory": dict(sorted(docs_by_dir.items())),
                          "architecture_documents": len(arch_docs),
                          "architecture_current_references": len(current),
                          "architecture_not_listed_as_current": [p for p in arch_docs if p not in current]},
        "requirements": {"documents": len(model["documents"]), "ids": len(model["defined"]),
                         "namespaces": len(model["prefixes"]), "per_document": req_per_doc},
        "tests": {"registered_groups": len(registry), "by_tier": dict(sorted(tiers.items())),
                  "test_source_files": len(model["tests"])},
        "build": build,
        "static": {"todo_fixme": todo_total, "analyzer_or_ci_config": analyzers,
                   "warnings_as_errors": build["werror"]},
    }


def _first_line(out: dict) -> str | None:
    return out["stdout"].splitlines()[0].strip() if out["rc"] == 0 and out["stdout"] else None
