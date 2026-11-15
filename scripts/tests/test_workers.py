#!/usr/bin/env python3
"""Worker Orchestrator (ADR-000200, WORKER-001..013): workers execute the
development system's requests; the request lifecycle stays the single source
of truth. Every test installs the system into a fresh project
(``devsys.py install``) and drives real worker processes with a stand-in
agent (scripts/tests/fake_agent.py) that uses the real framework tools.

Requirements verified: WORKER-001 (requests are the unit of work), WORKER-002
(no second ticket system), WORKER-003 (one active request per worker, never
shared), WORKER-004 (per-worker state), WORKER-005 (isolation), WORKER-006
(configuration and control), WORKER-007 (priority), WORKER-008 (gates kept),
WORKER-009 (recovery), WORKER-010 (status), WORKER-012 (no vendor identity), WORKER-013
(project-relative);
REQ-DEVOPS-014 (chat intake), REQ-DEVOPS-019 (gate).

Run: python3 -m unittest scripts.tests.test_workers -v
"""
import json
import os
import re
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from devsys_testlib import FRAMEWORK, Project, fast_config  # noqa: E402

FAKE = "scripts/tests/fake_agent.py"


def worker_config(**over):
    w = {"poll_seconds": 0.2, "max_workers": 2,
         "attempt": {"max_runtime_minutes": 2, "max_attempts_per_request": 2, "kill_grace_seconds": 2,
                     "max_consecutive_failures": 5}}
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(w.get(k), dict):
            w[k].update(v)
        else:
            w[k] = v
    return fast_config(workers=w)


class Base(unittest.TestCase):
    workers: dict = {}

    def setUp(self):
        self.p = Project(worker_config(**self.workers), install=True)
        cfg = json.loads(self.p.read("devsys.json"))
        cfg["workers"]["agent"] = {"command": [sys.executable, str(self.p.root / FAKE)]}
        self.p.write("devsys.json", json.dumps(cfg, indent=2))
        self.p.commit_all("configure the workers' agent")
        self.env = {"FAKE_AGENT_LOG": str(self.p.base / "fake-agent.jsonl")}

    def tearDown(self):
        for wid in ("worker-01", "worker-02", "worker-03"):
            rec = self.worker(wid)
            pid = (rec.get("process") or {}).get("pid")
            if pid:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            act = rec.get("active") or {}
            spid = (act.get("session") or {}).get("pid")
            if spid:
                try:
                    os.killpg(spid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        self.p.close()

    # ------------------------------------------------------------------ helpers
    def worker(self, wid: str) -> dict:
        f = self.p.root / ".agent/state/runtime/workers" / wid / "worker.json"
        try:
            return json.loads(f.read_text())
        except (OSError, ValueError):
            return {}

    def run_worker(self, wid="worker-01", *extra, env=None, timeout=240):
        cp = self.p.run("devsys.py", "workers", "run", wid, "--once", *extra, env={**self.env, **(env or {})},
                        timeout=timeout)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp.stdout

    def status(self) -> dict:
        return json.loads(self.p.ok("devsys.py", "workers", "status", "--json"))

    def rid(self, name) -> str:
        return json.loads(self.p.read(str(Path(self.p.path_of(name)).with_suffix(".json"))))["request_id"]

    def journal(self) -> list[dict]:
        f = self.p.root / ".agent/state/runtime/workers/attempts.jsonl"
        return [json.loads(line) for line in f.read_text().splitlines()] if f.exists() else []

    def attempts(self, wid) -> list[dict]:
        base = self.p.root / ".agent/state/runtime/workers" / wid / "attempts"
        return [json.loads((d / "attempt.json").read_text()) for d in sorted(base.iterdir())] if base.is_dir() else []

    def wait_for(self, fn, timeout=30.0):
        end = time.time() + timeout
        while time.time() < end:
            if fn():
                return True
            time.sleep(0.2)
        return fn()


# ---------------------------------------------------------------- requests and priority
class RequestsAndPriority(Base):
    def test_chat_request_creation_and_priority_persist(self):
        low = self.p.request("Tidy the demo page wording", "--priority", "LOW")
        high = self.p.request("Fix the demo greeting typo", "--priority", "HIGH")
        side = json.loads(self.p.read(f"requests/inbox/{Path(high).with_suffix('.json')}"))
        self.assertEqual(side["priority"], "HIGH")
        q = json.loads(self.p.ok("devsys.py", "queue", "--json"))
        self.assertEqual([i["request"] for i in q], [high, low])
        self.p.ok("devsys.py", "priority", low, "critical")
        side = json.loads(self.p.read(f"requests/inbox/{Path(low).with_suffix('.json')}"))
        self.assertEqual(side["priority"], "CRITICAL")
        self.assertEqual(side["priority_history"][-1]["from"], "LOW")
        self.assertIn("**Priority:** CRITICAL", self.p.read(f"requests/inbox/{low}"))
        q = json.loads(self.p.ok("devsys.py", "queue", "--json"))
        self.assertEqual([i["request"] for i in q], [low, high])
        self.assertEqual(self.p.run("devsys.py", "priority", low, "urgent").returncode, 2)   # argparse: invalid
        path = self.p.claim(high, "agent-test-a")
        self.p.ok("claim.py", "--reject-request", path, "--reason", "no", agent="agent-test-a")
        cp = self.p.run("devsys.py", "priority", high, "LOW")
        self.assertEqual(cp.returncode, 4, "a closed request's priority is not changed")


# ---------------------------------------------------------------- the worker model
class WorkerModel(Base):
    def test_multiple_workers_are_represented_and_controlled(self):
        cfg = json.loads(self.p.read("devsys.json"))
        cfg["workers"]["max_workers"] = 3
        self.p.write("devsys.json", json.dumps(cfg))
        st = self.status()
        self.assertEqual([w["id"] for w in st["workers"]], ["worker-01", "worker-02", "worker-03"])
        self.assertEqual(st["max_workers"], 3)
        self.assertTrue(st["agent_configured"])
        self.p.ok("devsys.py", "workers", "disable", "worker-02")
        cp = self.p.run("devsys.py", "workers", "start", "worker-02")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("disabled", cp.stdout)
        self.assertFalse([w for w in self.status()["workers"] if w["id"] == "worker-02"][0]["enabled"])
        self.p.ok("devsys.py", "workers", "enable", "--all")
        # agent ids are fixed per worker (written once) and distinct
        ids = [self.worker(w)["agent_id"] for w in ("worker-01", "worker-02", "worker-03")]
        self.assertEqual(len(set(ids)), 3)
        self.p.ok("devsys.py", "workers", "status")
        self.assertEqual([self.worker(w)["agent_id"] for w in ("worker-01", "worker-02", "worker-03")], ids)

    def test_start_is_refused_without_an_agent_and_beyond_the_maximum(self):
        cfg = json.loads(self.p.read("devsys.json"))
        cfg["workers"]["agent"]["command"] = []
        self.p.write("devsys.json", json.dumps(cfg))
        self.assertEqual(self.p.run("devsys.py", "workers", "start", "worker-01").returncode, 10)
        self.p.ok("devsys.py", "workers", "enable", "worker-02")              # its record keeps the slot
        cfg["workers"]["agent"]["command"] = [sys.executable, str(self.p.root / FAKE)]
        cfg["workers"]["max_workers"] = 1
        self.p.write("devsys.json", json.dumps(cfg))
        self.p.ok("devsys.py", "workers", "start", "worker-01")
        try:
            self.assertTrue(self.wait_for(lambda: self.status()["running"] == 1))
            cp = self.p.run("devsys.py", "workers", "start", "worker-02")
            self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
            self.assertIn("max_workers", cp.stdout)
        finally:
            self.p.ok("devsys.py", "workers", "stop", "worker-01", "--wait", "20")
        self.assertEqual(self.status()["running"], 0)

    def test_start_serves_the_given_root_not_the_callers(self):
        # Regression: the control panel calls the API from the system's own
        # code with --root pointing elsewhere; the spawned worker process ran
        # against the code's root instead of the given one.
        name = self.p.request("A page started from elsewhere [fake:complete]")
        code = (f"import sys; sys.path.insert(0, {str(FRAMEWORK / 'scripts')!r}); from workers import pool; "
                f"from pathlib import Path; print(pool.start(Path({str(self.p.root)!r}), 'worker-01'))")
        env = {**os.environ, **self.env}
        env.pop("AGENT_PROCESS_ROOT", None)
        out = subprocess.run([sys.executable, "-c", code], cwd=str(FRAMEWORK), env=env, capture_output=True,
                             text=True, timeout=60)
        self.assertIn("'started': True", out.stdout, out.stdout + out.stderr)
        try:
            self.assertTrue(self.wait_for(lambda: self.p.where(name) == ["completed"], 120))
        finally:
            self.p.ok("devsys.py", "workers", "stop", "worker-01", "--wait", "20")

    def test_worker_completes_a_request_through_the_gate(self):
        name = self.p.request("Write the demo page [fake:complete]")
        out = self.run_worker()
        self.assertIn("completed", out)
        self.assertEqual(self.p.where(name), ["completed"])
        a = self.attempts("worker-01")[-1]
        self.assertEqual(a["result"], "COMPLETED")
        self.assertEqual(a["validation"]["overall"], "PASS", a["validation"])
        steps = {s["name"]: s["status"] for s in a["validation"]["steps"]}
        self.assertEqual(steps["commit-binding"], "PASS")
        head = self.p.git("rev-parse", "HEAD").strip()
        self.assertEqual(json.loads(self.p.ok("devsys.py", "verify-commit", head, "--json"))[0]["status"], "VERIFIED")
        rec = self.worker("worker-01")
        self.assertEqual((rec["status"], rec["active"], rec["last"]["outcome"]), ("stopped", None, "completed"))
        self.assertIn(f"Agent-Id: {rec['agent_id']}", self.p.git("log", "-1", "--format=%B"))

    def test_the_gate_is_not_bypassed_by_workers(self):
        name = self.p.request("Write an ungated page [fake:complete_nogate]")
        self.run_worker()
        self.assertEqual(self.p.where(name), ["processing"], "the framework refused the ungated completion")
        self.assertEqual(self.worker("worker-01")["last"]["outcome"], "retry")
        self.run_worker()
        self.assertEqual(self.p.where(name), ["needs-review"])
        side = json.loads(self.p.read(f"requests/needs-review/{Path(name).with_suffix('.json')}"))
        self.assertIn("attempt(s) without completion", side["resolution"]["reason"])
        self.assertEqual(self.worker("worker-01")["last"]["outcome"], "escalated")
        self.assertFalse(list((self.p.root / "requests/completed").glob("*req-chat*")))
        self.assertEqual([e["event"] for e in self.journal() if e["event"] != "attempt"], ["escalated"])

    def test_partial_keeps_the_claim_and_the_worker_resumes(self):
        name = self.p.request("A two-step page [fake:partial_then_complete]")
        self.run_worker()
        self.assertEqual(self.p.where(name), ["processing"])
        claim = self.p.claim_data(name)
        self.assertEqual(claim["agent_id"], self.worker("worker-01")["agent_id"])
        self.assertEqual(self.worker("worker-01")["last"]["outcome"], "retry")
        other = self.p.request("An unrelated page [fake:complete]")
        self.run_worker()                         # resumes its own request before taking a new one
        self.assertEqual(self.p.where(name), ["completed"])
        self.assertEqual(self.p.where(other), ["inbox"])
        # The completion commit went through although the same file was already
        # committed once as progress (regression: the second commit conflicted
        # with the request's own first commit and the work stayed uncommitted).
        last = self.attempts("worker-01")[-1]
        self.assertEqual(last["validation"]["overall"], "PASS", last["validation"])
        self.assertFalse(self.p.lock_file(name).exists(), "the claim was released after the final commit")
        rid = self.rid(name)
        self.assertIn("attempt 2", self.p.git("show", f"HEAD:docs/fake/{rid}.md"))
        self.assertIn("attempt 2 of", (self.p.root / ".agent/state/runtime/workers/worker-01/attempts"
                                       / self.attempts("worker-01")[-1]["attempt_id"] / "prompt.md").read_text())

    def test_two_workers_never_share_an_active_request(self):
        self.p.request("The only page [fake:slow]")
        env = {**os.environ, **self.env, "FAKE_SLOW_SECONDS": "2"}
        env.pop("AGENT_PROCESS_ROOT", None)
        procs = [subprocess.Popen([sys.executable, str(self.p.root / "scripts/devsys.py"), "workers", "run", w,
                                   "--once"], cwd=str(self.p.root), env=env, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True) for w in ("worker-01", "worker-02")]
        outs = [p.communicate(timeout=240) for p in procs]
        self.assertEqual([p.returncode for p in procs], [0, 0], outs)
        attempts = [e for e in self.journal() if e["event"] == "attempt"]
        self.assertEqual(len(attempts), 1, attempts)
        self.assertEqual(sum("no eligible request" in o[0] for o in outs), 1, outs)

    def test_concurrent_workers_complete_through_the_gate(self):
        # Regression: with three full lifecycles at once, a gate read another
        # request's work record between its move and its manifest row
        # (WORK_STATE_MISMATCH) and failed; every completion here must pass the
        # gate and every commit must verify.
        cfg = json.loads(self.p.read("devsys.json"))
        cfg["workers"]["max_workers"] = 3
        self.p.write("devsys.json", json.dumps(cfg))
        self.p.commit_all("three workers")
        names = [self.p.request(f"Concurrent page {c} [fake:complete]") for c in "ABC"]
        env = {**os.environ, **self.env}
        env.pop("AGENT_PROCESS_ROOT", None)
        procs = [subprocess.Popen([sys.executable, str(self.p.root / "scripts/devsys.py"), "workers", "run", w,
                                   "--once"], cwd=str(self.p.root), env=env, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True) for w in ("worker-01", "worker-02", "worker-03")]
        for p in procs:
            p.communicate(timeout=300)
        self.assertEqual([self.p.where(n) for n in names], [["completed"]] * 3)
        for w in ("worker-01", "worker-02", "worker-03"):
            for a in self.attempts(w):
                self.assertEqual(a["validation"]["overall"], "PASS", (w, a["validation"]))
        shas = self.p.git("log", "--format=%H", "-3").split()
        verdicts = json.loads(self.p.ok("devsys.py", "verify-commit", *shas, "--json"))
        self.assertEqual([v["status"] for v in verdicts], ["VERIFIED"] * 3, verdicts)
        self.assertEqual(self.p.git("status", "--porcelain").strip(), "")

    def test_worker_state_is_isolated(self):
        a = self.p.request("Page A [fake:slow]")
        b = self.p.request("Page B [fake:slow]")
        env = {**os.environ, **self.env, "FAKE_SLOW_SECONDS": "1"}
        env.pop("AGENT_PROCESS_ROOT", None)
        procs = [subprocess.Popen([sys.executable, str(self.p.root / "scripts/devsys.py"), "workers", "run", w,
                                   "--once"], cwd=str(self.p.root), env=env, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True) for w in ("worker-01", "worker-02")]
        for p in procs:
            p.communicate(timeout=240)
        self.assertEqual(self.p.where(a), ["completed"])
        self.assertEqual(self.p.where(b), ["completed"])
        by_worker = {w: {x["request_id"] for x in self.attempts(w)} for w in ("worker-01", "worker-02")}
        self.assertEqual(sorted(len(v) for v in by_worker.values()), [1, 1])
        self.assertFalse(by_worker["worker-01"] & by_worker["worker-02"])
        for w in ("worker-01", "worker-02"):
            events = (self.p.root / ".agent/state/runtime/workers" / w / "events.jsonl").read_text()
            other = next(iter(by_worker["worker-02" if w == "worker-01" else "worker-01"]))
            self.assertNotIn(other, events, "a worker's log holds only its own work")

    def test_stop_now_interrupts_and_the_claim_is_kept(self):
        name = self.p.request("A long page [fake:timeout]")
        self.p.ok("devsys.py", "workers", "start", "worker-01", env=self.env)
        self.assertTrue(self.wait_for(lambda: (self.worker("worker-01").get("active") or {}).get("session")))
        self.p.ok("devsys.py", "workers", "stop", "worker-01", "--now", "--wait", "30")
        rec = self.worker("worker-01")
        self.assertEqual((rec["status"], rec["last"]["result"], rec["last"]["outcome"]),
                         ("stopped", "INTERRUPTED", "resume"))
        self.assertEqual(self.p.where(name), ["processing"])
        self.assertEqual(self.p.claim_data(name)["agent_id"], rec["agent_id"])
        self.assertFalse([e for e in self.journal() if e.get("counted")], "an interruption is not counted")

    def test_a_dead_worker_is_recovered_and_its_request_resumed(self):
        name = self.p.request("A slow page [fake:slow]")
        self.p.ok("devsys.py", "workers", "start", "worker-01", env={**self.env, "FAKE_SLOW_SECONDS": "60"})
        self.assertTrue(self.wait_for(lambda: (self.worker("worker-01").get("active") or {}).get("session")))
        rec = self.worker("worker-01")
        os.kill(rec["process"]["pid"], signal.SIGKILL)               # the worker dies; its session keeps running
        self.assertTrue(self.wait_for(lambda: self.status()["workers"][0]["process"]["verdict"] != "ALIVE"))
        st = self.status()["workers"][0]
        self.assertEqual(st["status"], "stopped")
        self.assertEqual(st["active"]["session"]["verdict"], "ALIVE")
        out = json.loads(self.p.ok("devsys.py", "workers", "recover", "worker-01", "--json"))
        self.assertFalse(out["changed"], "a still-running session is not ended without --force")
        out = json.loads(self.p.ok("devsys.py", "workers", "recover", "worker-01", "--force", "--json"))
        self.assertTrue(out["changed"], out)
        rec = self.worker("worker-01")
        self.assertEqual((rec["active"], rec["last"]["result"]), (None, "INTERRUPTED"))
        self.assertEqual(self.p.claim_data(name)["agent_id"], rec["agent_id"], "the claim stays with the worker")
        self.run_worker(env={"FAKE_SLOW_SECONDS": "0"})
        self.assertEqual(self.p.where(name), ["completed"])

    def test_timeout_ends_the_session_and_counts(self):
        cfg = json.loads(self.p.read("devsys.json"))
        cfg["workers"]["attempt"]["max_runtime_minutes"] = 0.03
        self.p.write("devsys.json", json.dumps(cfg))
        name = self.p.request("A hanging page [fake:timeout]")
        self.run_worker()
        a = self.attempts("worker-01")[-1]
        self.assertEqual(a["result"], "TIMEOUT")
        self.assertTrue([e for e in self.journal() if e.get("counted")])
        self.assertEqual(self.p.where(name), ["processing"])

    def test_escalation_by_the_agent_is_the_frameworks_needs_review(self):
        name = self.p.request("An ambiguous page [fake:escalate]")
        self.run_worker()
        self.assertEqual(self.p.where(name), ["needs-review"])
        self.assertEqual(self.worker("worker-01")["last"]["outcome"], "needs-review")
        # nothing else is picked up while it waits for a human, and a human resumes it through the framework
        self.run_worker()
        self.assertEqual(self.p.where(name), ["needs-review"])


class AgentLaunch(Base):
    """An agent command that cannot be started (a desktop launcher's PATH,
    a typo): refused up front by the API; in a running worker, an uncounted
    LAUNCH_FAILED attempt that stops the worker and keeps its claim."""

    def configure(self, command):
        cfg = json.loads(self.p.read("devsys.json"))
        cfg["workers"]["agent"] = {"command": command}
        self.p.write("devsys.json", json.dumps(cfg, indent=2))

    def test_start_refuses_an_agent_command_that_is_not_on_path(self):
        self.configure(["devsys-no-such-agent-command"])
        name = self.p.request("A page [fake:complete]")
        cp = self.p.run("devsys.py", "workers", "start", "worker-01")
        self.assertEqual(cp.returncode, 10, cp.stdout + cp.stderr)
        self.assertIn("not an executable on PATH", cp.stdout + cp.stderr)
        self.assertFalse((self.worker("worker-01").get("process") or {}).get("pid"))
        self.assertEqual(self.p.where(name), ["inbox"])

    def test_a_worker_whose_agent_cannot_start_stops_without_counting_and_resumes(self):
        self.configure(["devsys-no-such-agent-command"])
        name = self.p.request("A page [fake:complete]")
        out = self.run_worker()
        self.assertIn("agent could not be started", out)
        first = self.attempts("worker-01")[-1]
        self.assertEqual(first["result"], "LAUNCH_FAILED")
        self.assertEqual([e["counted"] for e in self.journal() if e.get("event") == "attempt"], [False])
        rec = self.worker("worker-01")
        self.assertEqual((rec["status"], rec["desired"]), ("stopped", "stopped"))
        self.assertIn("agent could not be started", rec["status_reason"])
        self.assertEqual(self.p.where(name), ["processing"])
        self.assertEqual(self.p.claim_data(name)["agent_id"], rec["agent_id"])     # the claim is kept
        # Once the agent can be started, the worker resumes its own request
        # under the same claim, in a new attempt (the failed one's record stays).
        self.configure([sys.executable, str(self.p.root / FAKE)])
        self.run_worker()
        ids = [a["attempt_id"] for a in self.attempts("worker-01")]
        self.assertEqual(len(ids), 2)
        self.assertEqual(self.attempts("worker-01")[0]["result"], "LAUNCH_FAILED")
        self.assertEqual(self.p.where(name), ["completed"])


class WorktreeIsolation(Base):
    workers = {"isolation": "worktree"}

    def test_a_worktree_request_completes_on_its_branch_with_a_verified_binding(self):
        name = self.p.request("Page C [fake:complete]")
        head = self.p.git("rev-parse", "HEAD").strip()
        self.run_worker("worker-01")
        a = self.attempts("worker-01")[-1]
        self.assertEqual((a["result"], a["request_state"]), ("COMPLETED", "completed"), a.get("result_reason"))
        binding = next(s for s in a["validation"]["steps"] if s["name"] == "commit-binding")
        self.assertEqual(binding["status"], "PASS", binding)
        self.assertIn("VERIFIED", binding["detail"])
        stem = Path(name).stem
        # The work record and evidence are repository paths of the worktree,
        # committed on the request branch; main is untouched.
        self.assertEqual(self.p.git("rev-parse", f"request/{stem}~1").strip(), head)
        self.assertEqual(self.p.git("rev-parse", "HEAD").strip(), head)
        files = self.p.git("show", "--name-only", "--format=", f"request/{stem}").split()
        self.assertTrue(any(f.startswith("work/completed/WORK-") for f in files), files)
        self.assertFalse(any(f.startswith("../") for f in files), files)

    def test_each_request_gets_its_own_worktree(self):
        a = self.p.request("Page A [fake:fail]")
        b = self.p.request("Page B [fake:fail]")
        self.run_worker("worker-01")
        self.run_worker("worker-02")
        cwds = [self.attempts(w)[-1]["cwd"] for w in ("worker-01", "worker-02")]
        self.assertNotEqual(cwds[0], cwds[1])
        for name, cwd in zip((a, b), cwds):
            self.assertNotEqual(Path(cwd).resolve(), self.p.root.resolve())
            self.assertEqual(self.p.claim_data(name)["worktree"], cwd)
            branch = subprocess.run(["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True,
                                    text=True).stdout.strip()
            self.assertEqual(branch, f"request/{Path(name).stem}")
        log = [json.loads(line) for line in (self.p.base / "fake-agent.jsonl").read_text().splitlines()]
        self.assertEqual(sorted(e["cwd"] for e in log), sorted(cwds))


# ---------------------------------------------------------------- identity and layout
class Identity(unittest.TestCase):
    # Agent products the documented profiles name; not spelled out: this file is scanned too.
    VENDOR = re.compile("cl" + "aude|open" + "code", re.IGNORECASE)
    # The documented external integrations (WORKER-012): the profile page and
    # the adapters directory beside the worker code.
    EXCEPTION = "docs/process/agent-profiles.md"
    EXCEPTION_DIR = "scripts/workers/profiles/"
    # Request records quote what a person asked for, verbatim; they are input
    # to the system, not part of it.
    INPUT_DIR = "requests/"
    SKIP = {".git", "tmp", "build", "__pycache__", "node_modules"}

    def files(self):
        for p in sorted(FRAMEWORK.rglob("*")):
            rel = p.relative_to(FRAMEWORK)
            if p.is_file() and not (set(rel.parts) & self.SKIP) and rel.parts[:2] != (".audit", "runs") \
                    and rel.parts[:3] not in ((".agent", "state", "runtime"), (".agent", "state", "claims")):
                yield rel.as_posix(), p                  # both are untracked runtime state (.gitignore)

    def test_no_vendor_identity_outside_the_documented_exception(self):
        hits = []
        for rel, p in self.files():
            if rel == self.EXCEPTION or rel.startswith((self.EXCEPTION_DIR, self.INPUT_DIR)):
                continue
            if self.VENDOR.search(rel):
                hits.append(f"{rel} (name)")
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for n, line in enumerate(text.splitlines(), 1):
                if self.VENDOR.search(line):
                    hits.append(f"{rel}:{n}: {line.strip()[:100]}")
        self.assertEqual(hits, [])

    def test_the_adapters_directory_holds_only_adapters(self):
        names = sorted(p.name for p in (FRAMEWORK / self.EXCEPTION_DIR).iterdir() if p.name != "__pycache__")
        self.assertTrue(names and all(n.endswith("_adapter.py") for n in names), names)

    def test_the_exception_is_documented_and_only_configuration(self):
        text = (FRAMEWORK / self.EXCEPTION).read_text()
        self.assertIn("workers.agent.command", text)
        self.assertIn("exception", text.lower())

    def test_no_machine_specific_paths(self):
        hits = [rel for rel, p in self.files() if p.suffix in (".py", ".md", ".json", ".sh", ".ui", "")
                and ("/ho" + "me/") in p.read_text(encoding="utf-8", errors="ignore")]
        self.assertEqual(hits, [])


class ProjectRoot(unittest.TestCase):
    def test_project_root_is_relative_to_the_system_directory(self):
        p = Project(fast_config())
        try:
            sys.path.insert(0, str(FRAMEWORK / "scripts"))
            import devsys_config as cfgmod
            self.assertEqual(cfgmod.project_root(p.root), p.root.resolve())
            system = p.root / ".devsys"
            system.mkdir()
            (system / "devsys.json").write_text(json.dumps({"paths": {"project_root": ".."}}))
            self.assertEqual(cfgmod.project_root(system), p.root.resolve())
            (system / "devsys.json").write_text(json.dumps({"paths": {"project_root": "../missing"}}))
            cfgmod.load(system, refresh=True)
            self.assertTrue(any("project_root" in x for x in cfgmod.validate(system)))
        finally:
            p.close()


if __name__ == "__main__":
    unittest.main()
