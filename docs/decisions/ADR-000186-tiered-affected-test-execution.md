# ADR-000186: Tiered, affected-test execution; package verification is build → package → verify → deploy → deployment smoke

**Status:** ACCEPTED (human review 2026-09-23: the user asked for the change — "process REQ-CHAT-20260923-015 and address all identified issues")
**Date:** 2026-09-23
**Work:** WORK-000305 (REQ-CHAT-20260923-015, process-change)
**Requirements:** `docs/requirements/development_operations.md` REQ-DEVOPS-046 (new), REQ-DEVOPS-009 (amended)

## Context

A read-only audit of the build, test, packaging and deployment flow
(REQ-CHAT-20260923-015, baseline in WORK-000305) found:

* **Nothing triggers tests.** No CI, no git hooks (and `scripts/commit_request.py`
  commits through `commit-tree`, which runs none), no `make check`. Every run is
  an agent's command; the completion gate only looked for the words PASS / FAIL
  in a VER record.
* **Selection by habit.** Almost every work item ran the same four suites
  whatever it changed; the application's automation test suite (~10 min) ran ~19 times a day,
  about once per commit, while 7 of the 12 C++ test binaries (the peer
  applications and a utility library) appeared in 1–6 of the last 60 VER
  records although they link the core libraries. The whole C++ set takes
  ~27 min run one after another; nothing ran in parallel; nothing was cached.
* **Results that could not be trusted.** the GUI test runner read only `argv[1]` (a
  documented `--automation --filter X` ran everything), its default suite was
  not a superset of `--automation`, it had no watchdog; `--unit --integration`
  ran the integration group only; tests wrote the real `~/.local/share/<binary>`
  and fixed `/tmp` / working-directory-relative scratch files (the HDF5 cases
  failed whenever the runner was not started from the repository root); a shared
  build tree relinked mid-run produced 9 spurious CLI-test failures.
* **Deployment verification that verified nothing.** the deployment script ran
  the package validation twice, built the archive twice, ran linuxdeployqt
  (which refuses this host) every time, and its smoke tests asserted nothing:
  `<app> --help` was not an option, so it started the full application on the
  user's real profile (the user's application database was written) and
  scanned real hardware until a timeout whose exit status was ignored; the
  smoke script's test call never ran (relative path after `cd`). The smoke
  requirements were not implemented. 12 documentation screenshot
  scenarios failed (the release build did not build two peer applications,
  one scenario was out of date) and strict documentation
  validation still passed on images captured weeks earlier.

## Decision

### 1. One orchestrator, derived selection, tiers

`scripts/run_tests.py` runs the suites registered in
`scripts/test_manifest.json`: a suite is one runner, a group one invocation.
Tiers are cumulative — **t0** static and process checks (always), **t1** + the
unit groups of the affected suites, **t2** + their integration / GUI groups
(the gate for completing a work item), **t3** every suite and group (nightly /
pre-release, on the Debug and the Release tree). **perf, stress, soak, live,
package** are explicit lanes, never implied by a tier.

*Affected* is derived, not declared: `scripts/test_selection.py` parses the
qmake project tree (the root project's SUBDIRS, nested subdirs, every `.pri`
included, every path referenced, `.depends`) into each project's input
directories; a changed path selects the projects whose inputs contain it
(a project nested in another's directory owns its own files) and the suites
that build or run them, plus suites whose declared non-code `inputs` match.
Global build files affect everything; a path nothing claims (and that is not
documentation or a process record) selects everything — never nothing.

### 2. The least time for that coverage

* **Cache.** A group whose binary (GNU build id), executed binaries, inputs
  and arguments equal its last passing run is reported CACHED, not re-run.
  Because the build is incremental, an unchanged test binary keeps its build
  id: this is the dependency-exact half of "affected".
* **Parallel.** Groups are split into shards (`PROJECT_TEST_SHARD=i/N`, in the
  shared runner frameworks) and run concurrently, longest first. The full t2
  set runs in ~5 min wall instead of ~27 min serial (VER-000305).
* **Performance assertions are not regression tests.** They run in the perf
  lane, alone (`exclusive`), never under the load of a parallel run.

### 3. Results that can be trusted

* Each test process gets private XDG data / config / cache / state / runtime
  and TMP directories (short: local socket paths are built from them); the
  runners keep their own private application-data roots.
* A binary older than its sources is STALE and not run (`--build` builds,
  `--allow-stale` overrides and is recorded); a binary replaced during its run
  makes the result ENVIRONMENTAL; a case that failed under parallel load is
  re-run alone and reported FLAKY when it then passes; failures listed in
  `scripts/test_quarantine.json` (owner, reason, expiry) are EXPECTED /
  PRE-EXISTING. Nothing is dropped from the report.
* A crash or a hang ends a process, not its shard: the case it ended in (the
  first selected case without a result — shard case lists are kept in
  registration order) is recorded CRASH / TIMEOUT and the rest of the shard
  runs in a continuation process. Before this, three crashing visualization
  cases silently removed ~220 of the GUI runner's 888 default-suite cases from every
  t3 run. A crash is never FLAKY and never quarantined, even when it passes
  alone (an intermittent crash was a real heap-corrupting race, VER-000305).
* Cases that open real hardware run in the live lane only (`cases` /
  `exclude_cases` split a runner between groups): unsupervised, they drove
  this host's hardware and hung a shard.
* Runners share one contract: cumulative selection flags, repeatable
  `--filter`, per-case JSON results with durations (`PROJECT_TEST_RESULTS`),
  a watchdog (the GUI runner included).
* Every run writes `<build>/test-results/runs/<run-id>.json` with the revision,
  the changed paths and — with `--request` — a fingerprint of the request's
  registered code. The work record names its runs on a `**Test-Run:**` line;
  `scripts/completion_gate.py` fails a linked run that failed and a request
  whose code changed after its latest linked run, and warns when none is
  linked. Enforcement sits in the gate because git hooks never run here.

### 4. Build → package → verify → deploy → deployment smoke

Packaging does not run the development test suites (they test the Debug tree,
not the artifact; their results are recorded per revision instead). It runs,
once each, what verifies *the artifact*: the release build script builds
(including the peer applications the release stages and the documentation
captures) and stages; the deployment script adds the web front end and the
documentation, then the package validation (structure, dependencies, runtimes,
relocation, web serving) and the asserting smoke test (`--version` / `--help`
exit 0, then the staged application opens real data, processes it through its
display path and exits 0 on SIGTERM — in a scratch profile, without hardware),
then the archive. A failing stage stops the deployment. Documentation screenshots are
reused when their provenance (version, binary build id, scenario sha256) is
current, otherwise captured four at a time; a release requires every image to
come from the current version. Remote deployment testing
tests the deployed artifact on the target (project-specific).

## Consequences

* Agents run `python3 scripts/run_tests.py --tier t2 --request <file>` (plus
  targeted `--suite` runs while iterating) instead of a fixed list, and link
  the run in the work record; the manifest is where a new suite is registered
  (a test that registers no suite fails `scripts/tests/test_run_tests.py`).
* The shared `build/debug` tree can still be relinked by another
  session mid-run; such results are marked ENVIRONMENTAL. A per-request build
  tree (worktree or `build/<name>-debug`) avoids it.
* Windows packaging cannot execute the tree on the Linux host; its smoke test
  is structural and says so (execution needs a Windows / wine runner — a
  follow-up request).
* In the source project t3 was not green yet at the time of this decision:
  the GUI runner's default suite had crashing and long-stale cases
  (quarantined, with an owning request). The t2 gate was green.
* The runners' combined selection flags and `--filter` now do what they say;
  evidence recorded earlier with those forms ran something else.
