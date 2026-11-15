#!/usr/bin/env python3
"""Affected-test selection (REQ-DEVOPS-046, ADR-000186).

Which test suites can a change affect? Derived, not declared:

* **qmake projects.** Starting at the root project (``ROOT_PROJECT``), every ``SUBDIRS``
  project (nested ``subdirs`` projects included) is parsed together with the
  ``.pri`` files it ``include()``s, transitively. A file such a file
  references (``SOURCES``, ``HEADERS``, ``FORMS``, ...) is an input; a
  directory named by ``INCLUDEPATH`` contributes its headers only; any other
  referenced directory, a library ``.pri`` (one that declares
  ``PRE_TARGETDEPS``) and a ``$$OUT_PWD/.../lib*.a`` link make the whole
  library directory an input (its sources are what is linked). A changed file
  under a project's own directory or any input directory affects the project;
  declared ``.depends`` edges propagate that to dependants. Relative paths in a
  ``.pri`` are resolved against both the ``.pri`` and the including ``.pro``
  (qmake resolves against the ``.pro``): over-inclusion only runs more tests,
  under-inclusion would miss one.
* **Global build files** — the root project, its project-wide ``.pri``
  includes (``GLOBAL_BUILD_FILES``) and vendored ``third_party/`` code other than a
  project of its own — affect every project.
* **Suites** (``scripts/test_manifest.json``) name the project that builds
  their binary, the projects whose binaries they execute (``uses``) and the
  non-code inputs they read (``inputs`` globs: fixtures, Python services,
  scripts, a web front end).
* A changed path that no project and no suite claims, and that is not known
  documentation / process-record material, is **unmapped**: the caller must
  treat it as affecting everything (never silently as affecting nothing).

The module is pure (no process execution besides ``git ls-files``) so
``scripts/tests/test_run_tests.py`` can exercise it on fixture trees.
"""

from __future__ import annotations

import fnmatch
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent))
import devsys_config as cfgmod  # noqa: E402  (tests.* settings, ADR-000199)

# Project configuration (devsys.json "tests"): the root qmake project, the
# project-wide build files every project includes or is built from, and the
# paths that carry no code any test executes (documentation, process records;
# they select no suite, t0 still runs). Defaults: devsys_config.DEFAULTS.
ROOT_PROJECT = cfgmod.get("tests.root_project")
GLOBAL_BUILD_FILES = frozenset(cfgmod.get("tests.global_build_files"))
NO_TEST_PATTERNS = tuple(cfgmod.get("tests.no_test_patterns"))


def selection_mode(root: Path) -> str:
    """qmake (the project graph) or globs (suite ``inputs`` / ``sources``
    only): tests.selection, where "auto" means qmake when the root project
    exists."""
    mode = cfgmod.get("tests.selection", root, "auto")
    if mode == "auto":
        return "qmake" if (Path(root) / cfgmod.get("tests.root_project", root)).is_file() else "globs"
    return mode

_INCLUDE = re.compile(r"^\s*include\s*\(\s*([^)]+?)\s*\)", re.MULTILINE)
_SUBDIRS = re.compile(r"^\s*SUBDIRS\s*\+?=\s*(.*)$", re.MULTILINE)
_ASSIGN = re.compile(r"^\s*([A-Za-z0-9_]+)\.(file|subdir|depends)\s*\+?=\s*(.*)$", re.MULTILINE)
_PATH_TOKEN = re.compile(r"(\$\$PWD|\$\$_PRO_FILE_PWD_|\$\${PWD}|\.\.)(/[A-Za-z0-9_./+-]*)")
# $$OUT_PWD/../dir/libX.a in PRE_TARGETDEPS / LIBS: a shadow build mirrors the
# source layout, so the library's source directory is <project dir>/../dir.
_OUT_PWD_TOKEN = re.compile(r"\$\$OUT_PWD(/[A-Za-z0-9_./+-]*)")
_ASSIGN_OP = re.compile(r"([A-Za-z_][A-Za-z0-9_.]*)\s*(\+=|\*=|-=|~=|=)")
# Directories named by these variables are include paths: only their headers
# are inputs (a .cpp next to a header is compiled by the library that owns it).
HEADER_VARIABLES = frozenset({"INCLUDEPATH", "DEPENDPATH", "QMAKE_INCDIR"})
HEADER_SUFFIXES = frozenset({".h", ".hh", ".hpp", ".hxx", ".inl", ".ipp", ".tcc"})


def _clean(text: str) -> str:
    """Comments removed and backslash continuations joined."""
    lines = []
    for line in text.splitlines():
        hash_at = line.find("#")
        if hash_at >= 0:
            line = line[:hash_at]
        lines.append(line)
    return re.sub(r"\\\s*\n", " ", "\n".join(lines))


def _norm(path: PurePosixPath) -> str | None:
    """A repository-relative path, or None when it leaves the repository."""
    parts: list[str] = []
    for part in path.parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
        else:
            parts.append(part)
    return "/".join(parts)


@dataclass
class Project:
    pro: str                                   # repository-relative .pro
    directory: str                             # its directory
    files: set[str] = field(default_factory=set)        # .pro, included .pri files, referenced files
    input_dirs: set[str] = field(default_factory=set)   # directories whose contents it compiles / links
    header_dirs: set[str] = field(default_factory=set)  # include paths: only their headers are inputs
    depends: set[str] = field(default_factory=set)      # declared .depends (other .pro paths)
    global_input: bool = False                 # includes a global build file


class ProjectGraph:
    def __init__(self, root: Path, tracked: list[str] | None = None) -> None:
        self.root = root
        self.tracked = sorted(tracked if tracked is not None else tracked_files(root))
        self._tracked_set = set(self.tracked)
        self.projects: dict[str, Project] = {}
        self.leaf: set[str] = set()
        self.root_project = cfgmod.get("tests.root_project", root, ROOT_PROJECT)
        self.global_files = frozenset(cfgmod.get("tests.global_build_files", root, sorted(GLOBAL_BUILD_FILES)))
        if selection_mode(root) == "qmake":
            self._load_subdirs(self.root_project, parent_deps={})

    # -- parsing ------------------------------------------------------
    def _exists(self, rel: str) -> bool:
        if rel in self._tracked_set:
            return True
        prefix = rel.rstrip("/") + "/"
        index = self._bisect(prefix)
        return index < len(self.tracked) and self.tracked[index].startswith(prefix)

    def _bisect(self, prefix: str) -> int:
        lo, hi = 0, len(self.tracked)
        while lo < hi:
            mid = (lo + hi) // 2
            if self.tracked[mid] < prefix:
                lo = mid + 1
            else:
                hi = mid
        return lo

    def _read(self, rel: str) -> str:
        try:
            return _clean((self.root / rel).read_text(encoding="utf-8", errors="replace"))
        except OSError:
            return ""

    def _load_subdirs(self, pro: str, parent_deps: dict[str, set[str]]) -> None:
        text = self._read(pro)
        base = PurePosixPath(pro).parent
        names: list[str] = []
        for match in _SUBDIRS.finditer(text):
            names += match.group(1).split()
        files: dict[str, str] = {}
        depends: dict[str, set[str]] = {}
        for match in _ASSIGN.finditer(text):
            name, kind, value = match.group(1), match.group(2), match.group(3).strip()
            if kind == "file":
                files[name] = value
            elif kind == "subdir":
                files[name] = f"{value}/{PurePosixPath(value).name}.pro"
            else:
                depends.setdefault(name, set()).update(value.split())
        resolved: dict[str, str] = {}
        for name in names:
            rel = files.get(name) or f"{name}/{PurePosixPath(name).name}.pro"
            path = _norm(base / rel)
            if path and path in self._tracked_set:
                resolved[name] = path
        for name, path in resolved.items():
            deps = {resolved[d] for d in depends.get(name, ()) if d in resolved}
            deps |= parent_deps.get(path, set())
            sub_text = self._read(path)
            if re.search(r"^\s*TEMPLATE\s*=\s*subdirs", sub_text, re.MULTILINE):
                # A nested subdirs project: its children inherit its dependencies.
                self._load_subdirs(path, parent_deps={child: deps for child in self._children(path)})
                continue
            project = self.projects.get(path) or Project(pro=path, directory=str(PurePosixPath(path).parent))
            project.depends |= deps
            self.projects[path] = project
            self.leaf.add(path)
            self._collect(project, path, set())

    def _children(self, pro: str) -> list[str]:
        text = self._read(pro)
        base = PurePosixPath(pro).parent
        names: list[str] = []
        for match in _SUBDIRS.finditer(text):
            names += match.group(1).split()
        files = {m.group(1): m.group(3).strip() for m in _ASSIGN.finditer(text) if m.group(2) == "file"}
        out = []
        for name in names:
            path = _norm(base / (files.get(name) or f"{name}/{PurePosixPath(name).name}.pro"))
            if path:
                out.append(path)
        return out

    def _resolve(self, token_dir: str, pro_dir: str, raw: str) -> list[str]:
        raw = raw.strip().strip('"')
        out = []
        if raw.startswith(("$$PWD", "$${PWD}")):
            rest = raw.split("/", 1)[1] if "/" in raw else ""
            candidates = [PurePosixPath(token_dir) / rest]
        elif raw.startswith("$$_PRO_FILE_PWD_"):
            rest = raw.split("/", 1)[1] if "/" in raw else ""
            candidates = [PurePosixPath(pro_dir) / rest]
        elif "$$" in raw or raw.startswith("/"):
            return []
        else:
            candidates = [PurePosixPath(token_dir) / raw, PurePosixPath(pro_dir) / raw]
        for candidate in candidates:
            rel = _norm(candidate)
            if rel is not None and rel != "" and self._exists(rel):
                out.append(rel)
        return out

    def _collect(self, project: Project, qmake_file: str, seen: set[str]) -> None:
        if qmake_file in seen:
            return
        seen.add(qmake_file)
        project.files.add(qmake_file)
        if qmake_file in self.global_files:
            project.global_input = True
            return
        text = self._read(qmake_file)
        file_dir = str(PurePosixPath(qmake_file).parent)
        for match in _INCLUDE.finditer(text):
            for rel in self._resolve(file_dir, project.directory, match.group(1)):
                if rel in self.global_files:
                    project.global_input = True
                    project.files.add(rel)
                    continue
                self._collect(project, rel, seen)
        # A .pri that declares PRE_TARGETDEPS links the library its directory
        # builds: every file of that library is an input of the project.
        if qmake_file != project.pro and re.search(r"PRE_TARGETDEPS\s*\+?=", text):
            pri_dir = str(PurePosixPath(qmake_file).parent)
            if pri_dir not in ("", "."):
                project.input_dirs.add(pri_dir)
        for line in text.splitlines():
            operators = list(_ASSIGN_OP.finditer(line))
            variable = operators[-1].group(1).split(".")[-1] if operators else ""
            header_only = variable in HEADER_VARIABLES
            tokens = [m.group(1) + m.group(2) for m in _PATH_TOKEN.finditer(line)]
            if variable in ("PRE_TARGETDEPS", "LIBS"):
                for m in _OUT_PWD_TOKEN.finditer(line):
                    rel = _norm(PurePosixPath(project.directory) / m.group(1).lstrip("/"))
                    if rel:
                        library_dir = str(PurePosixPath(rel).parent)
                        if library_dir not in ("", ".") and self._exists(library_dir):
                            project.input_dirs.add(library_dir)
            for token in tokens:
                for rel in self._resolve(file_dir, project.directory, token):
                    if rel in self._tracked_set:
                        project.files.add(rel)          # an exact file (SOURCES, HEADERS, FORMS, ...)
                    elif header_only:
                        project.header_dirs.add(rel)
                    elif rel not in ("", "."):
                        project.input_dirs.add(rel)

    # -- queries ------------------------------------------------------
    def _owner_directory(self, path: str) -> str:
        """The deepest project directory that contains path ('' if none)."""
        if not hasattr(self, "_project_dirs"):
            self._project_dirs = sorted({p.directory for p in self.projects.values()}, key=len, reverse=True)
        for directory in self._project_dirs:
            if directory and path.startswith(directory + "/"):
                return directory
        return ""

    def owns(self, project: Project, path: str) -> bool:
        if path in project.files:
            return True
        owner = self._owner_directory(path)
        for directory in (project.directory, *project.input_dirs):
            if directory and (path == directory or path.startswith(directory + "/")):
                # A file inside another project nested under this input
                # directory (core/tests inside core) belongs to that
                # project, not to every project that links core.
                if owner and owner != project.directory and len(owner) > len(directory):
                    continue
                return True
        if PurePosixPath(path).suffix in HEADER_SUFFIXES:
            for directory in project.header_dirs:
                if path.startswith(directory + "/"):
                    return True
        return False

    def is_global(self, path: str) -> bool:
        if path in self.global_files:
            return True
        if path.startswith("third_party/"):
            # A vendored tree that is a project of its own
            # affects its dependants only; any other vendored library is
            # linked by many projects.
            return not any(self.owns(p, path) for p in self.projects.values()
                           if p.directory.startswith("third_party/"))
        return False

    def affected_projects(self, changed: list[str]) -> tuple[set[str], set[str]]:
        """(affected .pro paths, changed paths that affect at least one)."""
        affected: set[str] = set()
        claimed: set[str] = set()
        for path in changed:
            if self.is_global(path):
                affected |= set(self.leaf)
                claimed.add(path)
                continue
            for pro, project in self.projects.items():
                if self.owns(project, path):
                    affected.add(pro)
                    claimed.add(path)
        # Declared .depends: a dependant of an affected project is affected.
        changed_set = True
        while changed_set:
            changed_set = False
            for pro, project in self.projects.items():
                if pro not in affected and project.depends & affected:
                    affected.add(pro)
                    changed_set = True
        return affected, claimed

    def project_files(self, pro: str) -> list[str]:
        """Tracked files that are inputs of a project (for build freshness)."""
        project = self.projects[pro]
        out = set(project.files)
        for directory in (project.directory, *project.input_dirs, *project.header_dirs):
            prefix = directory.rstrip("/") + "/"
            index = self._bisect(prefix)
            while index < len(self.tracked) and self.tracked[index].startswith(prefix):
                if self.owns(project, self.tracked[index]):
                    out.add(self.tracked[index])
                index += 1
        for dep in project.depends:
            if dep in self.projects:
                out.update(self.projects[dep].files)
        return sorted(out)


def tracked_files(root: Path) -> list[str]:
    """Tracked files plus new files git would track (not ignored)."""
    try:
        out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                             cwd=root, capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return []
    return [line for line in out.splitlines() if line]


def matches(path: str, patterns) -> bool:
    for pattern in patterns:
        if fnmatch.fnmatchcase(path, pattern):
            return True
        if pattern.endswith("/**") and (path == pattern[:-3] or path.startswith(pattern[:-2])):
            return True
    return False


def is_no_test_path(path: str, root: Path | None = None) -> bool:
    return matches(path, cfgmod.get("tests.no_test_patterns", root, list(NO_TEST_PATTERNS)))


def select_suites(graph: ProjectGraph, suites: list[dict], changed: list[str]) -> tuple[dict[str, list[str]], list[str]]:
    """{suite id: [reasons]} for the suites the changed paths affect, and the
    unmapped changed paths (no project, no suite input, not documentation)."""
    affected, claimed = graph.affected_projects(changed)
    selected: dict[str, list[str]] = {}
    for suite in suites:
        reasons = []
        for key in ("project", *(("uses",) if suite.get("uses") else ())):
            values = suite.get(key)
            for pro in ([values] if isinstance(values, str) else (values or [])):
                if pro in affected:
                    reasons.append(f"{'builds' if key == 'project' else 'runs'} {pro}")
        inputs = suite.get("inputs") or []
        hits = [p for p in changed if matches(p, inputs)]
        if hits:
            claimed.update(hits)
            reasons.append(f"input {hits[0]}" + (f" (+{len(hits) - 1})" if len(hits) > 1 else ""))
        # Glob mode (and any build system): the code a suite tests, declared
        # as "sources" globs, selects it like its project would.
        src_hits = [p for p in changed if matches(p, suite.get("sources") or [])]
        if src_hits:
            claimed.update(src_hits)
            reasons.append(f"source {src_hits[0]}" + (f" (+{len(src_hits) - 1})" if len(src_hits) > 1 else ""))
        if reasons:
            selected[suite["id"]] = reasons
    unmapped = [p for p in changed if p not in claimed and not is_no_test_path(p)]
    return selected, unmapped
