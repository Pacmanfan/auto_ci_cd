#!/usr/bin/env python3
"""Protected-artifact enforcement (REQ-DEVOPS-023, ADR-000185).

Runs the REAL completion gate and the REAL invariant_check against a
throw-away git tree through AGENT_PROCESS_ROOT. The tree is its own repository
(``git init``) so the claim store resolves inside it, not to this checkout's
(the convention of test_process_concurrency.py).

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
INVARIANT = SCRIPTS / "invariant_check.py"
CODE = "PROTECTED_FILES_TOUCHED_WITHOUT_PROCESS_CHANGE"

RECORD = """# {wid}

**Status:** active
**Type:** {type}
{decision}**Request:** `requests/processing/{req}.md`
**Requirements:** `docs/requirements/x.md` REQ-X
**Evidence:** VER-000001

Tests: unit tests added. Regression impact: none. Documentation updated. Traceability: REQ-X, {wid}.

- [x] Phase 1
"""


class Tree:
    def __init__(self):
        base = REPO / "tmp" / "process-protection-tests"
        base.mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(prefix="pp-", dir=str(base))
        self.root = Path(self._tmp.name)
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.root)], check=True)
        for d in ("work/active", "evidence", "requests/processing", "requests/completed", ".agent/state/claims"):
            (self.root / d).mkdir(parents=True, exist_ok=True)
        (self.root / "evidence/VER-000001-e.md").write_text(
            "**Status:** PASS\n**Requirements:** REQ-X\n**Work:** all\n**Command:** x\nResult: PASS\n")
        self.rows = []
        # Isolate the rule under test: the other completion rules are covered by
        # test_lifecycle_hardening.py (devsys.json overrides, ADR-000199).
        (self.root / "devsys.json").write_text(json.dumps({"completion": {
            "require_requirement": False, "require_decision_records": False, "require_test_run": False,
            "require_drift_clean": False, "require_doc_integrity_clean": False}}))

    def cleanup(self):
        self._tmp.cleanup()

    def request(self, req, rtype, paths, state="processing"):
        d = self.root / "requests" / state
        (d / f"{req}.md").write_text(f"# Request\n\n**Type:** {rtype}\n**Status:** {state}\n")
        (d / f"{req}.json").write_text(json.dumps({"request_id": f"REQ-CHAT-{req}", "type": rtype}))
        (self.root / ".agent/state/claims" / f"request-{req}.md.lock").write_text(json.dumps({
            "kind": "request", "target": f"{req}.md", "request_id": f"REQ-CHAT-{req}", "state": state,
            "request_path": f"requests/{state}/{req}.md",
            "files": {p: {"mode": "exclusive"} for p in paths}}))

    def work(self, wid, req, wtype, adr=True):
        decision = "**Decision:** ADR-000001\n" if adr else ""
        (self.root / "work/active" / f"{wid}-x.md").write_text(
            RECORD.format(wid=wid, type=wtype, decision=decision, req=req))
        self.rows.append(f"VER-000001 | pass | 2026-09-23 | {wid} | VER-000001-e.md")
        (self.root / "evidence/evidence.txt").write_text("\n".join(self.rows) + "\n")


def run(script, root, *args):
    env = dict(os.environ, AGENT_PROCESS_ROOT=str(root))
    r = subprocess.run([sys.executable, str(script), *args, "--json"], capture_output=True, text=True, env=env)
    try:
        return r.returncode, json.loads(r.stdout)
    except ValueError:  # pragma: no cover
        raise AssertionError(f"{script.name} produced no JSON (exit {r.returncode}):\n{r.stdout}\n{r.stderr}")


class GateProtectedArtifacts(unittest.TestCase):
    def setUp(self):
        self.t = Tree()
        self.addCleanup(self.t.cleanup)

    def test_protected_artifact_in_ordinary_work_fails(self):
        self.t.request("r1", "FEATURE", ["AGENTS.md", "src/x.cpp"])
        self.t.work("WORK-000001", "r1", "feature")
        code, out = run(GATE, self.t.root, "--work", "WORK-000001")
        self.assertEqual(code, 1, out)
        self.assertTrue(any("protected artifact(s) AGENTS.md" in f for f in out["fails"]), out)

    def test_process_change_with_adr_passes(self):
        self.t.request("r2", "PROCESS-CHANGE", ["docs/process/state-model.md"])
        self.t.work("WORK-000002", "r2", "process-change")
        code, out = run(GATE, self.t.root, "--work", "WORK-000002")
        self.assertEqual(code, 0, out)
        self.assertTrue(any("changed as a process change" in p for p in out["passes"]), out)

    def test_process_change_without_adr_fails(self):
        self.t.request("r3", "PROCESS-CHANGE", ["docs/process/escalation.md"])
        self.t.work("WORK-000003", "r3", "process-change", adr=False)
        code, out = run(GATE, self.t.root, "--work", "WORK-000003")
        self.assertEqual(code, 1, out)

    def test_no_protected_artifact_registered_passes(self):
        self.t.request("r4", "FEATURE", ["src/x.cpp"])
        self.t.work("WORK-000004", "r4", "feature")
        code, out = run(GATE, self.t.root, "--work", "WORK-000004")
        self.assertEqual(code, 0, out)
        self.assertIn("Protected artifacts: none registered by the request", out["passes"])

    def test_released_claim_has_nothing_to_check(self):
        # The request completed and its claim was released: nothing registered to check.
        (self.t.root / "requests/completed/gone.md").write_text("# Request\n\n**Type:** FEATURE\n**Status:** completed\n")
        self.t.work("WORK-000005", "gone", "feature")
        code, out = run(GATE, self.t.root, "--work", "WORK-000005")
        self.assertEqual(code, 0, out)


class InvariantProtectedArtifacts(unittest.TestCase):
    def setUp(self):
        self.t = Tree()
        self.addCleanup(self.t.cleanup)

    def findings(self, *args):
        _, out = run(INVARIANT, self.t.root, *args)
        return [f for f in out["fails"] if CODE in f], [w for w in out["warns"] if CODE in w]

    def test_ordinary_request_holding_a_protected_artifact(self):
        self.t.request("r1", "BUG", ["docs/requirements/development_operations.md"])
        fails, warns = self.findings()
        self.assertEqual((len(fails), len(warns)), (0, 1), warns)
        self.assertIn("REQ-CHAT-r1 (BUG)", warns[0])
        fails, warns = self.findings("--strict")
        self.assertEqual((len(fails), len(warns)), (1, 0), fails)

    def test_process_change_request_is_not_reported(self):
        self.t.request("r2", "PROCESS-CHANGE", ["AGENTS.md"])
        self.assertEqual(self.findings("--strict"), ([], []))

    def test_only_processing_claims_are_checked(self):
        self.t.request("r3", "FEATURE", ["AGENTS.md"], state="completed")
        self.assertEqual(self.findings("--strict"), ([], []))


if __name__ == "__main__":
    unittest.main()
