#!/usr/bin/env python3
"""Protected development-system artifacts (REQ-DEVOPS-023, ADR-000185).

The artifact list of docs/process/process-protection.md, and the one question
both enforcement points ask: which protected artifacts has a request's claim
registered (``scripts/claim.py --own``)? Registration is the ownership record
in a shared checkout; a dirty protected file may be another session's work, so
the working tree alone cannot answer it.

Users:
  scripts/completion_gate.py      fails a work record whose request registered
                                  a protected artifact unless the record is
                                  Type: process-change with a Decision ADR
  scripts/invariant_check.py      PROTECTED_FILES_TOUCHED_WITHOUT_PROCESS_CHANGE
                                  over every processing claim (FAIL with
                                  --strict, WARN without)
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_process  # noqa: E402
import devsys_config as cfgmod  # noqa: E402

# docs/process/process-protection.md "Protected Artifacts": the explicit files,
# configured as protected_artifacts in devsys.json (default list in
# devsys_config.DEFAULTS; the configuration file itself is protected too).
PROTECTED_ARTIFACTS = tuple(cfgmod.DEFAULTS["protected_artifacts"])


def protected_artifacts(root: Path | None = None) -> tuple:
    return tuple(cfgmod.get("protected_artifacts", root, list(PROTECTED_ARTIFACTS)))

PROCESS_CHANGE_TYPE = "PROCESS-CHANGE"


def _claims_dir(root: Path) -> Path:
    # agent_process.claims_dir() creates the directory; a check only reads.
    return agent_process.state_dir(root) / "claims"


def claim_for_request(root: Path, request_file: str) -> dict | None:
    """The claim of the request whose file name is ``request_file`` (any state
    directory prefix is ignored: a request moves, its claim does not)."""
    name = Path(request_file).name
    return agent_process.read_claim(_claims_dir(root) / f"request-{name}.lock")


def protected_paths(claim: dict | None, root: Path | None = None) -> list[str]:
    """The protected artifacts a claim has registered, in list order. The
    artifacts are named relative to the system directory; a claim registers
    repository paths (``.devsys/AGENTS.md`` in a subdirectory installation)."""
    base = root if root is not None else cfgmod.repo_root()
    owned = {agent_process.system_relative(base, p) for p in (claim or {}).get("files") or {}}
    return [p for p in protected_artifacts(root) if p in owned]


def request_type(root: Path, claim: dict) -> str:
    """The request's stored type from its sidecar, upper-case, or ''."""
    name = claim.get("target") or Path(claim.get("request_path") or "").name
    for _state, md in agent_process.locate_request(root, name):
        try:
            return str(json.loads(md.with_suffix(".json").read_text(encoding="utf-8")).get("type") or "").upper()
        except (OSError, ValueError):
            continue
    return ""


def processing_claims_touching_protected(root: Path):
    """(request id, request type, [protected paths]) for every processing claim
    that has registered a protected artifact."""
    out = []
    for lock in sorted(_claims_dir(root).glob("request-*.lock")):
        claim = agent_process.read_claim(lock)
        if not claim or claim.get("state") != "processing":
            continue
        paths = protected_paths(claim, root)
        if paths:
            claim.setdefault("target", lock.name[len("request-"):-len(".lock")])
            out.append((claim.get("request_id") or claim["target"], request_type(root, claim), paths))
    return out


_TYPE = re.compile(r"^\s*\*\*Type:\*\*\s*(.+)$", re.MULTILINE)
_DECISION = re.compile(r"^\s*\*\*Decisions?:\*\*\s*(.+)$", re.MULTILINE)


def is_process_change_record(work_text: str) -> bool:
    """A work record declared as a process change with a decision record:
    ``**Type:** process-change`` and a ``**Decision:**`` header naming an ADR
    (docs/process/process-protection.md, required mechanism steps 2-3)."""
    t = _TYPE.search(work_text)
    d = _DECISION.search(work_text)
    return bool(t and "process-change" in t.group(1).lower() and d and re.search(r"ADR-\d{6}", d.group(1)))
