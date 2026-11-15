#!/usr/bin/env python3
"""Web Control Panel: a browser view of the whole development system, with
full operator control (COORD-008, docs/process/agent-coordination.md).

    python3 tools/panel/web_panel.py [--root DIR] [--host 127.0.0.1] [--port 8765] [--open]

It shows the workers, the agents (names, liveness, worktree and branch), the
requests (queued, working, finished), the git worktrees and, live, every
message the agents send each other. The operator can start / stop / recover /
enable / disable workers, set a queued request's priority, submit a request
and write to any agent, role or everyone.

Like the Qt panel it holds no lifecycle logic: it is a client of
``workers.overview`` (``snapshot`` / ``act``) and ``workers.pool.peek``.
Standard library only.

Safety: it binds to the loopback interface by default. Every ``/api/`` call
needs the session token (printed in the start-up URL and embedded in the page
it serves), and the ``Host`` header must name this server, so another web
page open in the same browser cannot drive the workers. Binding to another
interface (``--host 0.0.0.0``) exposes control of the agents to that network:
the page itself then needs ``?token=...`` too.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import secrets
import sys
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.dont_write_bytecode = True

import agent_process as proc  # noqa: E402
from workers import overview, pool  # noqa: E402

PAGE = HERE / "web_panel.html"
LOOPBACK = ("127.0.0.1", "localhost", "::1")
MAX_BODY = 64 * 1024
# Refusals of the tooling mapped to HTTP: the body always carries the reason.
HTTP_FOR = {proc.NOT_FOUND: HTTPStatus.NOT_FOUND, proc.INVALID: HTTPStatus.BAD_REQUEST,
            proc.CONFLICT: HTTPStatus.CONFLICT, proc.BUSY: HTTPStatus.SERVICE_UNAVAILABLE}


class Panel(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"      # on Windows it would let two servers share the port

    def __init__(self, addr, root: Path, token: str, interval: float = 1.5):
        super().__init__(addr, Handler)
        self.root = Path(root)
        self.token = token
        self.interval = interval
        self.closing = threading.Event()
        self.loopback = addr[0] in LOOPBACK
        # Push: the hub (tools/panel/station.py) announces every message and
        # roster change; the event streams wait on this instead of sleeping.
        self.changed = threading.Condition()
        self.generation = 0

    def notify_change(self) -> None:
        with self.changed:
            self.generation += 1
            self.changed.notify_all()

    def wait_change(self, seen: int, timeout: float) -> int:
        """Block until something was announced after ``seen`` (or ``timeout``)."""
        with self.changed:
            if self.generation == seen:
                self.changed.wait(timeout)
            return self.generation

    def url(self) -> str:
        host, port = self.server_address[:2]
        host = f"[{host}]" if ":" in host else host
        return f"http://{host}:{port}/" + ("" if self.loopback else f"?token={self.token}")

    def allowed_hosts(self) -> set[str]:
        host, port = self.server_address[:2]
        names = {host, *LOOPBACK} if self.loopback else {host}
        return {f"{n}:{port}" for n in names} | {f"[{n}]:{port}" for n in names if ":" in n}


class Handler(BaseHTTPRequestHandler):
    server: Panel
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):                      # quiet: the terminal belongs to the operator
        if os.environ.get("DEVSYS_PANEL_LOG"):
            super().log_message(fmt, *args)

    # ------------------------------------------------------------ plumbing
    def _send(self, status, body: bytes, ctype: str):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status=HTTPStatus.OK):
        self._send(status, json.dumps(obj, default=str).encode("utf-8"), "application/json; charset=utf-8")

    def _host_ok(self) -> bool:
        # A page on another origin reaching this port through a DNS name it
        # controls (rebinding) sends that name as Host.
        return not self.server.loopback or self.headers.get("Host", "") in self.server.allowed_hosts()

    def _token_ok(self, query: dict) -> bool:
        given = self.headers.get("X-Panel-Token") or (query.get("token") or [""])[0]
        return hmac.compare_digest(given.encode(), self.server.token.encode())

    def _guard(self, query: dict) -> bool:
        if not self._host_ok():
            self._json({"error": "unexpected Host header"}, HTTPStatus.FORBIDDEN)
            return False
        if not self._token_ok(query):
            self._json({"error": "missing or wrong panel token: reload the panel page"}, HTTPStatus.FORBIDDEN)
            return False
        return True

    # ---------------------------------------------------------------- GET
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            if not self._host_ok() or (not self.server.loopback and not self._token_ok(q)):
                return self._json({"error": "forbidden"}, HTTPStatus.FORBIDDEN)
            page = PAGE.read_text(encoding="utf-8").replace("__PANEL_TOKEN__", self.server.token)
            return self._send(HTTPStatus.OK, page.encode("utf-8"), "text/html; charset=utf-8")
        if not u.path.startswith("/api/"):
            return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        if not self._guard(q):
            return None
        try:
            if u.path == "/api/snapshot":
                return self._json(overview.snapshot(self.server.root, after=int((q.get("after") or ["0"])[0])))
            if u.path == "/api/peek":
                return self._json(pool.peek(self.server.root, (q.get("worker") or [""])[0], lines=40))
            if u.path == "/api/events":
                return self._events()
        except proc.ProcessError as e:
            return self._json({"error": e.message}, HTTP_FOR.get(e.code, HTTPStatus.CONFLICT))
        except ValueError as e:
            return self._json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
        return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def _events(self):
        """Server-sent events: the whole snapshot, again whenever it changed
        (compared without its timestamps), and a comment as keep-alive."""
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        last, quiet, seen = None, 0.0, self.server.generation
        try:
            while not self.server.closing.is_set():
                snap = overview.snapshot(self.server.root)
                key = json.dumps(_stable(snap), sort_keys=True, default=str)
                if key != last:
                    last, quiet = key, 0.0
                    self.wfile.write(b"event: snapshot\ndata: " + json.dumps(snap, default=str).encode() + b"\n\n")
                elif quiet >= 15.0:
                    quiet = 0.0
                    self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
                # Woken at once by an announced change; the interval only
                # covers what nobody announces (worker status, elapsed time).
                seen = self.server.wait_change(seen, self.server.interval)
                quiet += self.server.interval
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass                                             # the browser went away

    # --------------------------------------------------------------- POST
    def do_POST(self):
        u = urlparse(self.path)
        if u.path != "/api/act":
            return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        if not self._guard(parse_qs(u.query)):
            return None
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > MAX_BODY:
                raise ValueError("request body missing or too large")
            data = json.loads(self.rfile.read(n).decode("utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("action"), str):
                raise ValueError("expected a JSON object with an action")
            allowed = ("worker", "request", "priority", "to", "body", "kind", "subject", "reply_to", "text")
            args = {k: data[k] for k in allowed if isinstance(data.get(k), str)}
            return self._json(overview.act(self.server.root, data["action"], **args))
        except proc.ProcessError as e:
            return self._json({"error": e.message}, HTTP_FOR.get(e.code, HTTPStatus.CONFLICT))
        except (ValueError, UnicodeDecodeError) as e:
            return self._json({"error": str(e)}, HTTPStatus.BAD_REQUEST)


def _stable(snap: dict) -> dict:
    """The snapshot without the values that change every second (clock,
    elapsed time), so an idle system sends nothing."""
    def strip(node):
        if isinstance(node, dict):
            return {k: strip(v) for k, v in node.items()
                    if k not in ("at", "elapsed_seconds", "seconds_left", "seen_at", "heartbeat_at")}
        if isinstance(node, list):
            return [strip(v) for v in node]
        return node
    return strip(snap)


def serve(root: Path, host: str = "127.0.0.1", port: int = 8765, token: str | None = None) -> Panel:
    """Create the server (not yet serving): ``serve_forever()`` runs it,
    ``closing.set(); shutdown()`` stops it. ``port`` 0 picks a free one."""
    return Panel((host, port), root, token or secrets.token_urlsafe(24))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=None, help="the development system to control (default: this checkout)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--open", action="store_true", help="open the panel in the default browser")
    a = ap.parse_args(argv)
    root = Path(a.root).resolve() if a.root else proc.repo_root()
    os.environ["AGENT_PROCESS_ROOT"] = str(root)            # workers started here serve this root
    try:
        srv = serve(root, a.host, a.port)
    except OSError as e:
        print(f"web-panel: cannot listen on {a.host}:{a.port}: {e.strerror or e} (try --port)", file=sys.stderr)
        return 1
    if not srv.loopback:
        print(f"web-panel: WARNING: listening on {a.host}: anyone who can reach this port and has the token "
              "controls the agents", file=sys.stderr)
    print(f"web-panel: {srv.url()}   (root {root}; Ctrl-C stops the panel, not the workers)", flush=True)
    import agent_messages
    stop = threading.Event()
    threading.Thread(target=agent_messages.follow, args=(root, lambda _ev: srv.notify_change(), stop),
                     name="hub-follow", daemon=True).start()
    if a.open:
        threading.Timer(0.3, webbrowser.open, args=(srv.url(),)).start()
    try:
        srv.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        srv.closing.set()
        srv.notify_change()
        srv.server_close()
        time.sleep(0.05)
    return 0


if __name__ == "__main__":
    sys.exit(main())
