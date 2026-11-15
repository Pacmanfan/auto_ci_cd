#!/usr/bin/env python3
"""Worker Control Panel: a Qt window over the Worker Orchestrator's API.

The layout is ``worker_panel.ui`` (Qt Designer), loaded at run time. This
controller only presents what ``workers.pool.status`` / ``peek`` return and
forwards the buttons to ``workers.pool`` (start, stop, stop now, recover,
enable, disable) and ``agent_process.set_priority``; every rule about
requests, claims and workers lives in the development system, not here.

    python3 tools/panel/worker_panel.py [--root DIR]      (or tools/worker-panel)

Needs PySide6 (QtUiTools). With ``QT_QPA_PLATFORM=offscreen`` it runs without
a display (the tests do).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.dont_write_bytecode = True

import agent_process as proc  # noqa: E402
import devsys_config as cfgmod  # noqa: E402
from workers import pool  # noqa: E402

from PySide6 import QtCore, QtWidgets  # noqa: E402
from PySide6.QtUiTools import QUiLoader  # noqa: E402

UI_FILE = HERE / "worker_panel.ui"
STATUS_MARK = {"working": "●", "idle": "○", "stopped": "■", "stopping": "◐"}


def _fmt_seconds(s) -> str:
    if s is None:
        return "-"
    s = int(s)
    return f"{s // 3600}h{s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m{s % 60:02d}s"


def worker_card(w: dict) -> str:
    """The text of one worker's card in the list."""
    head = f"{STATUS_MARK.get(w['status'], '?')} {w['id']}  {w['status'].upper()}" + ("" if w["enabled"] else "  (disabled)")
    act = w.get("active")
    if act:
        return (f"{head}\n    {act['request_id']}  [{act.get('priority') or '-'}]  {(act.get('title') or '')[:60]}\n"
                f"    running {_fmt_seconds(act.get('elapsed_seconds'))}, session {act['session']['verdict']}")
    last = w.get("last") or {}
    tail = f"last: {last.get('request_id')} {last.get('outcome')}" if last else (w.get("status_reason") or "")
    return f"{head}\n    {tail[:80]}"


def details_text(v: dict) -> str:
    """What the details pane shows for one worker (``pool.peek``)."""
    lines = [f"{v['id']}  ({'enabled' if v['enabled'] else 'disabled'}, desired {v.get('desired')})",
             f"status:   {v['status']}  {v.get('status_reason') or ''}",
             f"process:  {v['process']['verdict']}  {v['process']['reason']}",
             f"agent id: {v.get('agent_id')}",
             f"counters: {v.get('counters')}"]
    act = v.get("active")
    if act:
        lines += ["", "ACTIVE REQUEST",
                  f"  {act['request_id']}  [{act.get('priority')}]  {act.get('title')}",
                  f"  file:     {act['request']}  ({act.get('request_state')})",
                  f"  attempt:  {act['attempt_id']}  running {_fmt_seconds(act.get('elapsed_seconds'))}, "
                  f"{_fmt_seconds(act.get('seconds_left'))} left",
                  f"  cwd:      {act.get('cwd')}",
                  f"  session:  {act['session']['verdict']}  {act['session']['reason']}",
                  f"  transcript: {act.get('transcript')}"]
    at = v.get("attempt")
    if at:
        lines += ["", f"ATTEMPT {at['attempt_id']}: {at.get('status')} {at.get('result') or ''}",
                  f"  {at.get('result_reason') or ''}"]
        val = at.get("validation") or {}
        for s in val.get("steps") or []:
            lines.append(f"  validation {s['name']}: {s['status']}  {s.get('detail', '')[:90]}")
    if v.get("transcript_tail"):
        lines += ["", "TRANSCRIPT (tail)"] + [f"  {line}" for line in v["transcript_tail"]]
    if v.get("events"):
        lines += ["", "EVENTS (latest)"] + [f"  {e.get('at', '')[11:19]} {e.get('event')} "
                                             + " ".join(f"{k}={e[k]}" for k in e if k not in ("at", "event"))[:110]
                                             for e in v["events"][-12:]]
    return "\n".join(lines)


class WorkerPanel(QtCore.QObject):
    def __init__(self, root: Path, *, auto_refresh_ms: int = 3000):
        super().__init__()
        self.root = Path(root)
        loader = QUiLoader()
        f = QtCore.QFile(str(UI_FILE))
        f.open(QtCore.QFile.ReadOnly)
        self.window = loader.load(f)
        f.close()
        if self.window is None:
            raise RuntimeError(f"cannot load {UI_FILE}: {loader.errorString()}")
        w = self.widget
        self.workerList = w(QtWidgets.QListWidget, "workerList")
        self.detailsText = w(QtWidgets.QPlainTextEdit, "detailsText")
        self.queueTable = w(QtWidgets.QTableWidget, "queueTable")
        self.workingTable = w(QtWidgets.QTableWidget, "workingTable")
        self.finishedTable = w(QtWidgets.QTableWidget, "finishedTable")
        self.priorityCombo = w(QtWidgets.QComboBox, "priorityCombo")
        self.labels = {n: w(QtWidgets.QLabel, n) for n in ("maxWorkersLabel", "runningLabel", "queuedLabel",
                                                             "workingLabel", "finishedLabel", "agentLabel",
                                                             "isolationLabel")}
        self.statusBar = w(QtWidgets.QStatusBar, "statusBar")
        for name, fn in (("refreshButton", self.refresh), ("startButton", lambda: self.act("start")),
                         ("stopButton", lambda: self.act("stop")), ("stopNowButton", lambda: self.act("stop_now")),
                         ("recoverButton", lambda: self.act("recover")), ("enableButton", lambda: self.act("enable")),
                         ("disableButton", lambda: self.act("disable")), ("startAllButton", lambda: self.act_all("start")),
                         ("stopAllButton", lambda: self.act_all("stop")), ("setPriorityButton", self.set_priority)):
            w(QtWidgets.QPushButton, name).clicked.connect(fn)
        self.workerList.currentItemChanged.connect(lambda *_: self.show_details())
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.refresh)
        auto = w(QtWidgets.QCheckBox, "autoRefreshCheck")
        auto.toggled.connect(lambda on: self.timer.start(auto_refresh_ms) if on else self.timer.stop())
        if auto.isChecked() and auto_refresh_ms > 0:
            self.timer.start(auto_refresh_ms)
        self.window.setWindowTitle(f"Worker Control Panel: {cfgmod.get('project.name', self.root, '')}")
        self.state: dict = {}
        self.refresh()

    def widget(self, cls, name):
        found = self.window.findChild(cls, name)
        if found is None:
            raise RuntimeError(f"{UI_FILE.name} has no {cls.__name__} named {name}")
        return found

    # ------------------------------------------------------------------ display
    def refresh(self) -> None:
        try:
            st = pool.status(self.root)
        except Exception as e:  # the panel shows the problem instead of closing
            self.statusBar.showMessage(f"status failed: {type(e).__name__}: {e}")
            return
        self.state = st
        L = self.labels
        L["maxWorkersLabel"].setText(f"Max workers: {st['max_workers']}")
        L["runningLabel"].setText(f"Running: {st['running']}")
        L["queuedLabel"].setText(f"Queued: {len(st['queue'])}")
        L["workingLabel"].setText(f"Working: {len(st['working'])}")
        L["finishedLabel"].setText(f"Finished: {len(st['finished'])}")
        L["agentLabel"].setText("Agent: configured" if st["agent_configured"] else "Agent: NOT CONFIGURED")
        L["isolationLabel"].setText(f"Isolation: {st['isolation']}")
        selected = self.selected_worker()
        self.workerList.blockSignals(True)
        self.workerList.clear()
        for wv in st["workers"]:
            item = QtWidgets.QListWidgetItem(worker_card(wv))
            item.setData(QtCore.Qt.UserRole, wv["id"])
            self.workerList.addItem(item)
            if wv["id"] == selected:
                self.workerList.setCurrentItem(item)
        self.workerList.blockSignals(False)
        self._fill(self.queueTable, [(q["priority"], q["request_id"], q["type"], q["title"]) for q in st["queue"]],
                   [q["request"] for q in st["queue"]])
        self._fill(self.workingTable, [(r["request_id"], r["priority"], r.get("worker") or r["agent_id"],
                                        r["liveness"], r["title"]) for r in st["working"]])
        self._fill(self.finishedTable, [(r["state"], r["request_id"], r["priority"], r["at"], r["title"])
                                        for r in st["finished"]])
        self.show_details()

    @staticmethod
    def _fill(table: QtWidgets.QTableWidget, rows: list[tuple], keys: list[str] | None = None) -> None:
        table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, value in enumerate(row):
                item = QtWidgets.QTableWidgetItem(str(value if value is not None else ""))
                if keys is not None and c == 0:
                    item.setData(QtCore.Qt.UserRole, keys[r])
                table.setItem(r, c, item)
        table.resizeColumnsToContents()

    def selected_worker(self) -> str | None:
        item = self.workerList.currentItem()
        return item.data(QtCore.Qt.UserRole) if item is not None else None

    def show_details(self) -> None:
        wid = self.selected_worker()
        if not wid:
            self.detailsText.setPlainText("")
            return
        try:
            self.detailsText.setPlainText(details_text(pool.peek(self.root, wid)))
        except proc.ProcessError as e:
            self.detailsText.setPlainText(e.message)

    # ------------------------------------------------------------------ actions
    def act(self, what: str, wid: str | None = None) -> None:
        wid = wid or self.selected_worker()
        if not wid:
            self.statusBar.showMessage("select a worker first")
            return
        try:
            if what == "start":
                out = pool.start(self.root, wid, wait_seconds=5)
            elif what == "stop":
                out = pool.stop(self.root, wid)
            elif what == "stop_now":
                out = pool.stop(self.root, wid, now=True)
            elif what == "recover":
                out = pool.recover(self.root, wid)
            elif what == "enable":
                out = pool.enable(self.root, wid)
            else:
                out = pool.disable(self.root, wid)
            self.statusBar.showMessage(f"{what} {wid}: " + ", ".join(f"{k}={v}" for k, v in out.items()
                                                                     if k in ("started", "running", "changed",
                                                                              "detail", "reason", "enabled")))
        except proc.ProcessError as e:
            self.statusBar.showMessage(f"{what} {wid} refused: {e.message}")
        self.refresh()

    def act_all(self, what: str) -> None:
        for wv in self.state.get("workers") or []:
            if what == "start" and not wv["enabled"]:
                continue
            self.act(what, wv["id"])

    def set_priority(self) -> None:
        row = self.queueTable.currentRow()
        item = self.queueTable.item(row, 0) if row >= 0 else None
        if item is None:
            self.statusBar.showMessage("select a queued request first")
            return
        name = item.data(QtCore.Qt.UserRole)
        try:
            r = proc.set_priority(self.root, name, self.priorityCombo.currentText(), by="worker-panel")
            self.statusBar.showMessage(f"priority of {r['request']}: {r['from']} -> {r['to']}")
        except proc.ProcessError as e:
            self.statusBar.showMessage(f"priority refused: {e.message}")
        self.refresh()


# Exit status of a start failure the panel has already shown in a dialog
# (tools/worker-panel then does not show a second one).
REPORTED = 3
APP_NAME = "devsys-worker-control"      # WM_CLASS / app id (desktop_launcher.py: StartupWMClass)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", help="development system directory (default: the one this panel belongs to)")
    a = ap.parse_args(argv)
    root = Path(a.root).resolve() if a.root else cfgmod.repo_root()
    QtCore.QCoreApplication.setApplicationName(APP_NAME)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])
    if os.environ.get("DEVSYS_DESKTOP_ID"):
        app.setDesktopFileName(os.environ["DEVSYS_DESKTOP_ID"])
    try:
        panel = WorkerPanel(root)
    except Exception as e:  # shown, not a silent exit: the panel may have been started from a menu
        import traceback
        traceback.print_exc()
        QtWidgets.QMessageBox.critical(None, "Worker Control Panel",
                                       f"The Worker Control Panel cannot start for {root}:\n\n{type(e).__name__}: {e}")
        return REPORTED
    panel.window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
