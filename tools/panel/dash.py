#!/usr/bin/env python3
"""Terminal dashboard: the workers, agents, requests, worktrees and the live
conversation between agents, with operator control from the keyboard
(COORD-009, docs/process/agent-coordination.md).

    python3 tools/panel/dash.py [--root DIR] [--interval S]     # live (curses)
    python3 tools/panel/dash.py --once [--width N] [--json]      # print one frame and exit (pipes, CI, tests)
    python3 tools/panel/dash.py --plain                          # live without curses (the default where curses
                                                                 # is missing: Windows / PowerShell)

Keys:  Up/Down or j/k select a worker    s start    x stop    X stop now
       r recover    e enable / disable   a start all    z stop all
       m message (asks: recipient, then text)    n new request    p priority of a queued request
       PgUp/PgDn scroll messages    q quit

Like the web and Qt panels it is a client of ``workers.overview`` only and
holds no lifecycle logic. Standard library only (curses).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.dont_write_bytecode = True

import agent_process as proc  # noqa: E402
from workers import overview  # noqa: E402

MARK = {"working": "*", "idle": "o", "stopped": "-", "stopping": "~"}
HELP = "s start  x stop  X stop-now  r recover  e enable/disable  a/z all  m message  n request  p priority  q quit"


def _dur(s) -> str:
    if s is None:
        return "-"
    s = int(s)
    return f"{s // 3600}h{s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m{s % 60:02d}s"


def _clip(text: str, width: int) -> str:
    text = text.replace("\n", " ")
    return text if len(text) <= width else text[:max(0, width - 1)] + "~"


def _wrap(text: str, width: int) -> list[str]:
    out = []
    for para in text.splitlines() or [""]:
        while len(para) > width:
            cut = para.rfind(" ", 0, width + 1)
            cut = cut if cut > width // 3 else width
            out.append(para[:cut])
            para = para[cut:].lstrip()
        out.append(para)
    return out


def message_lines(snap: dict, width: int) -> list[str]:
    """The conversation, oldest first, wrapped to ``width``."""
    lines = []
    for m in snap["messages"]:
        head = f"#{m['seq']} {str(m['at'])[11:19]} {m.get('from_name') or m['from']} -> {m.get('to_name') or m['to']} [{m['kind']}]"
        if m.get("request"):
            head += f" ({m['request']})"
        lines.append(_clip(head, width))
        if m.get("subject"):
            lines.append(_clip("   " + m["subject"], width))
        lines += ["   " + part for part in _wrap(m["body"], max(10, width - 3))]
    return lines or ["(no messages yet: agents write with scripts/msg.py send, you with 'm')"]


def status_lines(snap: dict, width: int, selected: int = -1) -> list[str]:
    """Workers, agents, requests and worktrees as text lines (the selected
    worker is marked with '>')."""
    p = snap["pool"]
    out = [_clip(f"WORKERS  {p['running']} running of {p['max_workers']}  isolation {p['isolation']}  "
                 f"agent {'configured' if p['agent_configured'] else 'NOT CONFIGURED'}", width)]
    for i, w in enumerate(p["workers"]):
        act = w.get("active")
        what = (f"{act['request_id']} [{act.get('priority') or '-'}] {_dur(act.get('elapsed_seconds'))} "
                f"session {act['session']['verdict']}" if act else
                (f"last {w['last'].get('request_id')} {w['last'].get('outcome') or ''}" if w.get("last")
                 else (w.get("status_reason") or "")))
        flags = ("" if w["enabled"] else " disabled") + ("" if w.get("configured", True) else " unconfigured")
        out.append(_clip(f"{'>' if i == selected else ' '}{MARK.get(w['status'], '?')} {w['id']} {w['status']:<8} "
                         f"{w.get('profile') or '-':<10}{flags} {what}", width))
    out += ["", f"AGENTS  {len(snap['agents'])}"]
    for a in snap["agents"]:
        claim = a["claims"][0] if a["claims"] else None
        where = f"{a['branch']}@{Path(a['worktree']).name}" if a.get("branch") and a.get("worktree") else "-"
        out.append(_clip(f"  {a['name']:<10} {a['liveness']:<7} {a.get('profile') or '-':<10} "
                         f"{(claim or {}).get('request_id') or '-'} {where}"
                         + (f" step: {claim['step']}" if claim and claim.get("step") else "")
                         + (f" roles: {','.join(a['roles'])}" if a.get("roles") else "")
                         + (f" ({a['unread']} unread)" if a.get("unread") else ""), width))
    out += ["", f"REQUESTS  queued {len(p['queue'])}  working {len(p['working'])}  finished {len(p['finished'])}"]
    for q in p["queue"][:6]:
        out.append(_clip(f"  queued   {q['priority']:<8} {q['request_id']} {q['title']}", width))
    for w in p["working"][:6]:
        out.append(_clip(f"  working  {w.get('priority') or '-':<8} {w['request_id']} "
                         f"({w.get('worker') or w.get('agent_id')}, {w['liveness']}) {w.get('title') or ''}", width))
    for f in p["finished"][:4]:
        out.append(_clip(f"  {f['state']:<8} {f.get('priority') or '-':<8} {f['request_id']} {f.get('title') or ''}", width))
    linked = [w for w in snap["worktrees"] if not w["main"]]
    if linked:
        out += ["", f"WORKTREES  {len(linked)} besides the main checkout"]
        for w in linked:
            out.append(_clip(f"  {w.get('branch') or '-'}  {w['path']}  {', '.join(w['requests']) or 'no claim'}", width))
    return out


def frame(snap: dict, width: int = 100) -> str:
    """One complete text frame (``--once``)."""
    bar = "=" * min(width, 100)
    name = snap.get("project") or ""
    title = (name + " " if name and not name.startswith("<") else "") + f"dashboard  {str(snap['at'])[:19]}Z"
    return "\n".join([title, bar, *status_lines(snap, width), "", "MESSAGES", *message_lines(snap, width)]) + "\n"


# ------------------------------------------------------------------ curses

class Dash:
    def __init__(self, root: Path, interval: float):
        self.root, self.interval = root, interval
        self.selected, self.scroll, self.note = 0, 0, HELP
        self.snap = overview.snapshot(root)
        # Push: a hub announcement (a message, an agent joining) refreshes at once.
        import threading
        import agent_messages
        self.dirty = threading.Event()
        self._stop = threading.Event()
        threading.Thread(target=agent_messages.follow, args=(root, lambda _ev: self.dirty.set(), self._stop),
                         name="hub-follow", daemon=True).start()

    def worker(self) -> str | None:
        ws = self.snap["pool"]["workers"]
        return ws[min(self.selected, len(ws) - 1)]["id"] if ws else None

    def do(self, action: str, **kw) -> None:
        try:
            out = overview.act(self.root, action, **kw)
            errs = [f"{r['worker']}: {r['error']}" for r in out.get("results", []) if r.get("error")]
            detail = next((r.get("detail") or r.get("reason") for r in out.get("results", []) if r.get("detail") or r.get("reason")), "")
            self.note = "; ".join(errs) if errs else f"{action} ok {detail or ''}".strip()
        except proc.ProcessError as e:
            self.note = f"refused: {e.message}"

    def ask(self, scr, label: str) -> str:
        import curses
        h, w = scr.getmaxyx()
        curses.echo()
        curses.curs_set(1)
        scr.timeout(-1)
        scr.move(h - 1, 0)
        scr.clrtoeol()
        scr.addnstr(h - 1, 0, label, w - 1)
        scr.refresh()
        try:
            raw = scr.getstr(h - 1, min(len(label), w - 2), 1000)
        except KeyboardInterrupt:
            raw = b""
        curses.noecho()
        curses.curs_set(0)
        return raw.decode("utf-8", "replace").strip()

    def draw(self, scr) -> None:
        import curses
        h, w = scr.getmaxyx()
        scr.erase()
        top = status_lines(self.snap, w - 1, self.selected)
        for y, line in enumerate(top[:max(0, h - 6)]):
            attr = curses.A_BOLD if line[:1].isupper() else (curses.A_REVERSE if line.startswith(">") else 0)
            scr.addnstr(y, 0, line, w - 1, attr)
        y0 = min(len(top), max(0, h - 6)) + 1
        room = h - y0 - 2
        if room > 1:
            scr.addnstr(y0 - 1, 0, "MESSAGES" + (f"  (scrolled back {self.scroll})" if self.scroll else ""), w - 1, curses.A_BOLD)
            lines = message_lines(self.snap, w - 1)
            self.scroll = max(0, min(self.scroll, max(0, len(lines) - room)))
            end = len(lines) - self.scroll
            for i, line in enumerate(lines[max(0, end - room):end]):
                scr.addnstr(y0 + i, 0, line, w - 1, curses.A_BOLD if line.startswith("#") else 0)
        scr.addnstr(h - 1, 0, _clip(self.note, w - 1), w - 1, curses.A_DIM)
        scr.refresh()

    def key(self, scr, ch: int) -> bool:
        import curses
        n = len(self.snap["pool"]["workers"])
        c = chr(ch) if 0 <= ch < 256 else ""
        wid = self.worker()
        if c == "q":
            return False
        if ch in (curses.KEY_DOWN,) or c == "j":
            self.selected = min(n - 1, self.selected + 1) if n else 0
        elif ch in (curses.KEY_UP,) or c == "k":
            self.selected = max(0, self.selected - 1)
        elif ch == curses.KEY_PPAGE:
            self.scroll += 5
        elif ch == curses.KEY_NPAGE:
            self.scroll = max(0, self.scroll - 5)
        elif c in "sxXr" and c and wid:
            if c == "X" and self.ask(scr, f"end {wid}'s running session now? (y/N) ").lower() != "y":
                self.note = "stop now cancelled"
            else:
                self.do({"s": "start", "x": "stop", "X": "stop_now", "r": "recover"}[c], worker=wid)
        elif c == "e" and wid:
            w = next(w for w in self.snap["pool"]["workers"] if w["id"] == wid)
            self.do("disable" if w["enabled"] else "enable", worker=wid)
        elif c == "a":
            self.do("start", worker="all")
        elif c == "z":
            self.do("stop", worker="all")
        elif c == "m":
            names = ", ".join(a["name"] for a in self.snap["agents"] if a.get("registered"))
            to = self.ask(scr, f"to (all, {names}): ") or "all"
            body = self.ask(scr, f"message to {to}: ")
            if body:
                self.do("message", to=to, body=body)
        elif c == "n":
            text = self.ask(scr, "new request: ")
            if text:
                self.do("submit", text=text)
        elif c == "p":
            q = self.snap["pool"]["queue"]
            if not q:
                self.note = "no queued request"
            else:
                rid = self.ask(scr, f"request id (default {q[0]['request_id']}): ") or q[0]["request_id"]
                hit = next((x for x in q if x["request_id"] == rid), None)
                pr = self.ask(scr, "priority (CRITICAL HIGH NORMAL LOW): ").upper() if hit else ""
                if hit and pr:
                    self.do("priority", request=hit["request"], priority=pr)
                elif not hit:
                    self.note = f"{rid} is not queued"
        elif c == "?":
            self.note = HELP
        return True

    def loop(self, scr) -> None:
        import curses
        curses.curs_set(0)
        curses.use_default_colors()
        last = 0.0
        while True:
            if time.monotonic() - last >= self.interval or self.dirty.is_set():
                self.dirty.clear()
                self.snap, last = overview.snapshot(self.root), time.monotonic()
            self.draw(scr)
            scr.timeout(250)
            ch = scr.getch()
            if ch == -1:
                continue
            if not self.key(scr, ch):
                return
            last = 0.0                                       # show the effect of the action at once


def plain_loop(dash: "Dash", width: int = 0) -> None:
    """Live view without curses: the frame is redrawn in place with ANSI
    sequences (Windows Terminal, PowerShell 7 and every POSIX terminal). On
    Windows the same keys work (read with msvcrt, prompts with input());
    elsewhere it only watches: use the curses view to act."""
    import shutil
    try:
        import msvcrt
    except ImportError:
        msvcrt = None
    if os.name == "nt":
        os.system("")                                        # switches the classic console to ANSI processing
    keys = {"s": "start", "x": "stop", "X": "stop_now", "r": "recover"}

    def ask(label: str) -> str:
        try:
            return input("\n" + label).strip()
        except EOFError:
            return ""

    last = 0.0
    while True:
        if time.monotonic() - last >= dash.interval or dash.dirty.is_set():
            dash.dirty.clear()
            dash.snap, last = overview.snapshot(dash.root), time.monotonic()
            size = shutil.get_terminal_size((110, 40))
            w = width or size.columns - 1
            lines = frame(dash.snap, w).splitlines()
            head = status_lines(dash.snap, w, dash.selected)
            lines[2:2 + len(head)] = head                    # the selected worker's marker
            room = max(5, size.lines - 2)
            if len(lines) > room:                            # keep the top, and the newest messages
                keep = min(len(head) + 4, room - 3)
                lines = lines[:keep] + ["..."] + lines[-(room - keep - 1):]
            sys.stdout.write("\x1b[H\x1b[2J" + "\n".join(lines) + "\n" + _clip(
                dash.note if msvcrt else "watch only (Ctrl-C quits); keys need the curses view", w) + "\n")
            sys.stdout.flush()
        if msvcrt is None or not msvcrt.kbhit():
            time.sleep(0.2)
            continue
        c = msvcrt.getwch()
        if c in ("\x00", "\xe0"):                           # arrow keys arrive as a prefix and a code
            c = {"H": "k", "P": "j"}.get(msvcrt.getwch(), "")
        n, wid = len(dash.snap["pool"]["workers"]), dash.worker()
        if c == "q":
            return
        if c == "j":
            dash.selected = min(n - 1, dash.selected + 1) if n else 0
        elif c == "k":
            dash.selected = max(0, dash.selected - 1)
        elif c in keys and wid:
            if c != "X" or ask(f"end {wid}'s running session now? (y/N) ").lower() == "y":
                dash.do(keys[c], worker=wid)
        elif c == "e" and wid:
            enabled = next(x for x in dash.snap["pool"]["workers"] if x["id"] == wid)["enabled"]
            dash.do("disable" if enabled else "enable", worker=wid)
        elif c == "a":
            dash.do("start", worker="all")
        elif c == "z":
            dash.do("stop", worker="all")
        elif c == "m":
            to = ask("to (all, a name or a role): ") or "all"
            body = ask(f"message to {to}: ")
            if body:
                dash.do("message", to=to, body=body)
        elif c == "n":
            text = ask("new request: ")
            if text:
                dash.do("submit", text=text)
        last = 0.0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=None, help="the development system to show (default: this checkout)")
    ap.add_argument("--interval", type=float, default=2.0, help="seconds between refreshes")
    ap.add_argument("--once", action="store_true", help="print one frame and exit")
    ap.add_argument("--width", type=int, default=0)
    ap.add_argument("--json", action="store_true", help="with --once: the snapshot as JSON")
    ap.add_argument("--plain", action="store_true", help="live view without curses (automatic where curses is missing)")
    a = ap.parse_args(argv)
    root = Path(a.root).resolve() if a.root else proc.repo_root()
    os.environ["AGENT_PROCESS_ROOT"] = str(root)            # workers started here serve this root
    if a.once or not sys.stdout.isatty():
        snap = overview.snapshot(root)
        if a.json:
            print(json.dumps(snap, indent=2, default=str))
        else:
            sys.stdout.write(frame(snap, a.width or 110))
        return 0
    dash = Dash(root, max(0.5, a.interval))
    try:
        if not a.plain:
            try:
                import curses
            except ImportError:                              # Windows: curses is not in the standard library
                a.plain = True
        if a.plain:
            plain_loop(dash, a.width)
        else:
            curses.wrapper(dash.loop)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
