#!/usr/bin/env python3
"""Agent coordination (docs/requirements/agent_coordination.md): the roster
and friendly names (COORD-001), the message bus (COORD-002, -003), delivery
(COORD-004), no authority (COORD-005), shared state across git worktrees
(COORD-006), the one snapshot and operator actions (COORD-007), the web
control panel (COORD-008), the terminal dashboard (COORD-009); per-worker
agent profiles and the event-stream adapter (WORKER-006, WORKER-012).

Run: python3 -m unittest scripts.tests.test_agent_coordination -v
"""
import http.client
import json
import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from devsys_testlib import FRAMEWORK, SCRIPTS, Project, fast_config  # noqa: E402
from test_workers import FAKE, worker_config  # noqa: E402

sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(FRAMEWORK / "tools" / "panel"))
sys.dont_write_bytecode = True
import agent_messages as bus  # noqa: E402
import agent_process as proc  # noqa: E402
import devsys_config as cfgmod  # noqa: E402
from workers import agent as wagent, overview  # noqa: E402

A1, A2, A3 = "agent-test-one-aaaaaa", "agent-test-two-bbbbbb", "agent-test-three-cccccc"
PROFILES = FRAMEWORK / "scripts" / "workers" / "profiles"


def adapter() -> Path:
    """The event-stream adapter (found by location: this file names no product)."""
    found = sorted(PROFILES.glob("*_adapter.py"))
    assert len(found) == 1, found
    return found[0]


class BusBase(unittest.TestCase):
    config: dict = {}

    def setUp(self):
        self.p = Project(fast_config(**self.config))
        self.root = self.p.root

    def tearDown(self):
        self.p.close()


class Roster(BusBase):
    def test_agents_get_distinct_friendly_names_and_keep_them(self):
        a, b = bus.register(self.root, A1), bus.register(self.root, A2)
        self.assertNotEqual(a["name"], b["name"])
        self.assertIn(a["name"], bus.NAME_POOL)
        again = bus.register(self.root, A1, profile="alt", roles=["Lead"])
        self.assertEqual(again["name"], a["name"])
        self.assertEqual((again["profile"], again["roles"]), ("alt", ["lead"]))
        self.assertEqual([e["agent_id"] for e in bus.roster(self.root)], [A1, A2])

    def test_a_requested_name_is_unique_and_reserved_words_are_refused(self):
        bus.register(self.root, A1, name="scout")
        with self.assertRaises(bus.MessageError) as cm:
            bus.register(self.root, A2, name="scout")
        self.assertEqual(cm.exception.code, proc.CONFLICT)
        for bad in ("all", "operator", "X", "has space"):
            with self.assertRaises(bus.MessageError):
                bus.register(self.root, A2, name=bad)
        self.assertIsNone(bus.get(self.root, A2))

    def test_a_shared_uid_style_agent_id_is_refused(self):
        with self.assertRaises(proc.ProcessError):
            bus.register(self.root, "agent-1000")

    def test_liveness_is_verified_from_the_owner_process_and_dead_entries_are_pruned(self):
        sleeper = self.p.sleeper()
        bus.register(self.root, A1, owner_pid=sleeper.pid)
        bus.register(self.root, A2)
        self.assertEqual(bus.liveness(bus.get(self.root, A1))[0], "ALIVE")
        self.assertEqual(bus.liveness(bus.get(self.root, A2))[0], "UNKNOWN")
        self.p.kill(sleeper)
        self.assertEqual(bus.liveness(bus.get(self.root, A1))[0], "DEAD")
        self.assertEqual(bus.prune(self.root), [A1])
        self.assertEqual([e["agent_id"] for e in bus.roster(self.root)], [A2])   # unverifiable: kept


class Messages(BusBase):
    def setUp(self):
        super().setUp()
        self.a = bus.register(self.root, A1)
        self.b = bus.register(self.root, A2, roles=["lead"])
        self.c = bus.register(self.root, A3)

    def test_direct_broadcast_and_role_messages_reach_exactly_their_recipients(self):
        bus.send(self.root, A1, self.b["name"], "direct to two")
        bus.send(self.root, A1, "all", "to everyone", kind="status")
        bus.send(self.root, A3, "lead", "to the lead role", kind="question")
        self.assertEqual([m["body"] for m in bus.unread(self.root, A2)],
                         ["direct to two", "to everyone", "to the lead role"])
        self.assertEqual([m["body"] for m in bus.unread(self.root, A3)], ["to everyone"])
        self.assertEqual(bus.unread(self.root, A1), [])           # a sender is not sent its own broadcast

    def test_inbox_delivers_once_and_peek_does_not_mark_read(self):
        bus.send(self.root, A1, A2, "one")
        self.assertEqual(len(bus.inbox(self.root, A2, mark_read=False)), 1)
        self.assertEqual(len(bus.inbox(self.root, A2)), 1)
        self.assertEqual(bus.inbox(self.root, A2), [])
        bus.send(self.root, A1, A2, "two")
        self.assertEqual([m["body"] for m in bus.inbox(self.root, A2)], ["two"])

    def test_an_agent_that_joins_later_does_not_get_the_backlog(self):
        bus.send(self.root, A1, "all", "old news")
        late = "agent-test-late-dddddd"
        bus.register(self.root, late)
        self.assertEqual(bus.unread(self.root, late), [])
        self.assertEqual(len(bus.read(self.root)), 1)             # the log still has it

    def test_reply_goes_to_the_sender_and_keeps_the_request(self):
        q = bus.send(self.root, A1, A2, "may I edit src/app.txt?", kind="path-request", request="REQ-X-1",
                     subject="src/app.txt")
        r = bus.send(self.root, A2, "", "yes, I am done with it", kind="answer", reply_to=q["id"])
        self.assertEqual((r["to"], r["request"], r["subject"], r["reply_to"]), (A1, "REQ-X-1", "src/app.txt", q["id"]))
        self.assertEqual([m["id"] for m in bus.read(self.root, request="REQ-X-1")], [q["id"], r["id"]])
        self.assertEqual([m["id"] for m in bus.read(self.root, involving=A3)], [])

    def test_refusals_unknown_recipient_empty_unknown_kind_too_long_self(self):
        for kw, code in (({"to": "nobody", "body": "x"}, proc.NOT_FOUND), ({"to": "all", "body": "  "}, proc.INVALID),
                         ({"to": "all", "body": "x", "kind": "order"}, proc.INVALID),
                         ({"to": "all", "body": "x" * (bus.MAX_BODY + 1)}, proc.INVALID),
                         ({"to": self.a["name"], "body": "x"}, proc.INVALID)):
            with self.assertRaises(bus.MessageError) as cm:
                bus.send(self.root, A1, kw["to"], kw["body"], kind=kw.get("kind", "note"))
            self.assertEqual(cm.exception.code, code, kw)
        self.assertEqual(bus.read(self.root), [])

    def test_the_operator_can_write_and_be_written_to_without_a_roster_entry(self):
        m = bus.send(self.root, bus.OPERATOR, "all", "stand by")
        self.assertEqual(m["from_name"], "operator")
        bus.send(self.root, A1, "operator", "need a decision", kind="question")
        self.assertEqual(len(bus.roster(self.root)), 3)
        self.assertEqual([x["to"] for x in bus.read(self.root)], ["all", "operator"])

    def test_concurrent_senders_get_strictly_increasing_sequence_numbers(self):
        def worker(agent_id, n):
            for i in range(n):
                bus.send(self.root, agent_id, "all", f"{agent_id} {i}")
        threads = [threading.Thread(target=worker, args=(a, 15)) for a in (A1, A2, A3)]
        procs = [subprocess.Popen([sys.executable, str(SCRIPTS / "msg.py"), "send", "--to", "all", f"cli {i}"],
                                  cwd=str(self.root), env={**self.p.env, "AGENT_ID": A1},
                                  stdout=subprocess.DEVNULL) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual([p.wait(timeout=60) for p in procs], [0] * 6)
        seqs = [m["seq"] for m in bus.read(self.root)]
        self.assertEqual(seqs, list(range(1, 52)))
        self.assertEqual(bus.last_seq(self.root), 51)

    def test_a_torn_line_in_the_log_is_skipped(self):
        bus.send(self.root, A1, "all", "before")
        with open(bus.log_path(self.root), "a") as f:
            f.write('{"seq": 2, "id": "MSG-torn", "bo\n')
        bus.send(self.root, A1, "all", "after")
        self.assertEqual([m["body"] for m in bus.read(self.root)], ["before", "after"])

    def test_a_message_changes_no_claim_and_no_request(self):
        name = self.p.request("Document the widget")
        self.p.claim(name, A1)
        before = self.p.claim_data(name)
        bus.send(self.root, A2, A1, "give me that request", kind="handoff", request=name)
        bus.send(self.root, bus.OPERATOR, "all", "everything is released", kind="alert")
        after = self.p.claim_data(name)
        self.assertEqual((after["agent_id"], after["files"], self.p.where(name)),
                         (before["agent_id"], before["files"], ["processing"]))
        self.assertNotEqual(self.p.run("claim.py", "--claim-request", name, agent=A2).returncode, 0)


class Cli(BusBase):
    def msg(self, *args, agent=A1, **kw):
        return self.p.run("msg.py", *args, agent=agent, **kw)

    def test_join_send_inbox_reply_who_and_log(self):
        self.assertIn("JOINED as scout", self.p.ok("msg.py", "join", "--name", "scout", "--role", "lead", agent=A1))
        self.assertIn("JOINED as", self.p.ok("msg.py", "join", agent=A2))
        sent = self.p.ok("msg.py", "send", "--to", "lead", "--kind", "question", "--request", "REQ-X-9",
                         "which", "module", "first?", agent=A2)
        mid = sent.split()[1]
        inbox = json.loads(self.p.ok("msg.py", "inbox", "--json", agent=A1))
        self.assertEqual([(m["id"], m["body"], m["kind"]) for m in inbox], [(mid, "which module first?", "question")])
        self.p.ok("msg.py", "reply", mid, "core", "first", agent=A1)
        got = json.loads(self.p.ok("msg.py", "inbox", "--json", agent=A2))
        self.assertEqual((got[0]["body"], got[0]["kind"], got[0]["reply_to"], got[0]["request"]),
                         ("core first", "answer", mid, "REQ-X-9"))
        who = json.loads(self.p.ok("msg.py", "who", "--json", agent=A1))
        self.assertEqual({w["name"] for w in who} >= {"scout"}, True)
        self.assertIn("core first", self.p.ok("msg.py", "log", agent=A1))
        self.assertIn("no unread messages", self.p.ok("msg.py", "inbox", agent=A1))

    def test_wait_returns_on_a_message_and_times_out_with_exit_4(self):
        self.p.ok("msg.py", "join", agent=A1)
        self.assertEqual(self.msg("wait", "--timeout", "0.3").returncode, 4)
        threading.Timer(0.5, lambda: bus.send(self.root, bus.OPERATOR, A1, "go ahead")).start()
        cp = self.msg("wait", "--timeout", "20")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("go ahead", cp.stdout)

    def test_errors_use_the_tooling_exit_codes_and_no_agent_id_is_refused(self):
        self.assertEqual(self.msg("send", "--to", "nobody", "x").returncode, proc.NOT_FOUND)
        self.assertEqual(self.p.run("msg.py", "inbox").returncode, proc.INVALID)
        self.assertEqual(self.p.run("msg.py", "--as-operator", "inbox").returncode, proc.INVALID)
        self.assertIn("SENT", self.p.ok("msg.py", "--as-operator", "send", "--to", "all", "hello"))

    def test_the_claim_tool_tells_an_agent_about_unread_messages_on_stderr(self):
        name = self.p.request("Document the gadget")
        cp = self.p.run("claim.py", "--claim-request", name, agent=A1)
        self.assertEqual(cp.returncode, 0)
        self.assertNotIn("MESSAGES:", cp.stderr)                  # nothing to tell
        self.p.ok("msg.py", "join", agent=A1)
        bus.send(self.root, bus.OPERATOR, A1, "check the gadget spec")
        cp = self.p.run("claim.py", "--heartbeat", name, agent=A1)
        self.assertEqual((cp.returncode, cp.stdout.strip()), (0, "HEARTBEAT ok"))   # parsed output unchanged
        self.assertRegex(cp.stderr, r"MESSAGES: 1 unread for \S+: read them with scripts/msg.py inbox")
        self.p.ok("msg.py", "inbox", agent=A1)
        self.assertNotIn("MESSAGES:", self.p.run("claim.py", "--heartbeat", name, agent=A1).stderr)


class Worktrees(BusBase):
    def test_a_linked_worktree_shares_the_roster_and_the_bus_and_shows_its_branch(self):
        wt = self.p.base / "wt-feature"
        self.p.git("worktree", "add", "-q", "-b", "request/feature", str(wt))
        self.assertEqual(bus.coordination_dir(wt), bus.coordination_dir(self.root))
        bus.register(self.root, A1, cwd=self.root)
        there = bus.register(wt, A2, cwd=wt)
        self.assertEqual((there["branch"], Path(there["worktree"]).resolve()), ("request/feature", wt.resolve()))
        bus.send(wt, A2, "all", "written in the worktree")
        self.assertEqual([m["body"] for m in bus.inbox(self.root, A1)], ["written in the worktree"])
        # the tools run inside the worktree see the same conversation
        env = {k: v for k, v in self.p.env.items() if k != "AGENT_PROCESS_ROOT"}
        cp = subprocess.run([sys.executable, str(SCRIPTS / "msg.py"), "log", "--json"], cwd=str(wt),
                            env={**env, "AGENT_PROCESS_ROOT": str(wt), "AGENT_ID": A2}, capture_output=True, text=True)
        self.assertEqual([m["body"] for m in json.loads(cp.stdout)], ["written in the worktree"], cp.stderr)
        trees = overview.worktrees(self.root)
        self.assertEqual([(t["branch"], t["main"]) for t in trees], [("main", True), ("request/feature", False)])
        agents = {a["agent_id"]: a for a in overview.agents(self.root)}
        self.assertEqual(agents[A2]["branch"], "request/feature")


class Profiles(unittest.TestCase):
    def project(self, **workers):
        p = Project(worker_config(**workers))
        self.addCleanup(p.close)
        return p

    def test_a_slot_runs_its_assigned_profile_and_the_default_otherwise(self):
        p = self.project(agent={"command": ["base-agent", "{name}"], "env_scrub": ["KEEP_OUT"]},
                         profiles={"alt": {"command": ["{scripts}/wrap", "--id", "{session_id}"]}},
                         assign={"worker-02": "alt"})
        self.assertEqual(cfgmod.validate(p.root), [])
        self.assertEqual((wagent.profile_name(p.root, "worker-01"), wagent.profile_name(p.root, "worker-02")),
                         ("default", "alt"))
        kw = {"session_id": "S", "name": "N", "attempt_dir": Path("/x")}
        self.assertEqual(wagent.build_cmd(p.root, worker="worker-01", **kw), ["base-agent", "N"])
        self.assertEqual(wagent.build_cmd(p.root, worker="worker-02", **kw), [f"{SCRIPTS}/wrap", "--id", "S"])
        # a profile overrides key by key: the scrub list of the default still applies
        self.assertEqual(wagent.agent_settings(p.root, "worker-02")["env_scrub"], ["KEEP_OUT"])

    def test_an_unknown_assigned_profile_is_a_configuration_problem_never_a_silent_default(self):
        p = self.project(agent={"command": ["base-agent"]}, assign={"worker-01": "ghost"})
        self.assertTrue(any("ghost" in x for x in cfgmod.validate(p.root)))
        with self.assertRaises(wagent.NotConfigured):
            wagent.agent_settings(p.root, "worker-01")
        from workers import pool
        st = pool.status(p.root)
        self.assertFalse(st["agent_configured"])
        with self.assertRaises(proc.ProcessError) as cm:
            pool.start(p.root, "worker-01")
        self.assertEqual(cm.exception.code, cfgmod.NOT_CONFIGURED)

    def test_a_worker_with_a_profile_runs_it_and_its_agent_joins_the_roster(self):
        p = Project(worker_config(), install=True)
        self.addCleanup(p.close)
        cfg = json.loads(p.read("devsys.json"))
        fake = str(p.root / FAKE)
        cfg["workers"]["agent"] = {"command": [sys.executable, fake]}
        cfg["workers"]["profiles"] = {"alt": {"command": [sys.executable, fake, "--alt-profile"]}}
        cfg["workers"]["assign"] = {"worker-02": "alt"}
        p.write("devsys.json", json.dumps(cfg, indent=2))
        p.commit_all("agents")
        log = p.base / "fake-agent.jsonl"
        p.request("Profile page [fake:complete]")
        p.ok("devsys.py", "workers", "run", "worker-02", "--once", env={"FAKE_AGENT_LOG": str(log)}, timeout=240)
        launches = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual(launches[0]["argv"], ["--alt-profile"])
        st = json.loads(p.ok("devsys.py", "workers", "status", "--json"))
        self.assertEqual([(w["id"], w["profile"]) for w in st["workers"]],
                         [("worker-01", "default"), ("worker-02", "alt")])
        who = json.loads(p.ok("msg.py", "who", "--json", agent=A1))
        entry = next(w for w in who if w["worker"] == "worker-02")
        self.assertEqual((entry["profile"], entry["agent_id"]), ("alt", launches[0]["env"]["AGENT_ID"]))
        self.assertIn(entry["name"], bus.NAME_POOL)


class Adapter(unittest.TestCase):
    """The adapter that turns another agent's event stream into the worker
    protocol (scripts/workers/agent.py)."""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location("stream_adapter", adapter())
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)

    V1 = [{"type": "step_start", "sessionID": "ses_1", "part": {"type": "step-start"}},
          {"type": "tool_use", "sessionID": "ses_1", "part": {"type": "tool", "tool": "bash", "callID": "c1",
                                                              "state": {"status": "completed", "input": {"command": "ls"}}}},
          {"type": "step_finish", "sessionID": "ses_1", "part": {"type": "step-finish", "reason": "tool-calls", "cost": 0.01}},
          {"type": "text", "sessionID": "ses_1", "part": {"type": "text", "text": "all done"}},
          {"type": "step_finish", "sessionID": "ses_1", "part": {"type": "step-finish", "reason": "stop", "cost": 0.02}}]

    def test_version_1_events_become_assistant_blocks_and_a_success_result(self):
        tr = self.mod.Translator(None)
        out = [e for ev in self.V1 for e in tr.feed(ev)]
        self.assertEqual([e["message"]["content"][0]["type"] for e in out], ["tool_use", "text"])
        self.assertEqual(out[0]["message"]["content"][0]["name"], "bash")
        res = tr.result(0, 1200)
        self.assertEqual((res["type"], res["is_error"], res["result"], res["num_turns"], res["total_cost_usd"],
                          res["session_id"]), ("result", False, "all done", 2, 0.03, "ses_1"))

    def test_an_error_event_or_a_failing_exit_is_an_error_result(self):
        tr = self.mod.Translator("S")
        tr.feed({"type": "error", "sessionID": "ses_2",
                 "error": {"name": "APIError", "data": {"message": "usage limit reached"}}})
        res = tr.result(1, 5)
        self.assertEqual((res["is_error"], res["subtype"], res["result"], res["session_id"]),
                         (True, "error", "usage limit reached", "S"))
        self.assertTrue(self.mod.Translator("S").result(3, 5)["is_error"])

    def test_bus_shaped_events_are_translated_once_per_finished_part(self):
        tr = self.mod.Translator("S")
        part = {"id": "p1", "type": "text", "text": "partial"}
        done = {"id": "p1", "type": "text", "text": "complete answer", "time": {"start": 1, "end": 2}}
        tool = {"id": "p2", "type": "tool", "tool": "edit", "callID": "c9", "state": {"status": "completed", "input": {}}}
        events = [{"type": "message.part.updated", "properties": {"part": x}} for x in (part, done, done, tool, tool)]
        out = [e for ev in events for e in tr.feed(ev)]
        self.assertEqual([e["message"]["content"][0]["type"] for e in out], ["text", "tool_use"])
        self.assertEqual(tr.result(0, 1)["result"], "complete answer")
        tr.feed({"type": "session.error", "properties": {"error": {"data": {"message": "boom"}}}})
        self.assertEqual(tr.result(0, 1)["result"], "boom")

    def test_wrapped_command_gets_the_prompt_and_the_transcript_parses_as_a_normal_session(self):
        tmp = FRAMEWORK / "tmp" / "devsys-tests"
        tmp.mkdir(parents=True, exist_ok=True)
        script = tmp / f"stream-{os.getpid()}.py"
        script.write_text("import json, sys\nprompt = sys.stdin.read()\nprint('not json noise')\n"
                          "for e in json.loads(sys.argv[1]): print(json.dumps(e))\n"
                          "print(json.dumps({'type': 'text', 'part': {'type': 'text', 'text': 'echo ' + prompt}}))\n")
        out = tmp / f"stream-{os.getpid()}.jsonl"
        try:
            cp = subprocess.run([sys.executable, str(adapter()), "--session-id", "SID", "--", sys.executable,
                                 str(script), json.dumps(self.V1)], input="the prompt", capture_output=True, text=True)
            self.assertEqual(cp.returncode, 0, cp.stderr)
            self.assertIn("not json noise", cp.stderr)             # never in the transcript
            out.write_text(cp.stdout)
            info = wagent.parse_transcript(out)
            self.assertEqual((info["bad_lines"], info["tool_uses"], info["assistant_messages"], info["last_text"]),
                             (0, 1, 3, "echo the prompt"))
            self.assertEqual((info["result"]["is_error"], info["result"]["result"], info["init"]["cwd"]),
                             (False, "echo the prompt", os.getcwd()))
            failing = subprocess.run([sys.executable, str(adapter()), "--", sys.executable, "-c", "import sys; sys.exit(3)"],
                                     input="x", capture_output=True, text=True)
            self.assertEqual(failing.returncode, 3)
            self.assertTrue(json.loads(failing.stdout.splitlines()[-1])["is_error"])
        finally:
            script.unlink(missing_ok=True)
            out.unlink(missing_ok=True)


class Overview(BusBase):
    config = {"workers": {"max_workers": 2}}

    def test_snapshot_joins_workers_agents_claims_worktrees_and_messages(self):
        name = self.p.request("Document the lever", "--priority", "HIGH")
        self.p.claim(name, A1)
        bus.register(self.root, A1, profile="interactive", cwd=self.root)
        bus.send(self.root, A1, "all", "started on the lever", kind="status")
        queued = self.p.request("Document the pulley")
        snap = overview.snapshot(self.root)
        self.assertEqual([w["id"] for w in snap["pool"]["workers"]], ["worker-01", "worker-02"])
        self.assertEqual([q["request"] for q in snap["pool"]["queue"]], [queued])
        me = next(a for a in snap["agents"] if a["agent_id"] == A1)
        self.assertEqual((me["profile"], me["claims"][0]["request"], me["branch"]), ("interactive", name, "main"))
        self.assertEqual([m["body"] for m in snap["messages"]], ["started on the lever"])
        self.assertEqual(snap["last_seq"], 1)
        self.assertEqual(overview.snapshot(self.root, after=1)["messages"], [])
        json.dumps(snap, default=str)                             # what the panels send

    def test_an_agent_with_a_claim_but_no_roster_entry_is_still_shown(self):
        name = self.p.request("Document the spring")
        self.p.claim(name, A2)
        shown = [a for a in overview.agents(self.root) if a["agent_id"] == A2]
        self.assertEqual([(a["registered"], a["name"], a["claims"][0]["request"]) for a in shown], [(False, A2, name)])

    def test_operator_actions_message_priority_submit_and_refusals(self):
        bus.register(self.root, A1)
        out = overview.act(self.root, "message", to="all", body="please rebase", kind="alert")
        self.assertEqual((out["message"]["from"], bus.unread(self.root, A1)[0]["body"]), ("operator", "please rebase"))
        queued = self.p.request("Document the hinge")
        self.assertEqual(overview.act(self.root, "priority", request=queued, priority="critical")["to"], "CRITICAL")
        self.assertEqual(overview.snapshot(self.root)["pool"]["queue"][0]["priority"], "CRITICAL")
        made = overview.act(self.root, "submit", text="Add a page about the axle", priority="LOW")["created"]
        self.assertEqual((made["status"], made["priority"]), ("inbox", "LOW"))
        self.assertIn(made["file"], self.p.where(made["file"]) and [q["request"] for q in overview.snapshot(self.root)["pool"]["queue"]])
        self.assertEqual(overview.act(self.root, "disable", worker="worker-02")["results"][0]["enabled"], False)
        for action, kw, code in (("start", {"worker": "worker-01"}, cfgmod.NOT_CONFIGURED),
                                 ("start", {"worker": "worker-99"}, proc.NOT_FOUND),
                                 ("priority", {"request": queued, "priority": "urgent"}, proc.INVALID),
                                 ("message", {"to": "ghost", "body": "x"}, proc.NOT_FOUND),
                                 ("submit", {"text": " "}, proc.INVALID), ("explode", {}, proc.INVALID)):
            with self.assertRaises(proc.ProcessError) as cm:
                overview.act(self.root, action, **kw)
            self.assertEqual(cm.exception.code, code, action)
        # "all" reports per worker instead of failing on the first
        results = overview.act(self.root, "start", worker="all")["results"]
        self.assertEqual([r["worker"] for r in results if r.get("error")], ["worker-01"])   # worker-02 is disabled


class WebPanel(BusBase):
    config = {"workers": {"max_workers": 1}}

    def setUp(self):
        super().setUp()
        import web_panel
        self.srv = web_panel.serve(self.root, "127.0.0.1", 0, token="test-token")
        self.srv.interval = 0.1
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        bus.register(self.root, A1)

    def tearDown(self):
        self.srv.closing.set()
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def call(self, method, path, body=None, token="test-token", host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        headers = {"X-Panel-Token": token} if token else {}
        if host:
            headers["Host"] = host
        if body is not None:
            headers["Content-Type"] = "application/json"
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, data

    def test_the_page_is_served_with_the_session_token(self):
        status, page = self.call("GET", "/", token=None)
        self.assertEqual(status, 200)
        self.assertIn(b'const TOKEN = "test-token"', page)
        self.assertNotIn(b"__PANEL_TOKEN__", page)

    def test_api_calls_need_the_token_and_the_servers_own_host_name(self):
        self.assertEqual(self.call("GET", "/api/snapshot", token=None)[0], 403)
        self.assertEqual(self.call("GET", "/api/snapshot", token="wrong")[0], 403)
        self.assertEqual(self.call("GET", "/api/snapshot", host=f"evil.example:{self.port}")[0], 403)
        self.assertEqual(self.call("POST", "/api/act", {"action": "message", "to": "all", "body": "x"}, token=None)[0], 403)
        self.assertEqual(self.call("GET", "/", token=None, host="evil.example")[0], 403)
        self.assertEqual(bus.read(self.root), [])
        status, data = self.call("GET", "/api/snapshot")
        self.assertEqual((status, sorted(json.loads(data))[:3]), (200, ["agents", "at", "kinds"]))

    def test_the_operator_writes_to_agents_and_controls_requests_through_the_panel(self):
        status, data = self.call("POST", "/api/act", {"action": "message", "to": "all", "body": "hello from the panel",
                                                      "kind": "status"})
        self.assertEqual(status, 200, data)
        self.assertEqual([m["body"] for m in bus.inbox(self.root, A1)], ["hello from the panel"])
        queued = self.p.request("Document the valve")
        self.assertEqual(self.call("POST", "/api/act", {"action": "priority", "request": queued, "priority": "HIGH"})[0], 200)
        status, data = self.call("POST", "/api/act", {"action": "submit", "text": "Add a page about the piston"})
        self.assertEqual((status, json.loads(data)["created"]["status"]), (200, "inbox"))
        status, data = self.call("GET", "/api/peek?worker=worker-01")
        self.assertEqual((status, json.loads(data)["id"]), (200, "worker-01"))

    def test_refusals_carry_the_reason_and_a_fitting_status(self):
        for body, want in (({"action": "start", "worker": "worker-01"}, 409),      # agent not configured
                           ({"action": "message", "to": "ghost", "body": "x"}, 404),
                           ({"action": "explode"}, 400), (["not", "an", "object"], 400)):
            status, data = self.call("POST", "/api/act", body)
            self.assertEqual(status, want, data)
            self.assertTrue(json.loads(data)["error"])
        self.assertEqual(self.call("GET", "/api/peek?worker=worker-77")[0], 404)
        self.assertEqual(self.call("GET", "/api/nothing")[0], 404)

    def test_the_event_stream_sends_a_snapshot_and_again_when_a_message_arrives(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        c.request("GET", "/api/events?token=test-token")
        r = c.getresponse()
        self.assertEqual((r.status, r.getheader("Content-Type")), (200, "text/event-stream"))

        def next_snapshot():
            while True:
                line = r.readline().decode()
                self.assertTrue(line, "stream ended")
                if line.startswith("data: "):
                    return json.loads(line[6:])
        self.assertEqual(next_snapshot()["messages"], [])
        bus.send(self.root, A1, "all", "live update")
        self.assertEqual([m["body"] for m in next_snapshot()["messages"]], ["live update"])
        c.close()


class Dashboard(BusBase):
    config = {"workers": {"max_workers": 1}}

    def test_one_frame_shows_workers_agents_requests_and_the_conversation(self):
        import dash
        me = bus.register(self.root, A1, profile="interactive", cwd=self.root)
        name = self.p.request("Document the gear")
        self.p.claim(name, A1)
        bus.send(self.root, A1, "all", "a long message " + "word " * 40, kind="status")
        text = dash.frame(overview.snapshot(self.root), 80)
        for part in ("WORKERS  0 running of 1", "worker-01 stopped", f"{me['name']}", "interactive", "REQUESTS  queued 0  working 1",
                     "MESSAGES", f"{me['name']} -> all [status]", "a long message"):
            self.assertIn(part, text)
        self.assertTrue(all(len(line) <= 80 for line in text.splitlines()), "a line is wider than the terminal")

    def test_once_prints_a_frame_without_a_terminal(self):
        cp = subprocess.run([sys.executable, str(FRAMEWORK / "tools" / "panel" / "dash.py"), "--root", str(self.root),
                             "--once"], capture_output=True, text=True, env=self.p.env, timeout=60)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("WORKERS", cp.stdout)
        cp = subprocess.run([sys.executable, str(FRAMEWORK / "tools" / "panel" / "dash.py"), "--root", str(self.root),
                             "--once", "--json"], capture_output=True, text=True, env=self.p.env, timeout=60)
        self.assertEqual(json.loads(cp.stdout)["pool"]["max_workers"], 1)

    def test_keys_act_through_the_same_operator_actions(self):
        import dash
        bus.register(self.root, A1)
        d = dash.Dash(self.root, 1.0)
        d.do("message", to="all", body="from the keyboard")
        self.assertIn("message ok", d.note)
        d.do("start", worker="worker-01")
        self.assertIn("refused", d.note)                           # no agent configured
        self.assertEqual([m["body"] for m in bus.unread(self.root, A1)], ["from the keyboard"])


if __name__ == "__main__":
    unittest.main()
