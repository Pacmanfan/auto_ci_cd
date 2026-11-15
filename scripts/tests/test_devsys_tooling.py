#!/usr/bin/env python3
"""Framework tooling (ADR-000199): configuration, bootstrap (install / init /
doctor / status), build adapters, declarative invariants, audit levels
(drift, doc_integrity, health), the test orchestrator's honest results
(EMPTY, INTERRUPTED, JUnit, TAP, kept failure logs, configured build trees)
and the regressions found while running the framework on a fresh project
(B1..B10 in CI_CD.md "Verification").

Requirements verified: REQ-DEVOPS-018 (preflight), REQ-DEVOPS-020 (drift),
REQ-DEVOPS-022 (invariants), REQ-DEVOPS-026 (doc integrity), REQ-DEVOPS-027 (health,
status), REQ-DEVOPS-046 (test orchestration), REQ-DEVOPS-047 (configuration, bootstrap),
REQ-DEVOPS-049 (build adapters), REQ-DEVOPS-050 (honest results).

Run: python3 -m unittest scripts.tests.test_devsys_tooling -v
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from devsys_testlib import SCRIPTS, Project, fast_config  # noqa: E402

A = "agent-test-a"


class Base(unittest.TestCase):
    config: dict = fast_config()
    install = False

    def setUp(self):
        self.p = Project(self.config, install=self.install)

    def tearDown(self):
        self.p.close()

    def config_set(self, **sections):
        cfg = json.loads(self.p.read("devsys.json"))
        for key, value in sections.items():
            if isinstance(value, dict) and isinstance(cfg.get(key), dict):
                cfg[key].update(value)
            else:
                cfg[key] = value
        self.p.write("devsys.json", json.dumps(cfg, indent=2) + "\n")


# ---------------------------------------------------------------- configuration
class Configuration(Base):
    def validate(self):
        return self.p.run("devsys_config.py", "--validate")

    def test_defaults_and_overrides_merge(self):
        out = json.loads(self.p.ok("devsys_config.py"))
        self.assertEqual(out["claims"]["stale_hours"], 2.0)
        self.assertEqual(out["claims"]["dead_grace_minutes"], 0)          # fixture override
        self.assertEqual(out["completion"]["exempt_types"], ["INVESTIGATION"])
        self.assertEqual(self.p.ok("devsys_config.py", "--get", "project.name").strip(), "demo")

    def test_invalid_values_are_reported(self):
        self.config_set(build={"system": "ninja"}, claims={"dead_grace_minutes": -1},
                        invariants=[{"id": "x", "kind": "forbid_pattern", "files": ["*"], "pattern": "("}],
                        unknown_section={"a": 1})
        cp = self.validate()
        self.assertNotEqual(cp.returncode, 0)
        for text in ("build.system", "dead_grace_minutes", "regular expression", "unknown_section"):
            self.assertIn(text, cp.stdout + cp.stderr)

    def test_request_prefixes_and_source_roots_are_configuration(self):
        self.config_set(ids={"request_sources": {"chat": "REQ-TICKET", "inbox": "REQ-IN", "crash": "REQ-CRASH",
                                                 "automation": "REQ-BOT"}})
        name = self.p.request("Add a prefixed option to the demo")
        self.assertIn("-req-ticket-", name)
        self.config_set(ids={"request_sources": {"chat": "ticket", "inbox": "REQ-IN", "crash": "REQ-CRASH",
                                                 "automation": "REQ-BOT"}},
                        project={"source_roots": ["src", "no-such-dir"]})
        cp = self.validate()
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("ids.request_sources.chat", cp.stdout + cp.stderr)
        self.assertIn("no-such-dir", cp.stdout + cp.stderr)

    def test_rule_without_files_applies_to_the_source_roots(self):
        self.p.write("src/lib.c", "#include <stdio.h>\n")
        self.p.write("vendor/third.c", "#include <stdio.h>\n")
        self.config_set(project={"source_roots": ["src"]},
                        invariants=[{"id": "no-stdio", "kind": "forbid_pattern", "pattern": "<stdio.h>"}])
        out = json.loads(self.p.run("invariant_check.py", "--json").stdout)
        self.assertEqual(len(out["fails"]), 1, out)
        self.assertIn("src/lib.c", out["fails"][0])
        self.assertNotIn("vendor", out["fails"][0])

    def test_placeholders_ignore_regular_expressions(self):
        # B1: "<stdio.h>" in an invariant pattern is not a template placeholder.
        self.config_set(invariants=[{"id": "no-io", "kind": "forbid_pattern", "files": ["src/*"],
                                     "pattern": "#include\\s*<stdio.h>"}],
                        paths={"crash_dumps": "<CRASH_DIR>"})
        sys.path.insert(0, str(SCRIPTS))
        import devsys_config as cfgmod
        found = cfgmod.placeholders(self.p.root)
        cfgmod.load(self.p.root, refresh=True)
        self.assertTrue(any("<CRASH_DIR>" in f for f in found), found)
        self.assertFalse(any("stdio" in f for f in found), found)


# ---------------------------------------------------------------- bootstrap
class Bootstrap(unittest.TestCase):
    def test_install_init_doctor_on_a_fresh_project(self):
        p = Project(fast_config(), install=True, name="fresh")
        try:
            for rel in ("scripts/claim.py", "scripts/devsys.py", "AGENTS.md", "docs/process/state-model.md",
                        "requests/inbox/README.md", "work/active/README.md", "evidence/evidence.txt"):
                self.assertTrue((p.root / rel).exists(), rel)
            self.assertFalse(list(p.root.rglob("__pycache__")))
            self.assertEqual(list((p.root / "requests/inbox").glob("*.md")), [p.root / "requests/inbox/README.md"])
            gitignore = p.read(".gitignore")
            for line in (".agent/state/claims/", "/build/", "/tmp/"):
                self.assertIn(line, gitignore)          # B2: the build tree is local
            cfg = json.loads(p.read("devsys.json"))
            self.assertEqual(cfg["project"]["name"], "fresh")
            self.assertIn("_examples", cfg, "--project-name updates the shipped template, it does not replace it")
            doctor = p.run("devsys.py", "doctor", "--json")
            report = json.loads(doctor.stdout)
            levels = {c["name"]: c["level"] for c in report["checks"]}
            self.assertEqual(levels["devsys.json"], "PASS", report)
            self.assertEqual(levels["structure"], "PASS", report)
            self.assertIn("build.system is 'none' (no build integration)", report["not_configured"])
            status = json.loads(p.ok("devsys.py", "status", "--json"))
            self.assertEqual(status["requests"]["inbox"], 0)
            # A fresh project's first ids start at 1 (B6).
            name = p.request("Add a first feature to the fresh project")
            self.assertTrue(name.endswith("-001.md"), name)
        finally:
            p.close()

    def test_install_keeps_existing_files(self):
        p = Project(fast_config(), install=True)
        try:
            p.write("AGENTS.md", "# project's own constitution\n")
            out = subprocess.run([sys.executable, str(SCRIPTS / "devsys.py"), "install", "--target", str(p.root)],
                                 capture_output=True, text=True, env=dict(p.env, PYTHONDONTWRITEBYTECODE="1"))
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
            self.assertEqual(p.read("AGENTS.md"), "# project's own constitution\n")
        finally:
            p.close()

    def test_status_writes_mirrors_and_drift_notices_stale_ones(self):
        p = Project(fast_config(), install=True)
        try:
            p.request("Add a mirror feature to the demo")
            p.ok("devsys.py", "status", "--write-mirrors")
            self.assertIn("inbox", p.read(".agent/state/requests.md"))
            self.assertNotIn("MIRROR_STALE", p.run("drift_detect.py").stdout)
            p.request("Add another mirror feature to the demo")
            self.assertIn("MIRROR_STALE", p.run("drift_detect.py").stdout)
        finally:
            p.close()


class Templates(unittest.TestCase):
    def test_distribution_instance_files_equal_their_templates(self):
        framework = SCRIPTS.parent
        if json.loads((framework / "devsys.json").read_text()).get("project", {}).get("name") != "<PROJECT>":
            self.skipTest("an installed project: its instance files are its own")
        sys.path.insert(0, str(SCRIPTS))
        import devsys
        for rel, tpl in devsys.INSTANCE_TEMPLATES.items():
            self.assertEqual((framework / rel).read_bytes(), (SCRIPTS / "templates" / tpl).read_bytes(), rel)

    def test_install_from_a_configured_project_does_not_leak_it(self):
        # B12: install copied the source instance's devsys.json (and would
        # have copied its suites, requirement rows and ADRs).
        src = Project(fast_config(project={"source_roots": ["src"]}), install=True, name="source")
        try:
            manifest = json.loads(src.read("scripts/test_manifest.json"))
            manifest["suites"].append({"id": "sourceonly", "kind": "command", "results": "exit",
                                       "command": ["true"], "groups": {"unit": {"tiers": ["t1"]}}})
            src.write("scripts/test_manifest.json", json.dumps(manifest))
            src.write("docs/decisions/ADR-000900-source-project-decision.md", "# ADR-000900\n")
            src.write("docs/decisions/decisions.txt", src.read("docs/decisions/decisions.txt")
                      + "ADR-000900 | ACCEPTED | x | ADR-000900-source-project-decision.md | x\n")
            target = src.base / "target"
            out = subprocess.run([sys.executable, str(src.root / "scripts/devsys.py"), "install", "--target",
                                  str(target), "--project-name", "fresh"], capture_output=True, text=True,
                                 env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
            cfg = json.loads((target / "devsys.json").read_text())
            self.assertEqual(cfg["project"], {"name": "fresh", "source_roots": []})
            self.assertNotIn("claims", cfg)
            self.assertNotIn("sourceonly", (target / "scripts/test_manifest.json").read_text())
            self.assertNotIn("REQ-DEMO", (target / "docs/requirements/requirements.txt").read_text())
            self.assertFalse((target / "docs/decisions/ADR-000900-source-project-decision.md").exists())
            self.assertNotIn("ADR-000900", (target / "docs/decisions/decisions.txt").read_text())
            self.assertTrue((target / "docs/decisions/ADR-000199-framework-configuration-and-lifecycle-hardening.md")
                            .exists())
        finally:
            src.close()


# ---------------------------------------------------------------- build adapters
class Build(Base):
    def build(self, action="build"):
        cp = self.p.run("devsys_build.py", action, "--json")
        try:
            return cp.returncode, json.loads(cp.stdout)
        except ValueError:
            return cp.returncode, {"stdout": cp.stdout, "stderr": cp.stderr}

    def test_none_is_not_configured(self):
        cp = self.p.run("devsys_build.py", "build")
        self.assertEqual(cp.returncode, 10)
        self.assertIn("NOT_CONFIGURED", cp.stdout)

    @unittest.skipUnless(shutil.which("make"), "make not installed")
    def test_make_success_and_link_failure(self):
        self.config_set(build={"system": "make"})
        self.p.write("Makefile", "all:\n\tmkdir -p build/debug && echo built > build/debug/out.txt\n"
                                 "clean:\n\trm -f build/debug/out.txt\n")
        rc, rec = self.build()
        self.assertEqual((rc, rec["status"]), (0, "PASS"), rec)
        self.assertEqual(self.p.read("build/debug/out.txt"), "built\n")
        self.assertTrue((self.p.root / "build/debug/build-results/latest.json").exists())
        self.p.write("Makefile", "all:\n\t@echo 'main.o: undefined reference to `foo`' >&2; exit 1\n")
        rc, rec = self.build()
        self.assertEqual((rc, rec["status"], rec["failure_class"]), (1, "FAIL", "LINK"), rec)

    def test_custom_build_timeout_and_missing_tool(self):
        self.config_set(build={"system": "custom", "build": [sys.executable, "-c", "print('ok {build_dir}')"]})
        rc, rec = self.build()
        self.assertEqual((rc, rec["status"]), (0, "PASS"), rec)
        self.config_set(build={"system": "custom", "timeout_seconds": 1,
                               "build": [sys.executable, "-c", "import time; time.sleep(30)"]})
        rc, rec = self.build()
        self.assertEqual((rc, rec["failure_class"]), (1, "TIMEOUT"), rec)
        self.config_set(build={"system": "custom", "build": ["no-such-build-tool-xyz"]})
        cp = self.p.run("devsys_build.py", "check")
        self.assertEqual(cp.returncode, 1)
        self.assertIn("no-such-build-tool-xyz not found", cp.stdout)
        rc, rec = self.build()
        self.assertEqual(rec["failure_class"], "TOOL", rec)

    @unittest.skipUnless(shutil.which("cmake") and (shutil.which("cc") or shutil.which("gcc")), "cmake / cc missing")
    def test_cmake_configures_then_builds(self):
        self.config_set(build={"system": "cmake", "build_dir": "build/cmake"})
        self.p.write("CMakeLists.txt", "cmake_minimum_required(VERSION 3.10)\nproject(demo C)\n"
                                       "add_executable(demo main.c)\n")
        self.p.write("main.c", "int main(void) { return 0; }\n")
        rc, rec = self.build()
        self.assertEqual((rc, rec["status"]), (0, "PASS"), rec)
        self.assertEqual([s["action"] for s in rec["steps"]], ["configure", "build"])
        self.assertTrue((self.p.root / "build/cmake/CMakeCache.txt").exists())
        rc, rec = self.build()
        self.assertEqual([s["action"] for s in rec["steps"]], ["build"], "configured once")


# ---------------------------------------------------------------- invariants
class Invariants(Base):
    def check(self):
        cp = self.p.run("invariant_check.py", "--json")
        return cp.returncode, json.loads(cp.stdout)

    def test_no_rules_is_not_configured(self):
        cp = self.p.run("invariant_check.py")
        self.assertIn("NOT CONFIGURED", cp.stdout)
        self.assertEqual(cp.returncode, 0)

    def test_rule_kinds(self):
        self.p.write("src/core/engine.c", "#include <stdio.h>\n#include \"app/ui.h\"\n")
        self.p.write("src/app/ui.c", "#include \"core/engine.h\"\n")
        self.config_set(invariants=[
            {"id": "core-no-io", "kind": "forbid_pattern", "files": ["src/core/**"], "pattern": "#include\\s*<stdio"},
            {"id": "app-registers", "kind": "require_pattern", "files": ["src/app/*.c"], "pattern": "register_"},
            {"id": "core-not-app", "kind": "forbid_dependency", "from": ["src/core/**"], "to": ["app/"]},
            {"id": "layering", "kind": "layers", "layers": [
                {"name": "app", "files": ["src/app/**"], "names": ["app/"]},
                {"name": "core", "files": ["src/core/**"], "names": ["core/"]}]},
            {"id": "advisory", "kind": "forbid_pattern", "files": ["src/app/**"], "pattern": "core/",
             "severity": "warn"}])
        rc, out = self.check()
        self.assertEqual(rc, 1)
        failed = {f.split()[1] for f in out["fails"]}
        self.assertEqual(failed, {"core-no-io", "app-registers", "core-not-app", "layering"}, out)
        self.assertEqual([w.split()[1] for w in out["warns"]], ["advisory"])
        self.p.write("src/core/engine.c", "int engine;\n")
        self.p.write("src/app/ui.c", "#include \"core/engine.h\"\nvoid register_ui(void);\n")
        rc, out = self.check()
        self.assertEqual((rc, out["fails"]), (0, []), out)


# ---------------------------------------------------------------- audits
class Audits(Base):
    def test_broken_link_fails_doc_integrity(self):
        self.p.write("docs/guide.md", "# Guide\n\nSee [the spec](missing/spec.md).\n")
        cp = self.p.run("doc_integrity.py", "--json")
        self.assertEqual(cp.returncode, 1)
        self.assertIn("BROKEN_LINK", cp.stdout)
        self.config_set(doc_integrity={"fail_codes": []})
        self.assertEqual(self.p.run("doc_integrity.py").returncode, 0, "fail codes are configurable")

    def drift(self):
        cp = self.p.run("drift_detect.py", "--json")
        return cp.returncode, {(f["level"], f["code"]) for f in json.loads(cp.stdout)}

    def test_completed_request_without_resolution_is_unverified(self):
        self.p.write("requests/completed/2026-01-01-by-hand-req-chat-20260101-001.md",
                     "# Request: by hand\n\n**Request ID:** REQ-CHAT-20260101-001\n**Status:** completed\n")
        self.p.write("requests/completed/2026-01-01-by-hand-req-chat-20260101-001.json",
                     json.dumps({"request_id": "REQ-CHAT-20260101-001", "status": "completed"}))
        rc, found = self.drift()
        self.assertIn(("FAIL", "UNVERIFIED_COMPLETION"), found)
        self.config_set(drift={"unverified_completion": "WARN"})
        rc, found = self.drift()
        self.assertIn(("WARN", "UNVERIFIED_COMPLETION"), found)

    def test_duplicate_ids_lone_sidecars_and_claim_mismatch(self):
        name = self.p.request("Add a drift option to the demo")
        rid = json.loads(self.p.read(f"requests/inbox/{Path(name).with_suffix('.json')}"))["request_id"]
        self.p.write(f"requests/rejected/2026-01-01-copy-{rid.lower()}.md", f"# copy\n\n**Request ID:** {rid}\n")
        js = self.p.root / "requests/inbox" / Path(name).with_suffix(".json")
        js.rename(self.p.root / "requests/processing" / js.name)
        rc, found = self.drift()
        self.assertIn(("FAIL", "DUPLICATE_REQUEST_ID"), found)
        self.assertIn(("WARN", "LONE_SIDECAR"), found)

    def test_claim_state_mismatch(self):
        name = self.p.request("Add a mismatch option to the demo")
        self.p.claim(name, A)
        self.p.edit_claim(name, lambda d: d.update(state="completed"))
        rc, found = self.drift()
        self.assertTrue(any(code == "CLAIM_STATE_MISMATCH" for _, code in found), found)


class Health(Base):
    install = True

    def test_structure_check_that_cannot_finish_is_unknown(self):
        self.config_set(health={"structure_timeout_seconds": 0.001})
        cp = self.p.run("health_check.py", "--json")
        out = json.loads(cp.stdout)
        levels = {c["name"]: c["level"] for c in out["checks"]}
        self.assertEqual(levels["verify_development_system"], "UNKNOWN", out)
        self.assertEqual(out["overall"], "WARN", "a check that could not finish is never PASS")


# ---------------------------------------------------------------- test orchestrator
class Orchestrator(Base):
    def tier(self, *args):
        return self.p.run("run_tests.py", *args)

    def test_nothing_to_run_is_empty_not_pass(self):
        self.p.suites([{"id": "u", "kind": "command", "results": "exit", "command": ["python3", "-c", "pass"],
                        "groups": {"unit": {"tiers": ["t1"]}}}])
        cp = self.tier("--tier", "t0", "--all")
        self.assertEqual(cp.returncode, 4, cp.stdout + cp.stderr)
        self.assertIn("EMPTY", cp.stdout + cp.stderr)

    def test_tap_and_junit_cases_are_counted_and_failures_seen(self):
        tap = "print('ok 1 - add'); print('not ok 2 - div'); print('ok 3 - skip me # SKIP no env'); print('1..3')"
        junit = ("import os; open(os.environ['PROJECT_TEST_JUNIT'], 'w').write("
                 "'<testsuite><testcase classname=\"calc\" name=\"add\" time=\"0.1\"/>"
                 "<testcase classname=\"calc\" name=\"mul\"><failure>x</failure></testcase></testsuite>')")
        self.p.suites([
            {"id": "tap", "kind": "command", "results": "tap", "command": ["python3", "-c", tap],
             "groups": {"unit": {"tiers": ["t0"]}}},
            {"id": "junit", "kind": "command", "results": "junit", "command": ["python3", "-c", junit],
             "groups": {"unit": {"tiers": ["t0"]}}}])
        cp = self.tier("--tier", "t0", "--all")
        self.assertEqual(cp.returncode, 1, "a failed case fails the run even when the process exits 0")
        record = json.loads((self.p.root / "build/debug/test-results/latest.json").read_text())
        cases = {j["label"]: j["cases"] for j in record["jobs_detail"]}
        self.assertEqual([c.split()[0] for c in cases["tap/unit"]], ["PASS", "FAIL", "SKIP"])
        self.assertEqual([c.split()[0] + " " + c.split()[2] for c in cases["junit/unit"]],
                         ["PASS calc/add", "FAIL calc/mul"])
        # B5: the log of a failed job is kept beside the run record.
        for j in record["jobs_detail"]:
            self.assertTrue(Path(j["log"]).is_file(), j)
            self.assertIn(record["run_id"], j["log"])

    def test_interrupted_run_is_never_pass(self):
        self.p.suites([{"id": "slow", "kind": "command", "results": "exit",
                        "command": ["python3", "-c", "import time; time.sleep(60)"],
                        "groups": {"unit": {"tiers": ["t0"]}}}])
        proc = subprocess.Popen([sys.executable, str(self.p.scripts / "run_tests.py"), "--tier", "t0", "--all"],
                                cwd=str(self.p.root), env=self.p.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True)
        time.sleep(2)
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 130, out + err)
        record = json.loads((self.p.root / "build/debug/test-results/latest.json").read_text())
        self.assertEqual(record["classification"], "INTERRUPTED")

    def test_configured_build_tree_is_found_by_gate_and_evidence(self):
        # B4: run records outside build/* are found through paths.build_dir.
        self.config_set(paths={"build_dir": "out/dbg"})
        self.p.suites([{"id": "u", "kind": "command", "results": "exit", "command": ["python3", "-c", "pass"],
                        "inputs": ["src/**"], "groups": {"unit": {"tiers": ["t0"]}}}])
        self.p.commit_all("config")
        name = self.p.request("Add an output option to the demo")
        path = self.p.claim(name, A)
        work = self.p.work(name, A)
        self.p.ok("claim.py", "--own", path, "--paths", "src/app.txt", agent=A)
        self.p.write("src/app.txt", "hello out\n")
        run = self.p.run_id(self.p.ok("run_tests.py", "--tier", "t0", "--request", path, agent=A))
        self.assertTrue((self.p.root / f"out/dbg/test-results/runs/{run}.json").is_file())
        self.p.evidence(work, A, [run])
        self.assertEqual(self.p.gate(work, A)[1]["verdict"], "PASS")

    def test_absolute_build_and_dump_directories_outside_the_repository(self):
        # B11: an absolute paths.build_dir crashed run_tests at argument parsing.
        outside = self.p.base / "outside"
        self.config_set(paths={"build_dir": str(outside / "build"), "crash_dumps": str(outside / "dumps")})
        self.p.suites([{"id": "u", "kind": "command", "results": "exit", "command": ["python3", "-c", "pass"],
                        "groups": {"unit": {"tiers": ["t0"]}}}])
        cp = self.tier("--tier", "t0", "--all")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertTrue(list((outside / "build/test-results/runs").glob("*.json")))
        cp = self.p.run("build_health.py")
        self.assertIn(str(outside / "dumps"), cp.stdout + cp.stderr)

    def test_suite_sources_select_affected_suites(self):
        self.p.suites([
            {"id": "calc", "kind": "command", "results": "exit", "command": ["python3", "-c", "pass"],
             "sources": ["src/**"], "groups": {"unit": {"tiers": ["t1"]}}},
            {"id": "docs", "kind": "command", "results": "exit", "command": ["python3", "-c", "pass"],
             "inputs": ["docs/**"], "groups": {"unit": {"tiers": ["t1"]}}}])
        self.config_set(tests={"selection": "globs"})
        self.p.commit_all("suites")
        self.p.write("src/app.txt", "changed\n")
        plan = self.p.ok("run_tests.py", "--plan", "--tier", "t1")
        self.assertIn("calc/unit", plan)
        self.assertNotIn("docs/unit", plan)

    def test_globs_mode_binary_suite_needs_sources_not_a_qmake_project(self):
        # B3: the manifest consistency check is selection-mode aware.
        self.p.suites([{"id": "bin", "kind": "binary", "results": "tap", "binary": "tests/t",
                        "sources": ["src/**"], "groups": {"unit": {"tiers": ["t1"]}}}])
        self.config_set(tests={"selection": "globs"})
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import test_selection as s, run_tests as r, pathlib;"
                "root = pathlib.Path(sys.argv[2]); m = r.load_manifest(root / 'scripts/test_manifest.json', root);"
                "print(s.selection_mode(root), [x.get('project') for x in m['suites']])")
        out = subprocess.run([sys.executable, "-c", code, str(SCRIPTS), str(self.p.root)], env=self.p.env,
                             capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), "globs [None]", out.stderr)


class Preflight(Base):
    def test_intake_boilerplate_is_not_a_requirement_reference(self):
        # B8: every request's Classification section cites the intake's own
        # requirement; that is not the request referencing a requirement.
        self.p.write("docs/requirements/requirements.txt",
                     self.p.read("docs/requirements/requirements.txt") + "x | x.md | REQ-DEVOPS-045 | x\n")
        name = self.p.request("Add a greeting colour to the demo")
        path = self.p.claim(name, A)
        self.assertIn("REQ-DEVOPS-045", self.p.read(path))
        out = self.p.run("preflight.py", "--request", path, "--agent-id", A, "--json")
        self.assertNotIn("references existing requirement", out.stdout)

    def test_foreign_claim_fails_preflight(self):
        name = self.p.request("Add a greeting size to the demo")
        path = self.p.claim(name, A)
        cp = self.p.run("preflight.py", "--request", path, "--agent-id", "agent-other")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("not by agent-other", cp.stdout)


if __name__ == "__main__":
    unittest.main()


class WindowsBranches(unittest.TestCase):
    """The Windows code paths (docs/requirements/portability.md PORT-001..006),
    exercised on this host with the Win32 calls replaced: this checks their
    logic, not Windows itself."""

    def setUp(self):
        sys.path.insert(0, str(SCRIPTS))
        import devsys_platform as plat
        self.plat = plat
        self.saved = (plat.WINDOWS, getattr(plat, "_win_process"), plat.signal_group)
        plat.WINDOWS = True

    def tearDown(self):
        self.plat.WINDOWS, self.plat._win_process, self.plat.signal_group = self.saved

    def test_liveness_uses_process_existence_and_creation_time(self):
        plat = self.plat
        table = {100: (True, "T1"), 200: (False, None), 300: (None, None), 400: (True, None)}
        plat._win_process = lambda pid: table[int(pid)]
        self.assertEqual([plat.pid_alive(p) for p in (100, 200, 300, 400)], [True, False, None, True])
        self.assertEqual((plat.process_start(100), plat.process_start(400)), ("T1", None))
        ident = {"host": plat.hostname(), "boot_id": None, "pid": 100, "start": "T1"}
        self.assertEqual(plat.verify_session(ident)[0], plat.ALIVE)
        table[100] = (True, "T2")                              # the pid was reused by a later process
        self.assertEqual(plat.verify_session(ident)[0], plat.DEAD)
        self.assertEqual(plat.verify_session({**ident, "pid": 200})[0], plat.DEAD)
        self.assertEqual(plat.verify_session({**ident, "pid": 300})[0], plat.UNKNOWN)

        def broken(pid):
            raise OSError("no kernel32")
        plat._win_process = broken                             # an API failure is unknown, never an exception
        self.assertEqual((plat.pid_alive(100), plat.process_start(100)), (None, None))

    def test_kill_tree_asks_first_and_then_forces_the_whole_tree(self):
        plat = self.plat
        calls, alive = [], {"v": True}

        def group(pid, hard=False):
            calls.append((pid, hard))
            if hard:
                alive["v"] = False
            return True
        plat.signal_group = group
        plat._win_process = lambda pid: (alive["v"], "T")
        plat.kill_tree(4242, grace_seconds=0.3)
        self.assertEqual(calls, [(4242, False), (4242, True)])

    def test_an_agent_command_is_resolved_to_what_the_shell_would_run(self):
        import shutil
        from workers import agent
        real = shutil.which
        shutil.which = lambda name: {"agentcli": r"C:\\tools\\agentcli.CMD"}.get(name)
        try:
            self.assertEqual(agent.resolve_executable(["agentcli", "run"]), [r"C:\\tools\\agentcli.CMD", "run"])
            self.assertEqual(agent.resolve_executable(["missing", "x"]), ["missing", "x"])
            self.plat.WINDOWS = False
            self.assertEqual(agent.resolve_executable(["agentcli", "run"]), ["agentcli", "run"])
        finally:
            shutil.which = real


class NoShellNeeded(unittest.TestCase):
    """PORT-002: nothing the system runs itself needs bash or a python3 command."""

    def test_registered_commands_use_the_running_interpreter_and_no_shell(self):
        for rel in ("scripts/test_manifest.json", "scripts/templates/test_manifest.json", ".audit/manifest.json",
                    "scripts/templates/audit_manifest.json"):
            text = (SCRIPTS.parent / rel).read_text(encoding="utf-8")
            # (the package lane's placeholders are the project's own scripts)
            self.assertNotIn("verify_development_system.sh", text, rel)
        self.assertNotIn('"bash"', (SCRIPTS / "health_check.py").read_text(encoding="utf-8"))

    def test_python3_in_a_suite_command_means_this_interpreter(self):
        p = Project(fast_config())
        try:
            p.suites([{"id": "demo", "kind": "command", "results": "exit",
                       "command": ["python3", "-c", "import sys; print(sys.executable)"],
                       "groups": {"only": {"tiers": ["t0"]}}}])
            only_git = p.base / "only-git"                      # a PATH with git and no python3
            only_git.mkdir()
            (only_git / "git").symlink_to(shutil.which("git"))
            env = {"PATH": str(only_git)}
            cp = p.run("run_tests.py", "--tier", "t0", "--suite", "demo", env=env)
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertIn("PASS", cp.stdout)
        finally:
            p.close()
