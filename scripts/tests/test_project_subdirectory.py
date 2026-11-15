#!/usr/bin/env python3
"""The development system installed as a subdirectory of the project it
serves (``<project>/.devsys/``, ``paths.project_root`` ".."): the Phase 3
validation of the Worker Orchestrator merge (WORKER-013, REQ-DEVOPS-016 /
038 / 042 / 048, ADR-000200).

Repository paths -- claim registrations, commit paths, fingerprints, what is
handed to git -- are relative to the project checkout; the system's records
stay under ``.devsys/``; configuration globs are relative to ``.devsys/``.
Every test installs a fresh copy with ``devsys.py install`` into a project
repository and drives it only through the command-line tools, from the
project root as an agent working there would.

Run: python3 -m unittest scripts.tests.test_project_subdirectory -v
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from devsys_testlib import FRAMEWORK, REQUIREMENTS, SCRIPTS, fast_config  # noqa: E402

FAKE = ".devsys/scripts/tests/fake_agent.py"


class SubdirProject:
    """A git project with the system installed in ``.devsys/``."""

    def __init__(self, config: dict | None = None, suites: list | None = None):
        base = FRAMEWORK / "tmp" / "devsys-tests"
        base.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix="devsys-sub-", dir=str(base))
        self.base = Path(self.tmp.name)
        self.project = self.base / "project"
        self.system = self.project / ".devsys"
        self.project.mkdir()
        self.env = {k: v for k, v in os.environ.items() if k not in ("AGENT_ID", "AGENT_OWNER_PID",
                                                                      "AGENT_PROCESS_ROOT")}
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Devsys Test")
        self.write("src/app.txt", "hello\n")
        self.write("notes/unrelated.txt", "someone else's file\n")
        self.commit_all("the project")
        out = subprocess.run([sys.executable, str(SCRIPTS / "devsys.py"), "install", "--target", str(self.system),
                              "--project-name", "demo"], capture_output=True, text=True, env=self.env)
        assert out.returncode == 0, out.stdout + out.stderr
        self.install_output = out.stdout
        cfg = json.loads((self.system / "devsys.json").read_text())
        for key, value in fast_config(**(config or {})).items():
            if isinstance(value, dict) and isinstance(cfg.get(key), dict):
                cfg[key].update(value)
            else:
                cfg[key] = value
        (self.system / "devsys.json").write_text(json.dumps(cfg, indent=2) + "\n")
        self.write(".devsys/docs/requirements/demo.md", REQUIREMENTS)
        req = self.system / "docs/requirements/requirements.txt"
        req.write_text(req.read_text() + "demo | demo.md | REQ-DEMO-001..002 | demo tests\n")
        if suites is not None:
            manifest = json.loads((self.system / "scripts/test_manifest.json").read_text())
            manifest["suites"] += suites
            (self.system / "scripts/test_manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")
        self.commit_all("install the development system in .devsys/")

    def close(self):
        self.tmp.cleanup()

    # ------------------------------------------------------------- helpers
    def git(self, *args, cwd=None, check=True) -> str:
        return subprocess.run(["git", *args], cwd=str(cwd or self.project), capture_output=True, text=True,
                              check=check).stdout

    def write(self, rel: str, text: str) -> Path:
        p = self.project / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def commit_all(self, msg: str):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)

    def run(self, script: str, *args, agent: str | None = None, env: dict | None = None, cwd=None, timeout=240):
        """A tool of the installation, run from the project root (by default)."""
        e = dict(self.env)
        if agent:
            e["AGENT_ID"] = agent
        e.update(env or {})
        return subprocess.run([sys.executable, str(self.system / "scripts" / script), *args],
                              cwd=str(cwd or self.project), env=e, capture_output=True, text=True, timeout=timeout)

    def ok(self, script: str, *args, **kw) -> str:
        cp = self.run(script, *args, **kw)
        if cp.returncode != 0:
            raise AssertionError(f"{script} {' '.join(args)} -> {cp.returncode}\n{cp.stdout}\n{cp.stderr}")
        return cp.stdout

    def request(self, text: str) -> str:
        out = self.ok("create_request.py", "--source", "chat", "--text", text)
        return Path(next(l for l in out.splitlines() if l.startswith("Created ")).split()[1]).name

    def where(self, name: str) -> list[str]:
        base = self.system / "requests"
        return [s for s in ("inbox", "processing", "completed", "rejected", "needs-review") if (base / s / name).is_file()]

    def claim(self, name: str) -> dict:
        return json.loads((self.system / ".agent/state/claims" / f"request-{name}.lock").read_text())

    def attempts(self, wid: str) -> list[dict]:
        base = self.system / ".agent/state/runtime/workers" / wid / "attempts"
        return [json.loads((d / "attempt.json").read_text()) for d in sorted(base.iterdir())] if base.is_dir() else []

    def use_fake_agent(self, **workers):
        cfg = json.loads((self.system / "devsys.json").read_text())
        cfg["workers"] = {"poll_seconds": 0.2, "max_workers": 2,
                          "attempt": {"max_runtime_minutes": 2, "max_attempts_per_request": 2, "kill_grace_seconds": 2},
                          "agent": {"command": [sys.executable, str(self.project / FAKE)]}, **workers}
        (self.system / "devsys.json").write_text(json.dumps(cfg, indent=2) + "\n")
        self.commit_all("configure the workers' agent")

    def run_worker(self, wid="worker-01"):
        cp = self.run("devsys.py", "workers", "run", wid, "--once", cwd=self.system)
        assert cp.returncode == 0, cp.stdout + cp.stderr
        return cp.stdout


class Install(unittest.TestCase):
    def test_install_into_a_project_subdirectory_sets_the_project_root(self):
        p = SubdirProject()
        try:
            self.assertIn("paths.project_root = '..'", p.install_output)
            self.assertEqual(json.loads((p.system / "devsys.json").read_text())["paths"]["project_root"], "..")
            doctor = json.loads(p.ok("devsys.py", "doctor", "--json", cwd=p.system))
            git_check = next(c for c in doctor["checks"] if c["name"] == "git repository")
            self.assertEqual(git_check["level"], "PASS", git_check)
            self.assertIn("system directory .devsys/", git_check["detail"])
            # The system's runtime state is ignored inside the system directory.
            self.assertIn(".agent/state/claims/", (p.system / ".gitignore").read_text())
        finally:
            p.close()

    def test_an_ignored_directory_of_another_repository_is_not_a_project_subdirectory(self):
        p = SubdirProject()
        try:
            p.write(".gitignore", "/scratch/\n")
            target = p.project / "scratch" / "standalone"
            out = subprocess.run([sys.executable, str(SCRIPTS / "devsys.py"), "install", "--target", str(target)],
                                 capture_output=True, text=True, env=p.env)
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
            self.assertNotIn("project_root", out.stdout)
            self.assertNotIn("project_root", json.loads((target / "devsys.json").read_text()).get("paths", {}))
        finally:
            p.close()


class Lifecycle(unittest.TestCase):
    SUITE = {"id": "app", "kind": "command", "results": "exit", "cwd": "..",
             "command": [sys.executable, "-c", "import pathlib, sys; sys.exit(0 if pathlib.Path('src/app.txt')"
                                               ".read_text().startswith('hello') else 1)"],
             "inputs": ["../src/**"], "groups": {"unit": {"tiers": ["t1"], "expected_seconds": 1}}}

    def setUp(self):
        self.p = SubdirProject(suites=[self.SUITE])
        self.agent = self.p.ok("claim.py", "--new-agent-id").strip()

    def tearDown(self):
        self.p.close()

    def test_request_lifecycle_from_the_project_root(self):
        p, agent = self.p, self.agent
        name = p.request("Extend the greeting in src/app.txt")
        p.ok("claim.py", "--claim-request", name, agent=agent)
        preflight = p.run("preflight.py", "--request", f".devsys/requests/processing/{name}", agent=agent)
        self.assertEqual(preflight.returncode, 0, preflight.stdout + preflight.stderr)
        p.ok("claim.py", "--own", name, "--paths", "src/app.txt", str(p.project / ".devsys/docs/requirements/demo.md"),
             agent=agent)
        files = p.claim(name)["files"]
        # Repository paths: the project's file as it is, the system's records under .devsys/.
        self.assertIn("src/app.txt", files)
        self.assertIn(".devsys/docs/requirements/demo.md", files)
        self.assertIn(f".devsys/requests/processing/{name}", files)
        p.write("src/app.txt", "hello, world\n")
        out = p.ok("work.py", "create", "--request", name, "--title", "longer greeting", "--requirements",
                   "REQ-DEMO-001", agent=agent)
        wid = next(l.split()[1] for l in out.splitlines() if l.startswith("WORK-ID"))
        plan = p.ok("run_tests.py", "--plan", "--tier", "t2", "--request", name, agent=agent)
        self.assertIn("input ../src/app.txt", plan)          # manifest globs are relative to .devsys/
        run = p.ok("run_tests.py", "--tier", "t2", "--request", name, agent=agent)
        run_id = Path(next(l for l in run.splitlines() if l.startswith("record: ")).split()[1]).stem
        p.ok("evidence.py", "create", "--work", wid, "--status", "pass", "--test-run", run_id, "--command", "t2",
             "--result", "PASS", agent=agent)
        gate = p.run("completion_gate.py", "--work", wid, "--record", "--json", agent=agent)
        self.assertEqual(json.loads(gate.stdout)["verdict"], "PASS", gate.stdout)
        p.ok("work.py", "transition", wid, "--to", "completed", agent=agent)
        p.ok("claim.py", "--complete-request", name, agent=agent)
        out = p.ok("commit_request.py", "--request", name, "-m", "longer greeting", agent=agent)
        self.assertIn("COMMITTED", out)
        p.ok("claim.py", "--release-request", name, agent=agent)
        committed = p.git("show", "--name-only", "--format=", "HEAD").split()
        self.assertIn("src/app.txt", committed)
        self.assertIn(f".devsys/requests/completed/{name}", committed)
        self.assertIn(f".devsys/evidence/gates/{wid}.json", committed)
        self.assertIn(f"Gate-Record: .devsys/evidence/gates/{wid}.json", p.git("log", "-1", "--format=%B"))
        self.assertIn("VERIFIED", p.ok("devsys.py", "verify-commit", "HEAD", cwd=p.system))
        self.assertEqual(p.git("status", "--porcelain"), "")
        self.assertEqual(p.where(name), ["completed"])

    def test_absolute_and_relative_paths_name_the_same_repository_path(self):
        p, agent = self.p, self.agent
        name = p.request("Touch the app file")
        p.ok("claim.py", "--claim-request", name, agent=agent)
        p.ok("claim.py", "--own", name, "--paths", str(p.project / "src" / "app.txt"), agent=agent)
        self.assertIn("src/app.txt", p.claim(name)["files"])
        refused = p.run("claim.py", "--own", name, "--paths", str(p.base / "outside.txt"), agent=agent)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("outside the repository", refused.stdout + refused.stderr)

    def test_protected_artifacts_and_invariants_use_system_relative_names(self):
        sys.path.insert(0, str(p_scripts := self.p.system / "scripts"))
        try:
            import process_protection
            claim = {"files": {".devsys/AGENTS.md": {}, "src/app.txt": {}}}
            self.assertEqual(process_protection.protected_paths(claim, self.p.system), ["AGENTS.md"])
        finally:
            sys.path.remove(str(p_scripts))
        cfg = json.loads((self.p.system / "devsys.json").read_text())
        cfg["invariants"] = [{"id": "no-forbidden-word", "kind": "forbid_pattern", "files": ["../src/**"],
                              "pattern": "forbidden", "message": "the project's sources stay clean"}]
        (self.p.system / "devsys.json").write_text(json.dumps(cfg, indent=2) + "\n")
        self.assertEqual(self.p.run("invariant_check.py", cwd=self.p.system).returncode, 0)
        self.p.write("src/app.txt", "a forbidden word\n")
        failed = self.p.run("invariant_check.py", cwd=self.p.system)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("../src/app.txt", failed.stdout)


class Workers(unittest.TestCase):
    def test_worker_runs_its_agent_in_the_project_root(self):
        p = SubdirProject()
        try:
            p.use_fake_agent()
            name = p.request("Write the demo page [fake:complete]")
            p.run_worker()
            a = p.attempts("worker-01")[-1]
            self.assertEqual((a["result"], a["request_state"]), ("COMPLETED", "completed"), a.get("result_reason"))
            self.assertEqual(a["validation"]["overall"], "PASS", a["validation"])
            self.assertEqual(Path(a["cwd"]).resolve(), p.project.resolve())
            adir = p.system / ".agent/state/runtime/workers/worker-01/attempts" / a["attempt_id"]
            launch = json.loads((adir / "launch.json").read_text())
            self.assertEqual(Path(launch["env_added"]["AGENT_PROCESS_ROOT"]).resolve(), p.system.resolve())
            prompt = (adir / "prompt.md").read_text()
            self.assertIn("`.devsys/scripts/`", prompt)
            self.assertIn("`.devsys/AGENTS.md`", prompt)
            committed = p.git("show", "--name-only", "--format=", "HEAD").split()
            rid = json.loads((p.system / "requests/completed" / Path(name).with_suffix(".json")).read_text())["request_id"]
            self.assertIn(f"docs/fake/{rid}.md", committed)                  # a project path
            self.assertIn(f".devsys/requests/completed/{name}", committed)    # the system's record
            self.assertEqual(p.git("status", "--porcelain"), "")
        finally:
            p.close()

    def test_worktree_requests_are_isolated_and_their_commits_verified(self):
        p = SubdirProject()
        try:
            p.use_fake_agent(isolation="worktree")
            a_name = p.request("Page A [fake:complete]")
            b_name = p.request("Page B [fake:complete]")
            main_head = p.git("rev-parse", "HEAD").strip()
            p.run_worker("worker-01")
            p.run_worker("worker-02")
            done = [p.attempts(w)[-1] for w in ("worker-01", "worker-02")]
            trees = []
            for name, a in zip((a_name, b_name), done):
                stem = Path(name).stem
                wt = Path(a["cwd"])
                trees.append(wt)
                self.assertEqual(wt.resolve(), (p.base / "project-worktrees" / stem).resolve())
                self.assertEqual(a["request_state"], "completed", a.get("result_reason"))
                binding = next(s for s in a["validation"]["steps"] if s["name"] == "commit-binding")
                self.assertEqual(binding["status"], "PASS", binding)
                self.assertIn("VERIFIED", binding["detail"])                 # verified, not NOT_APPLICABLE
                launch = json.loads((p.system / ".agent/state/runtime/workers" / a["worker_id"] / "attempts"
                                     / a["attempt_id"] / "launch.json").read_text())
                self.assertEqual(Path(launch["env_added"]["AGENT_PROCESS_ROOT"]).resolve(), (wt / ".devsys").resolve())
                self.assertEqual(p.git("rev-parse", "--abbrev-ref", "HEAD", cwd=wt).strip(), f"request/{stem}")
                self.assertEqual(p.git("status", "--porcelain", cwd=wt), "")
                self.assertEqual(p.where(name), ["completed"])               # moved in the main checkout
            # Each branch holds only its own page; main has neither (publishing is manual).
            rid = [a["request_id"] for a in done]
            self.assertTrue((trees[0] / f"docs/fake/{rid[0]}.md").exists())
            self.assertFalse((trees[0] / f"docs/fake/{rid[1]}.md").exists())
            self.assertTrue((trees[1] / f"docs/fake/{rid[1]}.md").exists())
            self.assertFalse((trees[1] / f"docs/fake/{rid[0]}.md").exists())
            self.assertEqual(p.git("rev-parse", "HEAD").strip(), main_head)
            self.assertFalse((p.project / "docs").exists())
        finally:
            p.close()


if __name__ == "__main__":
    unittest.main()
