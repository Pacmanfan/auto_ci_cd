#!/usr/bin/env python3
"""
Request / work ownership for concurrent agents (REQ-DEVOPS-016/025, ADR-000090).

Every operation runs inside an inter-process critical section and checks
ownership under that lock (see scripts/agent_process.py), so two agents can
never both claim, transition or recover the same request.

Identity:
  python3 scripts/claim.py --new-agent-id                 # print a session-unique id; export AGENT_ID=<it>
  --owner-pid PID (or $AGENT_OWNER_PID)                   # the long-lived process of this agent session; with it
                                                          # the claim's liveness is verified (ALIVE / DEAD), not
                                                          # only inferred from the heartbeat (ACTIVE / STALE)

Lifecycle (agent id from --agent-id or $AGENT_ID; uid/pid defaults are refused):
  --claim-request <file>                                  # atomic claim + inbox|needs-review -> processing
  --own <file> --paths P [P ...] [--shared|--concurrent] [--adopt-existing-changes]
                                                          # register repository paths this request will change.
                                                          # one path = one file; a directory is refused
                                                          # (register each file under it, ADR-000197)
                                                          # exclusive (default): nobody else may register it
                                                          # --shared:     append-only manifest; only lines naming
                                                          #               this request's tokens are committed
                                                          # --concurrent: ordinary content several requests may
                                                          #               change (prose, code, configuration);
                                                          #               only this request's delta is committed
  --token <file> --tokens WORK-000123 VER-000123          # ids whose manifest lines belong to this request
  --snapshot <file> [--paths P ...] [--rebaseline|--from-head|--from-file F]
                                                          # record this request's own version of the paths it
                                                          # holds, so a later edit by another agent is not swept
                                                          # into its commit; --rebaseline instead declares the
                                                          # current content someone else's and attributes only
                                                          # what changes from now on; --from-head declares
                                                          # everything the file gained since the last commit
                                                          # this request's; --from-file records this request's
                                                          # version of ONE path from another file, for a shared
                                                          # file whose working copy also holds work that is not
                                                          # this request's
  --heartbeat <file>                                      # "still working" (commit/own/claim also refresh it)
  --run <file> [--label L] -- CMD ...                     # run a long step (build, test tier): the claim is ALIVE
                                                          # while it runs and its heartbeat is refreshed
  --complete-request | --reject-request | --needs-review-request <file> [--reason TEXT] [--waive-gate TEXT]
                                                          # owner-only, no-clobber transition out of processing
                                                          # (or needs-review). complete needs a current PASS gate
                                                          # record of every linked work item (completion_gate.py
                                                          # --record); reject needs --reason; --waive-gate is a
                                                          # recorded human override
  --release-request <file> [--requeue] [--abandon TEXT]   # owner-only; after the final commit. Refused while an
                                                          # owned path is uncommitted, unless --abandon <reason>
  --create-worktree <file> [--worktree-dir DIR]           # optional isolated git worktree + branch for the request

Inspection / recovery:
  --status [--json] [--stale-hours H]                     # who owns what, liveness verdict, owned paths
  --show <file>
  --recover [--stale-hours H]                             # REPORT ONLY: lists stale / dead claims and orphans
  --sweep [--apply] [--json]                              # diagnose claim/request consistency; --apply performs
                                                          # only repairs that cannot lose or steal work
  --recover-request <file> --reason TEXT [--stale-hours H] [--expect-owner ID] [--force-active]
                                                          # explicit ownership transfer of a DEAD / STALE / orphaned
                                                          # request; --force-active overrides an unverified ACTIVE
                                                          # owner; a verified ALIVE owner is never taken over

Work items:
  --claim-work WORK-ID | --release-work WORK-ID

Exit codes: 0 ok, 1 conflict (active owner), 2 stale/orphan (explicit recovery needed),
3 not owner, 4 bad state, 5 not found, 6 invalid, 7 collision, 8 busy (lock timeout, retry),
9 gate required (completion needs a current PASS gate record).
"""
import argparse
import json
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_process as proc  # noqa: E402


def _print_claim(d):
    print(proc.describe_owner(d))
    root = proc.repo_root()
    for p, f in sorted((d.get("files") or {}).items()):
        note = " (adopted existing changes)" if f.get("adopted") else ""
        if not f.get("baseline_blob") and "baseline_blob" not in f:
            note += " [no baseline: id-token attribution]"
        if proc.is_directory_entry(root, p):
            note += " [directory: owns no file; register the files under it (ADR-000197)]"
        print(f"    {f.get('mode', 'exclusive'):9s} {p}{note}")
    if d.get("tokens"):
        print(f"    tokens: {', '.join(d['tokens'])}")
    for c in d.get("commits") or []:
        print(f"    commit {c.get('sha', '')[:10]} {len(c.get('paths', []))} path(s) at {c.get('at')}")
    for h in d.get("history") or []:
        print(f"    history: {json.dumps(h)}")
    if d.get("worktree"):
        print(f"    worktree: {d['worktree']} (branch {d.get('branch')})")


def status(root, as_json, stale_hours):
    claims = proc.all_claims(root)
    if as_json:
        print(json.dumps([{**{k: v for k, v in c.items() if not k.startswith('_')},
                           "stale": proc.is_stale(c, stale_hours),
                           "liveness": dict(zip(("verdict", "reason"), proc.claim_liveness(c, stale_hours))),
                           "heartbeat_age_min": round(proc.heartbeat_age_hours(c) * 60, 1)} for c in claims], indent=2))
        return 0
    if not claims:
        print("No claims")
    for c in claims:
        tag = proc.claim_liveness(c, stale_hours)[0]
        print(f"{tag} ", end="")
        _print_claim(c)
    return 0


def sweep(root, apply, as_json):
    rep = proc.sweep(root, apply=apply)
    if as_json:
        print(json.dumps(rep, indent=2))
        return 0
    for f in rep["findings"]:
        verdict = f" [{f['verdict']}]" if f.get("verdict") else ""
        print(f"FINDING {f['target']}{verdict}: {f['issue']}")
    for a in rep["actions"]:
        print(f"APPLIED {a['action']} {a['target']}" + (f" ({a.get('reason')})" if a.get("reason") else ""))
    print(f"{len(rep['findings'])} finding(s), {len(rep['actions'])} repair(s) applied"
          + ("" if apply else " (report only; --apply performs the safe repairs)"))
    return 0


def recover_report(root, stale_hours):
    stale = [c for c in proc.all_claims(root) if proc.is_stale(c, stale_hours)]
    locked = {c.get("target") for c in proc.all_claims(root)}
    orphans = [p.name for p in (proc.main_root(root) / "requests" / "processing").glob("*.md")
               if p.name not in locked and p.name != "README.md"]
    for c in stale:
        print("STALE " + proc.describe_owner(c, stale_hours))
    for o in orphans:
        print(f"ORPHAN requests/processing/{o} has no claim")
    print(f"{len(stale)} stale claim(s), {len(orphans)} orphaned processing request(s). Nothing was removed.")
    if stale or orphans:
        print("Inspect the owner and its owned paths (--show), then transfer explicitly:\n"
              "  python3 scripts/claim.py --recover-request <file> --agent-id <you> --reason '<why>'")
    return 0


def create_worktree(root, name, agent_id, directory):
    wt = proc.create_worktree(root, name, agent_id, directory)
    print(f"WORKTREE {wt['path']} (branch {wt['branch']}) recorded for {name}")
    print(f"Work inside it; run scripts from that worktree so commits land on {wt['branch']}.")
    return 0


def claim_work(root, wid, agent_id):
    agent_id = proc.validate_agent_id(agent_id)
    with proc.exclusive(root, "claims"):
        lp = proc.lock_path(root, wid, "work")
        d = proc.read_claim(lp)
        if d is not None:
            if d.get("agent_id") == agent_id:
                print(f"ALREADY OWNED {lp}")
                return 0
            raise proc.ProcessError(proc.STALE if proc.is_stale(d) else proc.CONFLICT,
                                    "ALREADY CLAIMED: " + proc.describe_owner(d))
        now = proc.now_iso()
        proc.create_exclusive_text(lp, json.dumps({"kind": "work", "target": wid, "agent_id": agent_id,
                                                   "claim_token": uuid.uuid4().hex, "claimed_at": now,
                                                   "heartbeat_at": now, "state": "active"}, indent=2) + "\n")
    print(f"CLAIMED {lp}")
    return 0


def release_work(root, wid, agent_id):
    with proc.exclusive(root, "claims"):
        d = proc.require_owner(root, wid, agent_id, kind="work")
        proc._archive_claim(root, d, "released")
        proc.lock_path(root, wid, "work").unlink()
    print(f"RELEASED work {wid}")
    return 0


def run_step(root, name, agent_id, label, command):
    """Run ``command`` as a recorded step of the claim (CI_CD_ROADMAP CON-01)."""
    import subprocess
    if not command:
        raise proc.ProcessError(proc.INVALID, "--run needs a command after '--'")
    proc.set_step(root, name, agent_id, label or " ".join(command)[:80], pid=os.getpid())
    try:
        with proc.Heartbeat(root, name, agent_id):
            rc = subprocess.run(command).returncode
    finally:
        proc.set_step(root, name, agent_id, None)
    print(f"STEP finished rc={rc}")
    return rc


def main():
    argv = sys.argv[1:]
    command = []
    if "--" in argv:
        i = argv.index("--")
        argv, command = argv[:i], argv[i + 1:]
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    for flag in ("--claim-request", "--own", "--token", "--snapshot", "--heartbeat", "--complete-request", "--reject-request",
                 "--needs-review-request", "--release-request", "--recover-request", "--show",
                 "--create-worktree", "--claim-work", "--release-work", "--run"):
        g.add_argument(flag, metavar="TARGET")
    g.add_argument("--recover", action="store_true")
    g.add_argument("--sweep", action="store_true")
    g.add_argument("--status", action="store_true")
    g.add_argument("--new-agent-id", action="store_true")
    ap.add_argument("--agent-id", default=os.environ.get("AGENT_ID"))
    ap.add_argument("--owner-pid", type=int, default=None,
                    help="long-lived process of this agent session (default $AGENT_OWNER_PID)")
    ap.add_argument("--paths", nargs="+", default=[])
    ap.add_argument("--tokens", nargs="+", default=[])
    ap.add_argument("--shared", action="store_true", help="path may be shared (e.g. append-only manifests); "
                    "only hunks naming this request's tokens are committed")
    ap.add_argument("--adopt-existing-changes", action="store_true")
    ap.add_argument("--rebaseline", action="store_true", help="with --snapshot: move the baseline to the current "
                    "content instead of recording this request's version")
    ap.add_argument("--from-head", action="store_true", help="with --snapshot: move the baseline to HEAD, i.e. "
                    "everything this file gained since the last commit is this request's")
    ap.add_argument("--from-file", help="with --snapshot: record this request's version of one path from this "
                    "file instead of from the working tree")
    ap.add_argument("--concurrent", action="store_true", help="with --own: ordinary content several requests may "
                    "change; only this request's delta is committed")
    ap.add_argument("--requeue", action="store_true")
    ap.add_argument("--abandon", metavar="REASON", help="with --release-request: release although owned paths "
                    "are uncommitted (they stay in the working tree, unowned; recorded)")
    ap.add_argument("--reason", default="")
    ap.add_argument("--waive-gate", metavar="REASON", help="with --complete-request: recorded human override of "
                    "the completion-gate requirement")
    ap.add_argument("--label", help="with --run: step label")
    ap.add_argument("--apply", action="store_true", help="with --sweep: perform the safe repairs")
    ap.add_argument("--expect-owner")
    ap.add_argument("--force-active", action="store_true")
    ap.add_argument("--stale-hours", type=float, default=None)
    ap.add_argument("--worktree-dir")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    root = proc.repo_root()
    if a.stale_hours is None:
        a.stale_hours = proc.stale_hours(root)
    name = lambda t: Path(t).name  # noqa: E731
    try:
        if a.new_agent_id:
            print(proc.new_agent_id())
            return 0
        if a.status:
            return status(root, a.json, a.stale_hours)
        if a.recover:
            return recover_report(root, a.stale_hours)
        if a.sweep:
            return sweep(root, a.apply, a.json)
        if a.show:
            d = proc.read_claim(proc.lock_path(root, name(a.show)))
            if d is None:
                print(f"NO CLAIM for {name(a.show)} (locations: {[s for s, _ in proc.locate_request(root, name(a.show))]})")
                return proc.NOT_FOUND
            print(f"{proc.claim_liveness(d, a.stale_hours)[0]} ", end="")
            _print_claim(d)
            return 0
        if a.run:
            return run_step(root, name(a.run), a.agent_id, a.label, command)
        if a.claim_request:
            d, how = proc.claim_request(root, name(a.claim_request), a.agent_id, owner_pid=a.owner_pid)
            label = {"already-owned": "ALREADY OWNED (heartbeat refreshed) ", "resumed": "RESUMED ",
                     "claim-completed": "CLAIM COMPLETED ", "taken-over": "TAKEN OVER "}.get(how, "CLAIMED ")
            print(label + str(proc.lock_path(root, d["target"])))
            print(f"REQUEST {d.get('request_path')} owner {d['agent_id']}")
            return 0
        if a.own:
            if not a.paths:
                raise proc.ProcessError(proc.INVALID, "--own needs --paths")
            d = proc.own_paths(root, name(a.own), a.agent_id, a.paths, shared=a.shared,
                               adopt=a.adopt_existing_changes, concurrent=a.concurrent)
            print(f"OWNED {len(a.paths)} path(s) for {d.get('request_id')}")
            return 0
        if a.token:
            proc.add_tokens(root, name(a.token), a.agent_id, a.tokens)
            print(f"TOKENS {' '.join(a.tokens)}")
            return 0
        if a.snapshot:
            d = proc.snapshot_paths(root, name(a.snapshot), a.agent_id, a.paths or None,
                                    rebaseline=a.rebaseline, from_head=a.from_head, from_file=a.from_file)
            done = a.paths or sorted(d["files"])
            what = (f"this request's version is taken from {a.from_file}" if a.from_file
                    else "everything these paths gained since the last commit is this request's" if a.from_head
                    else "changes from now on are this request's" if a.rebaseline
                    else "their current content is recorded as this request's work")
            print(f"{'REBASELINED' if (a.rebaseline or a.from_head) else 'SNAPSHOT'} "
                  f"{len(done)} path(s) for {d.get('request_id')}: {what}")
            return 0
        if a.heartbeat:
            proc.heartbeat(root, name(a.heartbeat), a.agent_id, owner_pid=a.owner_pid)
            print("HEARTBEAT ok")
            return 0
        for flag, target in (("complete_request", "completed"), ("reject_request", "rejected"),
                             ("needs_review_request", "needs-review")):
            if getattr(a, flag):
                d = proc.transition_request(root, name(getattr(a, flag)), a.agent_id, target,
                                            reason=a.reason or None, waiver=a.waive_gate)
                print(f"TRANSITIONED {d.get('request_path')} ({target}); commit it, then --release-request")
                return 0
        if a.release_request:
            d = proc.release_request(root, name(a.release_request), a.agent_id, requeue=a.requeue, abandon=a.abandon)
            print(f"RELEASED {name(a.release_request)}")
            return 0
        if a.recover_request:
            d = proc.recover_request(root, name(a.recover_request), a.agent_id, a.reason, a.stale_hours,
                                     a.force_active, a.expect_owner, owner_pid=a.owner_pid)
            print("RECOVERED " + proc.describe_owner(d, a.stale_hours))
            return 0
        if a.create_worktree:
            return create_worktree(root, name(a.create_worktree), a.agent_id, a.worktree_dir)
        if a.claim_work:
            return claim_work(root, a.claim_work, a.agent_id)
        if a.release_work:
            return release_work(root, a.release_work, a.agent_id)
    except proc.ProcessError as e:
        print(e.message, file=sys.stderr)
        return e.code
    ap.print_help()
    return proc.INVALID


if __name__ == "__main__":
    sys.exit(main())
