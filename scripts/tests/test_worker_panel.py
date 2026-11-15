#!/usr/bin/env python3
"""The Qt Worker Control Panel (tools/panel/) loads its .ui file and shows
representative worker and request state through the Worker Orchestrator's API
(WORKER-011). It runs headless (QT_QPA_PLATFORM=offscreen); skipped when
PySide6 is not installed.

Run: python3 -m unittest scripts.tests.test_worker_panel -v
"""
import json
import os
import signal
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from devsys_testlib import FRAMEWORK, Project  # noqa: E402
from test_workers import FAKE, worker_config  # noqa: E402

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6 import QtWidgets
    HAVE_QT = True
except ImportError:  # pragma: no cover - depends on the host
    HAVE_QT = False


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class WorkerPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        sys.path.insert(0, str(FRAMEWORK / "tools" / "panel"))
        sys.dont_write_bytecode = True
        import worker_panel
        cls.wp = worker_panel

    def setUp(self):
        self.p = Project(worker_config(), install=True)
        cfg = json.loads(self.p.read("devsys.json"))
        cfg["workers"]["agent"] = {"command": [sys.executable, str(self.p.root / FAKE)]}
        self.p.write("devsys.json", json.dumps(cfg, indent=2))
        self.p.commit_all("agent")
        # finished, working (a PARTIAL kept by worker-01) and queued requests
        self.done = self.p.request("Finished page [fake:complete]", "--priority", "HIGH")
        self.p.ok("devsys.py", "workers", "run", "worker-01", "--once", timeout=240)
        self.partial = self.p.request("Half-done page [fake:partial]")
        self.p.ok("devsys.py", "workers", "run", "worker-01", "--once", timeout=240)
        self.queued = self.p.request("Waiting page [fake:complete]", "--priority", "LOW")
        self.panel = self.wp.WorkerPanel(self.p.root, auto_refresh_ms=0)

    def tearDown(self):
        for w in ("worker-01", "worker-02"):
            f = self.p.root / ".agent/state/runtime/workers" / w / "worker.json"
            if f.exists():
                rec = json.loads(f.read_text())
                for pid in ((rec.get("process") or {}).get("pid"),
                            ((rec.get("active") or {}).get("session") or {}).get("pid")):
                    if pid:
                        try:
                            os.killpg(pid, signal.SIGKILL)
                        except (ProcessLookupError, PermissionError):
                            pass
        self.panel.window.close()
        self.p.close()

    def rid(self, name, state):
        return json.loads(self.p.read(f"requests/{state}/{Path(name).with_suffix('.json')}"))["request_id"]

    def test_the_ui_loads_and_shows_workers_and_requests(self):
        L = self.panel.labels
        self.assertEqual(L["maxWorkersLabel"].text(), "Max workers: 2")
        self.assertEqual(L["queuedLabel"].text(), "Queued: 1")
        self.assertEqual(L["workingLabel"].text(), "Working: 1")
        self.assertEqual(L["agentLabel"].text(), "Agent: configured")
        self.assertEqual(self.panel.workerList.count(), 2)
        card = self.panel.workerList.item(0).text()
        self.assertIn("worker-01", card)
        self.assertIn(self.rid(self.partial, "processing"), card)
        q = self.panel.queueTable
        self.assertEqual((q.rowCount(), q.item(0, 0).text(), q.item(0, 1).text()),
                         (1, "LOW", self.rid(self.queued, "inbox")))
        w = self.panel.workingTable
        self.assertEqual((w.rowCount(), w.item(0, 2).text()), (1, "worker-01"))
        f = self.panel.finishedTable
        self.assertIn(("completed", self.rid(self.done, "completed")),
                      [(f.item(r, 0).text(), f.item(r, 1).text()) for r in range(f.rowCount())])

    def test_selecting_a_worker_shows_its_details(self):
        self.panel.workerList.setCurrentRow(0)
        text = self.panel.detailsText.toPlainText()
        for part in ("worker-01", "process:", "ATTEMPT", "EVENTS"):
            self.assertIn(part, text)

    def test_priority_is_changed_through_the_framework(self):
        self.panel.queueTable.selectRow(0)
        self.panel.priorityCombo.setCurrentText("CRITICAL")
        self.panel.set_priority()
        side = json.loads(self.p.read(f"requests/inbox/{Path(self.queued).with_suffix('.json')}"))
        self.assertEqual(side["priority"], "CRITICAL")
        self.assertEqual(self.panel.queueTable.item(0, 0).text(), "CRITICAL")

    def test_controls_go_through_the_worker_api(self):
        self.panel.workerList.setCurrentRow(1)
        self.panel.act("disable")
        self.assertIn("(disabled)", self.panel.workerList.item(1).text())
        self.panel.act("start")
        self.assertIn("refused", self.panel.statusBar.currentMessage())
        self.panel.act("enable")
        os.environ["FAKE_SLOW_SECONDS"] = "30"
        try:
            self.panel.act("start")
            end = time.time() + 30
            while time.time() < end and "WORKING" not in self.panel.workerList.item(1).text():
                time.sleep(0.3)
                self.panel.refresh()
            card = self.panel.workerList.item(1).text()
            self.assertIn("WORKING", card)
            self.assertIn(self.rid(self.queued, "processing"), card)
            self.panel.act("stop_now")
            end = time.time() + 30
            while time.time() < end and "STOPPED" not in self.panel.workerList.item(1).text():
                time.sleep(0.3)
                self.panel.refresh()
            self.assertIn("STOPPED", self.panel.workerList.item(1).text())
        finally:
            os.environ.pop("FAKE_SLOW_SECONDS", None)


if __name__ == "__main__":
    unittest.main()
