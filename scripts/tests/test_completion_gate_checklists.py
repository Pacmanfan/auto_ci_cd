#!/usr/bin/env python3
"""Open-item rule and record resolution of scripts/completion_gate.py
(REQ-DEVOPS-044, ADR-000155).

Runs the REAL gate against a throw-away work tree through AGENT_PROCESS_ROOT.

Run: python3 -m unittest discover -s scripts/tests -v
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
REPO = SCRIPTS.parent
GATE = SCRIPTS / "completion_gate.py"

BODY = """# {wid}

**Status:** active
**Requirements:** `docs/requirements/x.md` REQ-X
**Evidence:** VER-000001

Tests: unit tests added. Regression impact: none. Documentation updated. Traceability: REQ-X, {wid}.

{items}
"""


def run_gate(root: Path, wid: str):
    env = dict(os.environ, AGENT_PROCESS_ROOT=str(root))
    r = subprocess.run([sys.executable, str(GATE), "--work", wid, "--json"], capture_output=True, text=True, env=env)
    return r.returncode, r.stdout


class OpenItemTests(unittest.TestCase):
    def setUp(self):
        base = REPO / "tmp" / "gate-checklist-tests"
        base.mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(prefix="gate-", dir=str(base))
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        for state in ("active", "completed", "cancelled"):
            (self.root / "work" / state).mkdir(parents=True)
        (self.root / "evidence").mkdir(parents=True)
        works = " ".join(f"WORK-{n:06d}" for n in range(1, 20))
        (self.root / "evidence/evidence.txt").write_text(f"VER-000001 | pass | 2026-09-21 | {works} | e.md\n")
        (self.root / "evidence/VER-000001-e.md").write_text(
            "**Status:** PASS\n**Requirements:** REQ-X\n**Work:** WORK-000001..19\n**Command:** x\nResult: PASS\n")
        # Isolate the rule under test: the other completion rules are covered by
        # test_lifecycle_hardening.py (devsys.json overrides, ADR-000199).
        (self.root / "devsys.json").write_text(json.dumps({"completion": {
            "require_request_link": False, "require_requirement": False, "require_decision_records": False,
            "require_test_run": False, "require_drift_clean": False, "require_doc_integrity_clean": False}}))

    def record(self, wid, items, state="active", slug="x"):
        (self.root / "work" / state / f"{wid}-{slug}.md").write_text(BODY.format(wid=wid, items=items))

    def gate(self, wid, items, state="active"):
        self.record(wid, items, state)
        return run_gate(self.root, wid)

    def test_all_checked_passes(self):
        code, out = self.gate("WORK-000001", "- [x] Phase 1\n- [x] Phase 2\n")
        self.assertEqual(code, 0, out)
        self.assertIn("No open checklist items", out)

    def test_unchecked_item_fails(self):
        code, out = self.gate("WORK-000002", "- [x] Phase 1\n- [ ] Phase 2 - migrate the rest\n")
        self.assertEqual(code, 1, out)
        self.assertIn("open item", out)

    def test_partial_item_fails(self):
        code, out = self.gate("WORK-000003", "* [~] **Phase 3** (part)\n")
        self.assertEqual(code, 1, out)

    def test_not_done_phase_fails(self):
        code, out = self.gate("WORK-000004", "| 4 | Deep analysis quality | not done |\n")
        self.assertEqual(code, 1, out)

    def test_waived_item_passes(self):
        code, out = self.gate("WORK-000005", "- [ ] Live GNSS test (waived by the operator, 2026-09-21)\n"
                                             "- Phase 5: not done - waived, filed as REQ-CHAT-1\n")
        self.assertEqual(code, 0, out)

    def test_not_done_list_item_fails(self):
        code, out = self.gate("WORK-000006", "- Phase 4: not started\n")
        self.assertEqual(code, 1, out)

    # An item is a list item or a table row (2026-09-23 review).

    def test_narrative_not_done_passes(self):
        code, out = self.gate("WORK-000007", "## Deliberately not done\n\n"
                                             "The UI migration was not done here; it is out of this scope.\n\n"
                                             "- the creation printout confirms `Processing: NOT STARTED`\n")
        self.assertEqual(code, 0, out)

    def test_fenced_checklist_is_an_example(self):
        code, out = self.gate("WORK-000008", "```markdown\n- [ ] Phase 2\n| 3 | x | not done |\n```\n")
        self.assertEqual(code, 0, out)

    def test_waiver_in_quoted_code_does_not_waive(self):
        code, out = self.gate("WORK-000009", "- [ ] Document the `waived` keyword\n")
        self.assertEqual(code, 1, out)

    # A record is resolved by its file name, in any state (2026-09-23 review).

    def test_record_is_found_by_name_not_by_mention(self):
        # An active record that cites WORK-000010 must not stand in for it.
        self.record("WORK-000011", "- [x] Supersedes WORK-000010\n")
        code, out = self.gate("WORK-000010", "- [~] Phase 3 (part)\n", state="completed")
        self.assertEqual(code, 1, out)
        self.assertIn("completed/WORK-000010-x.md", out)

    def test_cancelled_record_is_found(self):
        # Found by name in any state directory; a cancelled record cannot
        # complete (docs/process/state-model.md §2).
        code, out = self.gate("WORK-000012", "- [x] Phase 1\n", state="cancelled")
        self.assertEqual(code, 1, out)
        self.assertIn("cancelled/WORK-000012-x.md", out)
        self.assertIn("only active, verification, completed work can complete", out)

    def test_duplicate_records_fail(self):
        self.record("WORK-000013", "- [x] Phase 1\n", state="completed", slug="other")
        code, out = self.gate("WORK-000013", "- [x] Phase 1\n")
        self.assertEqual(code, 1, out)
        self.assertIn("names 2 records", out)

    # Evidence is linked only where it is declared (WORK-000304). WORK-000020..
    # have no evidence.txt row.

    def evidence_record(self, wid, evidence_header=""):
        body = BODY.format(wid=wid, items="- [x] Phase 1\n").replace("**Evidence:** VER-000001\n", evidence_header)
        (self.root / "work/active" / f"{wid}-x.md").write_text(body)

    def test_evidence_that_only_mentions_the_work_is_not_linked(self):
        (self.root / "evidence/VER-000002-other.md").write_text(
            "**Status:** PASS\n**Requirements:** REQ-Y\n**Work:** WORK-000099\n**Command:** x\nResult: PASS\n\n"
            "Follow-up of WORK-000020.\n")
        self.evidence_record("WORK-000020")
        code, out = run_gate(self.root, "WORK-000020")
        self.assertEqual(code, 1, out)
        self.assertIn("No evidence linkage for WORK-000020", out)

    def test_evidence_named_by_the_work_record(self):
        (self.root / "evidence/VER-000003-x.md").write_text(
            "**Status:** PASS\n**Requirements:** REQ-X\n**Work:** see the record\n**Command:** x\nResult: PASS\n")
        self.evidence_record("WORK-000021", "**Evidence:** VER-000003\n")
        code, out = run_gate(self.root, "WORK-000021")
        self.assertEqual(code, 0, out)
        self.assertIn("VER-000003-x.md", out)

    def test_evidence_whose_work_header_names_the_work(self):
        (self.root / "evidence/VER-000004-y.md").write_text(
            "**Status:** PASS\n**Requirements:** REQ-X\n**Work:** WORK-000022\n**Command:** x\nResult: PASS\n")
        self.evidence_record("WORK-000022")
        code, out = run_gate(self.root, "WORK-000022")
        self.assertEqual(code, 0, out)
        self.assertIn("VER-000004-y.md", out)

    def test_mentioned_but_absent_record_is_not_found(self):
        self.record("WORK-000014", "- [x] Follow-up of WORK-000015\n")
        code, out = run_gate(self.root, "WORK-000015")
        self.assertEqual(code, 1, out)
        self.assertIn("work file not found", out)


if __name__ == "__main__":
    unittest.main()
