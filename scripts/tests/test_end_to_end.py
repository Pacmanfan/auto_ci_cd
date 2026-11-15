#!/usr/bin/env python3
"""End-to-end: a fresh project from bootstrap to committed work (ADR-000199,
CI_CD.md "Verification"). This is the acceptance scenario of the framework,
run on a throw-away repository and never on a real project:

 1. `devsys.py install` into an empty git repository, then configure a small
    C project (make build, a TAP test suite, requirements, two invariants);
 2. several agents file requests at the same instant, one statement twice;
 3. two agents claim with verifiable session processes; one dies with
    uncommitted work; the sweep finds it and repairs nothing; the survivor
    cannot take the dead agent's files; a third agent recovers the request
    explicitly and finishes it;
 4. the surviving agent resumes its own request; a code change after its
    test run fails the gate until the tests are re-run;
 5. a request is rejected with a reason;
 6. tiers t0..t3, drift / doc_integrity / invariants and the architecture
    audit pass; every commit holds only its request's paths; a human's
    unrelated edits are byte-identical at the end.

Requirements verified end to end: REQ-DEVOPS-005 (intake), REQ-DEVOPS-006 (lifecycle),
REQ-DEVOPS-008 (work traceability), REQ-DEVOPS-009 (evidence), REQ-DEVOPS-010 (gate),
REQ-DEVOPS-047 (bootstrap), REQ-DEVOPS-051 (CI entry point).

Needs make and a C compiler; skipped otherwise.
Run: python3 -m unittest scripts.tests.test_end_to_end -v
"""
import hashlib
import json
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from devsys_testlib import Project  # noqa: E402

CALC_H = "#ifndef CALC_H\n#define CALC_H\nint calc_add(int a, int b);\nint calc_mul(int a, int b);\n#endif\n"
CALC_C = '#include "calc.h"\nint calc_add(int a, int b) { return a + b; }\nint calc_mul(int a, int b) { return a * b; }\n'
TEST_C = """#include <stdio.h>
#include "../src/calc.h"
static int n;
static int check(const char *name, int cond) { printf("%sok %d - %s\\n", cond ? "" : "not ", ++n, name); return cond; }
int main(void) {
    int ok = 1;
    ok &= check("add", calc_add(2, 3) == 5);
    ok &= check("mul", calc_mul(2, 3) == 6);
    printf("1..%d\\n", n);
    return ok ? 0 : 1;
}
"""
MAKEFILE = """BUILD ?= build/debug
all: $(BUILD)/test_calc
$(BUILD)/test_calc: tests/test_calc.c src/calc.c src/calc.h
\tmkdir -p $(BUILD)
\t$(CC) -Wall -o $@ tests/test_calc.c src/calc.c
clean:
\trm -f $(BUILD)/test_calc
.PHONY: all clean
"""
REQS = "# Calculator Requirements\n\n### REQ-CALC-001 — Addition\n\nAdds.\n\n### REQ-CALC-002 — Multiplication\n\nMultiplies.\n"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@unittest.skipUnless(shutil.which("make") and (shutil.which("cc") or shutil.which("gcc")), "make / cc missing")
class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.p = Project({"claims": {"dead_grace_minutes": 0}, "tests": {"selection": "globs"},
                          "project": {"source_roots": ["src", "tests"]}, "build": {"system": "make"},
                          "invariants": [
                              {"id": "lib-no-io", "kind": "forbid_pattern", "files": ["src/*.c"],
                               "pattern": "#include\\s*<stdio.h>", "message": "the library does no I/O"},
                              {"id": "tests-use-lib", "kind": "require_pattern", "files": ["tests/*.c"],
                               "pattern": "calc\\.h"}]},
                         install=True, name="calc")
        p = self.p
        p.write("src/calc.h", CALC_H)
        p.write("src/calc.c", CALC_C)
        p.write("tests/test_calc.c", TEST_C)
        p.write("Makefile", MAKEFILE)
        p.write("docs/requirements/calc.md", REQS)
        p.write("docs/requirements/requirements.txt", p.read("docs/requirements/requirements.txt")
                + "calc | calc.md | REQ-CALC-001..002 | suite calc\n")
        manifest = json.loads(p.read("scripts/test_manifest.json"))
        # The project's suites: the framework's structure / audit checks and
        # its own calc suite (the framework's self-tests are the framework's).
        manifest["suites"] = [s for s in manifest["suites"] if s["id"] in ("devsys", "audits")]
        manifest["suites"].append({"id": "calc", "kind": "binary", "results": "tap", "binary": "test_calc",
                                   "sources": ["src/**", "tests/**", "Makefile"],
                                   "groups": {"unit": {"tiers": ["t1"]}}})
        p.write("scripts/test_manifest.json", json.dumps(manifest, indent=1) + "\n")
        p.commit_all("calc project with the development-control system")
        self.assertEqual(p.run("devsys.py", "doctor").returncode, 0)

    def tearDown(self):
        self.p.close()

    def rid(self, name):
        return json.loads(self.p.read(str(Path(self.p.path_of(name)).with_suffix(".json"))))["request_id"]

    def finish(self, name, agent, pid, work, message):
        """Tests, evidence, gate, completion, commit, release."""
        p = self.p
        path = p.path_of(name)
        out = p.ok("run_tests.py", "--tier", "t2", "--request", path, "--build", agent=agent, owner_pid=pid)
        run = p.run_id(out)
        p.evidence(work, agent, [run])
        p.ok("work.py", "transition", work, "--to", "verification", agent=agent)
        rc, gate = p.gate(work, agent)
        self.assertEqual(gate["verdict"], "PASS", gate)
        p.ok("work.py", "transition", work, "--to", "completed", agent=agent)
        p.ok("claim.py", "--complete-request", path, agent=agent, owner_pid=pid)
        done = f"requests/completed/{name}"
        out = p.ok("commit_request.py", "--request", done, "-m", message, agent=agent)
        self.assertFalse([l for l in out.splitlines() if l.startswith("NOTE ")], out)   # nothing left behind (B10)
        p.ok("claim.py", "--release-request", done, agent=agent)
        return run

    def test_fresh_project_lifecycle(self):
        p = self.p
        human_edit = p.write("docs/architecture/README.md", p.read("docs/architecture/README.md") + "\n- human note\n")
        human_new = p.write("NOTES.md", "a human's scratch notes\n")
        untouched = {human_edit: sha(human_edit), human_new: sha(human_new)}

        # 2. concurrent intake
        texts = ["Add a subtraction operation calc_sub to the calc library",
                 "Add an integer division calc_div that refuses division by zero",
                 "Rename the calc library to arith",
                 "Add an integer division calc_div that refuses division by zero"]
        go = p.base / "go"
        code = ("import os,sys,time\nwhile not os.path.exists(sys.argv[1]): time.sleep(0.001)\n"
                "os.execv(sys.executable,[sys.executable,sys.argv[2],'--source','chat','--text',sys.argv[3]])")
        procs = [subprocess.Popen([sys.executable, "-c", code, str(go), str(p.scripts / "create_request.py"), t],
                                  cwd=str(p.root), env=p.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                 for t in texts]
        time.sleep(0.8)
        go.write_text("go")
        self.assertEqual(sorted(x.wait(timeout=120) for x in procs), [0, 0, 0, 7])
        inbox = sorted(x.name for x in (p.root / "requests/inbox").glob("*.md") if x.name != "README.md")
        self.assertEqual(sorted(n[-7:-3] for n in inbox), ["-001", "-002", "-003"])

        # 3. two sessions claim in arrival order
        sa, sb = p.sleeper(), p.sleeper()
        a, b, c = "agent-e2e-a", "agent-e2e-b", "agent-e2e-c"
        first = p.ok("devsys.py", "next", "--claim", agent=a, owner_pid=sa.pid).split()[1]
        second = p.ok("devsys.py", "next", "--claim", agent=b, owner_pid=sb.pid).split()[1]
        by_text = {self.rid(n): n for n in (first, second)}
        self.assertEqual(sorted(k[-3:] for k in by_text), ["001", "002"])
        sub_name = first if "subtraction" in first else second
        div_name = second if sub_name == first else first
        sub_agent, sub_pid = (a, sa) if sub_name == first else (b, sb)
        div_agent, div_pid = (b, sb) if sub_name == first else (a, sa)

        # the subtraction agent starts, then its session dies
        sub_path = p.path_of(sub_name)
        p.ok("claim.py", "--own", sub_path, "--paths", "src/calc.c", "src/calc.h", "tests/test_calc.c",
             agent=sub_agent)
        p.ok("claim.py", "--own", sub_path, "--paths", "docs/requirements/calc.md",
             "docs/requirements/requirements.txt", "--concurrent", agent=sub_agent)
        p.write("docs/requirements/calc.md", p.read("docs/requirements/calc.md")
                + "\n### REQ-CALC-003 — Subtraction\n\nSubtracts.\n")
        p.write("docs/requirements/requirements.txt",
                p.read("docs/requirements/requirements.txt").replace("REQ-CALC-001..002", "REQ-CALC-001..003"))
        sub_work = p.work(sub_name, sub_agent, "REQ-CALC-003", "--test-suites", "calc")
        p.write("src/calc.h", p.read("src/calc.h").replace("#endif", "int calc_sub(int a, int b);\n#endif"))
        p.write("src/calc.c", p.read("src/calc.c") + "int calc_sub(int a, int b) { return a - b; }\n")
        p.kill(sub_pid)

        status = json.loads(p.ok("devsys.py", "status", "--json"))
        dead = [x for x in status["claims"] if x["target"] == sub_name][0]
        self.assertEqual(dead["liveness"], "DEAD", dead)
        report = json.loads(p.ok("claim.py", "--sweep", "--apply", "--json"))
        self.assertFalse([x for x in report["actions"] if x["target"] == sub_name], "uncommitted work is not swept")
        self.assertTrue([x for x in report["findings"] if x["target"] == sub_name and x.get("recoverable")])
        # the survivor cannot take the dead agent's files
        div_path = p.path_of(div_name)
        self.assertEqual(p.run("claim.py", "--own", div_path, "--paths", "src/calc.c", agent=div_agent).returncode, 1)
        # a live owner is never taken over
        self.assertEqual(p.run("claim.py", "--recover-request", div_name, "--reason", "x", "--force-active",
                               agent=c).returncode, 1)
        # explicit recovery by a new session, which finishes the work
        sc = p.sleeper()
        p.ok("claim.py", "--recover-request", sub_name, "--reason", "owner verified DEAD", agent=c, owner_pid=sc.pid)
        p.write("tests/test_calc.c", p.read("tests/test_calc.c").replace(
            '    printf("1..', '    ok &= check("sub", calc_sub(5, 3) == 2);\n    printf("1..'))
        self.finish(sub_name, c, sc.pid, sub_work, f"{sub_work} calc_sub (recovered from a dead session)")

        # 4. the surviving agent resumes its own request
        p.ok("claim.py", "--own", div_path, "--paths", "src/calc.c", "src/calc.h", "tests/test_calc.c",
             agent=div_agent)
        p.ok("claim.py", "--own", div_path, "--paths", "docs/requirements/calc.md",
             "docs/requirements/requirements.txt", "--concurrent", agent=div_agent)
        p.write("docs/requirements/calc.md", p.read("docs/requirements/calc.md")
                + "\n### REQ-CALC-004 — Division\n\nDivides; refuses zero.\n")
        p.write("docs/requirements/requirements.txt",
                p.read("docs/requirements/requirements.txt").replace("REQ-CALC-001..003", "REQ-CALC-001..004"))
        div_work = p.work(div_name, div_agent, "REQ-CALC-004", "--test-suites", "calc")
        p.write("src/calc.h", p.read("src/calc.h").replace("#endif", "int calc_div(int a, int b, int *e);\n#endif"))
        p.write("src/calc.c", p.read("src/calc.c") + "int calc_div(int a, int b, int *e) { *e = !b; return b ? a / b : 0; }\n")
        p.write("tests/test_calc.c", p.read("tests/test_calc.c").replace(
            '    printf("1..', '    { int e; ok &= check("div", calc_div(7, 2, &e) == 3 && !e); }\n'
                               '    { int e; calc_div(1, 0, &e); ok &= check("div_zero", e); }\n    printf("1..'))
        run = p.run_id(p.ok("run_tests.py", "--tier", "t2", "--request", div_path, "--build", agent=div_agent))
        p.evidence(div_work, div_agent, [run])
        p.write("src/calc.c", p.read("src/calc.c") + "/* edited after the tests */\n")
        rc, gate = p.gate(div_work, div_agent)
        self.assertEqual(gate["verdict"], "FAIL")
        self.assertEqual(p.run("claim.py", "--complete-request", div_path, agent=div_agent).returncode, 9)
        self.finish(div_name, div_agent, div_pid.pid, div_work, f"{div_work} calc_div")

        # 5. reject the third request with a reason
        third = p.ok("devsys.py", "next", "--claim", agent=div_agent, owner_pid=div_pid.pid).split()[1]
        tpath = p.path_of(third)
        p.ok("claim.py", "--reject-request", tpath, "--reason", "renaming breaks every user", agent=div_agent)
        p.ok("commit_request.py", "--request", f"requests/rejected/{third}", "-m", "rejected", agent=div_agent)
        p.ok("claim.py", "--release-request", f"requests/rejected/{third}", agent=div_agent)

        # 6. tiers, audits, isolation
        for tier in ("t0", "t1", "t2", "t3"):
            cp = p.run("run_tests.py", "--tier", tier, "--all")
            self.assertEqual(cp.returncode, 0, f"{tier}\n{cp.stdout}\n{cp.stderr}")
        for tool in ("drift_detect.py", "doc_integrity.py", "invariant_check.py"):
            cp = p.run(tool)
            self.assertEqual(cp.returncode, 0, cp.stdout)
        audit = subprocess.run([sys.executable, str(p.root / "tools/audit"), "standard", "--fail-on", "fail"],
                               cwd=str(p.root), env=p.env, capture_output=True, text=True, timeout=600)
        self.assertEqual(audit.returncode, 0, audit.stdout + audit.stderr)
        final = json.loads(p.ok("devsys.py", "status", "--json"))
        self.assertEqual(final["requests"]["completed"], 2)
        self.assertEqual(final["requests"]["rejected"], 1)
        self.assertEqual(final["claims"], [])
        dirty = sorted(l[3:] for l in p.git("status", "--porcelain").splitlines())
        self.assertEqual(dirty, ["NOTES.md", "docs/architecture/README.md"])
        for path, digest in untouched.items():
            self.assertEqual(sha(path), digest, path)
        for sha1 in p.git("log", "--format=%H", "-4").split()[:3]:
            files = p.git("show", "--name-only", "--format=", sha1).split()
            self.assertFalse({"NOTES.md", "docs/architecture/README.md"} & set(files))
        self.assertEqual(len(list((p.root / "evidence/gates").glob("WORK-*.json"))), 2)

        # 7. the CI entry point over the finished history: build, tier, audits,
        # architecture audit and the gate binding of every commit made here.
        cp = p.run("devsys.py", "ci", "--tier", "t2", "--verify-commits", "HEAD~3..HEAD", "--json", timeout=900)
        rec = json.loads(cp.stdout)
        self.assertEqual((cp.returncode, rec["overall"]), (0, "PASS"), json.dumps(rec["steps"], indent=1))
        verified = [s["detail"].split(":")[0] for s in rec["steps"] if s["step"].startswith("commit ")]
        self.assertEqual(verified, ["VERIFIED", "VERIFIED", "NOT_APPLICABLE"])
        self.assertTrue((p.root / "build/debug/ci-results/latest.json").is_file())


if __name__ == "__main__":
    unittest.main()
