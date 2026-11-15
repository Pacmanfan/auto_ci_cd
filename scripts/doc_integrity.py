#!/usr/bin/env python3
"""
Continuous documentation integrity check.

Detects:
  broken links [text](path)                       BROKEN_LINK (FAIL by default)
  stale paths (doc_integrity.stale_paths)         STALE_PATH
  obsolete terminology (doc_integrity.obsolete_terms)  OBSOLETE_TERM
  duplicate authority ("sole normative" outside docs/requirements)  DUPLICATE_AUTHORITY
Which codes FAIL is configured in devsys.json (doc_integrity.fail_codes);
exit 1 when any finding FAILs.

Scope (REQ-DEVOPS-026, ADR-000184): the repository's own documentation — every
*.md file git tracks or would track (not ignored), so third-party trees
(node_modules), scratch copies and build-only worktrees (tmp/) and
build output (build/) are never reported as the repository's findings, while a
new document that is not committed yet is. docs/history is historical and
skipped. Without git (an exported tree) the same trees are skipped by name.

Usage:
  python3 scripts/doc_integrity.py
  python3 scripts/doc_integrity.py --json
"""
import re
import subprocess
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devsys_config as cfgmod  # noqa: E402

ROOT = cfgmod.repo_root()

# Fallback when git is unavailable: directories that are never the
# repository's documentation.
NOT_REPOSITORY_DOCS = ("node_modules", ".git")
NOT_REPOSITORY_DOC_PREFIXES = ("tmp/", "build/")
REPOSITORY_DOC_EXCEPTIONS = ("build/README.md",)


def _git_documentation_files(root):
    """The git view of root's documentation, or None when git cannot give it.

    git answers only for the top level of a work tree: a root nested inside
    another repository (a tree under the gitignored tmp/, WORK-000304)
    would otherwise be read with the enclosing repository's index and ignore
    rules, which list none of its files."""
    try:
        top = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                             check=True, capture_output=True).stdout.decode("utf-8", "surrogateescape").strip()
        if Path(top).resolve() != Path(root).resolve():
            return None
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "*.md"],
            check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    files = [root / p for p in out.stdout.decode("utf-8", "surrogateescape").split("\0") if p]
    # --cached also lists tracked files deleted from the working tree.
    return [f for f in files if f.is_file()]


def documentation_files(root=ROOT):
    """The *.md files that are the repository's documentation (see Scope)."""
    files = _git_documentation_files(root)
    if files is None:
        files = []
        for md in root.rglob("*.md"):
            rel = md.relative_to(root).as_posix()
            if any(part in NOT_REPOSITORY_DOCS for part in md.relative_to(root).parts):
                continue
            if rel.startswith(NOT_REPOSITORY_DOC_PREFIXES) and rel not in REPOSITORY_DOC_EXCEPTIONS:
                continue
            files.append(md)
    return sorted(f for f in files if not f.relative_to(root).as_posix().startswith("docs/history/"))

LINK_RE = re.compile(r"\[.*?\]\((.*?)\)")

def _level(code: str, root) -> str:
    return "FAIL" if code in set(cfgmod.get("doc_integrity.fail_codes", root, [])) else "WARN"


def check(root=ROOT):
    """Findings as (level, code, message). The level of each code is policy:
    doc_integrity.fail_codes (default: BROKEN_LINK) FAIL, the rest WARN
    (CI_CD_ROADMAP PRO-06). Stale paths and obsolete terms come from
    doc_integrity.stale_paths / obsolete_terms."""
    findings = []
    stale_paths = list(cfgmod.get("doc_integrity.stale_paths", root, []))
    obsolete = list(cfgmod.get("doc_integrity.obsolete_terms", root, []))
    for md in documentation_files(root):
        try:
            txt = md.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            # Moved or removed after the listing, e.g. a request another
            # agent transitioned meanwhile: nothing left here to check.
            continue
        low = txt.lower()
        historical = "migration" in low or "historical" in low
        # broken markdown links
        for m in LINK_RE.finditer(txt):
            link = m.group(1).strip()
            if not link:
                continue
            # skip external, anchors, mailto
            if link.startswith(("http://", "https://", "mailto:", "#")):
                continue
            # strip anchor and query
            path_part = link.split("#")[0].split("?")[0].strip()
            if not path_part or "://" in path_part:
                continue
            candidate = (md.parent / path_part).resolve()
            alt = (Path(root) / path_part).resolve()
            if not candidate.exists() and not alt.exists():
                # ignore placeholder pending, example paths
                if path_part.startswith("pending") or "example" in path_part:
                    continue
                findings.append((_level("BROKEN_LINK", root), "BROKEN_LINK",
                                 f"{md.relative_to(root)} -> {link} (resolved {candidate} / {alt} missing)"))

        # stale paths: former documentation locations cited outside a
        # migration / historical context
        for stale in stale_paths:
            if stale in txt and not historical:
                findings.append((_level("STALE_PATH", root), "STALE_PATH",
                                 f"{md.relative_to(root)} references deleted {stale}"))

        # obsolete terminology outside historical / evidence documents
        for term in obsolete:
            if re.search(rf"\b{re.escape(term)}\b", txt) and "migration" not in low and "removed" not in low \
                    and "docs/history" not in str(md) and "evidence" not in str(md):
                findings.append((_level("OBSOLETE_TERM", root), "OBSOLETE_TERM",
                                 f"{md.relative_to(root)} mentions {term} outside historical context"))

        # duplicate authority: search for sole normative outside docs/requirements
        if re.search(r"sole normative", txt, re.IGNORECASE) and "docs/requirements" not in str(md):
            findings.append((_level("DUPLICATE_AUTHORITY", root), "DUPLICATE_AUTHORITY",
                             f"{md.relative_to(root)} claims sole normative outside docs/requirements"))

    return findings


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    findings = check()
    fails = [f for f in findings if f[0]=="FAIL"]
    warns = [f for f in findings if f[0]=="WARN"]
    if args.json:
        print(json.dumps([{"level":l,"code":c,"msg":m} for l,c,m in findings], indent=2))
    else:
        if not findings:
            print("DOC INTEGRITY: PASS")
        else:
            for lvl, code, msg in findings:
                print(f"{lvl} {code}: {msg}")
            print(f"\nSummary: {len(fails)} FAIL, {len(warns)} WARN")
    sys.exit(1 if fails else 0)

if __name__ == "__main__":
    main()
