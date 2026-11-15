#!/usr/bin/env python3
"""Submit-without-processing lifecycle tests (REQ-DEVOPS-043, WORK-000253).

Proves SUBMIT != CLAIM != PROCESS against the real CLIs in throw-away git
repositories (same harness pattern as test_process_concurrency.py):

* submit() creates a persistent inbox ticket with a valid id and stops;
* submission performs no processing: no claim lock, no processing entry,
  no ownership, no work artifacts;
* the ticket survives on disk and is discoverable via inbox selection;
* inbox selection honours sidecar priority (CRITICAL first);
* an explicit claim moves inbox -> processing; a competing claim is refused;
* the normal complete workflow still works afterwards.

Run: python3 -m unittest scripts.tests.test_submit_without_processing -v
     (or: python3 -m unittest discover -s scripts/tests -v)
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import agent_process as proc  # noqa: E402

TODAY = proc.request_date()["yyyymmdd"]  # the intake names the series after the local system date (REQ-DEVOPS-045)


class Repo:
    def __init__(self):
        # Project temp area (AGENTS.md: tmp/, not the system /tmp).
        tmp_base = SCRIPTS.parent / "tmp" / "process-tests"
        tmp_base.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix="submit-only-", dir=str(tmp_base))
        self.base = Path(self.tmp.name)
        self.root = self.base / "repo"
        self.root.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Submit Test")
        for s in proc.REQUEST_STATES:
            (self.root / "requests" / s).mkdir(parents=True, exist_ok=True)
            (self.root / "requests" / s / "README.md").write_text(f"# {s}\n")
        self.env = {k: v for k, v in os.environ.items() if k != "AGENT_ID"}
        self.env["AGENT_PROCESS_ROOT"] = str(self.root)

    def close(self):
        self.tmp.cleanup()

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=str(self.root),
                              capture_output=True, text=True, check=True).stdout

    def run(self, script, *args, agent=None):
        env = dict(self.env)
        cmd = [sys.executable, str(SCRIPTS / script), *args]
        if agent:
            cmd += ["--agent-id", agent]
        return subprocess.run(cmd, cwd=str(self.root), env=env,
                              capture_output=True, text=True, timeout=120)

    def inbox_files(self):
        return sorted((self.root / "requests" / "inbox").glob("*.md"))

    def locks(self):
        return list(proc.claims_dir(self.root).glob("*.lock")) if proc.claims_dir(self.root).exists() else []


class Base(unittest.TestCase):
    def setUp(self):
        self.repo = Repo()

    def tearDown(self):
        self.repo.close()

    def submit(self, text, *extra):
        cp = self.repo.run("create_request.py", "--source", "chat", "--text", text, *extra)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        m = re.search(r"Request ID: (\S+)", cp.stdout)
        self.assertIsNotNone(m, cp.stdout + cp.stderr)
        return m.group(1), cp

    def request_file(self, rid):
        hits = [p for p in self.repo.inbox_files() if rid.lower() in p.name.lower()]
        self.assertEqual(len(hits), 1, [p.name for p in self.repo.inbox_files()])
        return hits[0]


class Submit(Base):
    def test_submit_creates_persistent_inbox_ticket_and_stops(self):
        rid, cp = self.submit("submit-only probe request")
        md = self.request_file(rid)
        self.assertIn("**Status:** inbox", md.read_text(encoding="utf-8"))
        sidecar = md.with_suffix(".json")
        self.assertTrue(sidecar.exists())
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertEqual(data["request_id"], rid)
        self.assertEqual(data["status"], "inbox")
        self.assertIn("submit-only probe request", data["original_text"])
        self.assertIn("Processing: NOT STARTED", cp.stdout)

    def test_submit_preserves_priority(self):
        rid, _ = self.submit("priority probe", "--priority", "HIGH")
        data = json.loads(self.request_file(rid).with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(data["priority"], "HIGH")


class NoProcessing(Base):
    def test_submission_claims_processes_and_owns_nothing(self):
        rid, _ = self.submit("untouched probe request")
        name = self.request_file(rid).name
        # No claim lock anywhere.
        self.assertEqual(self.repo.locks(), [])
        self.assertIsNone(proc.read_claim(proc.lock_path(self.repo.root, name)))
        # Nothing left the inbox.
        self.assertEqual([s for s, _ in proc.locate_request(self.repo.root, name)], ["inbox"])
        leftovers = [p for p in (self.repo.root / "requests" / "processing").glob("*.md")
                     if p.name != "README.md"]
        self.assertEqual(leftovers, [])
        # No work artifacts, no worktree branches.
        self.assertFalse((self.repo.root / "work").exists())
        branches = self.repo.git("branch", "--list", "request/*").strip()
        self.assertEqual(branches, "")

    def test_tool_surface_states_submit_only(self):
        cp = self.repo.run("create_request.py", "--help")
        self.assertEqual(cp.returncode, 0)
        self.assertIn("Submit-only", cp.stdout)
        self.assertIn("does not claim", cp.stdout)


class Persistence(Base):
    def test_ticket_survives_as_files_locatable_from_disk(self):
        rid, _ = self.submit("persistence probe request")
        name = self.request_file(rid).name
        # A fresh lookup from disk (no live process state) finds it in inbox.
        self.assertEqual([s for s, _ in proc.locate_request(self.repo.root, name)], ["inbox"])
        md = (self.repo.root / "requests" / "inbox" / name).read_text(encoding="utf-8")
        self.assertIn("persistence probe request", md)


class InboxDiscovery(Base):
    def test_priority_order_beats_lexicographic_order(self):
        low_id, _ = self.submit("aaa low priority probe", "--priority", "LOW")
        high_id, _ = self.submit("zzz high priority probe", "--priority", "HIGH")
        low_name = self.request_file(low_id).name
        high_name = self.request_file(high_id).name
        self.assertLess(low_name, high_name)  # lexicographic order would pick LOW
        cp = self.repo.run("process_next_request.py", "--dry-run")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        m = re.search(r"Next eligible: (\S+)", cp.stdout)
        self.assertIsNotNone(m, cp.stdout)
        self.assertIn(high_name, m.group(1))
        # Dry run mutates nothing: both still inbox, no locks.
        self.assertEqual([s for s, _ in proc.locate_request(self.repo.root, high_name)], ["inbox"])
        self.assertEqual(self.repo.locks(), [])


class Claim(Base):
    def test_explicit_claim_moves_inbox_to_processing(self):
        rid, _ = self.submit("claim probe request")
        name = self.request_file(rid).name
        cp = self.repo.run("claim.py", "--claim-request", name, agent="agent-a")
        self.assertEqual(cp.returncode, 0, cp.stderr + cp.stdout)
        self.assertEqual([s for s, _ in proc.locate_request(self.repo.root, name)], ["processing"])
        self.assertIn("**Status:** processing",
                      (self.repo.root / "requests" / "processing" / name).read_text(encoding="utf-8"))
        self.assertEqual(proc.read_claim(proc.lock_path(self.repo.root, name))["agent_id"], "agent-a")

    def test_competing_claim_is_refused(self):
        rid, _ = self.submit("contested probe request")
        name = self.request_file(rid).name
        self.assertEqual(self.repo.run("claim.py", "--claim-request", name, agent="agent-a").returncode, 0)
        second = self.repo.run("claim.py", "--claim-request", name, agent="agent-b")
        self.assertEqual(second.returncode, proc.CONFLICT, second.stderr)
        self.assertEqual(proc.read_claim(proc.lock_path(self.repo.root, name))["agent_id"], "agent-a")
        self.assertEqual([s for s, _ in proc.locate_request(self.repo.root, name)], ["processing"])


class ExistingWorkflow(Base):
    def test_submit_claim_complete_lifecycle_still_works(self):
        rid, _ = self.submit("lifecycle probe request")
        name = self.request_file(rid).name
        self.assertEqual(self.repo.run("claim.py", "--claim-request", name, agent="agent-a").returncode, 0)
        # No work record exists in this probe: completion needs the recorded human override.
        done = self.repo.run("claim.py", "--complete-request", name, "--waive-gate", "lifecycle probe", agent="agent-a")
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
        self.assertEqual([s for s, _ in proc.locate_request(self.repo.root, name)], ["completed"])


if __name__ == "__main__":
    unittest.main()
