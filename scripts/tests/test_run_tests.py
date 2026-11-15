#!/usr/bin/env python3
"""Tiered, affected-test execution (REQ-DEVOPS-046, ADR-000186, WORK-000305,
REQ-CHAT-20260923-015): scripts/test_selection.py and scripts/run_tests.py.

Selection runs on a fixture repository (a git tree with qmake projects);
execution runs a fake test runner that honours PROJECT_TEST_SHARD /
PROJECT_TEST_RESULTS exactly like the C++ runners, so no build is needed.

Run: python3 -m unittest scripts.tests.test_run_tests -v
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
import unittest.mock
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


selection = load("test_selection")
run_tests = load("run_tests")


def write(root: Path, rel: str, text: str = "") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")


def fixture_repository(root: Path) -> None:
    """libio <- Engine (links libio through its .pri) <- Engine_Tests;
    Core (includes Engine) with a nested Core/tests project; App compiles
    ../Core sources directly; a nested subdirs project Suite/{Lib,Lib_Tests}."""
    write(root, "project.pro", """
        TEMPLATE = subdirs
        SUBDIRS = libio Engine Core Core_Tests App Engine_Tests Suite
        libio.file = libio/libio.pro
        Engine.file = Engine/Engine.pro
        Engine.depends = libio
        Core.file = Core/Core.pro
        Core.depends = Engine
        Core_Tests.file = Core/tests/Core_Tests.pro
        Core_Tests.depends = Core
        App.file = App/App.pro
        App.depends = Core
        Engine_Tests.file = Engine_Tests/Engine_Tests.pro
        Engine_Tests.depends = Engine
        Suite.file = Suite/Suite.pro
    """)
    write(root, "warnings.pri", "QMAKE_CXXFLAGS += -Wall\n")
    write(root, "libio/libio.pro", "include($$PWD/../warnings.pri)\nSOURCES += io.cpp\n")
    write(root, "libio/libio.pri", "LIBS += -lio\nPRE_TARGETDEPS += $$OUT_PWD/../libio/libio.a\n")
    write(root, "libio/io.cpp")
    write(root, "Engine/Engine.pro", "include(../libio/libio.pri)\nSOURCES += dsp/fft.cpp\n")
    write(root, "Engine/engine.pri", "include($$PWD/../libio/libio.pri)\nINCLUDEPATH += $$PWD/dsp\n")
    write(root, "Engine/dsp/fft.cpp")
    write(root, "Core/Core.pro", "include(../Engine/engine.pri)\nSOURCES += src/service.cpp\n")
    write(root, "Core/Core.pri", "LIBS += -lCore\nPRE_TARGETDEPS += $$OUT_PWD/../Core/libCore.a\n")
    write(root, "Core/src/service.cpp")
    write(root, "Core/tests/Core_Tests.pro", "include(../Core.pri)\nSOURCES += test_service.cpp\n")
    write(root, "Core/tests/test_service.cpp")
    write(root, "App/App.pro", "include(../Core/Core.pri)\nSOURCES += main.cpp ../Engine/dsp/fft.cpp\n")
    write(root, "App/main.cpp")
    write(root, "Engine_Tests/Engine_Tests.pro", "include(../Engine/engine.pri)\nSOURCES += main.cpp\n"
                                                "INCLUDEPATH += ../Suite/Lib\n")
    write(root, "Suite/Lib/lib.h")
    write(root, "Engine_Tests/main.cpp")
    write(root, "Engine_Tests/fixtures/a.bin", "x")
    write(root, "Suite/Suite.pro", "TEMPLATE = subdirs\nSUBDIRS = Lib Lib_Tests\nLib_Tests.depends = Lib\n")
    write(root, "Suite/Lib/Lib.pro", "SOURCES += lib.cpp\n")
    write(root, "Suite/Lib/lib.cpp")
    write(root, "Suite/Lib_Tests/Lib_Tests.pro", "SOURCES += t.cpp\n")
    write(root, "Suite/Lib_Tests/t.cpp")
    write(root, "docs/user/page.md", "# page\n")
    write(root, "tools/thing.py", "print(1)\n")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)


class SelectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        fixture_repository(cls.root)
        cls.graph = selection.ProjectGraph(cls.root)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def affected(self, *paths):
        return {Path(p).stem for p in self.graph.affected_projects(list(paths))[0]}

    def test_every_subdirs_project_is_found_including_nested_ones(self):
        self.assertEqual({Path(p).stem for p in self.graph.leaf},
                         {"libio", "Engine", "Core", "Core_Tests", "App", "Engine_Tests", "Lib", "Lib_Tests"})

    def test_a_library_change_reaches_every_project_that_includes_or_depends_on_it(self):
        self.assertEqual(self.affected("libio/io.cpp"),
                         {"libio", "Engine", "Core", "Core_Tests", "App", "Engine_Tests"})

    def test_a_leaf_change_selects_only_its_project(self):
        self.assertEqual(self.affected("Engine_Tests/main.cpp"), {"Engine_Tests"})
        self.assertEqual(self.affected("Suite/Lib_Tests/t.cpp"), {"Lib_Tests"})

    def test_a_nested_project_is_not_owned_by_the_projects_that_link_its_parent(self):
        # Core/tests lives inside Core/, which App links through Core.pri.
        self.assertEqual(self.affected("Core/tests/test_service.cpp"), {"Core_Tests"})
        self.assertIn("App", self.affected("Core/src/service.cpp"))

    def test_sources_compiled_from_another_directory_are_inputs(self):
        self.assertIn("App", self.affected("Engine/dsp/fft.cpp"))

    def test_an_include_path_contributes_its_headers_only(self):
        self.assertIn("Engine_Tests", self.affected("Suite/Lib/lib.h"))
        self.assertNotIn("Engine_Tests", self.affected("Suite/Lib/lib.cpp"))

    def test_nested_subdirs_dependencies_propagate(self):
        self.assertEqual(self.affected("Suite/Lib/lib.cpp"), {"Lib", "Lib_Tests"})

    def test_a_global_build_file_affects_every_project(self):
        self.assertEqual(self.affected("warnings.pri"), {Path(p).stem for p in self.graph.leaf})

    def test_suites_are_selected_by_project_uses_and_inputs_and_unmapped_paths_reported(self):
        suites = [{"id": "engine", "project": "Engine_Tests/Engine_Tests.pro", "inputs": ["Engine_Tests/fixtures/**"]},
                  {"id": "app", "project": "App/App.pro"},
                  {"id": "cli", "project": "Suite/Lib_Tests/Lib_Tests.pro", "uses": ["Core/Core.pro"]},
                  {"id": "tools", "inputs": ["tools/**"]}]
        chosen, unmapped = selection.select_suites(self.graph, suites, ["Engine_Tests/fixtures/a.bin"])
        self.assertEqual(set(chosen), {"engine"})
        self.assertEqual(unmapped, [])
        chosen, _ = selection.select_suites(self.graph, suites, ["Core/src/service.cpp"])
        self.assertEqual(set(chosen), {"app", "cli"})
        chosen, unmapped = selection.select_suites(self.graph, suites, ["docs/user/page.md", "README.md"])
        self.assertEqual(chosen, {})
        self.assertEqual(unmapped, [])
        chosen, unmapped = selection.select_suites(self.graph, suites, ["somewhere/new.cfg"])
        self.assertEqual(unmapped, ["somewhere/new.cfg"])

    def test_project_files_list_the_inputs_used_for_build_freshness(self):
        files = self.graph.project_files("App/App.pro")
        self.assertIn("App/main.cpp", files)
        self.assertIn("Core/src/service.cpp", files)
        self.assertNotIn("Core/tests/test_service.cpp", files)


FAKE_RUNNER = """\
#!/usr/bin/env python3
# A stand-in for a C++ test runner: PROJECT_TEST_SHARD / PROJECT_TEST_RESULTS,
# PASS/FAIL lines, exit status. Behaviour from FAKE_* variables.
import json, os, signal, sys, time
cases = [f"unit/case{i}" for i in range(int(os.environ.get("FAKE_CASES", "6")))]
filters = [sys.argv[i + 1] for i, a in enumerate(sys.argv) if a == "--filter"]
if filters:
    cases = [c for c in cases if any(f in c for f in filters)]
if os.environ.get("PROJECT_TEST_LIST") == "1":
    print("\\n".join("CASE " + c for c in cases))
    sys.exit(0)
shard = os.environ.get("PROJECT_TEST_SHARD")
if os.environ.get("PROJECT_TEST_CASES"):
    wanted = set(open(os.environ["PROJECT_TEST_CASES"]).read().split())
    cases = [c for c in cases if c in wanted]
    shard = shard or "parallel"
elif shard:
    index, count = map(int, shard.split("/"))
    cases = [c for i, c in enumerate(cases) if i % count == index]
fail = set(os.environ.get("FAKE_FAIL", "").split(",")) - {""}
flaky = set(os.environ.get("FAKE_FLAKY", "").split(",")) - {""}
crash = set(os.environ.get("FAKE_CRASH", "").split(",")) - {""}
hang = set(os.environ.get("FAKE_HANG", "").split(",")) - {""}
watchdog = set(os.environ.get("FAKE_WATCHDOG", "").split(",")) - {""}
out = open(os.environ["PROJECT_TEST_RESULTS"], "a") if os.environ.get("PROJECT_TEST_RESULTS") else None
time.sleep(float(os.environ.get("FAKE_SLEEP", "0")))
if os.environ.get("FAKE_REPLACE"):
    os.utime(os.environ["FAKE_REPLACE"], (1, 1))
failures = 0
for case in cases:
    if case in crash and (shard is not None or not os.environ.get("FAKE_CRASH_UNDER_LOAD")):
        if os.environ.get("FAKE_CRASH_EXIT"):
            os._exit(int(os.environ["FAKE_CRASH_EXIT"]))   # as xvfb-run reports a signal
        os.kill(os.getpid(), signal.SIGSEGV)
    if case in hang:
        time.sleep(60)
    if case in watchdog:   # the runners' watchdog: a TIMEOUT record, exit 124
        g, n = case.split("/", 1)
        out.write(json.dumps({"group": g, "name": n, "status": "TIMEOUT", "seconds": 1, "checks": 0,
                              "failures": 1}) + "\\n")
        out.flush()
        os._exit(124)
    bad = case in fail or (case in flaky and shard is not None)
    failures += bad
    print(("FAIL " if bad else "PASS ") + case, flush=True)
    if out:
        g, n = case.split("/", 1)
        out.write(json.dumps({"group": g, "name": n, "status": "FAIL" if bad else "PASS",
                              "seconds": 0.01, "checks": 1, "failures": int(bad)}) + "\\n")
        out.flush()   # per line, as the project's C++ test framework does
if out:
    out.write(json.dumps({"summary": True, "selected": len(cases)}) + "\\n")
sys.exit(1 if failures else 0)
"""


class ExecutionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.runner = self.dir / "fake_runner"
        self.runner.write_text(FAKE_RUNNER, encoding="utf-8")
        self.runner.chmod(0o755)

    def tearDown(self):
        self.tmp.cleanup()

    def job(self, shard=0, shards=1, env=None, timeout=30.0, results="json"):
        suite = {"id": "fake", "kind": "binary", "results": results}
        return run_tests.Job(suite=suite, group_name="unit", group={}, shard=shard, shards=shards,
                             command=[str(self.runner)], cwd=self.dir, env={**os.environ, **(env or {})},
                             timeout=timeout, exclusive=False, watch=[self.runner], expected=1.0, cache_key=None)

    def test_shards_partition_the_cases_and_results_are_parsed(self):
        names = []
        for shard in range(3):
            job = self.job(shard, 3)
            run_tests.execute(job, self.dir / "run")
            self.assertEqual(job.status, "PASS", job.detail)
            names += [c["name"] for c in job.cases]
        self.assertEqual(sorted(names), sorted(f"unit/case{i}" for i in range(6)))

    def test_every_process_gets_private_profile_directories(self):
        job = self.job()
        env = run_tests.isolated_env(job, self.dir / "w")
        for var in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME",
                    "XDG_RUNTIME_DIR", "TMPDIR"):
            self.assertTrue(env[var].startswith(str(self.dir / "w")), var)
        self.assertEqual(oct((self.dir / "w" / "runtime").stat().st_mode & 0o777), "0o700")

    def test_a_failing_case_fails_the_job_and_is_named(self):
        job = self.job(env={"FAKE_FAIL": "unit/case2"})
        run_tests.execute(job, self.dir / "run")
        self.assertEqual(job.status, "FAIL")
        self.assertIn("unit/case2", job.detail)

    def test_stdout_results_are_parsed_without_a_results_file(self):
        job = self.job(results="stdout", env={"FAKE_FAIL": "unit/case1"})
        run_tests.execute(job, self.dir / "run")
        self.assertEqual(job.status, "FAIL")
        self.assertEqual(len(job.cases), 6)

    def test_a_hung_job_times_out_and_its_process_group_is_killed(self):
        job = self.job(env={"FAKE_SLEEP": "30"}, timeout=1.0)
        run_tests.execute(job, self.dir / "run")
        self.assertEqual(job.status, "TIMEOUT")
        self.assertLess(job.seconds, 20)

    def test_a_binary_replaced_during_the_run_makes_the_result_environmental(self):
        job = self.job(env={"FAKE_REPLACE": str(self.runner)})
        run_tests.execute(job, self.dir / "run")
        self.assertEqual(job.status, "ENVIRONMENTAL")

    def test_a_case_failing_only_under_parallel_load_is_flaky_not_passed(self):
        job = self.job(0, 2, env={"FAKE_FLAKY": "unit/case0"})
        run_tests.execute(job, self.dir / "run")
        self.assertEqual(job.status, "FAIL")
        run_tests.retry_isolated(job, self.dir / "run")
        self.assertEqual(job.status, "FLAKY")
        self.assertEqual(job.flaky, ["unit/case0"])
        self.assertEqual(run_tests.classify([job]), "PASS (FLAKY cases listed)")

    def test_a_real_failure_stays_a_failure_after_the_isolated_retry(self):
        job = self.job(0, 2, env={"FAKE_FAIL": "unit/case0"})
        run_tests.execute(job, self.dir / "run")
        run_tests.retry_isolated(job, self.dir / "run")
        self.assertEqual(job.status, "FAIL")
        self.assertEqual(run_tests.classify([job]), "FAIL")

    def crash_job(self, crash, **env):
        job = self.job(env={"FAKE_CRASH": crash, **env})
        job.case_subset = [f"unit/case{i}" for i in range(6)]
        return job

    def test_a_crash_is_attributed_to_its_case_and_the_rest_of_the_shard_still_runs(self):
        job = self.crash_job("unit/case2")
        run_tests.execute(job, self.dir / "run")
        self.assertEqual(job.status, "CRASH")
        run_tests.continue_after_abort(job, self.dir / "run")
        statuses = {c["name"]: c["status"] for c in job.cases}
        self.assertEqual(statuses, {"unit/case0": "PASS", "unit/case1": "PASS", "unit/case2": "CRASH",
                                    "unit/case3": "PASS", "unit/case4": "PASS", "unit/case5": "PASS"})
        self.assertIn("in unit/case2", job.detail)
        self.assertNotIn("not run", job.detail)
        self.assertEqual(run_tests.summarise([job])[0]["failed"], ["unit/case2"])

    def test_every_crash_of_a_shard_is_named(self):
        job = self.crash_job("unit/case1,unit/case4")
        run_tests.execute(job, self.dir / "run")
        run_tests.continue_after_abort(job, self.dir / "run")
        self.assertEqual([c["name"] for c in job.cases if c["status"] == "CRASH"], ["unit/case1", "unit/case4"])
        self.assertEqual(len(job.cases), 6)

    def test_a_signal_reported_by_a_wrapper_is_a_crash(self):
        job = self.crash_job("unit/case3", FAKE_CRASH_EXIT="139")
        run_tests.execute(job, self.dir / "run")
        self.assertEqual(job.status, "CRASH")
        self.assertIn("signal 11", job.detail)

    def test_a_crash_that_passes_alone_is_never_downgraded_to_flaky(self):
        job = self.crash_job("unit/case0", FAKE_CRASH_UNDER_LOAD="1")
        run_tests.execute(job, self.dir / "run")
        run_tests.continue_after_abort(job, self.dir / "run")
        run_tests.retry_isolated(job, self.dir / "run")
        self.assertEqual(job.status, "CRASH")
        self.assertIn("intermittent crash", job.detail)
        self.assertEqual(job.flaky, [])
        self.assertEqual(run_tests.classify([job]), "FAIL")

    def test_a_hang_is_attributed_to_its_case_and_the_rest_of_the_shard_still_runs(self):
        job = self.crash_job("", FAKE_HANG="unit/case1")
        job.timeout = 2.0
        run_tests.execute(job, self.dir / "run")
        self.assertEqual(job.status, "TIMEOUT")
        run_tests.continue_after_abort(job, self.dir / "run")
        statuses = {c["name"]: c["status"] for c in job.cases}
        self.assertEqual(statuses["unit/case1"], "TIMEOUT")
        self.assertEqual(sorted(n for n, st in statuses.items() if st == "PASS"),
                         ["unit/case0", "unit/case2", "unit/case3", "unit/case4", "unit/case5"])
        self.assertIn("in unit/case1", job.detail)

    def test_a_watchdog_timeout_is_not_blamed_on_the_next_case(self):
        job = self.crash_job("", FAKE_WATCHDOG="unit/case2")
        run_tests.execute(job, self.dir / "run")
        self.assertEqual(job.status, "TIMEOUT")
        run_tests.continue_after_abort(job, self.dir / "run")
        statuses = {c["name"]: c["status"] for c in job.cases}
        self.assertEqual(statuses, {"unit/case0": "PASS", "unit/case1": "PASS", "unit/case2": "TIMEOUT",
                                    "unit/case3": "PASS", "unit/case4": "PASS", "unit/case5": "PASS"})

    def test_quarantine_marks_known_failures_expected_until_the_entry_expires(self):
        entry = {"suite": "fake", "case": "unit/case3", "reason": "r", "owner": "WORK-000000", "expires": "2999-01-01"}
        job = self.job(env={"FAKE_FAIL": "unit/case3"})
        run_tests.execute(job, self.dir / "run")
        warnings = []
        run_tests.apply_quarantine(job, [entry], "2026-09-23", warnings)
        self.assertEqual(job.status, "EXPECTED")
        job = self.job(env={"FAKE_FAIL": "unit/case3"})
        run_tests.execute(job, self.dir / "run")
        run_tests.apply_quarantine(job, [dict(entry, expires="2000-01-01")], "2026-09-23", warnings)
        self.assertEqual(job.status, "FAIL")
        self.assertTrue(warnings and "expired" in warnings[0])

    def test_quarantine_applies_inside_a_crashed_job_but_never_to_the_crash(self):
        entry = {"suite": "fake", "case": "unit/case4", "reason": "r", "owner": "WORK-000000", "expires": "2999-01-01"}
        job = self.crash_job("unit/case1", FAKE_FAIL="unit/case4")
        run_tests.execute(job, self.dir / "run")
        run_tests.continue_after_abort(job, self.dir / "run")
        run_tests.apply_quarantine(job, [entry, dict(entry, case="unit/case1")], "2026-09-23", [])
        self.assertEqual(job.status, "CRASH")
        self.assertEqual(job.expected_failures, ["unit/case4"])
        self.assertEqual(run_tests.summarise([job])[0]["failed"], ["unit/case1"])

    def test_quarantined_and_flaky_failures_together_are_expected(self):
        entry = {"suite": "fake", "case": "unit/case2", "reason": "r", "owner": "WORK-000000", "expires": "2999-01-01"}
        job = self.job(0, 2, env={"FAKE_FAIL": "unit/case2", "FAKE_FLAKY": "unit/case0"})
        run_tests.execute(job, self.dir / "run")
        run_tests.retry_isolated(job, self.dir / "run")
        self.assertEqual((job.status, job.flaky), ("FAIL", ["unit/case0"]))
        run_tests.apply_quarantine(job, [entry], "2026-09-23", [])
        self.assertEqual(job.status, "EXPECTED")
        self.assertIn("passed alone: unit/case0", job.detail)

    def test_the_run_record_names_every_case_and_its_status(self):
        job = self.job(env={"FAKE_FAIL": "unit/case3"})
        run_tests.execute(job, self.dir / "run")

        class Args:
            tier, request, jobs, allow_stale = "t1", None, 1, False

        info = {"scope": "all", "changes_from": "test", "changed": [], "unmapped": []}
        path = run_tests.write_record(self.dir / "build", "20260101T000000-1", Args(), info,
                                      run_tests.summarise([job]), [job], "FAIL", 1.0, None, [])
        cases = json.loads(path.read_text())["jobs_detail"][0]["cases"]
        self.assertEqual(len(cases), 6)
        self.assertIn("FAIL 0.010 unit/case3", cases)
        self.assertIn("PASS 0.010 unit/case0", cases)

    def test_classification_orders_failure_over_blocked_over_pass(self):
        a, b = self.job(), self.job()
        a.status, b.status = "PASS", "STALE"
        self.assertEqual(run_tests.classify([a, b]), "BLOCKED")
        b.status = "TIMEOUT"
        self.assertEqual(run_tests.classify([a, b]), "FAIL")
        b.status = "CACHED"
        self.assertEqual(run_tests.classify([a, b]), "PASS")

    def test_the_cache_merges_concurrent_saves(self):
        path = self.dir / "cache.json"
        first, second = run_tests.ResultCache(path), run_tests.ResultCache(path)
        first.put("a", {"status": "PASS"})
        second.put("b", {"status": "PASS"})
        first.save()
        second.save()
        self.assertEqual(set(json.loads(path.read_text())), {"a", "b"})


class Qt5MakefileTest(unittest.TestCase):
    """Every Qt 5-generated Makefile is named, nested ones included: the
    warning used to scan one level and print the first four, which hid
    one Makefile among 28 (VER-000317, ADR-000197)."""

    def test_every_qt5_makefile_up_to_three_levels_is_listed(self):
        (REPO / "tmp").mkdir(parents=True, exist_ok=True)   # was order-dependent: failed when run alone
        with tempfile.TemporaryDirectory(dir=str(REPO / "tmp")) as tmp:
            tree = Path(tmp)
            qt5 = "#" * 77 + "\n# Makefile for building: x\n# Generated by qmake (3.1) (Qt 5.15.13)\n"
            qt6 = "#" * 77 + "\n# Makefile for building: x\n# Generated by qmake (3.1) (Qt 6.4.2)\n"
            for rel in ("Makefile", "a/Makefile", "b/c/Makefile", "d/e/f/Makefile", "g/Makefile", "h/Makefile",
                        "i/Makefile"):
                write(tree, rel, qt5)
            write(tree, "app/Makefile", qt6)
            write(tree, "k/l/m/n/Makefile", qt5)                     # deeper than the scan
            self.assertEqual(run_tests.qt5_makefiles(tree),
                             ["Makefile", "a/Makefile", "b/c/Makefile", "d/e/f/Makefile", "g/Makefile",
                              "h/Makefile", "i/Makefile"])
            write(tree, "a/Makefile", qt6)
            self.assertNotIn("a/Makefile", run_tests.qt5_makefiles(tree))


class SchedulingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.runner = self.dir / "fake_runner"
        self.runner.write_text(FAKE_RUNNER, encoding="utf-8")
        self.runner.chmod(0o755)

    def tearDown(self):
        self.tmp.cleanup()

    def test_longest_cases_are_spread_first(self):
        known = {"a": 100.0, "b": 60.0, "c": 50.0, "d": 10.0, "e": 5.0}
        bins = run_tests.balance(list(known), known, 2, 1.0)
        loads = sorted(sum(known[c] for c in b) for b in bins)
        self.assertEqual(loads, [110.0, 115.0])
        self.assertEqual(sorted(c for b in bins for c in b), sorted(known))

    def plan(self, suite, group, durations=None):
        class Args:
            allow_stale = False
            no_cache = True
            max_shards = 0

        class Graph:
            projects = {}

        suite = dict({"id": "fake", "kind": "binary", "results": "json", "binary": "fake_runner",
                      "project": "none.pro"}, **suite)
        with unittest.mock.patch.dict(os.environ, {"FAKE_CASES": "8"}):
            return run_tests.make_jobs(suite, "unit", group, self.dir, Graph(), [],
                                       run_tests.ResultCache(self.dir / "c.json"), Args(), ["test"],
                                       durations or {})

    def test_shards_are_balanced_by_recorded_duration_and_cover_every_case(self):
        durations = {"fake": {"unit/case0": 50.0, "unit/case1": 1.0}}
        jobs = self.plan({}, {"args": [], "shards": 3, "expected_seconds": 60}, durations)
        self.assertEqual(len(jobs), 3)
        subsets = [j.case_subset for j in jobs]
        self.assertEqual(sorted(c for s in subsets for c in s), sorted(f"unit/case{i}" for i in range(8)))
        self.assertIn(["unit/case0"], subsets)          # the long case gets a shard of its own

    def test_load_sensitive_cases_run_alone_after_the_parallel_phase(self):
        jobs = self.plan({"load_sensitive": ["unit/case2", "unit/case5"]}, {"args": [], "shards": 2})
        serial = [j for j in jobs if j.exclusive]
        self.assertEqual(len(serial), 1)
        self.assertEqual(sorted(serial[0].case_subset), ["unit/case2", "unit/case5"])
        parallel = [c for j in jobs if not j.exclusive for c in j.case_subset]
        self.assertNotIn("unit/case2", parallel)
        self.assertEqual(len(parallel), 6)


    def test_a_group_can_take_or_leave_part_of_its_runners_cases(self):
        taken = self.plan({}, {"args": [], "cases": ["unit/case[12]"]})
        self.assertEqual([j.case_subset for j in taken], [["unit/case1", "unit/case2"]])
        left = self.plan({}, {"args": [], "shards": 2, "exclude_cases": ["unit/case[12]"]})
        cases = sorted(c for j in left for c in j.case_subset)
        self.assertEqual(cases, ["unit/case0", "unit/case3", "unit/case4", "unit/case5", "unit/case6", "unit/case7"])
        for job in left:
            self.assertEqual(job.case_subset, sorted(job.case_subset))   # registration order
        none = self.plan({}, {"args": [], "cases": ["other/*"]})
        self.assertEqual({j.status for j in none}, {"NO_TESTS"})


class StaleTest(unittest.TestCase):
    """Binaries against their inputs: sources by the binary's time, .pro / .pri
    by the Makefiles qmake generated from them."""

    def setUp(self):
        base = run_tests.ROOT / "tmp" / "run-tests-stale"
        base.mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=str(base))
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(run_tests._BUILD_TREES.clear)
        self.dir = Path(self._tmp.name)
        (self.dir / "src").mkdir()
        self.cpp, self.pri, self.other = (self.dir / "src/a.cpp", self.dir / "src/a.pri", self.dir / "src/b.pri")
        for path in (self.cpp, self.pri, self.other):
            path.write_text("x\n")
        self.build = self.dir / "build"
        (self.build / "a").mkdir(parents=True)
        self.binary = self.build / "a" / "a"
        self.binary.write_text("bin")
        rels = [str(p.relative_to(run_tests.ROOT)) for p in (self.cpp, self.pri, self.other)]

        class Graph:
            projects = {"a.pro": None}

            def project_files(self, pro):
                return rels

        self.graph = Graph()

    def at(self, path, seconds):
        os.utime(path, (seconds, seconds))

    def makefile(self, generated):
        makefile = self.build / "a" / "Makefile"
        makefile.write_text("SOURCES = ../../src/a.cpp\n\n.obj/a.o: ../../src/a.cpp \\\n\t\t../../src/a.h\n\n"
                            "Makefile: ../../src/a.pri \\\n\t\t/usr/share/qmake.conf\n\t$(QMAKE)\n")
        self.at(makefile, generated)

    def stale(self):
        run_tests._BUILD_TREES.clear()
        return run_tests.stale_reason(self.graph, self.binary, ["a.pro"], self.build)

    def test_a_project_file_the_makefile_was_regenerated_from_is_not_staleness(self):
        for path in (self.cpp, self.other):
            self.at(path, 1000)
        self.at(self.binary, 2000)
        self.at(self.pri, 3000)          # touched after the binary...
        self.makefile(4000)              # ...but qmake ran since and make found nothing to rebuild
        self.assertIsNone(self.stale())

    def test_a_project_file_changed_after_the_makefile_is_staleness(self):
        for path in (self.cpp, self.other):
            self.at(path, 1000)
        self.at(self.binary, 2000)
        self.makefile(2500)
        self.at(self.pri, 3000)
        self.assertIn("a.pri", self.stale() or "")

    def test_a_project_file_no_makefile_depends_on_is_ignored_but_sources_are_not(self):
        self.at(self.pri, 1000)
        self.makefile(1500)
        self.at(self.binary, 2000)
        self.at(self.other, 3000)        # in the graph, in no Makefile: cannot affect the binary
        self.at(self.cpp, 1000)
        self.assertIsNone(self.stale())
        self.at(self.cpp, 3000)
        self.assertIn("a.cpp", self.stale() or "")

    def test_a_header_the_build_does_not_include_cannot_make_a_binary_stale(self):
        for path in (self.cpp, self.pri, self.other):
            self.at(path, 1000)
        self.makefile(1500)
        self.at(self.binary, 2000)
        rels = self.graph.project_files("a.pro")
        unused = self.dir / "src/unused.h"          # new, in an include directory, included by nothing
        unused.write_text("x\n")
        used = self.dir / "src/a.h"                 # the header a.o depends on
        used.write_text("x\n")
        self.at(used, 1000)
        self.graph.project_files = lambda pro: rels + [str(p.relative_to(run_tests.ROOT)) for p in (unused, used)]
        self.assertIsNone(self.stale())
        self.at(used, 3000)
        self.assertIn("a.h", self.stale() or "")

    def test_without_makefiles_every_input_is_compared_with_the_binary(self):
        for path in (self.cpp, self.other):
            self.at(path, 1000)
        self.at(self.binary, 2000)
        self.at(self.pri, 3000)
        self.assertIn("a.pri", self.stale() or "")


class ManifestTest(unittest.TestCase):
    def test_the_repository_manifest_is_consistent(self):
        manifest = run_tests.load_manifest()
        tiers = manifest["tiers"]
        for name, tier in tiers.items():
            for included in tier.get("includes", []):
                self.assertIn(included, tiers, name)
        graph = selection.ProjectGraph(REPO)
        ids = set()
        for suite in manifest["suites"]:
            self.assertNotIn(suite["id"], ids)
            ids.add(suite["id"])
            for group, spec in suite["groups"].items():
                self.assertTrue(set(spec.get("tiers", [])) <= set(tiers), f"{suite['id']}/{group}")
            if suite["kind"] == "binary":
                # qmake selection maps a binary to its project; globs selection
                # (make / cmake / custom builds) to the sources it is built from.
                if selection.selection_mode(REPO) == "qmake":
                    self.assertIn(suite.get("project"), graph.projects, suite["id"])
                    for pro in suite.get("uses", []):
                        self.assertIn(pro, graph.projects, suite["id"])
                else:
                    self.assertTrue(suite.get("sources") or suite.get("inputs"),
                                    f"{suite['id']}: a binary suite needs 'sources' globs in globs mode")
        # Every test project the build produces has a suite (no silently unregistered binary).
        test_projects = {p for p in graph.leaf if "test" in Path(p).stem.lower()}
        registered = {s.get("project") for s in manifest["suites"]}
        self.assertEqual(test_projects - registered, set())

    def test_tiers_are_cumulative(self):
        manifest = run_tests.load_manifest()
        self.assertEqual(run_tests.tier_set(manifest, "t2"), {"t0", "t1", "t2"})
        self.assertEqual(run_tests.tier_set(manifest, "perf"), {"perf"})


if __name__ == "__main__":
    unittest.main()
