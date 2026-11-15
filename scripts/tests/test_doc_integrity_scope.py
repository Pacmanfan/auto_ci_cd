#!/usr/bin/env python3
"""scripts/doc_integrity.py audits the repository's own documentation only
(REQ-DEVOPS-026, ADR-000184, WORK-000303, REQ-AUTO-20260923-004).

A local `npm ci` in frontend/ adds hundreds of third-party READMEs under
frontend/node_modules, and build-only worktrees under tmp/ add a second
copy of docs/: the audit reported thousands of findings that were not the
repository's. The scope is now "every *.md git tracks or would track"; without
git the same trees are skipped by name.

Run: python3 -m unittest scripts.tests.test_doc_integrity_scope -v
"""
import importlib.util
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("doc_integrity", REPO / "scripts" / "doc_integrity.py")
doc_integrity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(doc_integrity)

BROKEN = "[missing](does-not-exist.md)\n"


def write(root: Path, rel: str, text: str = BROKEN) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class Tree:
    """A small repository: documentation that belongs to it, and trees that do not."""

    def __init__(self, git: bool, parent: Path | None = None):
        self.root = Path(tempfile.mkdtemp(prefix="doc_integrity_scope_", dir=str(parent) if parent else None))
        write(self.root, ".gitignore", "/build/*\n!/build/README.md\ntmp/\n")
        write(self.root, "frontend/.gitignore", "/node_modules\n")
        write(self.root, "docs/tracked.md")                              # tracked
        write(self.root, "build/README.md")                              # tracked, under build/
        write(self.root, "docs/history/old.md")                          # historical
        write(self.root, "frontend/node_modules/pkg/README.md")             # third party
        write(self.root, "tmp/wt/docs/tracked.md")               # scratch worktree copy
        write(self.root, "build/dist/linux-x86_64/app/docs/x.md")    # build output
        if git:
            run = lambda *a: subprocess.run(["git", "-C", str(self.root), *a], check=True, capture_output=True)
            run("init", "-q")
            run("add", ".gitignore", "frontend/.gitignore", "docs/tracked.md", "docs/history/old.md")
            run("add", "-f", "build/README.md")
            run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "fixture")
        write(self.root, "docs/new_not_committed.md")                    # untracked, not ignored

    def scanned(self):
        return sorted(p.relative_to(self.root).as_posix() for p in doc_integrity.documentation_files(self.root))

    def reported(self):
        return sorted({msg.split(" -> ")[0] for _, code, msg in doc_integrity.check(self.root) if code == "BROKEN_LINK"})

    def close(self):
        shutil.rmtree(self.root, ignore_errors=True)


EXPECTED = ["build/README.md", "docs/new_not_committed.md", "docs/tracked.md"]


class DocIntegrityScope(unittest.TestCase):
    def test_git_scope_is_the_repository_documentation(self):
        tree = Tree(git=True)
        try:
            self.assertEqual(tree.scanned(), EXPECTED)
            self.assertEqual(tree.reported(), EXPECTED)
        finally:
            tree.close()

    def test_without_git_the_same_trees_are_skipped(self):
        tree = Tree(git=False)
        try:
            self.assertEqual(tree.scanned(), EXPECTED)
        finally:
            tree.close()

    def test_tree_nested_in_another_repository_is_its_own_tree(self):
        # Under this repository's gitignored tmp/, git would answer
        # for the enclosing repository and list nothing (WORK-000304: the case
        # above failed whenever TMPDIR pointed there).
        parent = REPO / "tmp" / "doc-integrity-scope-tests"
        parent.mkdir(parents=True, exist_ok=True)
        tree = Tree(git=False, parent=parent)
        try:
            self.assertEqual(tree.scanned(), EXPECTED)
        finally:
            tree.close()

    def test_this_repository_never_reports_node_modules_or_scratch(self):
        for path in doc_integrity.documentation_files():
            rel = path.relative_to(REPO).as_posix()
            with self.subTest(path=rel):
                self.assertNotIn("node_modules", path.parts)
                self.assertFalse(rel.startswith("tmp/"))
                self.assertFalse(rel.startswith("docs/history/"))


if __name__ == "__main__":
    unittest.main()
