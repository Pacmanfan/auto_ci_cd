"""Tests of the architecture audit (ARCH-AUDIT-001..010, ADR-000195).

Every test builds a small synthetic git repository with its own
.audit/manifest.json, so the result states, parsers, builtins, run identity,
comparison, retention and the read-only guarantee are checked without the real
tree. Run: python3 -m unittest discover -s tools/audit_system/tests
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import audit  # noqa: E402
import checks as C  # noqa: E402
import report  # noqa: E402


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")


BASE_MANIFEST = {
    "schema": 1, "audit_version": "test",
    "project": {"name": "Synthetic", "product": "Synthetic"},
    "canonical_sources": {"requirements": "docs/requirements/"},
    "modules": [{"name": "Core", "paths": ["core/"]}, {"name": "Documentation", "paths": ["docs/"]}],
    "requirements": {"documents": "docs/requirements/*.md", "exclude": ["docs/requirements/README.md"],
                     "index": "docs/requirements/requirements.txt", "test_sources": ["tests/**"],
                     "test_suffixes": [".cpp", ".py"], "evidence": ["evidence/**/*.md"]},
    "documentation_paths": {"documents": ["AGENTS.md"], "architecture_index": "docs/architecture/README.md",
                            "indexes": ["AGENTS.md"], "indexed_prefixes": ["docs/architecture/"],
                            "extensions": [".h", ".cpp", ".md", ".json"], "skip_prefixes": ["build/"],
                            "context_words": ["removed", "~/"]},
    "profiles": {"quick": {"costs": ["fast"], "budget_seconds": 60},
                 "standard": {"costs": ["fast", "moderate"], "budget_seconds": 60},
                 "baseline": {"costs": ["fast", "moderate"], "budget_seconds": 60}},
    "retention": {"max_runs": 2, "keep_profiles": ["baseline"]},
    "checks": [
        {"id": "doc.referenced-paths", "domain": "documentation", "title": "paths", "rule": "R-DOC",
         "source": "AGENTS.md", "kind": "builtin", "function": "documented_paths", "cost": "fast"},
        {"id": "req.undefined-references", "domain": "requirements", "title": "refs", "rule": "R-REQ",
         "source": "x", "kind": "builtin", "function": "requirement_references", "cost": "fast"},
        {"id": "req.index-coverage", "domain": "requirements", "title": "index", "rule": "R-IDX",
         "source": "x", "kind": "builtin", "function": "requirement_index", "cost": "fast"},
        {"id": "proc.tool", "domain": "process", "title": "tool", "rule": "R-TOOL", "source": "x",
         "kind": "command", "command": ["python3", "tool.py"], "cost": "moderate", "parse": "exit"},
        {"id": "arch.rule-nobody-checks", "domain": "architecture", "title": "unverified", "rule": "R-U",
         "source": "x", "kind": "unverified", "reason": "no mechanism", "future_evidence": "a test",
         "cost": "fast"},
        {"id": "build.package", "domain": "build", "title": "package", "rule": "R-P", "source": "x",
         "kind": "command", "command": [], "cost": "expensive", "reason": "package lane"},
    ],
}


class SyntheticRepo:
    def __init__(self, manifest: dict | None = None) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        git(self.root, "init", "-q")
        git(self.root, "config", "user.email", "audit@test")
        git(self.root, "config", "user.name", "audit test")
        write(self.root, ".gitignore", "/.audit/runs/\n")
        write(self.root, ".audit/manifest.json", json.dumps(manifest or BASE_MANIFEST, indent=1))
        write(self.root, "AGENTS.md", """
            # Rules
            The core lives in `core/engine.h`. Settings keys such as `core/enabled` are not files.
            The engine design is `docs/architecture/engine.md`.
            The old viewer `core/old_viewer.h` was removed.
            Config is `~/.config` + `core/settings.json`.
            """)
        write(self.root, "core/engine.h", "// engine\n")
        write(self.root, "docs/architecture/README.md", """
            # Architecture

            ## Current references

            * `design.md` — the design
            """)
        write(self.root, "docs/architecture/design.md", "Uses `core/engine.h`.\n")
        write(self.root, "docs/architecture/engine.md", "Implemented in `core/engine.h`.\n")
        write(self.root, "docs/requirements/README.md", "index\n")
        write(self.root, "docs/requirements/core.md", """
            # Core
            ### CORE-X-001 — First
            ### CORE-X-002..003 — Range
            | CORE-T-001 | table form |
            **CORE-B-001** bold form
            """)
        write(self.root, "docs/requirements/requirements.txt",
              "Format: a | b | c | d\ncore | core.md | CORE-* | tests\n")
        write(self.root, "tests/test_core.cpp", "// CORE-X-001, CORE-X-002..004, REQ-CHAT-20260101-001\n")
        write(self.root, "tool.py", "print('tool ok')\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "synthetic")

    def ctx(self, profile: str = "quick") -> C.Context:
        manifest = json.loads((self.root / ".audit" / "manifest.json").read_text())
        return C.Context(self.root, manifest, profile, self.root / "build")

    def run(self, profile: str = "quick", **kw) -> dict:
        return audit.run_audit(self.root, profile, self.root / "build", None, False, None, None, False,
                               quiet=True, **kw)

    def close(self) -> None:
        self._tmp.cleanup()


class ResultStates(unittest.TestCase):
    """ARCH-AUDIT-003: UNKNOWN is never PASS; inability to verify is UNKNOWN."""

    def setUp(self) -> None:
        self.repo = SyntheticRepo()
        self.addCleanup(self.repo.close)

    def test_unverified_rule_is_unknown_with_reason(self):
        res = C.evaluate({"kind": "unverified", "reason": "no mechanism"}, self.repo.ctx())
        self.assertEqual(res["status"], C.UNKNOWN)

    def test_command_that_cannot_run_is_unknown(self):
        res = C.evaluate({"kind": "command", "command": ["/nonexistent/tool"], "parse": "exit"}, self.repo.ctx())
        self.assertEqual(res["status"], C.UNKNOWN)

    def test_command_timeout_is_unknown(self):
        res = C.evaluate({"kind": "command", "command": ["python3", "-c", "import time; time.sleep(5)"],
                          "timeout_seconds": 0.5, "parse": "exit"}, self.repo.ctx())
        self.assertEqual(res["status"], C.UNKNOWN)

    def test_builtin_that_raises_is_unknown(self):
        C.BUILTINS["_boom"] = lambda ctx, params, check: 1 / 0
        self.addCleanup(C.BUILTINS.pop, "_boom")
        res = C.evaluate({"kind": "builtin", "function": "_boom"}, self.repo.ctx())
        self.assertEqual(res["status"], C.UNKNOWN)

    def test_exit_parser(self):
        ok = C.parse_exit(["t"], {"rc": 0, "stdout": "all fine\n", "stderr": "", "error": None})
        bad = C.parse_exit(["t"], {"rc": 1, "stdout": "FAIL: x broken\n1 problem\n", "stderr": "", "error": None})
        self.assertEqual((ok["status"], bad["status"]), (C.PASS, C.DRIFT))
        self.assertEqual(bad["summary"], "FAIL: x broken")

    def test_findings_json_parser_keeps_the_tools_own_severity(self):
        warn_only = json.dumps([{"level": "WARN", "code": "BROKEN_LINK", "msg": "a"}])
        failing = json.dumps([{"level": "FAIL", "code": "DUP", "msg": "b"}])
        out = lambda text: {"rc": 0, "stdout": text, "stderr": "", "error": None}  # noqa: E731
        self.assertEqual(C.parse_findings_json(["t"], out(warn_only))["status"], C.PASS)
        self.assertEqual(C.parse_findings_json(["t"], out(warn_only))["metrics"]["warn"], 1)
        self.assertEqual(C.parse_findings_json(["t"], out(failing))["status"], C.DRIFT)
        self.assertEqual(C.parse_findings_json(["t"], out("not json"))["status"], C.UNKNOWN)

    def test_stale_test_binaries_leave_the_build_unknown(self):
        plan = ("PENDING  scripts/unit        ~    27s  input x\n"
                "STALE    engine/unit[1/3]    ~    32s  builds E  [Engine_Tests: main.cpp is newer than the binary]\n"
                "STALE    engine/unit[2/3]    ~    32s  builds E  [Engine_Tests: main.cpp is newer than the binary]\n")
        res = C.parse_test_plan(["t"], {"rc": 0, "stdout": plan, "stderr": "", "error": None})
        self.assertEqual(res["status"], C.UNKNOWN)
        self.assertEqual(res["metrics"]["not_current"], 1)
        fresh = C.parse_test_plan(["t"], {"rc": 0, "stdout": plan.splitlines()[0], "stderr": "", "error": None})
        self.assertEqual(fresh["status"], C.PASS)

    def test_heuristic_invariant_is_unknown_even_when_silent(self):
        ctx = self.repo.ctx()
        ctx.invariants = lambda: {"fails": [], "warns": [], "text": "{}"}
        res = C.invariant_slice(ctx, {"match": ["INVARIANT x"], "strength": "heuristic"})
        self.assertEqual(res["status"], C.UNKNOWN)
        enforced = C.invariant_slice(ctx, {"match": ["INVARIANT x"], "strength": "enforced"})
        self.assertEqual(enforced["status"], C.PASS)
        ctx.invariants = lambda: {"fails": ["INVARIANT x violated"], "warns": [], "text": "{}"}
        self.assertEqual(C.invariant_slice(ctx, {"match": ["INVARIANT x"], "strength": "enforced"})["status"],
                         C.DRIFT)


class Builtins(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = SyntheticRepo()
        self.addCleanup(self.repo.close)

    def test_requirement_definitions_in_every_form_and_ranges(self):
        model = self.repo.ctx().requirements()
        self.assertEqual(sorted(model["defined"]),
                         ["CORE-B-001", "CORE-T-001", "CORE-X-001", "CORE-X-002", "CORE-X-003"])

    def test_undefined_reference_only_for_written_ids_of_known_namespaces(self):
        ctx = self.repo.ctx()
        self.assertEqual(C.requirement_references(ctx, {}, {})["status"], C.PASS)
        # CORE-X-004 is implied by the range CORE-X-002..004 only: reported, not DRIFT;
        # REQ-CHAT-* is not a requirement namespace.
        self.assertEqual(C.requirement_references(ctx, {}, {})["metrics"]["undefined_via_range"], 1)
        write(self.repo.root, "tests/test_more.py", "# CORE-X-009 covers the new case\n")
        res = C.requirement_references(self.repo.ctx(), {}, {})
        self.assertEqual(res["status"], C.DRIFT)
        self.assertIn("CORE-X-009", res["summary"])

    def test_requirement_index_coverage(self):
        self.assertEqual(C.requirement_index(self.repo.ctx(), {}, {})["status"], C.PASS)
        write(self.repo.root, "docs/requirements/orphan.md", "### ORPH-001 — x\n")
        res = C.requirement_index(self.repo.ctx(), {}, {})
        self.assertEqual(res["status"], C.DRIFT)
        self.assertIn("orphan.md", res["summary"])

    def test_documented_paths_are_conservative(self):
        res = C.documented_paths(self.repo.ctx(), {}, {})
        # core/enabled (no extension), the removed viewer and the composed
        # config path are not findings; core/engine.h exists.
        self.assertEqual(res["status"], C.PASS, res["details"])
        # design.md from the architecture README, engine.md because AGENTS.md (the index here) names it.
        self.assertEqual(res["metrics"]["architecture_current"], 2)
        write(self.repo.root, "docs/architecture/engine.md", "Implemented in `core/engine_old.h`.\n")
        self.assertIn("engine_old.h", C.documented_paths(self.repo.ctx(), {}, {})["summary"] + "".join(
            C.documented_paths(self.repo.ctx(), {}, {})["details"]))
        write(self.repo.root, "docs/architecture/engine.md", "Implemented in `core/engine.h`.\n")
        write(self.repo.root, "docs/architecture/design.md", "Uses `core/engine.h` and `core/gone.h`.\n")
        res = C.documented_paths(self.repo.ctx(), {}, {})
        self.assertEqual(res["status"], C.DRIFT)
        self.assertIn("core/gone.h", res["details"][0])

    def test_declared_dependencies(self):
        write(self.repo.root, "project.pro", """
            TEMPLATE = subdirs
            SUBDIRS = lib core ui   # the products
            core.depends = lib
            ui.depends = core \\
                lib
            """)
        params = {"expect": {"ui": {"includes": ["core"]}, "core": {"only": ["lib"]}}}
        self.assertEqual(C.declared_dependencies(self.repo.ctx(), params, {"rule": "R"})["status"], C.PASS)
        params["expect"]["core"] = {"only": ["base"]}
        res = C.declared_dependencies(self.repo.ctx(), params, {"rule": "R"})
        self.assertEqual(res["status"], C.DRIFT)
        self.assertIn("extra: lib", res["summary"])

    def test_recorded_test_results(self):
        write(self.repo.root, "scripts/test_manifest.json", json.dumps({"suites": [
            {"id": "engine", "groups": {"unit": {"tiers": ["t1"]}, "bench": {"tiers": ["perf"]}}}]}))
        runs = self.repo.root / "build" / "test-results" / "runs"
        runs.mkdir(parents=True)
        record = lambda rid, status: {"run_id": rid, "started_at": rid, "head": "x",  # noqa: E731
                                      "groups": [{"suite": "engine", "group": "unit", "status": status}]}
        (runs / "1.json").write_text(json.dumps(record("2026-01-01", "PASS")))
        self.assertEqual(C.recorded_test_results(self.repo.ctx(), {}, {})["status"], C.PASS)
        (runs / "2.json").write_text(json.dumps(record("2026-01-02", "STALE")))   # not a result
        self.assertEqual(C.recorded_test_results(self.repo.ctx(), {}, {})["status"], C.PASS)
        (runs / "3.json").write_text(json.dumps(record("2026-01-03", "FAIL")))
        self.assertEqual(C.recorded_test_results(self.repo.ctx(), {}, {})["status"], C.DRIFT)


class Runs(unittest.TestCase):
    """ARCH-AUDIT-004..009: identity, artifacts, profiles, comparison, retention, read-only."""

    def setUp(self) -> None:
        self.repo = SyntheticRepo()
        self.addCleanup(self.repo.close)

    def test_audit_ids_are_sequential_per_day_and_never_shared(self):
        first, _ = audit.allocate_run(self.repo.root, "20260101")
        second, _ = audit.allocate_run(self.repo.root, "20260101")
        (audit.runs_dir(self.repo.root) / "AUDIT-20260101-007").mkdir()
        third, _ = audit.allocate_run(self.repo.root, "20260101")
        other_day, _ = audit.allocate_run(self.repo.root, "20260102")
        self.assertEqual([first, second, third, other_day],
                         ["AUDIT-20260101-001", "AUDIT-20260101-002", "AUDIT-20260101-008", "AUDIT-20260102-001"])

    def test_profiles_select_checks_by_cost_class(self):
        data = self.repo.run("quick")
        status = {c["id"]: c["status"] for c in data["checks"]}
        self.assertEqual(status["proc.tool"], C.SKIPPED)          # moderate: not in quick
        self.assertEqual(status["build.package"], C.SKIPPED)      # expensive: never in Phase 1
        self.assertIn("NOT APPLICABLE", next(c for c in data["checks"] if c["id"] == "build.package")["summary"])
        self.assertEqual(status["arch.rule-nobody-checks"], C.UNKNOWN)
        standard = self.repo.run("standard")
        self.assertEqual({c["id"]: c["status"] for c in standard["checks"]}["proc.tool"], C.PASS)

    def test_run_artifacts_reference_the_audit_id_and_leave_the_tree_unchanged(self):
        before = git(self.repo.root, "status", "--porcelain")
        data = self.repo.run("standard")
        run_dir = audit.runs_dir(self.repo.root) / data["audit_id"]
        for name in ("audit.json", "audit.md", "audit-packet.md"):
            self.assertIn(data["audit_id"], (run_dir / name).read_text())
        for check in data["checks"]:
            if check["status"] != C.SKIPPED:
                self.assertTrue((run_dir / check["evidence_file"]).is_file(), check["id"])
        self.assertEqual(git(self.repo.root, "status", "--porcelain"), before)
        self.assertEqual(next(c for c in data["checks"] if c["id"] == "audit.read-only")["status"], C.PASS)

    def test_repeat_run_is_consistent_and_compared(self):
        first = self.repo.run("standard")
        second = self.repo.run("standard")
        self.assertNotEqual(first["audit_id"], second["audit_id"])
        self.assertEqual({c["id"]: c["status"] for c in first["checks"]},
                         {c["id"]: c["status"] for c in second["checks"]})
        self.assertEqual(second["comparison"]["previous"], first["audit_id"])
        self.assertEqual(second["comparison"]["status_changes"], [])

    def test_controlled_change_is_detected_and_disappears_when_restored(self):
        baseline = self.repo.run("quick")
        doc = self.repo.root / "docs/architecture/design.md"
        original = doc.read_text()
        doc.write_text(original + "See `core/missing.h`.\n")
        changed = self.repo.run("quick")
        doc.write_text(original)
        restored = self.repo.run("quick")
        status = lambda d: {c["id"]: c["status"] for c in d["checks"]}["doc.referenced-paths"]  # noqa: E731
        self.assertEqual([status(baseline), status(changed), status(restored)], [C.PASS, C.DRIFT, C.PASS])
        self.assertIn({"id": "doc.referenced-paths", "from": "PASS", "to": "DRIFT"},
                      changed["comparison"]["status_changes"])
        self.assertIn({"id": "doc.referenced-paths", "from": "DRIFT", "to": "PASS"},
                      restored["comparison"]["status_changes"])

    def test_retention_keeps_baselines_and_the_newest_runs(self):
        ids = [self.repo.run(p)["audit_id"] for p in ("baseline", "quick", "quick", "quick", "quick")]
        left = sorted(p.name for p in audit.runs_dir(self.repo.root).iterdir())
        # max_runs 2: the newest two quick runs plus the baseline, whatever its age.
        self.assertEqual(left, [ids[0], ids[3], ids[4]])

    def test_reports_render_every_state(self):
        data = self.repo.run("standard")
        human = report.human(data)
        for heading in ("## Results", "## Important findings (DRIFT)", "## Changes since previous audit",
                        "## Unverified areas (UNKNOWN)", "## Next actions"):
            self.assertIn(heading, human)
        packet = report.packet(data)
        self.assertIn("UNKNOWN means not established", packet)
        self.assertIn("arch.rule-nobody-checks", packet)


class Levels(unittest.TestCase):
    """ADR-000199: a gate-severity rule that does not hold is FAIL (not
    advisory DRIFT); a check whose mechanism is not configured is SKIPPED,
    never PASS; --fail-on turns the advisory audit into a gate; the audit
    runs on a copy that is not a git repository."""

    def manifest(self, *checks):
        m = json.loads(json.dumps(BASE_MANIFEST))
        m["checks"] = list(checks)
        return m

    @staticmethod
    def tool(check_id, code, **extra):
        return dict({"id": check_id, "domain": "process", "title": check_id, "rule": "R", "source": "x",
                     "kind": "command", "command": ["python3", "-c", code], "cost": "fast", "parse": "exit"}, **extra)

    def run_repo(self, *checks):
        repo = SyntheticRepo(self.manifest(*checks))
        self.addCleanup(repo.close)
        return repo, {c["id"]: c for c in repo.run("quick")["checks"]}

    def test_gate_severity_turns_a_violation_into_fail(self):
        failing = "print('FAIL x'); raise SystemExit(1)"
        _, res = self.run_repo(self.tool("advisory", failing), self.tool("gated", failing, severity="gate"))
        self.assertEqual(res["advisory"]["status"], C.DRIFT)
        self.assertEqual(res["gated"]["status"], C.FAIL)

    def test_not_configured_is_skipped_not_pass(self):
        _, res = self.run_repo(
            self.tool("unset", "print('INVARIANTS: NOT CONFIGURED (no rule)')"),
            self.tool("exit10", "raise SystemExit(10)"),
            self.tool("warned", "print('WARN something'); print('ok')"))
        self.assertEqual(res["unset"]["status"], C.SKIPPED)
        self.assertIn("not configured", res["unset"]["summary"])
        self.assertEqual(res["exit10"]["status"], C.SKIPPED)
        self.assertEqual(res["warned"]["status"], C.PASS)
        self.assertIn("warning", res["warned"]["summary"])

    def test_fail_on_makes_the_audit_a_gate(self):
        repo, _ = self.run_repo(self.tool("drifting", "print('FAIL y'); raise SystemExit(1)"))
        env = dict(os.environ, AUDIT_ROOT=str(repo.root))
        old = os.environ.copy()
        os.environ.update(env)
        import contextlib
        import io
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(audit.main(["quick", "--json"]), 0)
                self.assertEqual(audit.main(["quick", "--json", "--fail-on", "fail"]), 0)
                self.assertEqual(audit.main(["quick", "--json", "--fail-on", "drift"]), 1)
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_architecture_rules_skipped_pass_with_warnings_and_fail(self):
        rules = {"id": "arch.rules", "domain": "architecture", "title": "rules", "rule": "R", "source": "x",
                 "kind": "builtin", "function": "architecture_rules", "cost": "fast", "severity": "gate"}
        cases = [({"fails": [], "warns": [], "rules": 0, "configured": False}, C.SKIPPED),
                 ({"fails": [], "warns": ["INVARIANT adv violated: x"], "rules": 2, "configured": True}, C.PASS),
                 ({"fails": ["INVARIANT core violated: y"], "warns": [], "rules": 2, "configured": True}, C.FAIL)]
        for payload, expected in cases:
            repo = SyntheticRepo(self.manifest(rules))
            self.addCleanup(repo.close)
            write(repo.root, "scripts/invariant_check.py", f"import json; print(json.dumps({payload!r}))\n")
            res = {c["id"]: c for c in repo.run("quick")["checks"]}["arch.rules"]
            self.assertEqual(res["status"], expected, (payload, res))

    def test_audit_runs_without_git(self):
        repo = SyntheticRepo()
        self.addCleanup(repo.close)
        import shutil
        shutil.rmtree(repo.root / ".git")
        data = repo.run("standard")
        status = {c["id"]: c["status"] for c in data["checks"]}
        self.assertEqual(status["req.index-coverage"], C.PASS)
        self.assertEqual(status["proc.tool"], C.PASS)
        self.assertEqual(status["audit.read-only"], C.PASS)


if __name__ == "__main__":
    unittest.main()
