"""Shared fixture of the framework self-tests (not a test module).

``Project`` is a throw-away git repository bootstrapped with the framework's
own ``scripts/devsys.py init`` (and, with ``install=True``, a full
``devsys.py install`` copy), configured through ``devsys.json`` and driven
only through the real command-line tools, with ``AGENT_PROCESS_ROOT``
pointing at it. Sessions are real processes (``sleeper()``) so liveness is
verified, not mocked. Temporary trees live under the framework's scratch
directory (``tmp/``), never in the system temp directory.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# A test run started from an agent's session inherits that session's identity
# (AGENT_ID, AGENT_OWNER_PID): tests that create their own agents would then
# act as the session, or be "owned" by its live process. And no test starts
# the coordination station (panels, hub) by joining the roster.
for _name in ("AGENT_ID", "AGENT_OWNER_PID"):
    os.environ.pop(_name, None)
os.environ["DEVSYS_COORDINATION_AUTOSTART"] = "0"

SCRIPTS = Path(__file__).resolve().parents[1]
FRAMEWORK = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

REQUIREMENTS = """# Demo Requirements

### REQ-DEMO-001 — Greeting

The program greets.

### REQ-DEMO-002 — Farewell

The program says goodbye.
"""


class Project:
    def __init__(self, config: dict | None = None, install: bool = False, name: str = "demo"):
        base = FRAMEWORK / "tmp" / "devsys-tests"
        base.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix="devsys-", dir=str(base))
        self.base = Path(self.tmp.name)
        self.root = self.base / "repo"
        self.root.mkdir()
        self.sleepers: list[subprocess.Popen] = []
        self.env = {k: v for k, v in os.environ.items() if k not in ("AGENT_ID", "AGENT_OWNER_PID")}
        self.env["AGENT_PROCESS_ROOT"] = str(self.root)
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Devsys Test")
        # The tools under test are this framework's scripts; with install=True
        # a copy made by `devsys.py install` is used instead.
        self.scripts = SCRIPTS
        if install:
            out = subprocess.run([sys.executable, str(SCRIPTS / "devsys.py"), "install", "--target", str(self.root),
                                  "--project-name", name], capture_output=True, text=True, env=self.env)
            assert out.returncode == 0, out.stdout + out.stderr
            self.scripts = self.root / "scripts"
            self.env.pop("AGENT_PROCESS_ROOT")   # the installed scripts find their own root
        else:
            out = self.run("devsys.py", "init", "--project-name", name)
            assert out.returncode == 0, out.stdout + out.stderr
        cfg = json.loads((self.root / "devsys.json").read_text())
        for key, value in (config or {}).items():
            if isinstance(value, dict) and isinstance(cfg.get(key), dict):
                cfg[key].update(value)
            else:
                cfg[key] = value
        self.write("devsys.json", json.dumps(cfg, indent=2) + "\n")
        self.write("docs/requirements/demo.md", REQUIREMENTS)
        req_txt = self.root / "docs/requirements/requirements.txt"
        prefix = req_txt.read_text() if req_txt.exists() else "# Normative Requirements Manifest\n\n"
        self.write("docs/requirements/requirements.txt", prefix + "demo | demo.md | REQ-DEMO-001..002 | demo tests\n")
        self.write("src/app.txt", "hello\n")
        self.write("notes/unrelated.txt", "someone else's file\n")
        self.commit_all("initial")

    # ------------------------------------------------------------- basics
    def close(self):
        for p in self.sleepers:
            if p.poll() is None:
                p.kill()
                p.wait()
        self.tmp.cleanup()

    def git(self, *args, check=True):
        return subprocess.run(["git", *args], cwd=str(self.root), capture_output=True, text=True, check=check).stdout

    def write(self, rel: str, text: str) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def read(self, rel: str) -> str:
        return (self.root / rel).read_text(encoding="utf-8")

    def commit_all(self, msg: str):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)

    def run(self, script: str, *args, agent: str | None = None, owner_pid: int | None = None,
            env: dict | None = None, timeout: float = 180, input: str | None = None):
        e = dict(self.env)
        if agent:
            e["AGENT_ID"] = agent
        if owner_pid:
            e["AGENT_OWNER_PID"] = str(owner_pid)
        e.update(env or {})
        return subprocess.run([sys.executable, str(self.scripts / script), *args], cwd=str(self.root), env=e,
                              capture_output=True, text=True, timeout=timeout, input=input)

    def ok(self, script: str, *args, **kw) -> str:
        cp = self.run(script, *args, **kw)
        if cp.returncode != 0:
            raise AssertionError(f"{script} {' '.join(args)} -> {cp.returncode}\n{cp.stdout}\n{cp.stderr}")
        return cp.stdout

    def suites(self, suites: list[dict], tiers: dict | None = None) -> None:
        """Register test suites (scripts/test_manifest.json of the fixture)."""
        manifest = {"schema": 1, "tiers": tiers or {
            "t0": {"includes": []}, "t1": {"includes": ["t0"]}, "t2": {"includes": ["t0", "t1"]},
            "t3": {"includes": ["t0", "t1", "t2"]}}, "suites": suites}
        self.write("scripts/test_manifest.json", json.dumps(manifest, indent=1) + "\n")

    def run_id(self, stdout: str) -> str:
        line = next(l for l in stdout.splitlines() if l.startswith("record: "))
        return Path(line.split()[1]).stem

    # ------------------------------------------------------------- sessions
    def sleeper(self) -> subprocess.Popen:
        """A real long-lived process standing for an agent session."""
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3600)"])
        self.sleepers.append(p)
        return p

    def kill(self, p: subprocess.Popen):
        p.kill()
        p.wait()

    # ------------------------------------------------------------- requests
    def request(self, text: str, *extra) -> str:
        out = self.ok("create_request.py", "--source", "chat", "--text", text, *extra)
        line = next(l for l in out.splitlines() if l.startswith("Created "))
        return Path(line.split()[1]).name

    def where(self, name: str) -> list[str]:
        base = self.root / "requests"
        return [s for s in ("inbox", "processing", "completed", "rejected", "needs-review") if (base / s / name).is_file()]

    def path_of(self, name: str) -> str:
        states = self.where(name)
        assert len(states) == 1, (name, states)
        return f"requests/{states[0]}/{name}"

    def lock_file(self, name: str) -> Path:
        return self.root / ".agent" / "state" / "claims" / f"request-{name}.lock"

    def claim_data(self, name: str) -> dict:
        return json.loads(self.lock_file(name).read_text())

    def edit_claim(self, name: str, fn) -> None:
        data = self.claim_data(name)
        fn(data)
        self.lock_file(name).write_text(json.dumps(data, indent=2))

    def age_claim(self, name: str, minutes: float) -> None:
        """Pretend the claim was last touched ``minutes`` ago."""
        at = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()

        def fn(d):
            d["heartbeat_at"] = at
            d["claimed_at"] = at
        self.edit_claim(name, fn)

    # ------------------------------------------------------------- lifecycle helpers
    def claim(self, name: str, agent: str, owner_pid: int | None = None) -> str:
        self.ok("claim.py", "--claim-request", name, agent=agent, owner_pid=owner_pid)
        return self.path_of(name)

    def work(self, name: str, agent: str, requirements: str = "REQ-DEMO-001", *extra) -> str:
        out = self.ok("work.py", "create", "--request", self.path_of(name), "--title", "demo work",
                      "--requirements", requirements, *extra, agent=agent)
        return next(l.split()[1] for l in out.splitlines() if l.startswith("WORK-ID"))

    def evidence(self, work_id: str, agent: str, runs=(), status: str = "pass") -> str:
        args = ["create", "--work", work_id, "--status", status, "--command", "demo", "--result", "ok"]
        if runs:
            args += ["--test-run", *runs]
        out = self.ok("evidence.py", *args, agent=agent)
        return next(l.split()[1] for l in out.splitlines() if l.startswith("VER-ID"))

    def gate(self, work_id: str, agent: str, record: bool = True):
        args = ["--work", work_id, "--json"] + (["--record"] if record else [])
        cp = self.run("completion_gate.py", *args, agent=agent)
        try:
            return cp.returncode, json.loads(cp.stdout)
        except ValueError:
            raise AssertionError(cp.stdout + cp.stderr)

    def wait_for(self, predicate, timeout: float = 10.0) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if predicate():
                return True
            time.sleep(0.05)
        return predicate()


def fast_config(**extra) -> dict:
    """Test policy: DEAD needs no 5-minute grace, t0 is enough for the gate
    (tests register no suite of higher tiers), drift / doc findings of the
    fixture's own scaffolding do not block."""
    cfg = {"claims": {"dead_grace_minutes": 0}, "completion": {"min_tier": "t0"}}
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(cfg.get(key), dict):
            cfg[key].update(value)
        else:
            cfg[key] = value
    return cfg


def copy_template_file(rel: str, root: Path) -> None:
    dst = root / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FRAMEWORK / rel, dst)
