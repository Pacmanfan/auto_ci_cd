# Project Configuration (`devsys.json`)

**Decision:** ADR-000199  **Requirement:** REQ-DEVOPS-047
**Code:** `scripts/devsys_config.py` (defaults, loading, validation)

Everything project-specific about the development-control system is
configuration. `devsys.json` at the repository root holds only what differs
from the defaults. It is deep-merged over `DEFAULTS` in
`scripts/devsys_config.py`: dictionaries merge key by key, and any other
value replaces the default. Keys starting with `_` are ignored, and serve as
comments and examples.

```bash
python3 scripts/devsys_config.py                 # effective configuration (JSON)
python3 scripts/devsys_config.py --get build.system
python3 scripts/devsys_config.py --validate      # unknown keys, invalid values, invalid regexes (exit 1)
python3 scripts/devsys.py doctor                 # environment + validity + what is still NOT CONFIGURED
```

`devsys.json` is a protected artifact. Change it through a PROCESS-CHANGE
request (`docs/process/process-protection.md`).

## Bootstrapping a project

```bash
python3 <framework>/scripts/devsys.py install --target /path/to/project \
        --project-name myproj --build-system make [--git-init]
cd /path/to/project
python3 scripts/devsys.py doctor
```

`install` copies the framework: the scripts, the audit tool, the process
documents, the framework's own ADRs, templates and READMEs. It does not copy
an instance's requests, work, evidence or gate records. The instance files
(`devsys.json`, `scripts/test_manifest.json`,
`scripts/test_quarantine.json`, `.audit/manifest.json`, `requirements.txt`,
`decisions.txt`) are written from `scripts/templates/`, never from the
instance `install` runs in, so installing from an already configured project
is safe (`scripts/templates/README.md`). Existing files are kept unless
`--force` is given. It then runs `init`, which creates:

- the state directories and their READMEs;
- the manifests;
- `devsys.json`, or updates only the name and build system of an existing
  one;
- the `.gitignore` entries: runtime state, the build tree, the scratch area.

A fresh instance's id series start at 1.

After that, configure the project's own settings (below). Register its test
suites in `scripts/test_manifest.json` and its requirements in
`docs/requirements/`. `doctor` lists what is still NOT CONFIGURED; NOT
CONFIGURED is never reported as PASS.

## Settings

| Key | Default | Meaning |
|---|---|---|
| `project.name` | `<PROJECT>` | shown by `devsys.py status` |
| `project.source_roots` | `[]` | the project's source directories: the files an invariant applies to when it names none; must exist |
| `paths.scratch` | `tmp` | scratch area (AGENTS.md "Temporary Work"); per-run test scratch lives under it |
| `paths.project_root` | `.` | the project this system serves, relative to the system directory: `.` when the system is the project root, `..` for `<project>/<system-directory>/`; workers run their agents there (must be a directory) |
| `paths.build_dir` | `build/debug` | the build tree of builds, test runs and run records; may be absolute |
| `paths.crash_dumps` | `log/dump` | where crash evidence looks for cores |
| `claims.stale_hours` | `2.0` | an unverifiable claim silent this long is STALE |
| `claims.dead_grace_minutes` | `5.0` | a verified-gone owner is DEAD only after this much silence |
| `claims.lock_timeout_seconds` | `60` | waiting for an inter-process lock; then exit 8 BUSY |
| `claims.heartbeat_interval_seconds` | `300` | refresh period of `claim.py --run` and request-scoped test runs |
| `ids.request_sources` | chat `REQ-CHAT`, inbox `REQ-IN`, crash `REQ-CRASH`, automation `REQ-AUTO` | id prefix per intake source (`REQ-<LETTERS>`) |
| `ids.scan_git_refs` | `true` | never reissue an id that exists only on a fetched branch |
| `intake.refuse_duplicates` | `true` | refuse a request identical to an open one (exit 7) |
| `protected_artifacts` | AGENTS.md, devsys.json, the process documents | changed only by PROCESS-CHANGE requests |
| `completion.enforce_on_transition` | `true` | completion paths require a PASS gate record (off: convention only) |
| `completion.exempt_types` | `["INVESTIGATION"]` | request types that may complete without work, with a reason |
| `completion.min_tier` | `t2` | lowest tier a code change's linked run must include |
| `completion.passing_evidence_statuses` | `pass`, `pre-existing` | evidence statuses that satisfy the gate |
| `completion.require_*` | `true` | gate rules: requirement, request link, decision records, test run, drift clean, doc integrity clean |
| `completion.require_audit_profile` | `null` | `quick` / `standard` / `baseline`: the gate needs an audit run of that profile (or a deeper one) on HEAD with no FAIL check |
| `build.system` | `none` | `none`, `make`, `cmake`, `qmake`, `custom` (`docs/process/build-contract.md`) |
| `build.source_dir` / `build.build_dir` / `build.jobs` | `.` / `paths.build_dir` / CPU count | adapter inputs |
| `build.configure` / `build.build` / `build.clean` | `null` | argv lists replacing the adapter's commands; `{build_dir}`, `{source_dir}`, `{jobs}` substituted |
| `build.timeout_seconds` | `3600` | per build step; then TIMEOUT |
| `build.generated` | `[]` | `[{"sources": [globs], "outputs": [globs]}]`; stale outputs are warned by `build_health.py` |
| `tests.manifest` / `tests.quarantine` | `scripts/test_manifest.json` / `scripts/test_quarantine.json` | suite registry, known failures |
| `tests.selection` | `auto` | `qmake` (project graph), `globs` (suite `sources` / `inputs`), `auto` (qmake when `tests.root_project` exists) |
| `tests.root_project` / `tests.global_build_files` | `project.pro` / qmake globals | qmake selection only |
| `tests.no_test_patterns` | docs, records, editor files | changes that need no test run |
| `tests.reject_qt5_makefiles` | `false` | warn about Qt 5-generated Makefiles in the tree (source-project policy) |
| `tests.scrubbed_env` | `[]` | environment variables removed from test processes |
| `tests.requirement_env` | testdata `PROJECT_TEST_DATA`, hardware `PROJECT_LIVE_HW_ARGS` | variable that opts a suite requirement in |
| `doc_integrity.fail_codes` | `["BROKEN_LINK"]` | finding codes that FAIL (others WARN) |
| `doc_integrity.stale_paths` / `obsolete_terms` | former doc locations / `[]` | what current docs must not cite |
| `invariants` | `[]` | architecture rules (below); empty is NOT CONFIGURED |
| `preflight.subsystem_keywords` | `[]` | words that mark a request as touching a subsystem |
| `requirement_impact.domains` | development | domain → path keywords for `requirement_impact.py` |
| `crash.*` | `*.core`, `log/app.log` | core glob, fallback dump directory, logs, debug binary for `crash_evidence.py` |
| `drift.unverified_completion` | `FAIL` | level of completions without the tooling's record (`WARN` / `OFF` while adopting history) |
| `workers.*` | 2 workers, shared isolation, no agent | the Worker Orchestrator: `max_workers`, `isolation` (`shared` / `worktree`), `poll_seconds`, `attempt.*` (runtime, attempts per request, kill grace, consecutive failures, usage-limit wait), `selection.exclude_types`, `agent.command` / `env_scrub` / `rate_limit_patterns` / `prompt`, `profiles` (named agent profiles overriding `agent`) and `assign` (worker slot -> profile), `validation` steps; `docs/process/worker-orchestrator.md` §4, `docs/process/agent-profiles.md` |
| `health.structure_timeout_seconds` | `300` | budget of each health check; a timeout is UNKNOWN |
| `structure.required_paths` / `required_dirs` / `extra_checks` | the framework scaffold / `[]` | what `verify-structure` requires; extra argv checks must exit 0 |

## Architecture rules

`invariants` is a list of rules, checked by `scripts/invariant_check.py`
and by the audit's `arch.rules` check. Every rule has an `id`, a `kind`, an
optional `message`, and a `severity` (`fail` by default, or `warn`). File
lists are repository-relative globs; `dir/**` matches everything below
`dir`. A pattern rule without `files`, or a dependency rule without `from`,
applies to `project.source_roots`.

```json
"invariants": [
  {"id": "core-ui-free", "kind": "forbid_pattern", "files": ["src/core/**"],
   "pattern": "#include\\s*<Q", "message": "the core library stays UI-framework-free"},
  {"id": "plugins-registered", "kind": "require_pattern", "files": ["src/plugins/*.cpp"],
   "pattern": "REGISTER_PLUGIN", "min_matches": 3},
  {"id": "lib-independent", "kind": "forbid_dependency", "from": ["lib/**"], "to": ["app/", "ui/"]},
  {"id": "layering", "kind": "layers", "layers": [
     {"name": "app",  "files": ["src/app/**"],  "names": ["app/"]},
     {"name": "core", "files": ["src/core/**"], "names": ["core/"]}]}
]
```

What each kind checks:

- **`forbid_pattern`:** no line of a matching file may match `pattern`.
- **`require_pattern`:** at least `min_matches` matching lines (default 1)
  across the matching files.
- **`forbid_dependency`:** a reference line (`reference`, a regex; by default
  `#include`, `import`, `from`, `use`, `require`) in a `from` file may not
  name a `to` token.
- **`layers`:** the layers are listed top-down, and a lower layer's reference
  lines may not name a higher layer's `names`.

## Record formats

The tools read and write records through `scripts/devsys_records.py`:

- **Markdown records** carry `**Field:** value` header lines, such as:
  - `**Status:**`, `**Request:**`, `**Requirements:**`;
  - `**Decision:**`, `**Evidence:**`, `**Work:**`;
  - `**Test-Run:**`, `**Test-Suites:**`, `**Type:**`.
- **Manifests** (`work/work.txt`, `evidence/evidence.txt`,
  `docs/decisions/decisions.txt`, `docs/requirements/requirements.txt`) are
  pipe-separated, with the id first. The tools change only the line of the
  id they write.
- **Requirement ids** are upper-case, dash-separated namespaces ending in a
  number (`REQ-DEVOPS-001`, `CORE-QT-004`); `X-001..004` is a range. A
  requirement is *defined* by:
  - a heading (`### ID — title`);
  - a table row (`| ID | ... |`);
  - a bold lead (`**ID** ...`).

## Adopting an existing instance

An instance whose history predates the gate records can adopt the new
checks gradually:

```json
"completion": {"enforce_on_transition": false},
"drift": {"unverified_completion": "WARN"}
```

Turn both back to the defaults once old completions are either recorded or
accepted.

## Project-subdirectory installation

The system is designed to live, eventually, as one directory inside the
project it serves (`<project>/<system-directory>/`; the name is not fixed).
Everything it does is derived from its own location. `paths.project_root`
names the project relative to it. Today:

- **Relative to their own directory:** the scripts, records (requests,
  work, evidence, requirements, decisions), configuration and runtime
  state.
- **Relative to `paths.project_root`:** workers run their agents in the
  project root, or in a request's worktree. The prompt and `DEVSYS_SCRIPTS`
  name the tools relative to the agent's working directory.
- **Not yet project-relative:** claims, owned-path commits and test
  selection compute repository paths relative to the system directory.
  With `paths.project_root: ".."` inside a larger git repository, those
  paths need to become relative to the git top level. That is the remaining
  step, and it is not done (`CI_CD.md` §11).
