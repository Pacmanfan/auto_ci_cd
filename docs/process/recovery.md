# Failure Recovery

**State model:** `docs/process/state-model.md`
**Claim tool:** `scripts/claim.py` (ownership record per request, ADR-000090)
**Health:** `scripts/health_check.py`

## Liveness: ALIVE, DEAD, ACTIVE, STALE

Every owner operation (claim, `--own`, `--token`, `--heartbeat`, commit via
`scripts/commit_request.py`, transitions) refreshes the claim's heartbeat.
A session that passes `--owner-pid <pid>` (or exports `AGENT_OWNER_PID`) with
the pid of its **long-lived** process (the agent session, not the short-lived
`claim.py`) also records that process's identity: host, boot id, pid and
kernel start time (`scripts/devsys_platform.py`). Every reader then reports
one verdict (ADR-000199):

| Verdict | Meaning | What may happen |
|---|---|---|
| **ALIVE** | the recorded owner process, or a step started with `claim.py --run`, is verified running | nothing; never taken over, not even with `--force-active` |
| **DEAD** | the recorded process is verified gone (exited, pid reused, earlier boot) and the claim was silent for `claims.dead_grace_minutes` (5) | `--recover-request`; the sweep requeues it when nothing owned is uncommitted |
| **ACTIVE** | no verifiable process, heartbeat newer than `claims.stale_hours` (2 h) | `--recover-request --force-active` after confirming the owner is gone |
| **STALE** | no verifiable process, heartbeat older than the threshold | `--recover-request` (silence is not proof; never automatic) |

A long step keeps the claim ALIVE and its heartbeat fresh when run through
`python3 scripts/claim.py --run <file> --label build -- <command ...>`;
`scripts/run_tests.py --request <file>` refreshes the heartbeat itself when
`$AGENT_ID` owns the claim.

## Sweep: diagnose, then repair only what is safe

```
python3 scripts/claim.py --sweep            # report only (also: devsys.py sweep)
python3 scripts/claim.py --sweep --apply    # perform the safe repairs, logged to .agent/state/runtime/sweeps.jsonl
```

`--apply` performs exactly these repairs, none of which can lose or steal work:

- finish an interrupted claim (lock written, request never moved) of an ALIVE / ACTIVE owner;
- drop an interrupted claim of a DEAD / STALE owner that owns no uncommitted change;
- resynchronise a claim's recorded state with the directory its request is in;
- move a request's JSON sidecar next to its markdown;
- remove work locks whose owner is gone or whose record no longer exists;
- requeue (processing -> inbox) the request of a verified-DEAD owner that has nothing uncommitted.

Everything else is reported and left for a human or an explicit recovery: a
DEAD owner with uncommitted work, every STALE owner, orphaned processing
requests, requests in several state directories.

```
python3 scripts/claim.py --status                 # owner, heartbeat age, owned paths, worktree
python3 scripts/claim.py --show <file>            # one request, incl. commits and history
python3 scripts/claim.py --recover                # REPORT ONLY: stale claims + orphaned processing/ requests
python3 scripts/claim.py --recover-request <file> --agent-id <you> --reason "<why>"
                                                  # explicit, atomic ownership transfer
```

`--recover-request` refuses when the owner's heartbeat is fresh (use
`--force-active` only after confirming the owner is really gone), requires a
reason, records the previous owner in `history`, and transfers the owned-path
list. Two concurrent recoveries: exactly one wins. The previous agent can no
longer heartbeat, transition or commit.

## Shared files after an interruption

A recovered claim carries the previous owner's **baselines** (REQ-DEVOPS-042):
the content each owned path had when that agent registered it. That is
deliberate — the uncommitted work in those files is the work being recovered,
and committing the delta over those baselines is what finishes it.

Two cases need a decision rather than a default:

* **Another request committed into one of those files while the claim was
  stale.** `commit_request.py` merges over it, so nothing is reverted; if the
  changes genuinely overlap the commit is refused with the path named.
  Reconcile the file, then
  `python3 scripts/claim.py --snapshot <file> --paths <path> --rebaseline`
  and commit again.
* **The interrupted work in a file is not wanted.** Restore the file from HEAD
  yourself — recovery never discards working-tree content on your behalf.

`python3 scripts/claim.py --show <file>` lists every owned path, its mode and
whether it still has a baseline; a path shown as
`[no baseline: id-token attribution]` comes from a claim made before
REQ-DEVOPS-042 and is attributed by the ids in its changed lines.

## Agent Crash

1. Crash before or during the claim: the claim is created and the request is
   moved in one critical section, so the request is either still in `inbox/`
   with no claim, or in `processing/` with a claim.
2. Crash while processing: the claim, `requests/processing/<file>` and any
   uncommitted owned changes remain. After the heartbeat goes stale, inspect
   (`--show`, `git status`, `scripts/commit_request.py --report`), then
   `--recover-request`. Review the transferred owned paths before committing:
   they are the previous session's uncommitted work.
3. A `processing/` request without a claim (orphan) cannot be claimed
   normally (exit 2); adopt it with `--recover-request`.

## Machine Reboot / Interrupted Build

Claims survive reboots. A claim that recorded its owner process is DEAD after
a reboot (the boot id changed) as soon as the grace period has passed; one
that did not is ACTIVE until it goes STALE. Recover as above. Partial builds
are irrelevant to state: rebuild (`python3 scripts/devsys.py build build`),
re-run the tests, then `scripts/completion_gate.py --work WORK-XXXX --record`
— a gate record made before the interruption no longer matches if the code
changed.

## Failed Tests

Do not advance to `verification -> completed` (`work.py` refuses without a
PASS gate record). Leave WORK in `verification` or `active`; record a `fail`
evidence entry (`evidence.py create --status fail`); fix and re-verify with a
new VER. A run that executed nothing (EMPTY) or was stopped (INTERRUPTED) is
not a result.

## Release refused

`--release-request` refuses while an owned path still differs from HEAD —
releasing would leave work that nobody owns. Commit it
(`scripts/commit_request.py`) or, to hand the request back deliberately,
`--release-request <file> --requeue --abandon "<reason>"`: the changes stay in
the working tree, unowned, and the reason is recorded in the claim history.

## Partial Changes / Tool Failure

- Inspect with `git status` and `python3 scripts/commit_request.py --report`
  (who owns each change). Never discard changes owned by another request.
- Idempotency: before creating requirement/work/decision/VER, search manifests
  and allocate new ids with `scripts/allocate_id.py`.

## Documentation Incomplete

`scripts/doc_integrity.py` and `scripts/drift_detect.py` flag missing links;
the completion gate fails until docs are updated.

## Recovery Procedure for Restarting Agent

```
1. export AGENT_ID=$(python3 scripts/claim.py --new-agent-id)
   export AGENT_OWNER_PID=<pid of this agent session's long-lived process>
2. python3 scripts/health_check.py
3. python3 scripts/devsys.py status           # workers, liveness, recoverable, next eligible
4. python3 scripts/claim.py --sweep           # then --apply for the safe repairs
5. python3 scripts/drift_detect.py
6. python3 scripts/process_next_request.py --dry-run
7. python3 scripts/devsys.py next --claim     # or --recover-request a DEAD owner's work to finish it
```

Never delete `requests/processing/` files or `.agent/state/claims/` records by hand.

## Worked Example: Two Agents Racing

```
Agent A: claim.py --claim-request foo.md -> CLAIMED (moved to processing/)
Agent B: claim.py --claim-request foo.md -> ALREADY CLAIMED: request ... owned by agent 'A' (exit 1)
```

Agent B must stop and must not create duplicate requirement/work.

## Worked Example: Abandoned Claim

```
Agent A crashed; heartbeat 5 h old
Agent C: claim.py --claim-request foo.md    -> ALREADY CLAIMED ... [STALE] (exit 2), nothing taken
Agent C: claim.py --recover-request foo.md --reason "A crashed at 04:00" -> RECOVERED ... owned by 'C'
```

## Worked Example: Dead Session With Uncommitted Work

```
Agent B (AGENT_OWNER_PID=4711) edits src/calc.c, then its session is killed
devsys.py status   -> DEAD request REQ-...-002 ... (owner process 4711 no longer exists)
                      recoverable: owner DEAD with 5 uncommitted owned path(s)
claim.py --sweep --apply            -> nothing applied to it (uncommitted work)
Agent A: claim.py --own <A's request> --paths src/calc.c -> refused, owned by B's request (exit 1)
Agent C: claim.py --recover-request <B's request> --reason "B's session died" -> RECOVERED
Agent C finishes the work: tests, evidence, gate --record, complete, commit, release
Agent A can now register src/calc.c
```

This is exercised end to end by `scripts/tests/test_end_to_end.py`.
