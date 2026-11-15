#!/usr/bin/env python3
"""Build adapters of the development-control system (ADR-000199).

The project's build is configured in ``devsys.json`` under ``build``:

    "build": {"system": "cmake", "source_dir": ".", "build_dir": "build/debug", "jobs": null}

Adapters (``build.system``):

    none    no build integration (every action reports NOT CONFIGURED)
    make    make [-C <build_dir or source_dir>] -j<jobs>        clean: make clean
    cmake   cmake -S <source_dir> -B <build_dir>; cmake --build <build_dir> -j <jobs>
            clean: cmake --build <build_dir> --target clean
    qmake   <qmake6|qmake> <source_dir>/<tests.root_project> in <build_dir>; make -C <build_dir>
    custom  the argv lists build.configure / build.build / build.clean

Any adapter's commands can be overridden with ``build.configure``,
``build.build`` and ``build.clean`` (argv lists; ``{build_dir}``,
``{source_dir}`` and ``{jobs}`` are substituted).

Actions: ``configure``, ``build`` (configures first when the tree is not
configured), ``clean``, ``rebuild`` (clean + build), ``fresh`` (delete the
build tree, configure, build), ``info``, ``check`` (tools present).

Every action writes ``<build_dir>/build-results/<id>.json`` and
``latest.json``: the commands, exit codes, times, the log, and on failure the
failure class of docs/process/build-contract.md (SOURCE, LINK, DEPENDENCY,
TOOL, ENVIRONMENT, TIMEOUT, RESOURCE, UNKNOWN).

    python3 scripts/devsys_build.py build [--build-dir DIR] [--json]
    python3 scripts/devsys_build.py check

Exit: 0 success, 1 build failure, 10 not configured, 2 usage.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devsys_config as cfgmod  # noqa: E402

SYSTEMS = ("none", "make", "cmake", "qmake", "custom")

# docs/process/build-contract.md "Distinguish Failures" (checked in order).
FAILURE_PATTERNS = (
    ("TOOL", re.compile(r"command not found|No such file or directory: '(?:make|cmake|qmake6?|ninja)'|"
                        r"not found on PATH", re.I)),
    ("LINK", re.compile(r"undefined reference|unresolved external|ld returned \d+ exit status|"
                        r"cannot find -l|linker command failed", re.I)),
    ("DEPENDENCY", re.compile(r"Could not find a package|Could NOT find|Package .* was not found|"
                              r"fatal error: .*: No such file or directory|No rule to make target|"
                              r"Unknown module\(s\) in QT", re.I)),
    ("RESOURCE", re.compile(r"No space left on device|Cannot allocate memory|virtual memory exhausted|"
                            r"internal compiler error: Killed", re.I)),
    ("SOURCE", re.compile(r"\berror\b[:\s]|error C\d{4}", re.I)),
    ("ENVIRONMENT", re.compile(r"Permission denied|cannot open display|Read-only file system", re.I)),
)


def classify_failure(output: str, rc: int | None) -> str:
    if rc is None:
        return "TIMEOUT"
    for label, rx in FAILURE_PATTERNS:
        if rx.search(output):
            return label
    return "UNKNOWN"


def settings(root: Path, build_dir: str | None = None) -> dict:
    cfg = cfgmod.load(root)
    b = dict(cfg["build"])
    b["build_dir"] = build_dir or b.get("build_dir") or cfg["paths"]["build_dir"]
    b["jobs"] = int(b.get("jobs") or os.cpu_count() or 2)
    b["root_project"] = cfg["tests"]["root_project"]
    return b


def _qmake() -> str:
    return "qmake6" if shutil.which("qmake6") else "qmake"


def plan(root: Path, action: str, s: dict) -> list[dict]:
    """The commands an action runs: [{"argv": [...], "cwd": path}, ...]."""
    system = s["system"]
    src = str((root / s["source_dir"]).resolve())
    bdir = str((root / s["build_dir"]).resolve())

    def sub(argv):
        return [str(a).format(build_dir=bdir, source_dir=src, jobs=s["jobs"]) for a in argv]

    def override(key, default):
        return [{"argv": sub(s[key]), "cwd": str(root)}] if s.get(key) else default

    if system == "none":
        return []
    if system == "custom":
        return override(action, [])
    if system == "make":
        where = bdir if (Path(bdir) / "Makefile").exists() else src
        if action == "configure":
            return override("configure", [])
        if action == "build":
            return override("build", [{"argv": ["make", "-C", where, f"-j{s['jobs']}"], "cwd": str(root)}])
        if action == "clean":
            return override("clean", [{"argv": ["make", "-C", where, "clean"], "cwd": str(root)}])
    if system == "cmake":
        if action == "configure":
            return override("configure", [{"argv": ["cmake", "-S", src, "-B", bdir], "cwd": str(root)}])
        if action == "build":
            return override("build", [{"argv": ["cmake", "--build", bdir, "-j", str(s["jobs"])], "cwd": str(root)}])
        if action == "clean":
            return override("clean", [{"argv": ["cmake", "--build", bdir, "--target", "clean"], "cwd": str(root)}])
    if system == "qmake":
        if action == "configure":
            return override("configure", [{"argv": [_qmake(), str(Path(src) / s["root_project"])], "cwd": bdir}])
        if action == "build":
            return override("build", [{"argv": ["make", "-C", bdir, f"-j{s['jobs']}"], "cwd": str(root)}])
        if action == "clean":
            return override("clean", [{"argv": ["make", "-C", bdir, "clean"], "cwd": str(root)}])
    return []


def configured(root: Path, s: dict) -> bool:
    bdir = root / s["build_dir"]
    if s["system"] == "cmake":
        return (bdir / "CMakeCache.txt").exists()
    if s["system"] == "qmake":
        return (bdir / "Makefile").exists()
    return True


def check(root: Path, s: dict) -> list[str]:
    """Missing tools for the configured adapter (empty = ready)."""
    missing = []
    for action in ("configure", "build", "clean"):
        for step in plan(root, action, s):
            exe = step["argv"][0]
            if not (shutil.which(exe) or Path(exe).is_file()):
                missing.append(f"{action}: {exe} not found")
    return sorted(set(missing))


def run(root: Path, action: str, build_dir: str | None = None, timeout: float | None = None) -> dict:
    """Run ``action`` and write its build record; returns the record."""
    s = settings(root, build_dir)
    started = time.monotonic()
    record = {"id": f"{time.strftime('%Y%m%dT%H%M%S')}-{os.getpid()}", "action": action, "system": s["system"],
              "build_dir": s["build_dir"], "started_at": datetime.now(timezone.utc).isoformat(), "steps": [],
              "status": "PASS", "failure_class": None}
    bdir = root / s["build_dir"]
    if s["system"] == "none" or s["system"] not in SYSTEMS:
        record.update(status="NOT_CONFIGURED", detail="build.system is 'none' (devsys.json)")
        return record
    actions = {"configure": ["configure"], "clean": ["clean"],
               "build": (["build"] if configured(root, s) else ["configure", "build"]),
               "rebuild": ["clean", "build"], "fresh": ["configure", "build"]}.get(action)
    if actions is None:
        raise SystemExit(f"unknown action {action!r}")
    if action == "fresh" and bdir.exists():
        shutil.rmtree(bdir)
    bdir.mkdir(parents=True, exist_ok=True)
    timeout = float(timeout or s.get("timeout_seconds") or 3600)
    log_lines: list[str] = []
    for step_action in actions:
        for step in plan(root, step_action, s):
            t0 = time.monotonic()
            try:
                r = subprocess.run(step["argv"], cwd=step["cwd"], capture_output=True, text=True, timeout=timeout)
                rc, out = r.returncode, r.stdout + r.stderr
            except FileNotFoundError as e:
                rc, out = 127, f"{step['argv'][0]}: command not found ({e})"
            except subprocess.TimeoutExpired as e:
                rc, out = None, (e.stdout or "") if isinstance(e.stdout, str) else ""
            log_lines.append(f"$ {' '.join(step['argv'])}  (cwd {step['cwd']})\n{out}")
            record["steps"].append({"action": step_action, "argv": step["argv"], "rc": rc,
                                    "seconds": round(time.monotonic() - t0, 2)})
            if rc != 0:
                record["status"] = "FAIL"
                record["failure_class"] = classify_failure(out, rc)
                record["detail"] = next((l for l in out.splitlines() if re.search(r"error|not found|undefined", l, re.I)),
                                        out.strip().splitlines()[-1] if out.strip() else f"exit {rc}")[:300]
                break
        if record["status"] == "FAIL":
            break
    record["seconds"] = round(time.monotonic() - started, 2)
    results = bdir / "build-results"
    results.mkdir(parents=True, exist_ok=True)
    log = results / f"{record['id']}.log"
    log.write_text("\n".join(log_lines), encoding="utf-8")
    record["log"] = str(log.relative_to(root)) if log.is_relative_to(root) else str(log)
    text = json.dumps(record, indent=1)
    (results / f"{record['id']}.json").write_text(text, encoding="utf-8")
    (results / "latest.json").write_text(text, encoding="utf-8")
    return record


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=("configure", "build", "clean", "rebuild", "fresh", "info", "check"))
    ap.add_argument("--build-dir")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    root = cfgmod.repo_root()
    s = settings(root, a.build_dir)
    if a.action == "info":
        info = {"system": s["system"], "build_dir": s["build_dir"], "configured": configured(root, s),
                "plan": {act: plan(root, act, s) for act in ("configure", "build", "clean")}}
        print(json.dumps(info, indent=2))
        return 0 if s["system"] != "none" else cfgmod.NOT_CONFIGURED
    if a.action == "check":
        if s["system"] == "none":
            print("BUILD: NOT CONFIGURED (build.system is 'none')")
            return cfgmod.NOT_CONFIGURED
        missing = check(root, s)
        for m in missing:
            print(f"MISSING {m}")
        print("BUILD TOOLS: OK" if not missing else f"BUILD TOOLS: {len(missing)} missing")
        return 1 if missing else 0
    record = run(root, a.action, a.build_dir)
    if a.json:
        print(json.dumps(record, indent=2))
    else:
        for st in record["steps"]:
            print(f"[{st['action']}] rc={st['rc']} {st['seconds']}s  {' '.join(st['argv'])}")
        print(f"BUILD {a.action}: {record['status']}"
              + (f" ({record['failure_class']}: {record.get('detail')})" if record["status"] == "FAIL" else "")
              + (f" — {record.get('detail')}" if record["status"] == "NOT_CONFIGURED" else "")
              + (f"  log: {record.get('log')}" if record.get("log") else ""))
    return {"PASS": 0, "FAIL": 1, "NOT_CONFIGURED": cfgmod.NOT_CONFIGURED}.get(record["status"], 1)


if __name__ == "__main__":
    sys.exit(main())
