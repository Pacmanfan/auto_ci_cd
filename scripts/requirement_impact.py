#!/usr/bin/env python3
"""
Requirement change impact analysis.

When a requirement changes, identify potentially affected:
  implementation, tests, work items, architecture, UI, automation, protocols,
  configuration, persistence, generated interfaces, documentation.

Usage:
  python3 scripts/requirement_impact.py --requirement docs/requirements/development_operations.md
  python3 scripts/requirement_impact.py --requirement REQ-DEVOPS-006
  python3 scripts/requirement_impact.py --diff  # analyze git diff for requirement docs changed

Output: impact report (not auto-modify).
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devsys_config as cfgmod  # noqa: E402

ROOT = cfgmod.repo_root()

# Requirement domain keyword -> grep patterns of the areas it likely affects
# (devsys.json requirement_impact.domains; the "development" entry is the
# framework's own).
IMPACT_MAP = dict(cfgmod.get("requirement_impact.domains", ROOT, {}))

def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""

def _grep_count(pattern: str):
    try:
        out = subprocess.check_output(["grep", "-r", "--include=*.cpp", "--include=*.h", "--include=*.md", "--include=*.py", "--include=*.proto", "-l", pattern, str(ROOT)], text=True, stderr=subprocess.DEVNULL)
        return [l.strip() for l in out.splitlines() if l.strip()]
    except subprocess.CalledProcessError:
        return []

def analyze_requirement(requirement_path: str):
    # requirement_path may be file or ID
    if requirement_path.startswith("REQ-"):
        # find owning doc
        owning = None
        for f in (ROOT/"docs"/"requirements").glob("*.md"):
            if requirement_path in _read(f):
                owning = f
                break
        if not owning:
            print(f"Requirement ID {requirement_path} not found in docs/requirements/*.md", file=sys.stderr)
            sys.exit(1)
        req_file = owning
        req_id = requirement_path
    else:
        req_file = ROOT / requirement_path if not Path(requirement_path).is_absolute() else Path(requirement_path)
        req_id = req_file.stem

    print(f"Impact report for {req_id} ({req_file.relative_to(ROOT) if req_file.exists() else req_file})")
    print("="*70)
    txt = _read(req_file) if req_file.exists() else ""
    # find requirement text snippet
    if txt:
        print(f"File lines: {len(txt.splitlines())}, size: {len(txt)} chars")
    # keyword-based impact
    lower = req_id.lower() + " " + (txt[:2000].lower() if txt else "")
    affected_keywords = []
    for kw, patterns in IMPACT_MAP.items():
        if kw in lower:
            affected_keywords.append((kw, patterns))
    if not affected_keywords:
        print("No keyword match — generic impact; manual review required.")
        affected_keywords = [("generic", ["tests", "docs"])]

    for kw, patterns in affected_keywords:
        print(f"\nKeyword '{kw}' likely affected:")
        for pat in patterns[:4]:
            hits = _grep_count(pat)
            if hits:
                print(f"  - {pat}: {len(hits)} files (e.g., {hits[:3]})")
            else:
                print(f"  - {pat}: no hits")

    # work items referencing this requirement
    print("\nWork items referencing requirement:")
    work_txt = _read(ROOT/"work"/"work.txt")
    for line in work_txt.splitlines():
        if req_id in line or req_file.name in line:
            print(f"  - {line.strip()}")

    # evidence
    print("\nEvidence referencing requirement:")
    ev_txt = _read(ROOT/"evidence"/"evidence.txt")
    for line in ev_txt.splitlines():
        if req_id in line or req_file.name in line:
            print(f"  - {line.strip()}")

    # docs linking
    print("\nDocumentation linking to requirement:")
    for f in ROOT.rglob("*.md"):
        if ".git" in str(f):
            continue
        if req_id in _read(f) and f != req_file:
            print(f"  - {f.relative_to(ROOT)}")

    # git diff affected files if requirement file changed
    try:
        diff_files = subprocess.check_output(["git", "diff", "--name-only", "HEAD", "--", str(req_file)], cwd=ROOT, text=True, stderr=subprocess.DEVNULL)
        if diff_files.strip():
            print(f"\nUncommitted changes in {req_file}:")
            print(diff_files)
    except Exception:
        pass

    print("\nSuggested checks:")
    print("  [ ] implementation files listed above reviewed")
    print("  [ ] tests referencing requirement re-run")
    print("  [ ] work items updated")
    print("  [ ] architecture docs updated if needed")
    print("  [ ] generated interfaces regenerated if a wire schema is affected")
    print("  [ ] docs/README and subsystem READMEs updated")

def analyze_diff():
    try:
        diff = subprocess.check_output(["git", "diff", "--name-only", "HEAD"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL)
        changed = [l.strip() for l in diff.splitlines() if "docs/requirements/" in l]
        if not changed:
            # also check diff against origin/main or last commit?
            diff_cached = subprocess.check_output(["git", "diff", "--cached", "--name-only"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL)
            changed = [l.strip() for l in diff_cached.splitlines() if "docs/requirements/" in l]
        if not changed:
            print("No changed requirement docs in git diff.")
            return
        for f in changed:
            print(f"\n--- Changed requirement doc: {f} ---")
            analyze_requirement(f)
    except Exception as e:
        print(f"error analyzing diff: {e}", file=sys.stderr)
        sys.exit(1)

def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--requirement", help="requirement file or ID (e.g., docs/requirements/development_operations.md or REQ-DEVOPS-006)")
    g.add_argument("--diff", action="store_true", help="analyze git diff for changed requirement docs")
    args = ap.parse_args()
    if args.diff:
        analyze_diff()
    else:
        analyze_requirement(args.requirement)

if __name__ == "__main__":
    main()
