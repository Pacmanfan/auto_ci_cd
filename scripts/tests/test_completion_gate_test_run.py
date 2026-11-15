#!/usr/bin/env python3
"""Test-run linkage of scripts/completion_gate.py (REQ-DEVOPS-046, ADR-000186,
WORK-000305).

A work record (or its evidence) names scripts/run_tests.py runs on a
**Test-Run:** line. The gate fails a linked run that failed, and a request
whose registered code changed after its latest linked run; it warns when no
run is linked. Runs the REAL gate against a throw-away tree through
AGENT_PROCESS_ROOT.

Run: python3 -m unittest scripts.tests.test_completion_gate_test_run -v
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

# Not the calling session's agent identity, and no coordination station (see devsys_testlib).
for _name in ("AGENT_ID", "AGENT_OWNER_PID"):
    __import__("os").environ.pop(_name, None)
__import__("os").environ["DEVSYS_COORDINATION_AUTOSTART"] = "0"
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
REPO = SCRIPTS.parent
GATE = SCRIPTS / "completion_gate.py"
sys.path.insert(0, str(SCRIPTS))
import run_tests  # noqa: E402

REQUEST = "2026-09-23-example-req-chat-20260923-999.md"
RUN_ID = "20260923T120000-4242"
BODY = """# WORK-000001

**Status:** active
**Request:** requests/processing/2026-09-23-example-req-chat-20260923-999.md
**Requirements:** `docs/requirements/x.md` REQ-X
**Evidence:** VER-000001
{test_run}

Tests: unit tests added. Regression impact: none. Documentation updated. Traceability: REQ-X, WORK-000001.
"""


class TestRunLinkageTests(unittest.TestCase):
    def setUp(self):
        base = REPO / "tmp" / "gate-test-run-tests"
        base.mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(prefix="gate-", dir=str(base))
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        # Its own repository: claims resolve through agent_process.main_root(),
        # which would otherwise find the enclosing checkout's .agent/state.
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        for directory in ("work/active", "evidence", ".agent/state/claims", "src", "docs",
                          "build/Debug/test-results/runs"):
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        (self.root / "evidence/evidence.txt").write_text("VER-000001 | pass | 2026-09-23 | WORK-000001 | e.md\n")
        (self.root / "evidence/VER-000001-e.md").write_text(
            "**Status:** PASS\n**Requirements:** REQ-X\n**Work:** WORK-000001\n**Command:** x\nResult: PASS\n")
        (self.root / "src/code.cpp").write_text("int x = 1;\n")
        (self.root / "docs/page.md").write_text("# page\n")
        (self.root / "requests/processing").mkdir(parents=True, exist_ok=True)
        (self.root / f"requests/processing/{REQUEST}").write_text("# Request\n\n**Request ID:** REQ-CHAT-20260923-999\n"
                                                              "**Status:** processing\n")
        self.claim_files({"src/code.cpp": {}, "docs/page.md": {}})
        # Isolate the rule under test: the other completion rules are covered by
        # test_lifecycle_hardening.py (devsys.json overrides, ADR-000199).
        (self.root / "devsys.json").write_text(json.dumps({"completion": {
            "require_requirement": False, "require_decision_records": False,
            "require_drift_clean": False, "require_doc_integrity_clean": False}}))

    def claim_files(self, files):
        claim = {"kind": "request", "target": REQUEST, "request_id": "REQ-CHAT-20260923-999", "state": "processing",
                 "files": files}
        (self.root / f".agent/state/claims/request-{REQUEST}.lock").write_text(json.dumps(claim))

    def record_run(self, classification="PASS", fingerprint=None, tier="t2"):
        if fingerprint is None:
            fingerprint, _ = run_tests.request_fingerprint(Path(REQUEST), self.root)
        (self.root / f"build/Debug/test-results/runs/{RUN_ID}.json").write_text(json.dumps({
            "run_id": RUN_ID, "classification": classification, "tier": tier, "scope": "affected",
            "request": REQUEST, "request_fingerprint": fingerprint, "started_at": "2026-09-23T12:00:00+00:00"}))

    def gate(self, test_run_line=f"**Test-Run:** {RUN_ID}"):
        (self.root / "work/active/WORK-000001-x.md").write_text(BODY.format(test_run=test_run_line))
        env = dict(os.environ, AGENT_PROCESS_ROOT=str(self.root))
        r = subprocess.run([sys.executable, str(GATE), "--work", "WORK-000001", "--json"],
                           capture_output=True, text=True, env=env)
        return r.returncode, r.stdout

    def test_a_passing_run_on_the_current_code_passes(self):
        self.record_run()
        code, out = self.gate()
        self.assertEqual(code, 0, out)
        self.assertIn("fingerprint match", out)

    def test_code_changed_after_the_run_fails(self):
        self.record_run()
        (self.root / "src/code.cpp").write_text("int x = 2;\n")
        code, out = self.gate()
        self.assertEqual(code, 1, out)
        self.assertIn("changed after test run", out)

    def test_documentation_changed_after_the_run_does_not_count(self):
        self.record_run()
        (self.root / "docs/page.md").write_text("# page, edited after the run\n")
        code, out = self.gate()
        self.assertEqual(code, 0, out)

    def test_a_failed_linked_run_fails(self):
        self.record_run(classification="FAIL")
        code, out = self.gate()
        self.assertEqual(code, 1, out)
        self.assertIn("a linked run failed", out)

    def test_no_linked_run_fails_when_the_request_changed_code(self):
        # Was a warning: prose-only evidence for a code change (CI_CD_ROADMAP COR-02).
        code, out = self.gate(test_run_line="")
        self.assertEqual(code, 1, out)
        self.assertIn("No recorded test run linked", out)

    def test_documentation_only_request_needs_no_run(self):
        self.claim_files({"docs/page.md": {}})
        code, out = self.gate(test_run_line="")
        self.assertEqual(code, 0, out)
        self.assertIn("no test run required", out)

    def test_an_empty_run_fails(self):
        self.record_run(classification="EMPTY")
        code, out = self.gate()
        self.assertEqual(code, 1, out)
        self.assertIn("executed nothing", out)

    def test_an_interrupted_run_fails(self):
        self.record_run(classification="INTERRUPTED")
        code, out = self.gate()
        self.assertEqual(code, 1, out)
        self.assertIn("was interrupted", out)

    def test_a_lane_run_does_not_cover_the_minimum_tier(self):
        self.record_run(tier="live")
        code, out = self.gate()
        self.assertEqual(code, 1, out)
        self.assertIn("includes tier t2", out)

    def test_the_evidence_copy_of_a_record_is_used_when_no_build_tree_has_it(self):
        self.record_run(classification="FAIL")
        build = self.root / f"build/Debug/test-results/runs/{RUN_ID}.json"
        (self.root / "evidence/VER-000001").mkdir()
        build.rename(self.root / f"evidence/VER-000001/run-{RUN_ID}.json")
        code, out = self.gate()
        self.assertNotEqual(code, 0, out)       # found, and its failure counts
        self.assertNotIn("no record under build", out)

    def test_a_missing_record_fails(self):
        # Was a warning: an unverifiable run proves nothing (CI_CD_ROADMAP BLD-02).
        code, out = self.gate()
        self.assertEqual(code, 1, out)
        self.assertIn("no record in the build tree", out)


if __name__ == "__main__":
    unittest.main()
