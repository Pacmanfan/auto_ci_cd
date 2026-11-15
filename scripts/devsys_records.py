#!/usr/bin/env python3
"""Record formats of the development-control system (ADR-000199): one
definition of how requests, work records, evidence records, ADRs and the id
manifests are read and written, shared by the gate, the lifecycle tools
(scripts/work.py, scripts/evidence.py) and the drift check.

Record syntax (docs/process/configuration.md "Record formats"):

* Markdown records carry ``**Field:** value`` header lines (Status, Request,
  Requirements, Decision, Evidence, Work, Test-Run, Test-Suites, Type ...).
* The id manifests are pipe-separated: ``ID | status | ... | record | ...``.
* Requirement ids are upper-case dash-separated namespaces ending in a
  number (``REQ-DEVOPS-001``, ``CORE-QT-004``); ``X-001..004`` is a range.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devsys_config as cfgmod  # noqa: E402

REQ_ID = r"[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*-\d{2,4}[a-z]?"
REQ_TOKEN = re.compile(r"\b(" + REQ_ID + r")(?:\.\.(\d{2,4}))?\b")
DEFINITION = (
    re.compile(r"^#{2,6}\s+\**`?(" + REQ_ID + r")(?:\.\.(\d{2,4}))?"),      # ### REQ-X-001 — ...
    re.compile(r"^\|\s*\**`?(" + REQ_ID + r")(?:\.\.(\d{2,4}))?"),          # | REQ-X-001 | ...
    re.compile(r"^(?:[*-]\s+)?\*\*(" + REQ_ID + r")(?:\.\.(\d{2,4}))?"),    # **REQ-X-001** ...
)
_NUMBER_TAIL = re.compile(r"^(.*-)(\d{2,4})([a-z]?)$")
# Ids of the framework's own record kinds are never requirement ids.
RECORD_ID = re.compile(r"^(?:WORK|VER|ADR|INT|AUDIT|TEST)-|^REQ-[A-Z]+-\d{8}")


def expand(start: str, end: str | None) -> list[str]:
    """REQ-X-001..004 -> the four ids; a single id -> itself."""
    m = _NUMBER_TAIL.match(start)
    if not end or not m or m.group(3):
        return [start]
    first, last = int(m.group(2)), int(end)
    if last < first or last - first > 200:
        return [start]
    width = len(m.group(2))
    return [f"{m.group(1)}{n:0{width}d}" for n in range(first, last + 1)]


def header(text: str, field: str) -> str | None:
    """Value of the first ``**Field:**`` line (``Field`` may be a regex
    alternative such as ``Requirements?``)."""
    m = re.search(rf"^\s*\*\*(?:{field}):\*\*\s*(.*)$", text, re.MULTILINE)
    return m.group(1).strip() if m else None


def set_header(text: str, field: str, value: str) -> str:
    """Replace the first ``**Field:**`` line, or add it after the title."""
    rx = re.compile(rf"^(\s*\*\*{re.escape(field)}:\*\*).*$", re.MULTILINE)
    if rx.search(text):
        return rx.sub(lambda m: f"{m.group(1)} {value}", text, count=1)
    lines = text.splitlines(keepends=True)
    insert = 1 if lines and lines[0].startswith("#") else 0
    while insert < len(lines) and not lines[insert].strip():
        insert += 1
    lines.insert(insert, f"**{field}:** {value}\n")
    return "".join(lines)


def status_token(text: str) -> str | None:
    value = header(text, "Status")
    if value is None:
        m = re.search(r"^\s*Status:\s*(.+)$", text, re.MULTILINE | re.IGNORECASE)
        value = m.group(1) if m else None
    if not value:
        return None
    return re.sub(r"[^a-z\-]", "", value.split()[0].lower().strip("*_`")) or None


def cited_requirements(text: str) -> list[str]:
    """Requirement ids a record cites: its ``**Requirement(s):**`` header, or
    (no header) every requirement-shaped id in the record."""
    source = header(text, "Requirements?") or ""
    if not source:
        source = text
    out = []
    for m in REQ_TOKEN.finditer(source):
        if RECORD_ID.match(m.group(1)):
            continue
        for rid in expand(m.group(1), m.group(2)):
            if rid not in out:
                out.append(rid)
    return out


def defined_requirements(root: Path) -> dict[str, str]:
    """Requirement id -> defining document (docs/requirements/*.md)."""
    defined: dict[str, str] = {}
    base = cfgmod.path(root, "requirements")
    for doc in sorted(base.glob("*.md")):
        if doc.name == "README.md":
            continue
        for line in doc.read_text(encoding="utf-8", errors="ignore").splitlines():
            for pattern in DEFINITION:
                m = pattern.match(line)
                if m:
                    for rid in expand(m.group(1), m.group(2)):
                        defined.setdefault(rid, doc.relative_to(root).as_posix())
                    break
    return defined


def known_decisions(root: Path) -> set[str]:
    """ADR ids with a record file or a decisions.txt row."""
    ids = set()
    base = cfgmod.path(root, "decisions")
    for p in base.glob("ADR-*.md"):
        m = re.match(r"ADR-\d{6}", p.name)
        if m:
            ids.add(m.group(0))
    manifest = cfgmod.path(root, "decisions_manifest")
    if manifest.exists():
        for line in manifest.read_text(encoding="utf-8", errors="ignore").splitlines():
            m = re.match(r"\s*(ADR-\d{6})\s*\|", line)
            if m:
                ids.add(m.group(1))
    return ids


def manifest_rows(path: Path) -> list[list[str]]:
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("Format") or "|" not in s:
            continue
        rows.append([c.strip() for c in s.split("|")])
    return rows


def manifest_row(path: Path, record_id: str) -> list[str] | None:
    for row in manifest_rows(path):
        if row and row[0].split()[0] == record_id:
            return row
    return None


def upsert_manifest_row(text: str, record_id: str, row: str) -> str:
    """Replace the row whose first column is ``record_id``, or append it.

    Every other line is kept byte for byte: a shared manifest is committed
    per line naming the request's ids (scripts/commit_request.py), so a
    reformatted header or a dropped blank line would stay behind as an
    uncommitted change no request owns."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        first = line.split("|")[0].strip()
        if "|" in line and first.split()[:1] == [record_id]:
            lines[i] = row
            return "\n".join(lines) + "\n"
    if text and not text.endswith("\n"):
        text += "\n"
    return text + row + "\n"


def slug(text: str, length: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:length].strip("-") or "record"
