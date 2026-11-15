#!/usr/bin/env python3
"""Work-state consistency checks of scripts/drift_detect.py (REQ-DEVOPS-044, ADR-000155).

Each case builds a throw-away repository tree under tmp/ and runs the
REAL validator against it through AGENT_PROCESS_ROOT (the convention of
test_drift_id_collisions.py), so these are end-to-end runs of drift_detect.

No case asserts on the repository itself: other sessions' in-flight records
share the checkout, so such a case failed whenever one of them was mid-change
(2026-09-23 review). The repository's state is the drift audit's job
(scripts/drift_detect.py in the request workflow), not a unit test's.

Run: python3 -m unittest discover -s scripts/tests -v
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
REPO = SCRIPTS.parent
DRIFT = SCRIPTS / "drift_detect.py"


def run_drift(root: Path):
    env = dict(os.environ, AGENT_PROCESS_ROOT=str(root))
    r = subprocess.run([sys.executable, str(DRIFT), "--json"], capture_output=True, text=True, env=env)
    try:
        return r.returncode, json.loads(r.stdout)
    except ValueError:  # pragma: no cover
        raise AssertionError(f"drift_detect produced no JSON (exit {r.returncode}):\n{r.stdout}\n{r.stderr}")


def of(findings, code):
    return [f for f in findings if f["code"] == code]


class Tree:
    def __init__(self):
        base = REPO / "tmp" / "drift-work-state-tests"
        base.mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(prefix="ws-", dir=str(base))
        self.path = Path(self._tmp.name)
        for d in ("work/active", "work/completed", "work/needs-review", "evidence", "docs/decisions", "docs/requirements",
                  "requests/inbox", "requests/processing", "requests/completed", "requests/rejected",
                  ".agent/state/claims"):
            (self.path / d).mkdir(parents=True, exist_ok=True)
        self.work_rows, self.ver_rows = [], []

    def cleanup(self):
        self._tmp.cleanup()

    def request(self, state, name):
        (self.path / "requests" / state / name).write_text(f"# Request\n\n**Status:** {state}\n")

    def work(self, state, wid, header, request_path=None, listed=None):
        rec = self.path / "work" / state / f"{wid}-synthetic.md"
        req = f"**Request:** `{request_path}`\n" if request_path else ""
        rec.write_text(f"# {wid}\n\n**Status:** {header}\n{req}**Requirements:** REQ-X\n")
        self.work_rows.append(f"{wid} | {listed or state} | synthetic | {state}/{rec.name} | REQ-X | VER-000001")
        (self.path / "evidence" / f"VER-000001-{wid}.md").write_text(f"# evidence for {wid}\n")

    def claim(self, request_id, hours_old, state="processing"):
        beat = (datetime.now(timezone.utc) - timedelta(hours=hours_old)).isoformat()
        (self.path / ".agent/state/claims" / f"request-{request_id}.lock").write_text(json.dumps(
            {"kind": "request", "request_id": request_id, "state": state, "heartbeat_at": beat}))

    def write(self):
        (self.path / "work/work.txt").write_text("# Active Work Manifest\n\nFormat: `WORK-ID | status | ...`\n\n"
                                                 + "\n".join(self.work_rows) + "\n")
        (self.path / "evidence/evidence.txt").write_text("# Evidence\n\nFormat: `VER-ID | ...`\n\n"
                                                         + "\n".join(self.ver_rows) + "\n")
        (self.path / "docs/decisions/decisions.txt").write_text("# Decisions\n\nFormat: `ADR-ID | ...`\n")
        # Synthetic completed records carry no gate records; completion
        # verification is covered by test_lifecycle_hardening.py.
        (self.path / "devsys.json").write_text(json.dumps({"drift": {"unverified_completion": "OFF"}}))
        (self.path / "docs/requirements/requirements.txt").write_text("# Requirements\n")
        return self.path


class WorkStateTests(unittest.TestCase):
    def setUp(self):
        self.t = Tree()
        self.addCleanup(self.t.cleanup)

    def test_consistent_records_are_clean(self):
        self.t.request("completed", "r1.md")
        self.t.work("completed", "WORK-000001", "completed (2026-09-21)", "requests/processing/r1.md")
        code, f = run_drift(self.t.write())
        for c in ("WORK_STATE_MISMATCH", "WORK_REQUEST_MISSING", "WORK_OPEN_REQUEST_CLOSED", "STALE_CLAIM"):
            self.assertEqual(of(f, c), [], c)
        self.assertEqual(code, 0)

    def test_directory_disagrees_with_header(self):
        self.t.work("completed", "WORK-000002", "active")
        code, f = run_drift(self.t.write())
        self.assertEqual([x["level"] for x in of(f, "WORK_STATE_MISMATCH")], ["FAIL"])
        self.assertEqual(code, 1)

    def test_directory_disagrees_with_work_txt(self):
        self.t.work("active", "WORK-000003", "active", listed="completed")
        code, f = run_drift(self.t.write())
        m = of(f, "WORK_STATE_MISMATCH")
        self.assertEqual(len(m), 1)
        self.assertIn("work/work.txt says 'completed'", m[0]["msg"])

    def test_open_work_with_closed_request(self):
        self.t.request("completed", "r4.md")
        self.t.request("rejected", "r5.md")
        self.t.work("active", "WORK-000004", "active", "requests/processing/r4.md")
        self.t.work("needs-review", "WORK-000005", "needs-review", "requests/inbox/r5.md")
        _, f = run_drift(self.t.write())
        m = of(f, "WORK_OPEN_REQUEST_CLOSED")
        self.assertEqual(sorted(x["level"] for x in m), ["WARN", "WARN"])
        self.assertTrue(any("request is completed" in x["msg"] for x in m))
        self.assertTrue(any("request is rejected" in x["msg"] for x in m))

    def test_request_moved_between_states_is_not_drift(self):
        # Written while processing, now completed: the same request.
        self.t.request("completed", "r6.md")
        self.t.work("completed", "WORK-000006", "completed", "requests/processing/r6.md")
        _, f = run_drift(self.t.write())
        self.assertEqual(of(f, "WORK_REQUEST_MISSING"), [])

    def test_request_that_exists_nowhere(self):
        self.t.work("completed", "WORK-000007", "completed", "requests/processing/gone.md")
        code, f = run_drift(self.t.write())
        self.assertEqual([x["level"] for x in of(f, "WORK_REQUEST_MISSING")], ["FAIL"])
        self.assertEqual(code, 1)

    def test_stale_claim_is_reported_live_claim_is_not(self):
        self.t.claim("REQ-CHAT-1", hours_old=3.0)
        self.t.claim("REQ-CHAT-2", hours_old=0.1)
        self.t.claim("REQ-CHAT-3", hours_old=9.0, state="completed")
        _, f = run_drift(self.t.write())
        m = of(f, "STALE_CLAIM")
        self.assertEqual(len(m), 1)
        self.assertIn("REQ-CHAT-1", m[0]["msg"])
        self.assertEqual(m[0]["level"], "WARN")

    def test_evidence_status_outside_state_model(self):
        self.t.ver_rows += ["VER-000010 | PASS with classified failures (x) | 2026-09-21 | W | a.md",
                            "VER-000011 | INSUFFICIENT_EVIDENCE (draft, handed off) | 2026-09-21 | W | b.md",
                            "VER-000012 | pre-existing | 2026-09-21 | W | c.md"]
        _, f = run_drift(self.t.write())
        stale = [x for x in of(f, "STALE_STATUS") if "evidence.txt" in x["msg"]]
        self.assertEqual(len(stale), 1)
        self.assertIn("VER-000011", stale[0]["msg"])


if __name__ == "__main__":
    unittest.main()
