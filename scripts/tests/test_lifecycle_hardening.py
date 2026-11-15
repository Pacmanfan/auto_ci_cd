#!/usr/bin/env python3
"""Lifecycle hardening (ADR-000199, CI_CD_ROADMAP COR-*/CON-*/PRO-01): verified
liveness, safe sweep repairs, explicit recovery, gated transitions, release
refusal, commit binding, the work / evidence tools, intake duplicates and
follow-ups, id allocation. Every case drives the real CLIs on a fresh
``devsys.py init`` repository (scripts/tests/devsys_testlib.py); sessions are
real processes, so ALIVE / DEAD are verified, not mocked.

Requirements verified: REQ-DEVOPS-003 (ids), REQ-DEVOPS-014 (intake, follow-ups),
REQ-DEVOPS-015 (state model), REQ-DEVOPS-016 (claims, liveness), REQ-DEVOPS-019 (gate),
REQ-DEVOPS-024 (escalation), REQ-DEVOPS-025 (recovery, sweep), REQ-DEVOPS-042 (commit
attribution), REQ-DEVOPS-048 (work / evidence tools), REQ-DEVOPS-051 (verify-commit).

Run: python3 -m unittest scripts.tests.test_lifecycle_hardening -v
"""
import json
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from devsys_testlib import Project, fast_config  # noqa: E402

A, B, C = "agent-test-a", "agent-test-b", "agent-test-c"
CHECK_SUITE = {"id": "check", "kind": "command", "results": "exit", "command": ["python3", "-c", "pass"],
               "inputs": ["src/**"], "groups": {"unit": {"tiers": ["t0"]}}}


class Base(unittest.TestCase):
    config: dict = fast_config()

    def setUp(self):
        self.p = Project(self.config)

    def tearDown(self):
        self.p.close()


# ---------------------------------------------------------------- liveness
class Liveness(Base):
    def verdict(self, name):
        out = json.loads(self.p.ok("claim.py", "--status", "--json"))
        rows = out if isinstance(out, list) else out.get("claims", out)
        for row in rows:
            if row.get("target") == name:
                return row["liveness"]["verdict"]
        raise AssertionError(out)

    def test_owner_process_alive_then_dead(self):
        s = self.p.sleeper()
        name = self.p.request("Add a greeting option to the demo")
        self.p.claim(name, A, s.pid)
        self.assertEqual(self.verdict(name), "ALIVE")
        self.p.kill(s)
        self.assertEqual(self.verdict(name), "DEAD")

    def test_gone_owner_within_grace_is_active_not_dead(self):
        p = Project(fast_config(claims={"dead_grace_minutes": 30}))
        try:
            s = p.sleeper()
            name = p.request("Add a farewell option to the demo")
            p.claim(name, A, s.pid)
            p.kill(s)
            out = p.ok("claim.py", "--status")
            self.assertIn("ACTIVE", out)
            self.assertNotIn("DEAD", out)
        finally:
            p.close()

    def test_silent_claim_without_process_is_stale(self):
        name = self.p.request("Add a colour option to the demo")
        self.p.claim(name, A)
        self.assertIn("ACTIVE", self.p.ok("claim.py", "--status"))
        self.p.age_claim(name, 3 * 60)
        self.assertIn("STALE", self.p.ok("claim.py", "--status"))

    def test_earlier_boot_is_dead(self):
        s = self.p.sleeper()
        name = self.p.request("Add a verbose option to the demo")
        self.p.claim(name, A, s.pid)
        self.p.edit_claim(name, lambda d: d["session"].update(boot_id="00000000-0000-0000-0000-000000000000"))
        self.assertIn("earlier boot", self.p.ok("claim.py", "--status"))

    def test_reused_pid_is_dead(self):
        s = self.p.sleeper()
        name = self.p.request("Add a quiet option to the demo")
        self.p.claim(name, A, s.pid)
        self.p.edit_claim(name, lambda d: d["session"].update(start="1"))
        self.assertIn("another process", self.p.ok("claim.py", "--status"))

    def test_run_step_keeps_claim_alive_and_refreshes(self):
        name = self.p.request("Add a timing option to the demo")
        path = self.p.claim(name, A)
        self.p.age_claim(name, 3 * 60)
        proc = subprocess.Popen([sys.executable, str(self.p.scripts / "claim.py"), "--run", path, "--label", "tests",
                                 "--agent-id", A, "--", sys.executable, "-c", "import time; time.sleep(3)"],
                                cwd=str(self.p.root), env=self.p.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True)
        try:
            self.assertTrue(self.p.wait_for(lambda: "step 'tests' running" in self.p.ok("claim.py", "--status")))
        finally:
            out, err = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 0, out + err)
        self.assertNotIn("STALE", self.p.ok("claim.py", "--status"))
        self.assertNotIn("step", self.p.claim_data(name))

    def test_run_step_propagates_exit_code(self):
        name = self.p.request("Add a failing step to the demo")
        path = self.p.claim(name, A)
        cp = self.p.run("claim.py", "--run", path, "--agent-id", A, "--", sys.executable, "-c", "raise SystemExit(3)")
        self.assertEqual(cp.returncode, 3)


# ---------------------------------------------------------------- sweep / recovery
class Sweep(Base):
    def sweep(self, apply=False):
        args = ["--sweep", "--json"] + (["--apply"] if apply else [])
        return json.loads(self.p.ok("claim.py", *args))

    def test_report_only_changes_nothing(self):
        s = self.p.sleeper()
        name = self.p.request("Add an alpha option to the demo")
        self.p.claim(name, A, s.pid)
        self.p.kill(s)
        before = sorted(str(x) for x in (self.p.root / "requests").rglob("*"))
        report = self.sweep()
        self.assertTrue(any("DEAD" in f.get("issue", "") for f in report["findings"]), report)
        self.assertEqual(report["actions"], [])
        self.assertEqual(before, sorted(str(x) for x in (self.p.root / "requests").rglob("*")))
        self.assertEqual(self.p.where(name), ["processing"])

    def test_dead_owner_without_work_is_requeued(self):
        s = self.p.sleeper()
        name = self.p.request("Add a beta option to the demo")
        self.p.claim(name, A, s.pid)
        self.p.kill(s)
        report = self.sweep(apply=True)
        self.assertIn("requeued-dead-owner", [a["action"] for a in report["actions"]], report)
        self.assertEqual(self.p.where(name), ["inbox"])
        self.assertFalse(self.p.lock_file(name).exists())
        self.assertIn("requeued", self.p.read(f"requests/inbox/{Path(name).with_suffix('.json')}")
                      + (self.p.root / ".agent/state/runtime/sweeps.jsonl").read_text())

    def test_dead_owner_with_uncommitted_work_is_left_for_explicit_recovery(self):
        s = self.p.sleeper()
        name = self.p.request("Add a gamma option to the demo")
        path = self.p.claim(name, A, s.pid)
        self.p.ok("claim.py", "--own", path, "--paths", "src/app.txt", agent=A)
        self.p.write("src/app.txt", "hello gamma\n")
        self.p.kill(s)
        report = self.sweep(apply=True)
        self.assertEqual([a for a in report["actions"] if a["target"] == name], [])
        self.assertTrue(any(f.get("recoverable") and f["target"] == name for f in report["findings"]))
        self.assertEqual(self.p.where(name), ["processing"])
        self.assertEqual(self.p.read("src/app.txt"), "hello gamma\n")

    def test_stale_owner_is_never_auto_repaired(self):
        name = self.p.request("Add a delta option to the demo")
        self.p.claim(name, A)
        self.p.age_claim(name, 5 * 60)
        report = self.sweep(apply=True)
        self.assertEqual(report["actions"], [])
        self.assertTrue(any("STALE" in f.get("issue", "") for f in report["findings"]))
        self.assertEqual(self.p.where(name), ["processing"])

    def interrupt(self, name):
        """A claim whose request move never happened (crash between the lock
        and the rename): move the files back to the inbox."""
        for suffix in (".md", ".json"):
            src = self.p.root / "requests/processing" / Path(name).with_suffix(suffix)
            src.rename(self.p.root / "requests/inbox" / src.name)

    def test_interrupted_claim_of_live_owner_is_completed(self):
        s = self.p.sleeper()
        name = self.p.request("Add an epsilon option to the demo")
        self.p.claim(name, A, s.pid)
        self.interrupt(name)
        report = self.sweep(apply=True)
        self.assertIn("completed-interrupted-claim", [a["action"] for a in report["actions"]], report)
        self.assertEqual(self.p.where(name), ["processing"])

    def test_owner_reclaiming_completes_its_interrupted_claim(self):
        name = self.p.request("Add a zeta option to the demo")
        self.p.claim(name, A)
        self.interrupt(name)
        out = self.p.ok("claim.py", "--claim-request", name, agent=A)
        self.assertIn("CLAIM COMPLETED", out)
        self.assertEqual(self.p.where(name), ["processing"])

    def test_interrupted_claim_of_dead_owner_is_dropped(self):
        s = self.p.sleeper()
        name = self.p.request("Add an eta option to the demo")
        self.p.claim(name, A, s.pid)
        self.interrupt(name)
        self.p.kill(s)
        report = self.sweep(apply=True)
        self.assertIn("dropped-interrupted-claim", [a["action"] for a in report["actions"]], report)
        self.assertFalse(self.p.lock_file(name).exists())
        self.assertEqual(self.p.where(name), ["inbox"])
        self.p.claim(name, B)   # claimable again

    def test_lone_sidecar_is_moved_beside_its_request(self):
        name = self.p.request("Add a theta option to the demo")
        js = self.p.root / "requests/inbox" / Path(name).with_suffix(".json")
        js.rename(self.p.root / "requests/processing" / js.name)
        report = self.sweep(apply=True)
        self.assertIn("moved-sidecar", [a["action"] for a in report["actions"]], report)
        self.assertTrue(js.exists())

    def test_recovery_refuses_verified_alive_owner_even_forced(self):
        s = self.p.sleeper()
        name = self.p.request("Add an iota option to the demo")
        self.p.claim(name, A, s.pid)
        cp = self.p.run("claim.py", "--recover-request", name, "--reason", "test", "--force-active", agent=B)
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
        self.assertEqual(self.p.claim_data(name)["agent_id"], A)

    def test_recovery_of_active_owner_needs_force(self):
        name = self.p.request("Add a kappa option to the demo")
        self.p.claim(name, A)
        cp = self.p.run("claim.py", "--recover-request", name, "--reason", "test", agent=B)
        self.assertNotEqual(cp.returncode, 0)
        self.p.ok("claim.py", "--recover-request", name, "--reason", "owner confirmed gone", "--force-active", agent=B)
        self.assertEqual(self.p.claim_data(name)["agent_id"], B)

    def test_recovery_of_dead_owner_keeps_its_work(self):
        s = self.p.sleeper()
        name = self.p.request("Add a lambda option to the demo")
        path = self.p.claim(name, A, s.pid)
        self.p.ok("claim.py", "--own", path, "--paths", "src/app.txt", agent=A)
        self.p.write("src/app.txt", "hello lambda\n")
        self.p.kill(s)
        cp = self.p.run("claim.py", "--recover-request", name, agent=B)
        self.assertNotEqual(cp.returncode, 0, "a recovery needs a reason")
        self.p.ok("claim.py", "--recover-request", name, "--reason", "owner DEAD", agent=B)
        data = self.p.claim_data(name)
        self.assertEqual(data["agent_id"], B)
        self.assertIn("src/app.txt", data["files"])
        self.assertEqual(self.p.read("src/app.txt"), "hello lambda\n")


# ---------------------------------------------------------------- transitions
class Transitions(Base):
    def test_reject_needs_reason_and_records_it(self):
        name = self.p.request("Add a mu option to the demo")
        path = self.p.claim(name, A)
        cp = self.p.run("claim.py", "--reject-request", path, agent=A)
        self.assertEqual(cp.returncode, 6)
        self.p.ok("claim.py", "--reject-request", path, "--reason", "out of scope", agent=A)
        side = json.loads(self.p.read(f"requests/rejected/{Path(name).with_suffix('.json')}"))
        self.assertEqual(side["resolution"]["reason"], "out of scope")

    def test_complete_without_gate_is_refused(self):
        name = self.p.request("Add a nu option to the demo")
        path = self.p.claim(name, A)
        self.p.work(name, A)
        cp = self.p.run("claim.py", "--complete-request", path, agent=A)
        self.assertEqual(cp.returncode, 9, cp.stdout + cp.stderr)
        self.assertEqual(self.p.where(name), ["processing"])

    def test_complete_with_no_linked_work_is_refused(self):
        name = self.p.request("Add a xi option to the demo")
        path = self.p.claim(name, A)
        cp = self.p.run("claim.py", "--complete-request", path, agent=A)
        self.assertEqual(cp.returncode, 9, cp.stdout + cp.stderr)

    def test_waiver_is_recorded_and_lets_the_commit_through(self):
        name = self.p.request("Add an omicron option to the demo")
        path = self.p.claim(name, A)
        self.p.ok("claim.py", "--complete-request", path, "--waive-gate", "maintainer accepted untested change",
                  agent=A)
        done = f"requests/completed/{name}"
        side = json.loads(self.p.read(str(Path(done).with_suffix(".json"))))
        self.assertEqual(side["resolution"]["gate_waiver"], "maintainer accepted untested change")
        out = self.p.ok("commit_request.py", "--request", done, "-m", "waived", agent=A)
        self.assertIn("COMMITTED", out)
        self.p.ok("claim.py", "--release-request", done, agent=A)

    def test_needs_review_parks_and_owner_resumes(self):
        name = self.p.request("Add a pi option to the demo")
        path = self.p.claim(name, A)
        self.p.ok("claim.py", "--needs-review-request", path, "--reason", "which colour?", agent=A)
        self.assertEqual(self.p.where(name), ["needs-review"])
        self.assertTrue(self.p.claim_data(name).get("parked"))
        self.assertIn("RESUMED", self.p.ok("claim.py", "--claim-request", name, agent=A))
        self.assertEqual(self.p.where(name), ["processing"])

    def test_parked_needs_review_is_taken_over_by_another_agent(self):
        name = self.p.request("Add a rho option to the demo")
        path = self.p.claim(name, A)
        self.p.ok("claim.py", "--needs-review-request", path, "--reason", "which size?", agent=A)
        self.assertIn("TAKEN OVER", self.p.ok("claim.py", "--claim-request", name, agent=B))
        self.assertEqual(self.p.claim_data(name)["agent_id"], B)

    def test_processing_request_is_not_taken_over(self):
        name = self.p.request("Add a sigma option to the demo")
        self.p.claim(name, A)
        cp = self.p.run("claim.py", "--claim-request", name, agent=B)
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)

    def test_release_refuses_uncommitted_owned_work_unless_abandoned(self):
        name = self.p.request("Add a tau option to the demo")
        path = self.p.claim(name, A)
        self.p.ok("claim.py", "--own", path, "--paths", "src/app.txt", agent=A)
        self.p.write("src/app.txt", "hello tau\n")
        cp = self.p.run("claim.py", "--release-request", path, "--requeue", agent=A)
        self.assertEqual(cp.returncode, 4, cp.stdout + cp.stderr)
        self.assertIn("src/app.txt", cp.stdout + cp.stderr)
        self.p.ok("claim.py", "--release-request", path, "--requeue", "--abandon", "handing back to the inbox", agent=A)
        self.assertEqual(self.p.where(name), ["inbox"])
        self.assertEqual(self.p.read("src/app.txt"), "hello tau\n")   # abandoned, not reverted
        hist = (self.p.root / ".agent/state/runtime/claims-history.jsonl").read_text()
        self.assertIn("handing back to the inbox", hist)


# ---------------------------------------------------------------- gate / commit binding
class GateAndCommit(Base):
    def setUp(self):
        super().setUp()
        self.p.suites([CHECK_SUITE])
        self.p.commit_all("suite")

    def change_and_test(self, name, agent, text):
        path = self.p.path_of(name)
        self.p.ok("claim.py", "--own", path, "--paths", "src/app.txt", agent=agent)
        self.p.write("src/app.txt", text)
        out = self.p.ok("run_tests.py", "--tier", "t0", "--request", path, agent=agent)
        return self.p.run_id(out)

    def test_full_lifecycle_with_trailers(self):
        name = self.p.request("Add an upsilon option to the demo")
        path = self.p.claim(name, A)
        work = self.p.work(name, A)
        run = self.change_and_test(name, A, "hello upsilon\n")
        self.p.evidence(work, A, [run])
        self.p.ok("work.py", "transition", work, "--to", "verification", agent=A)
        rc, gate = self.p.gate(work, A)
        self.assertEqual(gate["verdict"], "PASS", gate)
        self.p.ok("work.py", "transition", work, "--to", "completed", agent=A)
        self.p.ok("claim.py", "--complete-request", path, agent=A)
        done = f"requests/completed/{name}"
        self.p.ok("commit_request.py", "--request", done, "-m", "upsilon", agent=A)
        body = self.p.git("log", "-1", "--format=%B")
        for trailer in ("Request-Id:", "Agent-Id: " + A, "Work-Id: " + work, "Gate-Record:", "Test-Run: " + run):
            self.assertIn(trailer, body)
        self.p.ok("claim.py", "--release-request", done, agent=A)
        # Only the request's files were committed; the manifests are clean (B10).
        status = self.p.git("status", "--porcelain")
        self.assertEqual(status.strip(), "", status)
        self.assertTrue((self.p.root / f"evidence/gates/{work}.json").exists())

    def test_commit_is_verified_against_its_gate_record(self):
        name = self.p.request("Add a verify option to the demo")
        path = self.p.claim(name, A)
        work = self.p.work(name, A)
        run = self.change_and_test(name, A, "hello verify\n")
        self.p.evidence(work, A, [run])
        self.assertEqual(self.p.gate(work, A)[1]["verdict"], "PASS")
        self.p.ok("work.py", "transition", work, "--to", "completed", agent=A)
        self.p.ok("claim.py", "--complete-request", path, agent=A)
        self.p.ok("commit_request.py", "--request", f"requests/completed/{name}", "-m", "verify", agent=A)
        out = json.loads(self.p.ok("devsys.py", "verify-commit", "HEAD", "--json"))[0]
        self.assertEqual(out["status"], "VERIFIED", out)
        # The same commit message on different code does not verify.
        self.p.write("src/app.txt", "hello verify, but not the tested code\n")
        self.p.git("commit", "-q", "-a", "--amend", "--no-edit")
        cp = self.p.run("devsys.py", "verify-commit", "HEAD", "--json")
        self.assertEqual(cp.returncode, 1)
        self.assertEqual(json.loads(cp.stdout)[0]["status"], "FAIL")

    def test_waived_and_rejected_commits_are_classified(self):
        waived = self.p.request("Add a waived option to the demo")
        path = self.p.claim(waived, A)
        self.p.ok("claim.py", "--complete-request", path, "--waive-gate", "maintainer accepted", agent=A)
        self.p.ok("commit_request.py", "--request", f"requests/completed/{waived}", "-m", "w", agent=A)
        self.assertEqual(json.loads(self.p.ok("devsys.py", "verify-commit", "--json"))[0]["status"], "WAIVED")
        rejected = self.p.request("Add a rejected option to the demo")
        path = self.p.claim(rejected, B)
        self.p.ok("claim.py", "--reject-request", path, "--reason", "no", agent=B)
        self.p.ok("commit_request.py", "--request", f"requests/rejected/{rejected}", "-m", "r", agent=B)
        self.assertEqual(json.loads(self.p.ok("devsys.py", "verify-commit", "--json"))[0]["status"], "NOT_APPLICABLE")

    def test_commit_after_completion_needs_the_tested_code(self):
        name = self.p.request("Add a late option to the demo")
        path = self.p.claim(name, A)
        work = self.p.work(name, A)
        run = self.change_and_test(name, A, "hello late\n")
        self.p.evidence(work, A, [run])
        self.assertEqual(self.p.gate(work, A)[1]["verdict"], "PASS")
        self.p.ok("work.py", "transition", work, "--to", "completed", agent=A)
        self.p.ok("claim.py", "--complete-request", path, agent=A)
        self.p.write("src/app.txt", "hello late, edited after completion\n")
        cp = self.p.run("commit_request.py", "--request", f"requests/completed/{name}", "-m", "late", agent=A)
        self.assertEqual(cp.returncode, 9, cp.stdout + cp.stderr)
        self.assertEqual(self.p.git("log", "--oneline").count("\n"), 2, "nothing was committed")

    def test_a_request_commits_the_same_file_twice(self):
        # Regression: a progress commit and a later commit of the same file by
        # the same request conflicted (the baseline stayed at registration).
        name = self.p.request("Add a twice option to the demo")
        path = self.p.claim(name, A)
        self.p.ok("claim.py", "--own", path, "--paths", "src/app.txt", agent=A)
        self.p.write("src/app.txt", "hello, first version\n")
        self.p.ok("commit_request.py", "--request", path, "-m", "progress", agent=A)
        self.p.write("src/app.txt", "hello, second version\n")
        self.p.ok("commit_request.py", "--request", path, "-m", "more progress", agent=A)
        self.assertEqual(self.p.git("show", "HEAD:src/app.txt"), "hello, second version\n")
        self.assertEqual(self.p.git("status", "--porcelain", "src/app.txt").strip(), "")

    def test_drift_reads_under_the_work_and_claims_locks(self):
        # Regression: drift_detect, which every gate runs, read another agent's
        # records half-written: a work record moved before work.txt was updated
        # (the "work" lock), and a request moved to completed/ before its
        # resolution was written (the "claims" lock).
        import threading
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        import agent_process as proc_mod
        name = self.p.request("Add a lock-wait option to the demo")
        self.p.claim(name, A)
        work = self.p.work(name, A)
        for lock in ("work", "claims"):
            for tool in ("drift", "gate"):
                held = threading.Event()

                def hold():
                    with proc_mod.exclusive(self.p.root, lock):
                        held.set()
                        time.sleep(4.0)
                th = threading.Thread(target=hold)
                th.start()
                held.wait(5)
                t0 = time.time()
                if tool == "drift":
                    self.p.ok("drift_detect.py")
                else:
                    self.p.gate(work, A, record=False)
                waited = time.time() - t0
                th.join()
                self.assertGreaterEqual(waited, 3.5, f"{tool} waited for the {lock} lock")

    def test_doc_integrity_skips_a_file_moved_while_it_scans(self):
        # Regression: another agent's --complete-request moved a request file
        # between doc_integrity's listing and its read; the gate crashed.
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from unittest import mock
        import doc_integrity
        name = self.p.request("A request that moves during the scan")
        listed = doc_integrity.documentation_files(self.p.root)
        gone = self.p.root / "requests" / "processing" / name
        self.assertFalse(gone.exists())
        with mock.patch.object(doc_integrity, "documentation_files", return_value=[*listed, gone]):
            findings = doc_integrity.check(self.p.root)
        self.assertFalse([f for f in findings if f[0] == "FAIL"], findings)

    def test_gate_fails_when_code_changes_after_the_run(self):
        name = self.p.request("Add a phi option to the demo")
        self.p.claim(name, A)
        work = self.p.work(name, A)
        run = self.change_and_test(name, A, "hello phi\n")
        self.p.evidence(work, A, [run])
        self.p.write("src/app.txt", "hello phi, changed after the tests\n")
        rc, gate = self.p.gate(work, A)
        self.assertEqual(gate["verdict"], "FAIL")
        self.assertTrue(any("changed after test run" in f for f in gate["fails"]), gate["fails"])
        cp = self.p.run("work.py", "transition", work, "--to", "completed", agent=A)
        self.assertEqual(cp.returncode, 9)
        self.assertIn("FAIL gate record", cp.stdout + cp.stderr)

    def test_code_change_after_a_pass_gate_blocks_completion(self):
        name = self.p.request("Add a chi option to the demo")
        path = self.p.claim(name, A)
        work = self.p.work(name, A)
        run = self.change_and_test(name, A, "hello chi\n")
        self.p.evidence(work, A, [run])
        self.assertEqual(self.p.gate(work, A)[1]["verdict"], "PASS")
        self.p.write("src/app.txt", "hello chi, edited after the gate\n")
        cp = self.p.run("claim.py", "--complete-request", path, agent=A)
        self.assertEqual(cp.returncode, 9, cp.stdout + cp.stderr)

    def test_code_change_without_a_run_fails_the_gate(self):
        name = self.p.request("Add a psi option to the demo")
        path = self.p.claim(name, A)
        work = self.p.work(name, A)
        self.p.ok("claim.py", "--own", path, "--paths", "src/app.txt", agent=A)
        self.p.write("src/app.txt", "hello psi\n")
        self.p.evidence(work, A)
        rc, gate = self.p.gate(work, A)
        self.assertEqual(gate["verdict"], "FAIL")

    def test_documentation_only_change_needs_no_run(self):
        name = self.p.request("Document the demo greeting")
        path = self.p.claim(name, A)
        self.p.ok("claim.py", "--own", path, "--paths", "docs/requirements/demo.md", "--concurrent", agent=A)
        self.p.write("docs/requirements/demo.md", self.p.read("docs/requirements/demo.md") + "\nMore prose.\n")
        work = self.p.work(name, A)
        self.p.evidence(work, A)
        self.assertEqual(self.p.gate(work, A)[1]["verdict"], "PASS")

    def test_gate_record_needs_the_claim_owner(self):
        name = self.p.request("Add an omega option to the demo")
        self.p.claim(name, A)
        work = self.p.work(name, A)
        self.p.evidence(work, A)
        cp = self.p.run("completion_gate.py", "--work", work, "--record", agent=B)
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("needs the claim's owner", cp.stdout + cp.stderr)
        self.assertFalse((self.p.root / f"evidence/gates/{work}.json").exists())

    def test_unrelated_changes_are_never_committed(self):
        self.p.write("notes/unrelated.txt", "someone else's uncommitted edit\n")
        self.p.write("notes/new.txt", "someone else's new file\n")
        name = self.p.request("Add an alpha2 option to the demo")
        path = self.p.claim(name, A)
        self.p.ok("claim.py", "--complete-request", path, "--waive-gate", "test", agent=A)
        self.p.ok("commit_request.py", "--request", f"requests/completed/{name}", "-m", "x", agent=A)
        committed = self.p.git("show", "--name-only", "--format=", "HEAD").split()
        self.assertNotIn("notes/unrelated.txt", committed)
        self.assertNotIn("notes/new.txt", committed)
        self.assertEqual(self.p.read("notes/unrelated.txt"), "someone else's uncommitted edit\n")
        self.assertIn("notes/new.txt", self.p.git("status", "--porcelain"))


# ---------------------------------------------------------------- work / evidence tools
class WorkTool(Base):
    def test_create_requires_the_owner_and_known_requirements(self):
        name = self.p.request("Add a beta2 option to the demo")
        path = self.p.claim(name, A)
        cp = self.p.run("work.py", "create", "--request", path, "--title", "t", "--requirements", "REQ-DEMO-001",
                        agent=B)
        self.assertEqual(cp.returncode, 3, cp.stdout + cp.stderr)
        cp = self.p.run("work.py", "create", "--request", path, "--title", "t", "--requirements", "REQ-DEMO-099",
                        agent=A)
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("REQ-DEMO-099", cp.stdout + cp.stderr)

    def test_create_links_request_and_manifest(self):
        name = self.p.request("Add a gamma2 option to the demo")
        self.p.claim(name, A)
        work = self.p.work(name, A)
        self.assertIn(work, self.p.read(self.p.path_of(name)))
        self.assertIn(work, self.p.read("work/work.txt"))
        side = json.loads(self.p.read(str(Path(self.p.path_of(name)).with_suffix(".json"))))
        self.assertIn(work, json.dumps(side.get("links", {})))

    def test_transitions_are_validated_recorded_and_idempotent(self):
        name = self.p.request("Add a delta2 option to the demo")
        self.p.claim(name, A)
        work = self.p.work(name, A)
        cp = self.p.run("work.py", "transition", work, "--to", "blocked", agent=A)
        self.assertEqual(cp.returncode, 6, "blocked needs a reason")
        self.p.ok("work.py", "transition", work, "--to", "blocked", "--reason", "waiting for input", agent=A)
        cp = self.p.run("work.py", "transition", work, "--to", "completed", agent=A)
        self.assertEqual(cp.returncode, 4, "blocked -> completed is not a transition")
        self.p.ok("work.py", "transition", work, "--to", "active", agent=A)
        self.p.ok("work.py", "transition", work, "--to", "active", agent=A)   # idempotent
        record = next((self.p.root / "work/active").glob(f"{work}-*.md")).read_text()
        self.assertIn("**Status:** active", record)
        self.assertIn("-> blocked", record)
        self.assertIn("waiting for input", record)
        self.assertIn(f"{work} | active", self.p.read("work/work.txt"))
        journal = (self.p.root / ".agent/state/runtime/transitions.jsonl").read_text()
        self.assertIn("transition-start", journal)

    def test_evidence_links_work_and_keeps_manifest_bytes(self):
        header = self.p.read("evidence/evidence.txt")
        name = self.p.request("Add an eps2 option to the demo")
        self.p.claim(name, A)
        work = self.p.work(name, A)
        ver = self.p.evidence(work, A)
        text = self.p.read("evidence/evidence.txt")
        self.assertTrue(text.startswith(header), "existing manifest lines are kept byte for byte (B10)")
        self.assertIn(f"{ver} | pass", text)
        record = next((self.p.root / "work/active").glob(f"{work}-*.md")).read_text()
        self.assertIn(ver, record)

    def test_evidence_attaches_run_records(self):
        self.p.suites([CHECK_SUITE])
        self.p.commit_all("suite")
        name = self.p.request("Add a zeta2 option to the demo")
        path = self.p.claim(name, A)
        work = self.p.work(name, A)
        run = self.p.run_id(self.p.ok("run_tests.py", "--tier", "t0", "--request", path, agent=A))
        ver = self.p.evidence(work, A, [run])
        self.assertTrue((self.p.root / "evidence" / ver / f"run-{run}.json").is_file())
        cp = self.p.run("evidence.py", "create", "--work", work, "--status", "pass", "--command", "x",
                        "--result", "x", "--test-run", "20990101T000000-1", agent=A)
        self.assertEqual(cp.returncode, 5, "an unknown run is refused before anything is written")


# ---------------------------------------------------------------- intake / ids
class Intake(Base):
    def test_duplicate_open_request_is_refused(self):
        self.p.request("Add a logging option to the demo")
        cp = self.p.run("create_request.py", "--source", "chat", "--text", "add a  LOGGING option to the demo.")
        self.assertEqual(cp.returncode, 7, cp.stdout + cp.stderr)
        self.p.request("Add a logging option to the demo", "--allow-duplicate")

    def test_concurrent_duplicates_create_exactly_one(self):
        go = self.p.base / "go"
        code = ("import os,sys,time\nwhile not os.path.exists(sys.argv[1]): time.sleep(0.001)\n"
                "os.execv(sys.executable,[sys.executable,sys.argv[2],'--source','chat','--text',sys.argv[3]])")
        procs = [subprocess.Popen([sys.executable, "-c", code, str(go), str(self.p.scripts / "create_request.py"),
                                   "Add a tracing option to the demo"], cwd=str(self.p.root), env=self.p.env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for _ in range(4)]
        time.sleep(0.8)
        go.write_text("go")
        codes = sorted(p.wait(timeout=120) for p in procs)
        self.assertEqual(codes, [0, 7, 7, 7])
        self.assertEqual(len(list((self.p.root / "requests/inbox").glob("*.md"))) - 1, 1)

    def test_follow_up_targets_exactly_one_open_request(self):
        name = self.p.request("Add a metrics option to the demo")
        rid = json.loads(self.p.read(f"requests/inbox/{Path(name).with_suffix('.json')}"))["request_id"]
        self.p.ok("create_request.py", "--source", "chat", "--follow-up", rid, "--text", "also export them")
        self.assertIn("also export them", self.p.read(f"requests/inbox/{name}"))
        cp = self.p.run("create_request.py", "--source", "chat", "--follow-up", rid[:-1] + "9", "--text", "x")
        self.assertEqual(cp.returncode, 5, cp.stdout + cp.stderr)

    def test_follow_up_to_a_closed_request_is_refused(self):
        name = self.p.request("Add a profile option to the demo")
        rid = json.loads(self.p.read(f"requests/inbox/{Path(name).with_suffix('.json')}"))["request_id"]
        path = self.p.claim(name, A)
        self.p.ok("claim.py", "--reject-request", path, "--reason", "no", agent=A)
        cp = self.p.run("create_request.py", "--source", "chat", "--follow-up", rid, "--text", "more")
        self.assertEqual(cp.returncode, 4, cp.stdout + cp.stderr)

    def test_concurrent_follow_ups_are_all_kept(self):
        name = self.p.request("Add a cache option to the demo")
        rid = json.loads(self.p.read(f"requests/inbox/{Path(name).with_suffix('.json')}"))["request_id"]
        procs = [subprocess.Popen([sys.executable, str(self.p.scripts / "create_request.py"), "--source", "chat",
                                   "--follow-up", rid, "--text", f"follow-up number {i}"], cwd=str(self.p.root),
                                  env=self.p.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for i in range(5)]
        self.assertEqual([p.wait(timeout=120) for p in procs], [0] * 5)
        text = self.p.read(f"requests/inbox/{name}")
        for i in range(5):
            self.assertIn(f"follow-up number {i}", text)

    def test_next_is_priority_then_arrival(self):
        first = self.p.request("Add a zebra mode to the demo")          # slug sorts last
        self.p.request("Add an aardvark mode to the demo")              # slug sorts first
        out = self.p.ok("devsys.py", "next")
        self.assertIn(first, out, "arrival order, not the slug (B7)")
        high = self.p.request("Add a critical fix to the demo", "--priority", "HIGH")
        self.assertIn(high, self.p.ok("devsys.py", "next"))

    def test_fresh_repository_ignores_request_ids_cited_in_manifests(self):
        # A manifest row that cites another project's request id (history,
        # templates) must not advance this repository's series (B6).
        today = json.loads(self.p.read(
            f"requests/inbox/{Path(self.p.request('Add a sample option to the demo')).with_suffix('.json')}"))["request_id"]
        series = today.rsplit("-", 1)[0]
        self.p.write("docs/decisions/decisions.txt", self.p.read("docs/decisions/decisions.txt")
                     + f"ADR-000001 | accepted | x | ADR-000001-x.md | {series}-777\n")
        self.p.commit_all("history row")
        name = self.p.request("Add another sample option to the demo")
        rid = json.loads(self.p.read(f"requests/inbox/{Path(name).with_suffix('.json')}"))["request_id"]
        self.assertEqual(rid, f"{series}-002")

    def test_ids_on_a_fetched_branch_are_not_reissued(self):
        clone = self.p.base / "clone"
        subprocess.run(["git", "clone", "-q", str(self.p.root), str(clone)], check=True)
        env = dict(self.p.env, AGENT_PROCESS_ROOT=str(clone))
        subprocess.run(["git", "-C", str(clone), "config", "user.email", "c@example.com"], check=True)
        subprocess.run(["git", "-C", str(clone), "config", "user.name", "Clone"], check=True)
        out = subprocess.run([sys.executable, str(self.p.scripts / "allocate_id.py"), "WORK"], cwd=str(clone),
                             env=env, capture_output=True, text=True, check=True).stdout.strip()
        clone_id = out.split()[-1]
        (clone / "work/active" / f"{clone_id}-from-clone.md").write_text("# x\n")
        subprocess.run(["git", "-C", str(clone), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(clone), "commit", "-qm", "clone work"], check=True)
        self.p.git("fetch", "-q", str(clone), "main:refs/remotes/clone/main")
        mine = self.p.ok("allocate_id.py", "WORK").strip().split()[-1]
        self.assertGreater(mine, clone_id)


class NestedRoot(unittest.TestCase):
    def test_a_repository_inside_another_is_its_own_root(self):
        outer = Project(fast_config())
        try:
            inner = outer.root / "vendor" / "inner"
            inner.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", str(inner)], check=True)
            env = dict(outer.env, AGENT_PROCESS_ROOT=str(inner))
            out = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]);"
                                  "import agent_process as p, pathlib; print(p.main_root(pathlib.Path(sys.argv[2])))",
                                  str(outer.scripts), str(inner)], env=env, capture_output=True, text=True, check=True)
            self.assertEqual(Path(out.stdout.strip()).resolve(), inner.resolve())
        finally:
            outer.close()


if __name__ == "__main__":
    unittest.main()
