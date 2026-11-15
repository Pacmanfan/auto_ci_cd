#!/usr/bin/env python3
"""Talk to the other agents working in this repository (COORD-001..006,
docs/process/agent-coordination.md). Messages inform; they never replace a
claim, a path registration or the gate.

  python3 scripts/msg.py join [--name N] [--role lead ...] [--profile P]   # register / rename (optional: send and inbox register you)
  python3 scripts/msg.py who [--json]                                       # the roster: names, liveness, request, worktree
  python3 scripts/msg.py send --to NAME|ROLE|all|operator [--kind K] [--request R] [--subject S] TEXT...
  python3 scripts/msg.py reply MSG-ID TEXT...                               # an answer to the sender of MSG-ID
  python3 scripts/msg.py inbox [--peek] [--json]                            # your unread messages (marks them read)
  python3 scripts/msg.py wait [--timeout S]                                 # block until a message for you arrives
  python3 scripts/msg.py log [--last N] [--request R] [--mine] [--follow]   # the conversation of all agents
  python3 scripts/msg.py leave | prune                                      # remove your entry / entries of dead sessions

The agent id comes from --agent-id or $AGENT_ID (scripts/claim.py
--new-agent-id); ``--as-operator`` sends as the human operator instead.
Kinds: note, question, answer, status, handoff, path-request, alert.

Exit codes: 0 ok (wait: 0 a message arrived, 4 timed out), else the process
tooling's codes (2 conflict, 5 not found, 6 invalid).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_messages as bus  # noqa: E402
import agent_process as proc  # noqa: E402

WAIT_TIMEOUT_EXIT = 4


def _who(root: Path, as_json: bool) -> int:
    claims = {c.get("agent_id"): c for c in proc.all_claims(root) if c.get("kind", "request") == "request"}
    rows = []
    for e in bus.roster(root):
        verdict, why = bus.liveness(e)
        claim = claims.get(e["agent_id"]) or {}
        rows.append({**e, "liveness": verdict, "liveness_reason": why, "unread": bus.unread_count(root, e["agent_id"]),
                     "claimed_request": claim.get("target")})
    if as_json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("no agents registered (scripts/msg.py join)")
    for r in rows:
        where = r.get("branch") and f"{r['branch']} @ {r.get('worktree')}" or r.get("cwd") or "-"
        print(f"{r['name']:<12} {r['liveness']:<8} {r.get('profile') or '-':<12} "
              f"roles={','.join(r.get('roles') or []) or '-'} unread={r['unread']} "
              f"request={r.get('claimed_request') or r.get('request') or '-'}\n"
              f"             {r['agent_id']}  {where}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--agent-id", default=os.environ.get("AGENT_ID"))
    ap.add_argument("--as-operator", action="store_true", help="send as the human operator")
    sub = ap.add_subparsers(dest="cmd", required=True)
    j = sub.add_parser("join")
    j.add_argument("--name")
    j.add_argument("--role", action="append")
    j.add_argument("--profile")
    j.add_argument("--request")
    sub.add_parser("who").add_argument("--json", action="store_true")
    s = sub.add_parser("send")
    s.add_argument("--to", required=True)
    s.add_argument("--kind", default="note", choices=bus.KINDS)
    s.add_argument("--request")
    s.add_argument("--subject")
    s.add_argument("text", nargs="+")
    r = sub.add_parser("reply")
    r.add_argument("message_id")
    r.add_argument("--kind", default="answer", choices=bus.KINDS)
    r.add_argument("text", nargs="+")
    i = sub.add_parser("inbox")
    i.add_argument("--peek", action="store_true", help="do not mark the messages read")
    i.add_argument("--json", action="store_true")
    w = sub.add_parser("wait")
    w.add_argument("--timeout", type=float, default=300.0)
    w.add_argument("--json", action="store_true")
    lg = sub.add_parser("log")
    lg.add_argument("--last", type=int, default=30)
    lg.add_argument("--request")
    lg.add_argument("--mine", action="store_true", help="only messages you sent or received")
    lg.add_argument("--follow", action="store_true")
    lg.add_argument("--json", action="store_true")
    sub.add_parser("leave")
    sub.add_parser("prune")
    a = ap.parse_args(argv)
    root = proc.repo_root()
    me = bus.OPERATOR if a.as_operator else a.agent_id

    def need_agent() -> str:
        if a.as_operator:
            raise bus.MessageError(proc.INVALID, f"{a.cmd} is an agent's command; --as-operator only sends")
        return proc.validate_agent_id(a.agent_id)

    def show(msgs, as_json=False):
        if as_json:
            print(json.dumps(msgs, indent=2))
        else:
            for m in msgs:
                print(bus.format_message(m))

    try:
        if a.cmd == "join":
            e = bus.register(root, need_agent(), name=a.name, roles=a.role, profile=a.profile,
                             owner_pid=proc.owner_pid_from_env(), cwd=os.getcwd(), request=a.request)
            print(f"JOINED as {e['name']} ({e['agent_id']})" + (f" roles {','.join(e['roles'])}" if e["roles"] else ""))
        elif a.cmd == "who":
            return _who(root, a.json)
        elif a.cmd in ("send", "reply"):
            if not me:
                need_agent()
            m = bus.send(root, me, getattr(a, "to", ""), " ".join(a.text), kind=a.kind,
                         request=getattr(a, "request", None), subject=getattr(a, "subject", None),
                         reply_to=getattr(a, "message_id", None))
            print(f"SENT {m['id']} #{m['seq']} to {m['to_name']}")
        elif a.cmd == "inbox":
            msgs = bus.inbox(root, need_agent(), mark_read=not a.peek)
            show(msgs, a.json)
            if not msgs and not a.json:
                print("no unread messages")
        elif a.cmd == "wait":
            msgs = bus.wait(root, need_agent(), timeout=a.timeout)
            show(msgs, a.json)
            if not msgs:
                if not a.json:
                    print(f"no message within {a.timeout:.0f}s")
                return WAIT_TIMEOUT_EXIT
        elif a.cmd == "log":
            involving = need_agent() if a.mine else None
            msgs = bus.read(root, limit=a.last, request=a.request, involving=involving)
            show(msgs, a.json)
            seq = msgs[-1]["seq"] if msgs else bus.last_seq(root)
            while a.follow:
                time.sleep(1.0)
                new = bus.read(root, after=seq, request=a.request, involving=involving)
                if new:
                    show(new, a.json)
                    sys.stdout.flush()
                    seq = new[-1]["seq"]
        elif a.cmd == "leave":
            print("LEFT" if bus.leave(root, need_agent()) else "not registered")
        elif a.cmd == "prune":
            gone = bus.prune(root)
            print(f"PRUNED {len(gone)}: {', '.join(gone) or '-'}")
    except proc.ProcessError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return e.code
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
