#!/usr/bin/env python3
"""Concurrency tests for the process tooling (ADR-000090, WORK-000167).

Each test builds a throw-away git repository with the request/work/evidence
layout and drives the real CLIs (scripts/create_request.py, claim.py,
allocate_id.py, commit_request.py) through AGENT_PROCESS_ROOT. Concurrent
cases start N real processes that block on a "go" file and are released at
the same instant, so they genuinely race.

Run: python3 -m unittest discover -s scripts/tests -v
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest

# Not the calling session's agent identity, and no coordination station (see devsys_testlib).
for _name in ("AGENT_ID", "AGENT_OWNER_PID"):
    __import__("os").environ.pop(_name, None)
__import__("os").environ["DEVSYS_COORDINATION_AUTOSTART"] = "0"
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import agent_process as proc  # noqa: E402

WAITER = """import os, sys, time
go = sys.argv[1]
while not os.path.exists(go):
    time.sleep(0.001)
os.execv(sys.executable, [sys.executable] + sys.argv[2:])
"""
TODAY = proc.request_date()["yyyymmdd"]  # the intake names the series after the local system date (REQ-DEVOPS-045)


class Repo:
    def __init__(self):
        # Project temp area (AGENTS.md: tmp/, not the system /tmp).
        tmp_base = SCRIPTS.parent / "tmp" / "process-tests"
        tmp_base.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix="process-concurrency-", dir=str(tmp_base))
        self.base = Path(self.tmp.name)
        self.root = self.base / "repo"
        self.root.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Process Test")
        for s in proc.REQUEST_STATES:
            self.write(f"requests/{s}/README.md", f"# {s}\n")
        for d in ("work/active", "work/completed", "work/cancelled"):
            self.write(f"{d}/README.md", "#\n")
        self.write("work/work.txt", "# Work manifest\n")
        self.write("evidence/evidence.txt", "# Evidence manifest\n")
        self.write("docs/decisions/decisions.txt", "# Decisions\n")
        self.write(".gitignore", ".agent/state/claims/\n.agent/state/runtime/\n")
        # These tests exercise transition and commit mechanics; the completion
        # gate enforcement is covered by test_lifecycle_hardening.py.
        self.write("devsys.json", json.dumps({"completion": {"enforce_on_transition": False}}) + "\n")
        self.write("src/a.cpp", "int a = 1;\n")
        self.write("src/b.cpp", "int b = 1;\n")
        self.write("notes/u.txt", "unrelated\n")
        self.commit_all("initial")
        self.env = {k: v for k, v in os.environ.items() if k != "AGENT_ID"}
        self.env["AGENT_PROCESS_ROOT"] = str(self.root)

    def close(self):
        self.tmp.cleanup()

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=str(cwd or self.root), capture_output=True, text=True, check=True).stdout

    def write(self, relpath, text, root=None):
        p = (root or self.root) / relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def commit_all(self, msg):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)

    def run(self, script, *args, agent=None, root=None):
        env = dict(self.env)
        if root is not None:
            env["AGENT_PROCESS_ROOT"] = str(root)
        cmd = [sys.executable, str(SCRIPTS / script), *args]
        if agent:
            cmd += ["--agent-id", agent]
        return subprocess.run(cmd, cwd=str(root or self.root), env=env, capture_output=True, text=True, timeout=120)

    def race(self, commands):
        """commands: list of (script, args, agent). Returns list of CompletedProcess-like tuples."""
        waiter = self.base / "waiter.py"
        waiter.write_text(WAITER)
        go = self.base / f"go-{time.time_ns()}"
        procs = []
        for script, args, agent in commands:
            cmd = [sys.executable, str(waiter), str(go), str(SCRIPTS / script), *args]
            if agent:
                cmd += ["--agent-id", agent]
            procs.append(subprocess.Popen(cmd, cwd=str(self.root), env=self.env, stdout=subprocess.PIPE,
                                          stderr=subprocess.PIPE, text=True))
        time.sleep(0.8)  # every process is now polling for the go file
        go.write_text("go")
        out = []
        for p in procs:
            so, se = p.communicate(timeout=180)
            out.append((p.returncode, so, se))
        return out

    def add_request(self, state, rid, slug="demo", commit=True):
        name = f"2026-09-14-{slug}-{rid.lower()}.md"
        self.write(f"requests/{state}/{name}", f"# Request: {slug}\n\n**Request ID:** {rid}\n**Status:** {state}\n")
        self.write(f"requests/{state}/{Path(name).with_suffix('.json')}",
                   json.dumps({"request_id": rid, "status": state}) + "\n")
        if commit:
            self.commit_all(f"add {rid}")
        return name

    def locations(self, name):
        return [s for s, _ in proc.locate_request(self.root, name)]

    def claim(self, name):
        return proc.read_claim(proc.lock_path(self.root, name))


class Base(unittest.TestCase):
    def setUp(self):
        self.repo = Repo()

    def tearDown(self):
        self.repo.close()

    def rid_of(self, cp):
        m = re.search(r"Request ID: (\S+)", cp.stdout)
        self.assertIsNotNone(m, cp.stdout + cp.stderr)
        return m.group(1)


# ----------------------------------------------------------------- request ids

class RequestIdAllocation(Base):
    def test_completed_request_id_is_never_reused(self):
        # The date now, not the one at import: a suite that runs across local
        # midnight would otherwise compare against yesterday's series.
        today = proc.request_date()["yyyymmdd"]
        self.repo.add_request("completed", f"REQ-CHAT-{today}-007", "done")
        cp = self.repo.run("create_request.py", "--source", "chat", "--text", "a brand new request")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        rid = self.rid_of(cp)
        self.assertNotEqual(rid, f"REQ-CHAT-{today}-007")
        self.assertGreater(int(rid.rsplit("-", 1)[1]), 7)

    def test_rejected_and_needs_review_ids_are_never_reused(self):
        today = proc.request_date()["yyyymmdd"]
        self.repo.add_request("rejected", f"REQ-CHAT-{today}-011", "rej")
        self.repo.add_request("needs-review", f"REQ-CHAT-{today}-012", "nr")
        rid = self.rid_of(self.repo.run("create_request.py", "--source", "chat", "--text", "another request"))
        self.assertGreaterEqual(int(rid.rsplit("-", 1)[1]), 13)

    def test_scenario_a_archived_work_id_is_never_reused(self):
        self.repo.write("work/completed/WORK-000123-archived.md", "# WORK-000123\n")
        self.repo.write("work/work.txt", "# Work manifest\nWORK-000123 | completed | x | completed/WORK-000123-archived.md\n")
        first = self.repo.run("allocate_id.py", "WORK")
        self.assertEqual(first.stdout.strip(), "WORK-000124", first.stderr)
        # Even if the archived record and the fresh allocation disappear from disk,
        # the monotonic counter never hands 123 or 124 out again.
        (self.repo.root / "work/completed/WORK-000123-archived.md").unlink()
        self.repo.write("work/work.txt", "# Work manifest\n")
        second = self.repo.run("allocate_id.py", "WORK")
        self.assertEqual(second.stdout.strip(), "WORK-000125")

    def test_concurrent_intake_receives_unique_ids(self):
        n = 16
        results = self.repo.race([("create_request.py", ["--source", "chat", "--text", f"concurrent request {i}"], None)
                                  for i in range(n)])
        ids = []
        for rc, so, se in results:
            self.assertEqual(rc, 0, se)
            ids.append(re.search(r"Request ID: (\S+)", so).group(1))
        self.assertEqual(len(set(ids)), n, ids)
        inbox_md = [p for p in (self.repo.root / "requests/inbox").glob("*.md") if p.name != "README.md"]
        self.assertEqual(len(inbox_md), n)
        for rid in ids:
            self.assertEqual(len([p for p in proc.find_request_files_by_id(self.repo.root, rid) if p.suffix == ".md"]), 1)

    def test_concurrent_work_id_allocation_is_unique(self):
        results = self.repo.race([("allocate_id.py", ["WORK"], None) for _ in range(16)])
        ids = [so.strip() for rc, so, se in results if rc == 0]
        self.assertEqual(len(ids), 16)
        self.assertEqual(len(set(ids)), 16, ids)

    def test_collision_never_overwrites_existing_request(self):
        name = self.repo.add_request("completed", f"REQ-CHAT-{TODAY}-020", "held")
        held = (self.repo.root / "requests/completed" / name).read_bytes()
        with self.assertRaises(proc.ProcessError) as e:
            proc.create_request_files(self.repo.root, f"REQ-CHAT-{TODAY}-020", "new-file.md", "# new\n", {"x": 1})
        self.assertEqual(e.exception.code, proc.COLLISION)
        self.assertFalse((self.repo.root / "requests/inbox/new-file.md").exists())
        self.assertEqual((self.repo.root / "requests/completed" / name).read_bytes(), held)
        # A squatting file at the target path is never overwritten either, and the
        # half-created request is removed again.
        squat = self.repo.write("requests/inbox/squat.json", "foreign content\n")
        with self.assertRaises(proc.ProcessError) as e2:
            proc.create_request_files(self.repo.root, f"REQ-CHAT-{TODAY}-900", "squat.md", "# mine\n", {"x": 1})
        self.assertEqual(e2.exception.code, proc.COLLISION)
        self.assertEqual(squat.read_text(), "foreign content\n")
        self.assertFalse((self.repo.root / "requests/inbox/squat.md").exists())


# --------------------------------------------------------------------- claims

class ClaimOwnership(Base):
    def test_scenario_b_simultaneous_claim_has_exactly_one_owner(self):
        name = self.repo.add_request("inbox", f"REQ-CHAT-{TODAY}-030")
        agents = [f"agent-race-{i}" for i in range(8)]
        results = self.repo.race([("claim.py", ["--claim-request", name], a) for a in agents])
        winners = [agents[i] for i, (rc, so, se) in enumerate(results) if rc == 0]
        self.assertEqual(len(winners), 1, results)
        for i, (rc, so, se) in enumerate(results):
            if agents[i] != winners[0]:
                self.assertEqual(rc, proc.CONFLICT, se)
                self.assertIn("ALREADY CLAIMED", se)
                self.assertIn(winners[0], se)  # clear ownership information
        self.assertEqual(self.repo.claim(name)["agent_id"], winners[0])
        self.assertEqual(len(list(proc.claims_dir(self.repo.root).glob("*.lock"))), 1)
        self.assertEqual(self.repo.locations(name), ["processing"])
        self.assertIn("**Status:** processing", (self.repo.root / "requests/processing" / name).read_text())

    def test_same_agent_reclaim_is_idempotent_and_default_ids_are_refused(self):
        name = self.repo.add_request("inbox", f"REQ-CHAT-{TODAY}-031")
        self.assertEqual(self.repo.run("claim.py", "--claim-request", name, agent="agent-a").returncode, 0)
        again = self.repo.run("claim.py", "--claim-request", name, agent="agent-a")
        self.assertEqual(again.returncode, 0)
        self.assertIn("ALREADY OWNED", again.stdout)
        other = self.repo.add_request("inbox", f"REQ-CHAT-{TODAY}-032", "other")
        bad = self.repo.run("claim.py", "--claim-request", other, agent="agent-1000")
        self.assertEqual(bad.returncode, proc.INVALID)
        self.assertIsNone(self.repo.claim(other))
        self.assertEqual(self.repo.run("claim.py", "--claim-request", other).returncode, proc.INVALID)  # no id at all

    def test_non_owner_cannot_release_or_transition(self):
        name = self.repo.add_request("inbox", f"REQ-CHAT-{TODAY}-033")
        self.repo.run("claim.py", "--claim-request", name, agent="agent-a")
        for flag in ("--release-request", "--complete-request", "--reject-request", "--needs-review-request", "--heartbeat"):
            cp = self.repo.run("claim.py", flag, name, agent="agent-b")
            self.assertEqual(cp.returncode, proc.NOT_OWNER, flag + cp.stderr)
        self.assertEqual(self.repo.claim(name)["agent_id"], "agent-a")
        self.assertEqual(self.repo.locations(name), ["processing"])

    def test_concurrent_complete_reject_and_foreign_attempts_single_transition(self):
        name = self.repo.add_request("inbox", f"REQ-CHAT-{TODAY}-034")
        self.repo.run("claim.py", "--claim-request", name, agent="agent-a")
        cmds = ([("claim.py", ["--complete-request", name], "agent-a")] * 3
                + [("claim.py", ["--reject-request", name, "--reason", "race"], "agent-a")] * 2
                + [("claim.py", ["--complete-request", name], "agent-b")] * 2
                + [("claim.py", ["--claim-request", name], "agent-c")] * 2)
        results = self.repo.race(cmds)
        succeeded = [c for c, (rc, so, se) in zip(cmds, results) if rc == 0]
        self.assertEqual(len(succeeded), 1, results)
        locs = self.repo.locations(name)
        self.assertEqual(len(locs), 1)
        self.assertIn(locs[0], ("completed", "rejected"))
        for c, (rc, so, se) in zip(cmds, results):
            if c[2] == "agent-b":
                self.assertEqual(rc, proc.NOT_OWNER)
            if c[2] == "agent-c":
                self.assertEqual(rc, proc.CONFLICT)
        self.assertEqual(self.repo.claim(name)["agent_id"], "agent-a")
        self.assertEqual(self.repo.claim(name)["state"], locs[0])

    def test_release_requires_transition_or_requeue_and_completed_cannot_be_claimed(self):
        name = self.repo.add_request("inbox", f"REQ-CHAT-{TODAY}-035")
        self.repo.run("claim.py", "--claim-request", name, agent="agent-a")
        self.assertEqual(self.repo.run("claim.py", "--release-request", name, agent="agent-a").returncode, proc.BAD_STATE)
        self.assertEqual(self.repo.run("claim.py", "--complete-request", name, agent="agent-a").returncode, 0)
        # The request's move is uncommitted: release is refused until it is committed (COR-04).
        self.assertEqual(self.repo.run("claim.py", "--release-request", name, agent="agent-a").returncode, proc.BAD_STATE)
        self.assertEqual(self.repo.run("commit_request.py", "--request", name, "-m", "done", agent="agent-a").returncode, 0)
        self.assertEqual(self.repo.run("claim.py", "--release-request", name, agent="agent-a").returncode, 0)
        self.assertIsNone(self.repo.claim(name))
        self.assertEqual(self.repo.run("claim.py", "--claim-request", name, agent="agent-b").returncode, proc.BAD_STATE)


# ------------------------------------------------------------------- recovery

def make_stale(repo, name, hours=5):
    lp = proc.lock_path(repo.root, name)
    data = json.loads(lp.read_text())
    data["heartbeat_at"] = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    lp.write_text(json.dumps(data))


class Recovery(Base):
    def test_scenario_d_stale_owner_is_recovered_explicitly_and_cleanly(self):
        name = self.repo.add_request("inbox", f"REQ-CHAT-{TODAY}-040")
        self.repo.run("claim.py", "--claim-request", name, agent="agent-dead")
        self.assertEqual(self.repo.run("claim.py", "--own", name, "--paths", "src/a.cpp", agent="agent-dead").returncode, 0)
        make_stale(self.repo, name)
        # A plain claim does not steal it; it reports the stale owner.
        cp = self.repo.run("claim.py", "--claim-request", name, agent="agent-new")
        self.assertEqual(cp.returncode, proc.STALE)
        self.assertIn("agent-dead", cp.stderr)
        # --recover only reports.
        rep = self.repo.run("claim.py", "--recover")
        self.assertIn("STALE", rep.stdout)
        self.assertNotIn("README.md", rep.stdout)  # directory READMEs are not orphaned requests
        self.assertEqual(self.repo.claim(name)["agent_id"], "agent-dead")
        # Recovery needs a reason.
        self.assertEqual(self.repo.run("claim.py", "--recover-request", name, agent="agent-new").returncode, proc.INVALID)
        ok = self.repo.run("claim.py", "--recover-request", name, "--reason", "previous session crashed", agent="agent-new")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        data = self.repo.claim(name)
        self.assertEqual(data["agent_id"], "agent-new")
        self.assertEqual(data["history"][-1]["previous_agent_id"], "agent-dead")
        self.assertEqual(data["history"][-1]["reason"], "previous session crashed")
        self.assertIn("src/a.cpp", data["files"])  # ownership of in-flight paths transferred
        self.assertEqual(self.repo.locations(name), ["processing"])
        # The dead agent, if it comes back, no longer owns anything.
        self.assertEqual(self.repo.run("claim.py", "--heartbeat", name, agent="agent-dead").returncode, proc.NOT_OWNER)
        self.repo.write("src/a.cpp", "int a = 2;\n")
        late = self.repo.run("commit_request.py", "--request", name, "-m", "late", agent="agent-dead")
        self.assertEqual(late.returncode, proc.NOT_OWNER)

    def test_active_owner_is_not_recovered(self):
        name = self.repo.add_request("inbox", f"REQ-CHAT-{TODAY}-041")
        self.repo.run("claim.py", "--claim-request", name, agent="agent-live")
        cp = self.repo.run("claim.py", "--recover-request", name, "--reason", "impatient", agent="agent-other")
        self.assertEqual(cp.returncode, proc.CONFLICT)
        self.assertIn("still active", cp.stderr)
        self.assertEqual(self.repo.claim(name)["agent_id"], "agent-live")

    def test_concurrent_recovery_has_single_winner(self):
        name = self.repo.add_request("inbox", f"REQ-CHAT-{TODAY}-042")
        self.repo.run("claim.py", "--claim-request", name, agent="agent-dead")
        make_stale(self.repo, name)
        agents = [f"agent-rescue-{i}" for i in range(6)]
        results = self.repo.race([("claim.py", ["--recover-request", name, "--reason", "crash"], a) for a in agents])
        winners = [agents[i] for i, (rc, so, se) in enumerate(results) if rc == 0]
        self.assertEqual(len(winners), 1, results)
        self.assertEqual(self.repo.claim(name)["agent_id"], winners[0])
        self.assertEqual(len(self.repo.claim(name)["history"]), 1)

    def test_orphaned_processing_request_is_recoverable(self):
        name = self.repo.add_request("processing", f"REQ-CHAT-{TODAY}-043")
        self.assertEqual(self.repo.run("claim.py", "--claim-request", name, agent="agent-x").returncode, proc.STALE)
        self.assertEqual(self.repo.run("claim.py", "--recover-request", name, "--reason", "orphan", agent="agent-x").returncode, 0)
        self.assertEqual(self.repo.claim(name)["agent_id"], "agent-x")


# ---------------------------------------------------------- git commit safety

class GitCommitBoundaries(Base):
    def setUp(self):
        super().setUp()
        self.req_a = self.repo.add_request("inbox", f"REQ-CHAT-{TODAY}-050", "req-a")
        self.req_b = self.repo.add_request("inbox", f"REQ-CHAT-{TODAY}-051", "req-b")
        r = self.repo
        self.assertEqual(r.run("claim.py", "--claim-request", self.req_a, agent="agent-a").returncode, 0)
        self.assertEqual(r.run("claim.py", "--claim-request", self.req_b, agent="agent-b").returncode, 0)
        self.assertEqual(r.run("claim.py", "--own", self.req_a, "--paths", "src/a.cpp", agent="agent-a").returncode, 0)
        self.assertEqual(r.run("claim.py", "--own", self.req_b, "--paths", "src/b.cpp", agent="agent-b").returncode, 0)

    def committed_paths(self, rev="HEAD"):
        return set(self.repo.git("show", "--name-only", "--format=", rev).split())

    def test_scenario_c_commit_contains_only_the_committing_requests_changes(self):
        r = self.repo
        r.write("src/a.cpp", "int a = 2; // agent A work in progress\n")
        r.write("src/b.cpp", "int b = 2; // agent B work\n")
        r.write("notes/u.txt", "unrelated edit by a human\n")
        r.git("add", "src/a.cpp")  # agent A even staged its file in the shared index
        head_before = r.git("rev-parse", "HEAD").strip()
        cp = r.run("commit_request.py", "--request", self.req_b, "-m", "B: implement b", agent="agent-b")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("other-request", cp.stdout)
        self.assertIn("unowned", cp.stdout)
        paths = self.committed_paths()
        self.assertIn("src/b.cpp", paths)
        self.assertNotIn("src/a.cpp", paths)
        self.assertNotIn("notes/u.txt", paths)
        self.assertTrue(all(p == "src/b.cpp" or "req-b" in p for p in paths), paths)
        self.assertEqual(r.git("rev-parse", "HEAD~1").strip(), head_before)
        self.assertIn("Request-Id: REQ-CHAT-%s-051" % TODAY, r.git("log", "-1", "--format=%B"))
        # Agent A's work and the unrelated edit are untouched, A's staging intact.
        self.assertIn("agent A work in progress", (r.root / "src/a.cpp").read_text())
        self.assertIn("src/a.cpp", r.git("diff", "--cached", "--name-only").split())
        self.assertIn("notes/u.txt", r.git("diff", "--name-only").split())
        self.assertNotIn("src/b.cpp", r.git("status", "--porcelain"))
        self.assertEqual(self.repo.claim(self.req_b)["commits"][-1]["sha"], r.git("rev-parse", "HEAD").strip())

    def test_commit_of_another_requests_path_is_refused_and_nothing_is_committed(self):
        r = self.repo
        r.write("src/a.cpp", "int a = 3;\n")
        head = r.git("rev-parse", "HEAD").strip()
        cp = r.run("commit_request.py", "--request", self.req_b, "-m", "sweep", "--paths", "src/a.cpp", agent="agent-b")
        self.assertEqual(cp.returncode, proc.NOT_OWNER)
        self.assertIn("REQ-CHAT-%s-050" % TODAY, cp.stderr)
        self.assertEqual(r.git("rev-parse", "HEAD").strip(), head)

    def test_owning_a_path_owned_by_another_request_is_refused(self):
        cp = self.repo.run("claim.py", "--own", self.req_b, "--paths", "src/a.cpp", agent="agent-b")
        self.assertEqual(cp.returncode, proc.CONFLICT)
        self.assertIn("agent-a", cp.stderr)

    def test_unowned_dirty_path_needs_explicit_adoption(self):
        r = self.repo
        r.write("notes/u.txt", "someone's edit\n")
        self.assertEqual(r.run("claim.py", "--own", self.req_b, "--paths", "notes/u.txt", agent="agent-b").returncode,
                         proc.NOT_OWNER)
        self.assertEqual(r.run("claim.py", "--own", self.req_b, "--paths", "notes/u.txt", "--adopt-existing-changes",
                               agent="agent-b").returncode, 0)
        self.assertTrue(self.repo.claim(self.req_b)["files"]["notes/u.txt"]["adopted"])

    def test_shared_manifest_commits_only_own_lines(self):
        r = self.repo
        r.run("claim.py", "--token", self.req_a, "--tokens", "WORK-000201", agent="agent-a")
        r.run("claim.py", "--token", self.req_b, "--tokens", "WORK-000202", agent="agent-b")
        self.assertEqual(r.run("claim.py", "--own", self.req_a, "--paths", "work/work.txt", "--shared", agent="agent-a").returncode, 0)
        self.assertEqual(r.run("claim.py", "--own", self.req_b, "--paths", "work/work.txt", "--shared", agent="agent-b").returncode, 0)
        r.write("work/work.txt", "# Work manifest\nWORK-000201 | active | A item | a.md\nWORK-000202 | active | B item | b.md\n")
        cp = r.run("commit_request.py", "--request", self.req_b, "-m", "B manifest", "--paths", "work/work.txt", agent="agent-b")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        head_manifest = r.git("show", "HEAD:work/work.txt")
        self.assertIn("WORK-000202", head_manifest)
        self.assertNotIn("WORK-000201", head_manifest)
        self.assertIn("+WORK-000201", r.git("diff", "work/work.txt"))  # A's line is still A's to commit

    def test_shared_manifest_adjacent_foreign_edit_is_left_out(self):
        r = self.repo
        r.write("work/work.txt", "# Work manifest\nWORK-000166 | active | other | x.md\n")
        r.commit_all("manifest")
        r.run("claim.py", "--token", self.req_b, "--tokens", "WORK-000167", agent="agent-b")
        self.assertEqual(r.run("claim.py", "--own", self.req_b, "--paths", "work/work.txt", "--shared", agent="agent-b").returncode, 0)
        # Another agent edits the last line in place; this request appends right after it.
        r.write("work/work.txt", "# Work manifest\nWORK-000166 | completed | other | x.md\nWORK-000167 | active | mine | y.md\n")
        cp = r.run("commit_request.py", "--request", self.req_b, "-m", "B manifest", "--paths", "work/work.txt", agent="agent-b")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        head = r.git("show", "HEAD:work/work.txt")
        self.assertIn("WORK-000167 | active", head)
        self.assertIn("WORK-000166 | active", head)       # the foreign edit was not committed
        self.assertNotIn("WORK-000166 | completed", head)
        self.assertIn("WORK-000166 | completed", (r.root / "work/work.txt").read_text())  # and not lost

    # ---------------------------------------------------------- REQ-DEVOPS-042
    # Attribution without ids in the content (REQ-CHAT-20260920-011 sections
    # 8-13): two requests legitimately edit one user-facing prose document,
    # which contains no request ids anywhere. Committing one must leave the
    # other's work untouched and uncommitted.

    PROSE = ("# Workspace Guide\n\n"
             "## Overview\n\nThe workspace shows a project.\n\n"
             "## Legacy section\n\nText that request A is removing.\n\n"
             "## Evidence\n\nText that request B is rewriting.\n")

    def prose_requests(self, mode="--concurrent"):
        r = self.repo
        r.write("docs/user/workspace-guide.md", self.PROSE)
        r.commit_all("prose")
        for req, agent in ((self.req_a, "agent-a"), (self.req_b, "agent-b")):
            cp = r.run("claim.py", "--own", req, "--paths", "docs/user/workspace-guide.md", mode, agent=agent)
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return r

    def test_shared_prose_without_ids_commits_only_the_committing_request(self):
        r = self.prose_requests()
        # A removes its section and records that version as its own work.
        r.write("docs/user/workspace-guide.md",
                self.PROSE.replace("## Legacy section\n\nText that request A is removing.\n\n", ""))
        self.assertEqual(r.run("claim.py", "--snapshot", self.req_a, agent="agent-a").returncode, 0)
        # B then rewrites a different part of the same file.
        current = (r.root / "docs/user/workspace-guide.md").read_text()
        r.write("docs/user/workspace-guide.md",
                current.replace("Text that request B is rewriting.", "Rewritten by request B."))
        cp = r.run("commit_request.py", "--request", self.req_a, "-m", "A: drop the legacy section",
                   "--paths", "docs/user/workspace-guide.md", agent="agent-a")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        head = r.git("show", "HEAD:docs/user/workspace-guide.md")
        self.assertNotIn("Text that request A is removing.", head)   # A's change landed
        self.assertIn("Text that request B is rewriting.", head)     # B's did not
        # and B's work is still in the working tree, still B's to commit.
        self.assertIn("Rewritten by request B.", (r.root / "docs/user/workspace-guide.md").read_text())
        self.assertIn("not this request", cp.stdout)

    def test_shared_prose_baseline_survives_a_foreign_commit_in_between(self):
        # Registered --shared (an append-only manifest's mode) although the
        # content carries no ids: the token attribution finds nothing and the
        # delta takes over, so a mis-registered path still commits correctly.
        r = self.prose_requests("--shared")
        r.write("docs/user/workspace-guide.md",
                self.PROSE.replace("Text that request B is rewriting.", "Rewritten by request B."))
        cp = r.run("commit_request.py", "--request", self.req_b, "-m", "B: rewrite evidence",
                   "--paths", "docs/user/workspace-guide.md", agent="agent-b")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        # A now makes ITS change on top of B's committed one.
        current = (r.root / "docs/user/workspace-guide.md").read_text()
        r.write("docs/user/workspace-guide.md",
                current.replace("## Legacy section\n\nText that request A is removing.\n\n", ""))
        cp = r.run("commit_request.py", "--request", self.req_a, "-m", "A: drop the legacy section",
                   "--paths", "docs/user/workspace-guide.md", agent="agent-a")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        head = r.git("show", "HEAD:docs/user/workspace-guide.md")
        self.assertNotIn("Text that request A is removing.", head)
        self.assertIn("Rewritten by request B.", head)   # B's commit was not reverted by A's
        self.assertEqual(r.git("status", "--porcelain", "docs/user/workspace-guide.md").strip(), "")

    def test_overlapping_edits_are_refused_not_guessed(self):
        r = self.prose_requests()
        # B commits a rewrite of the very line A is also rewriting.
        r.write("docs/user/workspace-guide.md",
                self.PROSE.replace("Text that request A is removing.", "B's version of the same line."))
        self.assertEqual(r.run("commit_request.py", "--request", self.req_b, "-m", "B: same line",
                               "--paths", "docs/user/workspace-guide.md", agent="agent-b").returncode, 0)
        # A, which registered before that, rewrites the same line differently.
        r.write("docs/user/workspace-guide.md",
                self.PROSE.replace("Text that request A is removing.", "A's version of the same line."))
        head = r.git("rev-parse", "HEAD").strip()
        cp = r.run("commit_request.py", "--request", self.req_a, "-m", "A: same line",
                   "--paths", "docs/user/workspace-guide.md", agent="agent-a")
        self.assertEqual(cp.returncode, proc.CONFLICT, cp.stdout + cp.stderr)
        self.assertIn("overlap", cp.stderr)
        self.assertIn("--snapshot", cp.stderr)
        self.assertEqual(r.git("rev-parse", "HEAD").strip(), head)   # nothing committed

    def commit_without_touching_the_worktree(self, relpath, content, message):
        """Another session commits into `relpath` from its own private index,
        exactly as commit_request.py does -- so this session's working copy
        still holds the pre-commit content."""
        r = self.repo
        index = r.base / f"foreign-index-{time.time_ns()}"
        env = dict(os.environ, GIT_INDEX_FILE=str(index))
        run = lambda *a, **kw: subprocess.run(["git", *a], cwd=str(r.root), env=env, capture_output=True,
                                              text=kw.pop("text", True), check=True, **kw)
        blob = run("hash-object", "-w", "--stdin", input=content).stdout.strip()
        run("read-tree", "HEAD")
        run("update-index", "--add", "--cacheinfo", f"100644,{blob},{relpath}")
        tree = run("write-tree").stdout.strip()
        head = run("rev-parse", "HEAD").stdout.strip()
        commit = run("commit-tree", tree, "-p", head, "-m", message).stdout.strip()
        run("update-ref", "HEAD", commit, head)

    def test_exclusive_path_commits_only_the_delta_since_registration(self):
        r = self.repo
        # This request edits a file it holds exclusively...
        r.write("src/a.cpp", "int a = 1;\nint added_by_a_request = 1;\n")
        # ...and another session commits into the same file meanwhile, without
        # touching this working copy. Committing this request must add its own
        # line and must NOT revert the other session's commit.
        self.commit_without_touching_the_worktree(
            "src/a.cpp", "// header added by someone else\nint a = 1;\n", "foreign commit")
        cp = r.run("commit_request.py", "--request", self.req_a, "-m", "A: add a line", agent="agent-a")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        head = r.git("show", "HEAD:src/a.cpp")
        self.assertIn("int added_by_a_request = 1;", head)          # the request's delta landed
        self.assertIn("// header added by someone else", head)      # the foreign commit survived

    # ------------------------------------------------- REQ-DEVOPS-025 section 7
    # A healthy session whose short-lived helper processes have all exited is
    # not orphaned by a recovery sweep run from another session.

    def test_recovery_sweep_leaves_a_healthy_session_alone(self):
        r = self.repo
        before = json.dumps(self.repo.claim(self.req_a), sort_keys=True)
        for _ in range(2):      # idempotent: run the sweep twice
            cp = r.run("claim.py", "--recover", agent="agent-c")
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertIn("0 stale claim(s)", cp.stdout)
            self.assertIn("Nothing was removed", cp.stdout)
        # No PID is consulted anywhere: the claim records one, but the owner's
        # helper processes are long gone and the claim is still ACTIVE.
        claim = self.repo.claim(self.req_a)
        self.assertIn("pid", claim)
        self.assertFalse(proc.is_stale(claim))
        self.assertEqual(json.dumps(claim, sort_keys=True), before)
        self.assertEqual(self.repo.locations(self.req_a), ["processing"])
        # And another session cannot take it while the heartbeat is fresh.
        cp = r.run("claim.py", "--recover-request", self.req_a, "--reason", "sweeping", agent="agent-c")
        self.assertEqual(cp.returncode, proc.CONFLICT)
        self.assertIn("still active", cp.stderr)
        self.assertEqual(self.repo.claim(self.req_a)["agent_id"], "agent-a")

    def test_concurrent_registration_is_refused_against_an_exclusive_holder(self):
        r = self.repo
        cp = r.run("claim.py", "--own", self.req_b, "--paths", "src/a.cpp", "--concurrent", agent="agent-b")
        self.assertEqual(cp.returncode, proc.CONFLICT)
        self.assertIn("exclusive", cp.stderr)

    def test_snapshot_from_head_adopts_the_current_diff(self):
        r = self.repo
        r.write("docs/user/guide.md", "one\n")
        r.commit_all("guide")
        r.write("docs/user/guide.md", "one\ntwo added before registration\n")
        # Registered after the edit: the baseline is the edited content, so the
        # delta is empty and there is nothing to commit...
        self.assertEqual(r.run("claim.py", "--own", self.req_a, "--paths", "docs/user/guide.md",
                               "--concurrent", agent="agent-a").returncode, 0)
        cp = r.run("commit_request.py", "--request", self.req_a, "-m", "A: guide",
                   "--paths", "docs/user/guide.md", agent="agent-a")
        self.assertEqual(cp.returncode, proc.INVALID, cp.stdout + cp.stderr)
        # ...until the request says the current diff is its own.
        self.assertEqual(r.run("claim.py", "--snapshot", self.req_a, "--paths", "docs/user/guide.md",
                               "--from-head", agent="agent-a").returncode, 0)
        cp = r.run("commit_request.py", "--request", self.req_a, "-m", "A: guide",
                   "--paths", "docs/user/guide.md", agent="agent-a")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("two added before registration", r.git("show", "HEAD:docs/user/guide.md"))

    def test_snapshot_from_file_commits_a_prepared_version_only(self):
        r = self.prose_requests()
        # The working copy of the shared document holds this request's change
        # AND another session's, which must not be touched at all.
        r.write("docs/user/workspace-guide.md",
                self.PROSE.replace("Text that request A is removing.", "A's new text.")
                          .replace("Text that request B is rewriting.", "B's untouchable text."))
        prepared = r.base / "a-version.md"
        prepared.write_text(self.PROSE.replace("Text that request A is removing.", "A's new text."))
        self.assertEqual(r.run("claim.py", "--snapshot", self.req_a, "--paths", "docs/user/workspace-guide.md",
                               "--from-file", str(prepared), agent="agent-a").returncode, 0)
        cp = r.run("commit_request.py", "--request", self.req_a, "-m", "A: prepared version",
                   "--paths", "docs/user/workspace-guide.md", agent="agent-a")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        head = r.git("show", "HEAD:docs/user/workspace-guide.md")
        self.assertIn("A's new text.", head)
        self.assertNotIn("B's untouchable text.", head)
        self.assertIn("not this request", cp.stdout)
        # The working copy is untouched: both changes are still there.
        text = (r.root / "docs/user/workspace-guide.md").read_text()
        self.assertIn("B's untouchable text.", text)
        self.assertIn("A's new text.", text)

    # ------------------------------------------------ REQ-DEVOPS-042 file mode
    # The committed mode is the one a plain `git add` would record from the
    # working tree (REQ-AUTO-20260924-003, ADR-000193).

    def mode_of(self, relpath, rev="HEAD"):
        return self.repo.git("ls-tree", rev, "--", relpath).split()[0]

    def test_new_executable_file_is_committed_executable(self):
        r = self.repo
        self.assertEqual(r.run("claim.py", "--own", self.req_a, "--paths", "tools/run.sh", "tools/data.txt",
                               agent="agent-a").returncode, 0)
        os.chmod(r.write("tools/run.sh", "#!/bin/sh\necho run\n"), 0o755)
        r.write("tools/data.txt", "plain\n")
        cp = r.run("commit_request.py", "--request", self.req_a, "-m", "A: tools", agent="agent-a")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(self.mode_of("tools/run.sh"), "100755")
        self.assertEqual(self.mode_of("tools/data.txt"), "100644")
        self.assertEqual(r.git("status", "--porcelain", "--", "tools"), "")

    def test_mode_only_change_is_committed_with_the_content_unchanged(self):
        r = self.repo
        r.write("tools/tool.sh", "#!/bin/sh\necho tool\n")
        r.commit_all("tool, not executable yet")
        self.assertEqual(self.mode_of("tools/tool.sh"), "100644")
        self.assertEqual(r.run("claim.py", "--own", self.req_a, "--paths", "tools/tool.sh",
                               agent="agent-a").returncode, 0)
        os.chmod(r.root / "tools/tool.sh", 0o755)
        cp = r.run("commit_request.py", "--request", self.req_a, "-m", "A: tool is executable", agent="agent-a")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(self.mode_of("tools/tool.sh"), "100755")
        self.assertEqual(r.git("show", "HEAD:tools/tool.sh"), "#!/bin/sh\necho tool\n")
        self.assertEqual(r.git("status", "--porcelain", "--", "tools/tool.sh"), "")

    def test_new_executable_with_an_empty_delta_is_refused_then_adopted_executable(self):
        r = self.repo
        os.chmod(r.write("tools/late.sh", "#!/bin/sh\n"), 0o755)
        self.assertEqual(r.run("claim.py", "--own", self.req_a, "--paths", "tools/late.sh",
                               "--adopt-existing-changes", agent="agent-a").returncode, 0)
        # A baseline equal to the current content (not in HEAD, executable):
        # the delta is empty, so the commit is refused as before -- not a crash
        # on the missing HEAD blob of the mode-only path...
        self.assertEqual(r.run("claim.py", "--snapshot", self.req_a, "--paths", "tools/late.sh",
                               "--rebaseline", agent="agent-a").returncode, 0)
        cp = r.run("commit_request.py", "--request", self.req_a, "-m", "A: late",
                   "--paths", "tools/late.sh", agent="agent-a")
        self.assertEqual(cp.returncode, proc.INVALID, cp.stdout + cp.stderr)
        # ...until the request adopts it; then it lands executable.
        self.assertEqual(r.run("claim.py", "--snapshot", self.req_a, "--paths", "tools/late.sh",
                               "--from-head", agent="agent-a").returncode, 0)
        cp = r.run("commit_request.py", "--request", self.req_a, "-m", "A: late",
                   "--paths", "tools/late.sh", agent="agent-a")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(self.mode_of("tools/late.sh"), "100755")

    def test_core_filemode_false_keeps_the_recorded_mode(self):
        r = self.repo
        r.git("config", "core.fileMode", "false")
        self.assertEqual(r.run("claim.py", "--own", self.req_a, "--paths", "tools/run.sh",
                               agent="agent-a").returncode, 0)
        os.chmod(r.write("tools/run.sh", "#!/bin/sh\n"), 0o755)
        cp = r.run("commit_request.py", "--request", self.req_a, "-m", "A: tool", agent="agent-a")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        # Where the repository does not trust the executable bit, the commit
        # records what git itself would: the default mode for a new file.
        self.assertEqual(self.mode_of("tools/run.sh"), "100644")

    def test_head_moving_concurrently_commits_nothing(self):
        r = self.repo
        r.write("src/b.cpp", "int b = 9;\n")
        r.write("other.txt", "landed meanwhile\n")

        def someone_commits():
            env = dict(os.environ, GIT_INDEX_FILE=str(r.base / "other-index"))
            subprocess.run(["git", "read-tree", "HEAD"], cwd=r.root, env=env, check=True)
            subprocess.run(["git", "update-index", "--add", "other.txt"], cwd=r.root, env=env, check=True)
            tree = subprocess.run(["git", "write-tree"], cwd=r.root, env=env, capture_output=True, text=True, check=True).stdout.strip()
            c = subprocess.run(["git", "commit-tree", tree, "-p", "HEAD", "-m", "concurrent"], cwd=r.root,
                               capture_output=True, text=True, check=True).stdout.strip()
            subprocess.run(["git", "update-ref", "HEAD", c], cwd=r.root, check=True)
            self.other = c

        with self.assertRaises(proc.ProcessError) as e:
            proc.commit_request(r.root, self.req_b, "agent-b", "B work", _before_update_ref=someone_commits)
        self.assertEqual(e.exception.code, proc.CONFLICT)
        self.assertEqual(r.git("rev-parse", "HEAD").strip(), self.other)
        self.assertIn("src/b.cpp", r.git("status", "--porcelain"))

    def test_report_and_strict_worktree_check_detect_conflicts(self):
        r = self.repo
        r.write("src/a.cpp", "int a = 5;\n")
        rep = json.loads(r.run("commit_request.py", "--report", "--request", self.req_b, "--json").stdout)
        cats = {row["path"]: row["category"] for row in rep["changes"]}
        self.assertEqual(cats["src/a.cpp"], "other-request")
        # Forge a double exclusive ownership (e.g. a hand-edited lock) -> CONFLICT.
        lp = proc.lock_path(r.root, self.req_b)
        d = json.loads(lp.read_text())
        d["files"]["src/a.cpp"] = {"mode": "exclusive"}
        lp.write_text(json.dumps(d))
        strict = r.run("worktree_check.py", "--strict")
        self.assertEqual(strict.returncode, 3, strict.stdout)
        self.assertIn("CONFLICT", strict.stdout)
        refused = r.run("commit_request.py", "--request", self.req_b, "-m", "x", "--paths", "src/a.cpp", agent="agent-b")
        self.assertEqual(refused.returncode, proc.NOT_OWNER)

    def test_separate_worktree_isolates_request_branch(self):
        r = self.repo
        wt = r.base / "wt-b"
        cp = r.run("claim.py", "--create-worktree", self.req_b, "--worktree-dir", str(wt), agent="agent-b")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        main_head = r.git("rev-parse", "HEAD").strip()
        r.write("src/b.cpp", "int b = 42; // in worktree\n", root=wt)
        r.write("src/a.cpp", "int a = 7; // A in main worktree\n")
        # Ownership for the worktree claim is evaluated in that worktree.
        self.assertEqual(r.run("claim.py", "--own", self.req_b, "--paths", "src/b.cpp", agent="agent-b", root=wt).returncode, 0)
        done = r.run("commit_request.py", "--request", self.req_b, "-m", "B in worktree", agent="agent-b", root=wt)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        branch_head = r.git("rev-parse", "HEAD", cwd=wt).strip()
        self.assertEqual(set(r.git("show", "--name-only", "--format=", branch_head, cwd=wt).split()), {"src/b.cpp"})
        self.assertEqual(r.git("rev-parse", "main").strip(), main_head)
        self.assertIn("A in main worktree", (r.root / "src/a.cpp").read_text())
        # Shared state: the main worktree sees the same claim and commit.
        self.assertEqual(self.repo.claim(self.req_b)["commits"][-1]["sha"], branch_head)


class DirectoryRegistration(Base):
    """A registration owns exactly one file (REQ-DEVOPS-016, REQ-DEVOPS-042,
    ADR-000197). Before the fix `--own <dir>` was accepted and owned nothing:
    files written under it were "unowned" and silently left out of the
    request's commit (REQ-AUTO-20260924-004, seen in WORK-000317)."""

    def setUp(self):
        super().setUp()
        self.req_a = self.repo.add_request("inbox", f"REQ-CHAT-{TODAY}-060", "dir-a")
        self.req_b = self.repo.add_request("inbox", f"REQ-CHAT-{TODAY}-061", "dir-b")
        r = self.repo
        self.assertEqual(r.run("claim.py", "--claim-request", self.req_a, agent="agent-a").returncode, 0)
        self.assertEqual(r.run("claim.py", "--claim-request", self.req_b, agent="agent-b").returncode, 0)

    def own(self, req, agent, *paths, adopt=False):
        args = ["--own", req, "--paths", *paths] + (["--adopt-existing-changes"] if adopt else [])
        return self.repo.run("claim.py", *args, agent=agent)

    def owned(self, req):
        # Paths the request registered itself (the claim also holds its own request files).
        return {p for p in (self.repo.claim(req) or {}).get("files") or {} if not p.startswith("requests/")}

    def committed_paths(self, rev="HEAD"):
        # The first commit of a request also carries its own request file's
        # move (inbox -> processing), which the claim registered.
        return {p for p in self.repo.git("show", "--name-only", "--format=", rev).split()
                if not p.startswith("requests/")}

    def forge_directory_entry(self, req, path):
        """A claim holding a directory entry, as claims recorded before the
        fix (or a path registered before it became a directory) do."""
        lp = proc.lock_path(self.repo.root, req)
        d = json.loads(lp.read_text())
        d["files"][path] = {"mode": "exclusive", "adopted": False, "registered_at": proc.now_iso(),
                            "baseline_blob": "", "baseline_at": proc.now_iso()}
        lp.write_text(json.dumps(d))

    def test_individual_file_registration_owns_and_commits_that_file(self):
        r = self.repo
        self.assertEqual(self.own(self.req_a, "agent-a", "src/a.cpp").returncode, 0)
        r.write("src/a.cpp", "int a = 10;\n")
        cp = r.run("commit_request.py", "--request", self.req_a, "-m", "A", agent="agent-a")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("src/a.cpp", self.committed_paths())

    def test_existing_directory_registration_is_refused_and_records_nothing(self):
        r = self.repo
        r.write("evidence/VER-000900/one.log", "one\n")
        r.commit_all("existing evidence dir")
        for path in ("evidence/VER-000900", "evidence/VER-000900/", "./evidence/VER-000900"):
            cp = self.own(self.req_a, "agent-a", path)
            self.assertEqual(cp.returncode, proc.INVALID, path + cp.stdout + cp.stderr)
            self.assertIn("is a directory", cp.stderr)
            self.assertIn("evidence/VER-000900/<file>", cp.stderr)
        self.assertEqual(self.owned(self.req_a), set())

    def test_trailing_slash_marks_a_future_directory_and_is_refused(self):
        cp = self.own(self.req_a, "agent-a", "evidence/VER-000901/")
        self.assertEqual(cp.returncode, proc.INVALID, cp.stdout + cp.stderr)
        self.assertEqual(self.owned(self.req_a), set())

    def test_nested_directories_are_refused_at_every_level(self):
        r = self.repo
        r.write("pkg/sub/deeper/x.txt", "x\n")
        r.commit_all("nested")
        for path in ("pkg", "pkg/sub", "pkg/sub/deeper", "."):
            cp = self.own(self.req_a, "agent-a", path)
            self.assertEqual(cp.returncode, proc.INVALID, path + cp.stdout + cp.stderr)
        # A refused directory in a list refuses the whole call: nothing half-registered.
        cp = self.own(self.req_a, "agent-a", "src/a.cpp", "pkg/sub")
        self.assertEqual(cp.returncode, proc.INVALID)
        self.assertEqual(self.owned(self.req_a), set())
        # The file inside the nested directory is registrable on its own.
        self.assertEqual(self.own(self.req_a, "agent-a", "pkg/sub/deeper/x.txt").returncode, 0)
        self.assertEqual(self.owned(self.req_a), {"pkg/sub/deeper/x.txt"})

    def test_new_files_registered_before_creation_under_new_nested_directories_are_committed(self):
        """Worker-created evidence: register each file, then write it."""
        r = self.repo
        files = ["evidence/VER-000902/run.json", "evidence/VER-000902/logs/core.log",
                 "evidence/VER-000902/logs/deep/app.log"]
        self.assertEqual(self.own(self.req_a, "agent-a", *files).returncode, 0)
        for f in files:
            r.write(f, f"{f}\n")
        cp = r.run("commit_request.py", "--request", self.req_a, "-m", "evidence", agent="agent-a")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(self.committed_paths(), set(files))

    def test_files_created_after_the_claim_but_never_registered_are_not_committed(self):
        r = self.repo
        self.assertEqual(self.own(self.req_a, "agent-a", "src/a.cpp").returncode, 0)
        r.write("src/a.cpp", "int a = 11;\n")
        r.write("evidence/VER-000903/late.log", "late\n")    # created after the claim, not registered
        cp = r.run("commit_request.py", "--request", self.req_a, "-m", "A", agent="agent-a")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("unowned", cp.stdout)
        self.assertEqual(self.committed_paths(), {"src/a.cpp"})
        self.assertTrue((r.root / "evidence/VER-000903/late.log").exists())

    def test_path_registered_before_it_existed_that_became_a_directory_refuses_the_commit(self):
        """The WORK-000317 sequence: register evidence/VER-NNNNNN, then write
        files into it. The commit must not silently leave them out."""
        r = self.repo
        self.assertEqual(self.own(self.req_a, "agent-a", "src/a.cpp", "evidence/VER-000904").returncode, 0)
        r.write("src/a.cpp", "int a = 12;\n")
        r.write("evidence/VER-000904/one.log", "1\n")
        r.write("evidence/VER-000904/sub/two.log", "2\n")
        head = r.git("rev-parse", "HEAD").strip()
        for extra in ([], ["--dry-run"]):
            cp = r.run("commit_request.py", "--request", self.req_a, "-m", "A", *extra, agent="agent-a")
            self.assertEqual(cp.returncode, proc.NOT_OWNER, cp.stdout + cp.stderr)
            self.assertIn("evidence/VER-000904 is registered but is a directory", cp.stderr)
            self.assertIn("evidence/VER-000904/one.log", cp.stderr)
            self.assertIn("evidence/VER-000904/sub/two.log", cp.stderr)
        self.assertEqual(r.git("rev-parse", "HEAD").strip(), head)
        # The explicit way out: register the files this request wrote, adopting them knowingly.
        self.assertEqual(self.own(self.req_a, "agent-a", "evidence/VER-000904/one.log",
                                  "evidence/VER-000904/sub/two.log", adopt=True).returncode, 0)
        cp = r.run("commit_request.py", "--request", self.req_a, "-m", "A", agent="agent-a")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(self.committed_paths(),
                         {"src/a.cpp", "evidence/VER-000904/one.log", "evidence/VER-000904/sub/two.log"})

    def test_adopt_existing_changes_never_extends_to_a_directory(self):
        r = self.repo
        r.write("scratch/a.txt", "someone's\n")
        r.write("scratch/b.txt", "someone else's\n")
        for adopt in (False, True):
            cp = self.own(self.req_a, "agent-a", "scratch", adopt=adopt)
            self.assertEqual(cp.returncode, proc.INVALID, cp.stdout + cp.stderr)
        self.assertEqual(self.owned(self.req_a), set())
        # Adoption stays per file and explicit: without it a dirty file is refused ...
        self.assertEqual(self.own(self.req_a, "agent-a", "scratch/a.txt").returncode, proc.NOT_OWNER)
        # ... with it that one file (only) is adopted, baseline HEAD (absent).
        self.assertEqual(self.own(self.req_a, "agent-a", "scratch/a.txt", adopt=True).returncode, 0)
        f = self.repo.claim(self.req_a)["files"]["scratch/a.txt"]
        self.assertTrue(f["adopted"])
        cp = r.run("commit_request.py", "--request", self.req_a, "-m", "adopt a", agent="agent-a")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(self.committed_paths(), {"scratch/a.txt"})
        self.assertIn("scratch/b.txt", r.git("status", "--porcelain", "--untracked-files=all"))

    def test_a_directory_entry_does_not_let_its_holder_take_another_requests_file(self):
        """Ownership cannot be bypassed through a directory entry: the file
        another request registered under it stays that request's."""
        r = self.repo
        r.write("shared_dir/keep.txt", "k\n")
        r.commit_all("shared dir")
        self.forge_directory_entry(self.req_a, "shared_dir")
        self.assertEqual(self.own(self.req_b, "agent-b", "shared_dir/b.txt").returncode, 0)
        r.write("shared_dir/b.txt", "B's\n")
        # A cannot commit B's file, by name or by holding the directory.
        cp = r.run("commit_request.py", "--request", self.req_a, "-m", "A", "--paths", "shared_dir/b.txt",
                   agent="agent-a")
        self.assertEqual(cp.returncode, proc.NOT_OWNER)
        self.assertEqual(self.own(self.req_a, "agent-a", "shared_dir/b.txt").returncode, proc.CONFLICT)
        # B's file is B's, so it is not "loose" under A's directory entry: A's
        # own commit of other work is not blocked by it, and does not take it.
        self.assertEqual(self.own(self.req_a, "agent-a", "src/a.cpp").returncode, 0)
        r.write("src/a.cpp", "int a = 13;\n")
        cp = r.run("commit_request.py", "--request", self.req_a, "-m", "A", agent="agent-a")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(self.committed_paths(), {"src/a.cpp"})
        cp = r.run("commit_request.py", "--request", self.req_b, "-m", "B", agent="agent-b")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(self.committed_paths(), {"shared_dir/b.txt"})

    def test_protected_artifacts_cannot_be_registered_through_their_directory(self):
        r = self.repo
        r.write("docs/process/state-model.md", "# states\n")
        r.commit_all("protected")
        self.assertEqual(self.own(self.req_a, "agent-a", "docs/process").returncode, proc.INVALID)
        self.assertEqual(self.own(self.req_a, "agent-a", "docs").returncode, proc.INVALID)
        self.assertEqual(self.owned(self.req_a), set())

    def test_status_marks_a_directory_entry(self):
        r = self.repo
        r.write("olddir/f.txt", "f\n")
        r.commit_all("olddir")
        self.forge_directory_entry(self.req_a, "olddir")
        out = r.run("claim.py", "--status").stdout
        self.assertIn("olddir [directory: owns no file", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
