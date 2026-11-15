#!/usr/bin/env python3
"""Adapter: run the opencode CLI as a worker's agent (WORKER-012: this
directory and docs/process/agent-profiles.md are the documented places that
name an agent product).

The Worker Orchestrator expects the generic protocol of
scripts/workers/agent.py: the prompt on stdin and a JSON-lines stream of
``system`` / ``assistant`` / ``result`` events on stdout. ``opencode run
--format json`` writes its own event stream and no final result event, so
this wrapper translates:

    python3 event_stream_adapter.py [--dialect auto|1|2] [--prompt stdin|arg] -- opencode run --format json ...

* the prompt read from stdin is handed to the wrapped command on its stdin
  (``--prompt stdin``, the default) or appended as its last argument
  (``--prompt arg``);
* text parts become ``assistant`` text blocks, tool calls ``assistant``
  ``tool_use`` blocks;
* when the command exits, one ``result`` event is written: an error when the
  command failed or its last event was an error, with the cost and step
  count the stream reported;
* the wrapper exits with the command's exit code.

Dialects (``auto`` decides per event, so one profile serves both):

* ``1``  major version 1: ``{"type": "text" | "tool_use" | "step_start" |
  "step_finish" | "error", "sessionID", "part": {...}}``. Verified against
  1.18.
* ``2``  the bus-event shape ``{"type": "message.part.updated", "properties":
  {"part": {...}}}`` / ``{"type": "session.error", "properties": {"error":
  {...}}}``. Major version 2 was not released when this was written: this
  mapping follows the server's event format and is UNVERIFIED against a real
  2.x; re-check it with ``--raw-log`` when 2.x is installed.

``--raw-log FILE`` keeps the untranslated stream for that check. Lines that
are not JSON go to stderr (the attempt's stderr.log), never into the
transcript.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def error_text(err) -> str:
    """The readable message inside an error object of either dialect."""
    if isinstance(err, str):
        return err
    if not isinstance(err, dict):
        return "error"
    data = err.get("data") if isinstance(err.get("data"), dict) else {}
    return str(data.get("message") or err.get("message") or err.get("name") or "error")


class Translator:
    """Stateful translation of one event stream."""

    def __init__(self, session_id: str | None, dialect: str = "auto"):
        self.session_id = session_id
        self.dialect = dialect
        self.last_text = ""
        self.error: str | None = None
        self.turns = 0
        self.cost = 0.0
        self.seen_text: dict[str, str] = {}       # part id -> text already emitted (dialect 2 repeats parts)
        self.seen_tools: set[str] = set()

    def _assistant(self, block: dict) -> dict:
        return {"type": "assistant", "session_id": self.session_id, "message": {"content": [block]}}

    def _part(self, part: dict, repeated: bool) -> list[dict]:
        ptype = part.get("type")
        pid = str(part.get("id") or "")
        if ptype == "text" and part.get("text"):
            text = part["text"]
            if repeated:
                # Updates carry the whole text so far: emit a part once, when it is complete.
                if not (part.get("time") or {}).get("end") or self.seen_text.get(pid) == text:
                    return []
                self.seen_text[pid] = text
            self.last_text, self.error = text, None
            return [self._assistant({"type": "text", "text": text})]
        if ptype == "tool":
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            if repeated:
                key = pid or str(part.get("callID") or "")
                if state.get("status") not in ("completed", "error") or key in self.seen_tools:
                    return []
                self.seen_tools.add(key)
            return [self._assistant({"type": "tool_use", "name": str(part.get("tool") or "tool"),
                                     "id": part.get("callID"), "input": state.get("input") or {}})]
        if ptype in ("step-finish", "step_finish"):
            if repeated and pid:
                if pid in self.seen_tools:
                    return []
                self.seen_tools.add(pid)
            self.turns += 1
            try:
                self.cost += float(part.get("cost") or 0)
            except (TypeError, ValueError):
                pass
        return []

    def feed(self, event: dict) -> list[dict]:
        """Protocol events for one event of the wrapped command."""
        etype = str(event.get("type") or "")
        self.session_id = self.session_id or event.get("sessionID")
        props = event.get("properties") if isinstance(event.get("properties"), dict) else None
        v2 = self.dialect == "2" or (self.dialect == "auto" and props is not None)
        if v2:
            props = props or {}
            if etype == "message.part.updated" and isinstance(props.get("part"), dict):
                return self._part(props["part"], repeated=True)
            if etype in ("session.error", "error"):
                self.error = error_text(props.get("error") or event.get("error"))
            return []
        if etype == "error":
            self.error = error_text(event.get("error"))
            return []
        if isinstance(event.get("part"), dict):
            return self._part(event["part"], repeated=False)
        return []

    def result(self, exit_code: int | None, duration_ms: int) -> dict:
        failed = bool(exit_code) or self.error is not None
        text = self.error if self.error is not None else self.last_text
        if failed and not text:
            text = f"the agent command exited with code {exit_code} and no message"
        return {"type": "result", "session_id": self.session_id, "subtype": "error" if failed else "success",
                "is_error": failed, "num_turns": self.turns, "duration_ms": duration_ms,
                "total_cost_usd": round(self.cost, 6), "result": text,
                "stop_reason": "error" if failed else "end_turn"}


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--" not in argv:
        print("usage: event_stream_adapter.py [options] -- <agent command ...>", file=sys.stderr)
        return 2
    cut = argv.index("--")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dialect", choices=("auto", "1", "2"), default="auto")
    ap.add_argument("--prompt", choices=("stdin", "arg"), default="stdin")
    ap.add_argument("--session-id", default=os.environ.get("DEVSYS_WORKER_SESSION"))
    ap.add_argument("--raw-log", help="also write the untranslated event stream to this file")
    a = ap.parse_args(argv[:cut])
    command = argv[cut + 1:]
    if not command:
        print("event_stream_adapter: no agent command after --", file=sys.stderr)
        return 2
    prompt = sys.stdin.read()
    if a.prompt == "arg":
        command.append(prompt)
    tr = Translator(a.session_id, a.dialect)
    started = time.time()
    emit({"type": "system", "subtype": "init", "session_id": a.session_id, "cwd": os.getcwd()})
    try:
        child = subprocess.Popen(command, stdin=subprocess.PIPE if a.prompt == "stdin" else subprocess.DEVNULL,
                                 stdout=subprocess.PIPE)
    except OSError as e:
        tr.error = f"{command[0]}: {e.strerror or e}"
        emit(tr.result(127, 0))
        return 127
    # A stop ends the whole process group (the worker does that); a signal sent
    # to the wrapper alone is passed on so the agent never outlives it.
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda s, _f: child.send_signal(s))
    if a.prompt == "stdin":
        try:
            child.stdin.write(prompt.encode("utf-8"))
            child.stdin.close()
        except (BrokenPipeError, OSError):
            pass
    raw = open(a.raw_log, "ab") if a.raw_log else None
    try:
        for line in child.stdout:
            if raw:
                raw.write(line)
                raw.flush()
            text = line.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                event = json.loads(text)
            except ValueError:
                event = None
            if not isinstance(event, dict):
                print(text, file=sys.stderr)
                continue
            for out in tr.feed(event):
                emit(out)
    finally:
        if raw:
            raw.close()
    code = child.wait()
    emit(tr.result(code, int((time.time() - started) * 1000)))
    return code if code >= 0 else 128 - code


if __name__ == "__main__":
    sys.exit(main())
