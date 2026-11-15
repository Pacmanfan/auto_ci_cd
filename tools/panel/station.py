#!/usr/bin/env python3
"""The coordination station: one background process per clone that makes
messaging push-based and keeps the panels running while agents are here
(COORD-010, COORD-011, docs/process/agent-coordination.md).

    python3 tools/panel/station.py [--root DIR] [--stay]      # normally started by the first agent
    python3 tools/panel/station.py --open                     # open the web control panel in the browser; starts a
                                                              # station that stays (web panel only) when none runs
    python3 tools/panel/station.py --stop                     # end the running station

It runs:

* the **hub**: a loopback TCP socket speaking line-delimited JSON. A client
  sends ``{"token", "mode": "pub" | "sub"}``; a publisher then sends one
  event line, which every subscriber receives at once. The hub carries
  wake-ups, not content: the message log stays the only record, so a client
  that misses the hub loses promptness, never a message;
* the **web control panel** (tools/panel/web_panel.py), whose live stream is
  woken by the hub;
* the **Qt control panel** (tools/panel/worker_panel.py) as a child process,
  when a display and PySide6 are available.

It writes ``.agent/state/runtime/coordination/station.json`` (its process,
the hub address and token, the panel addresses), holds ``station.lock`` so
only one runs, and ends by itself ``coordination.linger_seconds`` after the
last agent has left the roster's "present" set (``--stay``: never). On the
way out it closes the panels it started and prunes roster entries of
sessions verified dead.

Standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import signal
import socketserver
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(HERE))
sys.dont_write_bytecode = True

import agent_messages as bus  # noqa: E402
import agent_process as proc  # noqa: E402
import devsys_config as cfgmod  # noqa: E402
import devsys_platform as plat  # noqa: E402


class Hub(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"

    def __init__(self, token: str, on_event=None):
        super().__init__(("127.0.0.1", 0), HubHandler)
        self.token = token
        self.on_event = on_event
        self.subscribers: set = set()
        self.lock = threading.Lock()

    def broadcast(self, line: bytes) -> None:
        with self.lock:
            targets = list(self.subscribers)
        for conn in targets:
            try:
                conn.sendall(line)
            except OSError:
                with self.lock:
                    self.subscribers.discard(conn)
        if self.on_event:
            try:
                self.on_event(json.loads(line.decode("utf-8", "replace")))
            except Exception:                                # noqa: BLE001 - a listener never breaks the hub
                pass


class HubHandler(socketserver.StreamRequestHandler):
    server: Hub

    def handle(self):
        self.connection.settimeout(5.0)
        try:
            hello = json.loads(self.rfile.readline(4096).decode("utf-8", "replace") or "{}")
        except (ValueError, OSError):
            return
        if not isinstance(hello, dict) or not secrets.compare_digest(str(hello.get("token") or ""), self.server.token):
            return
        if hello.get("mode") == "pub":
            try:
                line = self.rfile.readline(65536)
                json.loads(line.decode("utf-8", "replace"))   # only well-formed events are passed on
            except (ValueError, OSError):
                return
            self.server.broadcast(line if line.endswith(b"\n") else line + b"\n")
        elif hello.get("mode") == "sub":
            self.connection.settimeout(None)
            with self.server.lock:
                self.server.subscribers.add(self.connection)
            try:
                while self.rfile.read(1):                     # subscribers only listen; EOF = gone
                    pass
            except OSError:
                pass
            finally:
                with self.server.lock:
                    self.server.subscribers.discard(self.connection)


def start_web(root: Path, hub: Hub) -> tuple[object | None, str | None, str | None]:
    """(server, url, why not) for the web control panel."""
    try:
        import web_panel
    except Exception as e:                                   # noqa: BLE001
        return None, None, f"web panel not importable: {e}"
    host = str(cfgmod.get("coordination.web_host", root, "127.0.0.1"))
    want = int(cfgmod.get("coordination.web_port", root, 8765))
    last = None
    for port in (want, 0):                                   # the configured port, else any free one
        try:
            srv = web_panel.serve(root, host, port)
        except OSError as e:
            last = e
            continue
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.2}, name="web-panel", daemon=True).start()
        hub.on_event = lambda _ev: srv.notify_change()
        return srv, srv.url(), None
    return None, None, f"cannot listen on {host}: {last}"


def start_qt(root: Path) -> tuple[subprocess.Popen | None, str | None]:
    """(process, why not) for the Qt control panel."""
    script = HERE / "worker_panel.py"
    if os.name != "nt" and sys.platform != "darwin" and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return None, "no display in this session (DISPLAY / WAYLAND_DISPLAY unset)"
    import importlib.util
    if importlib.util.find_spec("PySide6") is None:
        return None, "PySide6 is not installed"
    if not script.is_file():
        return None, "tools/panel/worker_panel.py is missing"
    try:
        with open(bus.coordination_dir(root) / "control-panel.log", "ab") as log:
            p = subprocess.Popen([sys.executable, str(script), "--root", str(root)], cwd=str(root),
                                 stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                 env={**os.environ, "AGENT_PROCESS_ROOT": str(root)}, **plat.group_popen_kwargs())
    except OSError as e:
        return None, f"could not start: {e.strerror or e}"
    time.sleep(0.8)
    if p.poll() is not None:
        return None, f"exited at once (code {p.returncode}); see control-panel.log"
    return p, None


def open_dashboard(root: Path) -> int:
    """The desktop "dashboard" icon: open the running station's web panel in
    the browser. With no station, start one that stays (the operator asked
    for it, no agent did) with the web panel only, then open it."""
    import webbrowser
    info = bus.station_info(root)
    if info is None:
        with open(bus.coordination_dir(root) / "station.log", "ab") as log:
            subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--root", str(root), "--stay",
                              "--panels", "web"], cwd=str(root), stdin=subprocess.DEVNULL, stdout=log,
                             stderr=subprocess.STDOUT, env={**os.environ, "AGENT_PROCESS_ROOT": str(root)},
                             **plat.group_popen_kwargs())
        end = time.monotonic() + 10
        while time.monotonic() < end and not (info and info.get("ready")):
            time.sleep(0.1)
            info = bus.station_info(root)
    if not info or not info.get("web"):
        print("station: the web control panel is not running: "
              + str((info or {}).get("web_error") or "the station did not start (see station.log)"), file=sys.stderr)
        return 1
    print(info["web"], flush=True)
    webbrowser.open(info["web"])
    return 0


def stop_station(root: Path) -> int:
    info = bus.station_info(root)
    pid = ((info or {}).get("process") or {}).get("pid")
    if not pid:
        print("station: none running")
        return 0
    if os.name == "nt":
        plat.signal_group(pid, hard=True)
    else:
        os.kill(int(pid), signal.SIGTERM)                    # handled: the panels are closed in order
    end = time.monotonic() + 15
    while time.monotonic() < end and bus.station_info(root) is not None:
        time.sleep(0.1)
    gone = bus.station_info(root) is None
    print(f"station: {'stopped' if gone else 'did not stop'} (pid {pid})")
    return 0 if gone else 1


def run(root: Path, *, stay: bool = False, linger: float | None = None, panels: list | None = None) -> int:
    cdir = bus.coordination_dir(root)
    fd = os.open(cdir / "station.lock", os.O_RDWR | os.O_CREAT, 0o644)
    try:
        plat.lock(fd)
    except plat.WouldBlock:
        os.close(fd)
        print("station: another station is running for this clone", file=sys.stderr)
        return proc.CONFLICT
    if linger is None:
        linger = float(cfgmod.get("coordination.linger_seconds", root, 20))
    if panels is None:
        panels = list(cfgmod.get("coordination.panels", root, ["web", "qt"]) or [])
    stopping = threading.Event()
    for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), lambda _s, _f: stopping.set())
    hub = Hub(secrets.token_urlsafe(24))
    threading.Thread(target=hub.serve_forever, kwargs={"poll_interval": 0.2}, name="hub", daemon=True).start()
    info = {"process": plat.session_identity(os.getpid()), "started_at": proc.now_iso(), "root": str(root),
            "hub": {"host": "127.0.0.1", "port": hub.server_address[1], "token": hub.token},
            "web": None, "web_error": "disabled (coordination.panels)", "qt": {"pid": None, "reason": "disabled (coordination.panels)"},
            "ready": False}
    sfile = bus.station_file(root)

    def save():
        proc.atomic_write_text(sfile, json.dumps(info, indent=2) + "\n")
        if os.name != "nt":
            os.chmod(sfile, 0o600)                           # it holds the hub token

    save()
    web = qt = None
    try:
        if "web" in panels:
            web, info["web"], info["web_error"] = start_web(root, hub)
        if "qt" in panels:
            qt, why = start_qt(root)
            info["qt"] = {"pid": qt.pid if qt else None, "reason": why}
        info["ready"] = True
        save()
        for line in bus.panel_lines(info):
            print(line, flush=True)
        empty_since = None
        started = time.monotonic()
        while not stopping.wait(1.0):
            if stay:
                continue
            if bus.present(root):
                empty_since = None
                continue
            # At start the first agent may still be registering.
            empty_since = empty_since or time.monotonic()
            if time.monotonic() - empty_since >= linger and time.monotonic() - started >= min(linger, 5.0):
                print("station: the last agent has left; closing the panels", flush=True)
                break
        return 0
    finally:
        try:
            sfile.unlink()
        except OSError:
            pass
        if web is not None:
            web.closing.set()
            web.notify_change()
            web.shutdown()
            web.server_close()
        if qt is not None and qt.poll() is None:
            plat.kill_tree(qt.pid, 5)
        hub.shutdown()
        hub.server_close()
        try:
            bus.prune(root)
        except Exception:                                    # noqa: BLE001
            pass
        plat.unlock(fd)
        os.close(fd)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=None, help="the development system to serve (default: this checkout)")
    ap.add_argument("--stay", action="store_true", help="do not end when the last agent leaves")
    ap.add_argument("--linger", type=float, default=None, help="seconds to stay after the last agent left")
    ap.add_argument("--panels", nargs="*", default=None, choices=["web", "qt"], help="panels to start (default: coordination.panels)")
    ap.add_argument("--open", action="store_true", help="open the web control panel in the browser (starting a station if needed)")
    ap.add_argument("--stop", action="store_true", help="end the running station")
    a = ap.parse_args(argv)
    root = proc.main_root(Path(a.root).resolve() if a.root else proc.repo_root())
    os.environ["AGENT_PROCESS_ROOT"] = str(root)            # workers started from a panel serve this root
    os.environ[bus.AUTOSTART_ENV] = "0"                     # nothing started from here starts a station
    if shutil.which("git") is None:
        print("station: git is not on PATH", file=sys.stderr)
        return 1
    if a.stop:
        return stop_station(root)
    if a.open:
        return open_dashboard(root)
    return run(root, stay=a.stay, linger=a.linger, panels=a.panels)


if __name__ == "__main__":
    sys.exit(main())
