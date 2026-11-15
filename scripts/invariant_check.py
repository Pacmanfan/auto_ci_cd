#!/usr/bin/env python3
"""
Architectural invariant checking (REQ-DEVOPS-022, ADR-000199).

Normative requirements vs invariants:
 - Normative requirements: observable behavior in docs/requirements/*.md
 - Architectural invariants: structural rules that must never silently
   disappear. They are declared by the project in devsys.json ("invariants"),
   listed in docs/audits/current-state.md and AGENTS.md, and checked here; the
   framework embeds no project-specific rule.

Rule kinds (docs/process/configuration.md "Architecture rules"):

  {"id": "core-no-ui", "kind": "forbid_pattern", "files": ["src/core/**"],
   "pattern": "#include\\\\s*<Q", "message": "core stays UI-free", "severity": "fail"}
      no line of a matching file may match ``pattern``
  {"id": "server-registered", "kind": "require_pattern", "files": ["src/**/*.cpp"],
   "pattern": "ServerRegistry::add", "min_matches": 1}
      at least ``min_matches`` matching lines across the files
  {"id": "lib-independent", "kind": "forbid_dependency", "from": ["lib/**"],
   "to": ["app/", "ui/"], "reference": "^\\\\s*(#include|import|from)\\\\b"}
      a reference line in ``from`` files may not name a ``to`` token
  {"id": "layering", "kind": "layers", "layers": [
      {"name": "app", "files": ["app/**"], "names": ["app/"]},
      {"name": "core", "files": ["core/**"], "names": ["core/"]},
      {"name": "lib", "files": ["lib/**"], "names": ["lib/"]}]}
      layers are listed top-down; a lower layer may not reference a higher one

``severity`` is "fail" (default) or "warn". Messages start with
``INVARIANT <id>`` so the audit can report each rule separately.

Also checked: process protection (REQ-DEVOPS-023, ADR-000185) — a processing
request that registered a protected artifact is a PROCESS-CHANGE request
(PROTECTED_FILES_TOUCHED_WITHOUT_PROCESS_CHANGE; FAIL with --strict).

With no rule configured the architecture part is NOT CONFIGURED (never a
vacuous PASS).

Usage:
  python3 scripts/invariant_check.py [--strict] [--json]
"""
import fnmatch
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devsys_config as cfgmod  # noqa: E402
import process_protection  # noqa: E402

ROOT = cfgmod.repo_root()
DEFAULT_REFERENCE = r"^\s*(#\s*include|import|from|use|require)\b"
_SKIP_DIRS = {".git", "node_modules", "tmp", "build", "__pycache__", ".agent", ".audit"}


def repository_files(root: Path) -> list:
    """Tracked or new, not ignored files (git); without git a walk that skips
    runtime, build and scratch trees. Paths are relative to ``root`` (the
    form of the rules' globs): installed as a project subdirectory, the
    project's files are listed as ``../src/a.c``."""
    import agent_process
    checkout = Path(agent_process.checkout_root(root))
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=root, capture_output=True, text=True)
    if top.returncode == 0 and Path(top.stdout.strip()).resolve() == checkout.resolve():
        out = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                             cwd=checkout, capture_output=True)
        listed = [agent_process.system_relative(root, p) for p in
                  out.stdout.decode("utf-8", "surrogateescape").split("\0") if p]
        return sorted(p for p in listed if (root / p).is_file())
    files = []
    for p in Path(root).rglob("*"):
        rel = p.relative_to(root)
        if p.is_file() and not (set(rel.parts) & _SKIP_DIRS):
            files.append(rel.as_posix())
    return sorted(files)


def glob_match(path: str, patterns) -> bool:
    for pattern in patterns or []:
        if fnmatch.fnmatchcase(path, pattern):
            return True
        if pattern.endswith("/**") and (path == pattern[:-3] or path.startswith(pattern[:-2])):
            return True
        if "**/" in pattern and fnmatch.fnmatchcase(path, pattern.replace("**/", "")):
            return True
    return False


def _lines(root: Path, rel: str):
    try:
        return (root / rel).read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []


def evaluate(rule: dict, root: Path, files: list) -> list:
    """Violation messages of one rule (empty = holds). A rule that names no
    files applies to the project's source roots (project.source_roots)."""
    rid, kind = rule["id"], rule["kind"]
    roots = [f"{r.rstrip('/')}/**" for r in cfgmod.get("project.source_roots", root, []) or []]
    if kind in ("forbid_pattern", "require_pattern") and not rule.get("files"):
        rule = dict(rule, files=roots)
    if kind == "forbid_dependency" and not rule.get("from"):
        rule = dict(rule, **{"from": roots})
    msg = rule.get("message", "")
    out = []
    if kind in ("forbid_pattern", "require_pattern"):
        rx = re.compile(rule["pattern"])
        hits = []
        for rel in (f for f in files if glob_match(f, rule.get("files", []))):
            for n, line in enumerate(_lines(root, rel), 1):
                if rx.search(line):
                    hits.append(f"{rel}:{n}")
        if kind == "forbid_pattern" and hits:
            out.append(f"INVARIANT {rid} violated: {msg + ': ' if msg else ''}{len(hits)} match(es) of "
                       f"/{rule['pattern']}/: {', '.join(hits[:5])}")
        if kind == "require_pattern" and len(hits) < int(rule.get("min_matches", 1)):
            out.append(f"INVARIANT {rid} violated: {msg + ': ' if msg else ''}/{rule['pattern']}/ found "
                       f"{len(hits)} time(s), at least {rule.get('min_matches', 1)} required")
    elif kind == "forbid_dependency":
        ref = re.compile(rule.get("reference", DEFAULT_REFERENCE))
        tokens = rule.get("to", [])
        hits = []
        for rel in (f for f in files if glob_match(f, rule.get("from", []))):
            for n, line in enumerate(_lines(root, rel), 1):
                if ref.search(line) and any(t in line for t in tokens):
                    hits.append(f"{rel}:{n}")
        if hits:
            out.append(f"INVARIANT {rid} violated: {msg + ': ' if msg else ''}references to "
                       f"{', '.join(tokens)}: {', '.join(hits[:5])}")
    elif kind == "layers":
        ref = re.compile(rule.get("reference", DEFAULT_REFERENCE))
        layers = rule.get("layers", [])
        for i, layer in enumerate(layers):
            higher = [t for upper in layers[:i] for t in upper.get("names", [])]
            if not higher:
                continue
            hits = []
            for rel in (f for f in files if glob_match(f, layer.get("files", []))):
                for n, line in enumerate(_lines(root, rel), 1):
                    if ref.search(line) and any(t in line for t in higher):
                        hits.append(f"{rel}:{n}")
            if hits:
                out.append(f"INVARIANT {rid} violated: layer {layer.get('name')} references a higher layer "
                           f"({', '.join(higher)}): {', '.join(hits[:5])}")
    else:
        out.append(f"INVARIANT {rid} invalid: unknown kind {kind!r}")
    return out


def check(strict=False):
    fails, warns = [], []
    rules = list(cfgmod.get("invariants", ROOT, []) or [])
    if rules:
        files = repository_files(ROOT)
        for rule in rules:
            try:
                violations = evaluate(rule, ROOT, files)
            except (KeyError, re.error) as e:
                violations = [f"INVARIANT {rule.get('id', '?')} invalid: {e}"]
            (warns if rule.get("severity") == "warn" else fails).extend(violations)

    # Process protection (REQ-DEVOPS-023, docs/process/process-protection.md):
    # a request that registered a protected artifact must be a process change.
    # Read from the claims, not the working tree: in a shared checkout a dirty
    # protected file may be another session's.
    for rid, rtype, paths in process_protection.processing_claims_touching_protected(ROOT):
        if rtype != process_protection.PROCESS_CHANGE_TYPE:
            (fails if strict else warns).append(
                f"PROTECTED_FILES_TOUCHED_WITHOUT_PROCESS_CHANGE: {rid} ({rtype or 'type unknown'}) registered "
                f"{', '.join(paths)}; a process change needs a PROCESS-CHANGE request, process-change work and an ADR")
    return fails, warns


def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict", action="store_true",
                    help="PROTECTED_FILES_TOUCHED_WITHOUT_PROCESS_CHANGE is a FAIL instead of a WARN")
    args = ap.parse_args()
    rules = list(cfgmod.get("invariants", ROOT, []) or [])
    fails, warns = check(strict=args.strict)
    if args.json:
        print(json.dumps({"fails": fails, "warns": warns, "rules": len(rules), "configured": bool(rules)}, indent=2))
    else:
        for f in fails:
            print(f"FAIL {f}")
        for w in warns:
            print(f"WARN {w}")
        if not rules:
            print("INVARIANTS: NOT CONFIGURED (no architecture rule in devsys.json \"invariants\")"
                  + (f"; {len(fails)} FAIL, {len(warns)} WARN from process protection" if fails or warns else ""))
        elif not fails and not warns:
            print(f"INVARIANTS: PASS ({len(rules)} rule(s))")
        else:
            print(f"\nSummary: {len(fails)} FAIL, {len(warns)} WARN ({len(rules)} rule(s))")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
