#!/usr/bin/env python3
"""Identity-collision validation tests (ADR-000149, WORK-000248).

Every case builds a throw-away manifest set and runs the REAL
scripts/drift_detect.py against it through AGENT_PROCESS_ROOT (the same
root-override convention scripts/agent_process.py uses), so these are
end-to-end runs of the validator, not of a test-only reimplementation.

The repository's own manifests are exercised too: the collisions it
declares in docs/process/historical-id-collisions.md (REPO_DECLARED) must be
accepted, and nothing else may be.

Run: python3 -m unittest discover -s scripts/tests -v
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
REPO = SCRIPTS.parent
DRIFT = SCRIPTS / "drift_detect.py"
DECLARATIONS = "docs/process/historical-id-collisions.md"

REASON = "2026-09-12 subset-scan allocator collision, fixed by WORK-000167 / ADR-000090"

# Four historical collisions of the shape the source project declared (two
# distinct records per id, created by concurrent sessions before the monotonic
# allocator existed). Synthetic trees reproduce them; record names are neutral.
HISTORICAL = {
    "WORK-000128": ("work/work.txt", ["completed/WORK-000128-first-record.md",
                                      "completed/WORK-000128-second-record.md"]),
    "VER-000128": ("evidence/evidence.txt", ["VER-000128-first-record.md",
                                             "VER-000128-second-record.md"]),
    "ADR-000064": ("docs/decisions/decisions.txt", ["ADR-000064-first-decision.md",
                                                    "ADR-000064-second-decision.md"]),
    "ADR-000069": ("docs/decisions/decisions.txt", ["ADR-000069-first-decision.md",
                                                    "ADR-000069-second-decision.md"]),
}

# PROJECT-SPECIFIC: the collisions this repository declares in
# docs/process/historical-id-collisions.md, in the HISTORICAL format. The
# extracted template declares none (the source project declared four).
REPO_DECLARED: dict = {}


def run_drift(root: Path):
    """The real validator over `root`; returns (exit code, findings)."""
    env = dict(os.environ, AGENT_PROCESS_ROOT=str(root))
    r = subprocess.run([sys.executable, str(DRIFT), "--json"], capture_output=True, text=True, env=env)
    try:
        findings = json.loads(r.stdout)
    except ValueError:  # pragma: no cover - only on a validator crash
        raise AssertionError(f"drift_detect produced no JSON (exit {r.returncode}):\n{r.stdout}\n{r.stderr}")
    return r.returncode, findings


def codes(findings, code):
    return [f for f in findings if f["code"] == code]


def levels_for(findings, code):
    return {f["level"] for f in codes(findings, code)}


class Root:
    """A synthetic repository root with the three id manifests."""

    MANIFESTS = {
        "work/work.txt": ("# Active Work Manifest\n\nFormat: `WORK-ID | status | title | record | requirements | evidence`\n\n", "work"),
        "evidence/evidence.txt": ("# Verification Evidence Manifest\n\nFormat: `VER-ID | classification | date | requirements/work | record`\n\n", "evidence"),
        "docs/decisions/decisions.txt": ("# Active Decision Manifest\n\nFormat: `ADR-ID | status | title | record | affected requirements/work`\n\n", "docs/decisions"),
    }

    def __init__(self):
        base = REPO / "tmp" / "drift-collision-tests"
        base.mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(prefix="drift-", dir=str(base))
        self.path = Path(self._tmp.name)
        self.rows = {m: [] for m in self.MANIFESTS}
        self.declarations = []
        for d in ("work/completed", "evidence", "docs/decisions", "docs/process", "docs/requirements", "requests/inbox"):
            (self.path / d).mkdir(parents=True, exist_ok=True)

    def cleanup(self):
        self._tmp.cleanup()

    def record(self, manifest: str, rid: str, record: str):
        """Add a manifest row for `rid` whose record column is `record`, and
        create that record file so the row is not merely a broken link."""
        _, record_dir = self.MANIFESTS[manifest]
        target = self.path / record_dir / record
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# {rid}\n\n**Status:** completed\n")
        if manifest == "evidence/evidence.txt":
            self.rows[manifest].append(f"{rid} | PASS | 2026-09-12 | REQ-X / WORK-000001 | {record}")
        elif manifest == "work/work.txt":
            self.rows[manifest].append(f"{rid} | completed | synthetic | {record} | REQ-X | VER-000001")
            # drift_detect's separate "completed work without evidence" check
            # looks for evidence/*<WORK-ID>*.md; give it one so that unrelated
            # rule cannot colour these collision tests.
            (self.path / "evidence" / f"VER-000001-{rid}.md").write_text(f"# evidence for {rid}\n")
        else:
            self.rows[manifest].append(f"{rid} | ACCEPTED | synthetic | {record} | REQ-X, WORK-000001")

    def declare(self, rid: str, manifest: str, records, reason: str = REASON):
        self.declarations.append(f"{rid} | {manifest} | {' ; '.join(records)} | {reason}")

    def declare_raw(self, line: str):
        self.declarations.append(line)

    def write(self):
        for manifest, (header, _) in self.MANIFESTS.items():
            p = self.path / manifest
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(header + "\n".join(self.rows[manifest]) + "\n")
        # Synthetic completed records carry no gate records; completion
        # verification is covered by test_lifecycle_hardening.py.
        (self.path / "devsys.json").write_text(json.dumps({"drift": {"unverified_completion": "OFF"}}))
        doc = self.path / DECLARATIONS
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text("# Historical Identity Collisions\n\nDeclarations:\n\n```text\n"
                       + "\n".join(self.declarations) + "\n```\n")
        return self.path

    def with_all_historical(self):
        """The repository's four accepted collisions, declared and present."""
        for rid, (manifest, records) in HISTORICAL.items():
            for rec in records:
                self.record(manifest, rid, rec)
            self.declare(rid, manifest, records)
        return self


class HistoricalCollisionTests(unittest.TestCase):
    def setUp(self):
        self.root = Root()
        self.addCleanup(self.root.cleanup)

    # ---- Test A -----------------------------------------------------------
    def test_a_repository_historical_collisions_are_accepted(self):
        """The repository's declared collisions validate as accepted and no
        duplicate-id failure remains — run against the REAL repository."""
        code, findings = run_drift(REPO)
        accepted = codes(findings, "HISTORICAL_ID_COLLISION")
        self.assertEqual(len(accepted), len(REPO_DECLARED), accepted)
        if accepted:
            self.assertEqual({f["level"] for f in accepted}, {"ACCEPTED"})
        for rid in REPO_DECLARED:
            self.assertTrue(any(rid in f["msg"] for f in accepted), f"{rid} not accepted: {accepted}")
            # ... and each record is named in the output, so it stays auditable.
            for rec in REPO_DECLARED[rid][1]:
                self.assertTrue(any(rec in f["msg"] for f in accepted), f"{rec} not shown")
        self.assertEqual(codes(findings, "UNEXPECTED_ID_COLLISION"), [])
        self.assertEqual(codes(findings, "MALFORMED_COLLISION_DECLARATION"), [])
        self.assertEqual(codes(findings, "STALE_COLLISION_DECLARATION"), [])
        # No duplicate-id FAIL of any kind remains in the real repository.
        self.assertEqual([f for f in findings if f["level"] == "FAIL" and "COLLISION" in f["code"]], [])

    def test_a2_synthetic_historical_collisions_are_accepted(self):
        root = self.root.with_all_historical().write()
        code, findings = run_drift(root)
        self.assertEqual(len(codes(findings, "HISTORICAL_ID_COLLISION")), 4)
        self.assertEqual(levels_for(findings, "HISTORICAL_ID_COLLISION"), {"ACCEPTED"})
        self.assertEqual(codes(findings, "UNEXPECTED_ID_COLLISION"), [])

    # ---- Tests B, C, D, E -------------------------------------------------
    def _third_record_fails(self, rid: str, third: str):
        self.root.with_all_historical()
        manifest = HISTORICAL[rid][0]
        self.root.record(manifest, rid, third)          # a THIRD row on a declared id
        root = self.root.write()
        code, findings = run_drift(root)
        unexpected = codes(findings, "UNEXPECTED_ID_COLLISION")
        self.assertEqual(len(unexpected), 1, findings)
        self.assertEqual(unexpected[0]["level"], "FAIL")
        self.assertIn(rid, unexpected[0]["msg"])
        self.assertIn("does NOT match", unexpected[0]["msg"])
        # The historical exception did not swallow it, and the run fails.
        self.assertFalse(any(rid in f["msg"] for f in codes(findings, "HISTORICAL_ID_COLLISION")))
        self.assertEqual(code, 1)

    def test_b_third_record_using_work_000128_fails(self):
        self._third_record_fails("WORK-000128", "completed/WORK-000128-third-record.md")

    def test_c_third_record_using_ver_000128_fails(self):
        self._third_record_fails("VER-000128", "VER-000128-third-record.md")

    def test_d_third_record_using_adr_000064_fails(self):
        self._third_record_fails("ADR-000064", "ADR-000064-third-record.md")

    def test_e_third_record_using_adr_000069_fails(self):
        self._third_record_fails("ADR-000069", "ADR-000069-third-record.md")

    # ---- Test F -----------------------------------------------------------
    def test_f_new_duplicate_id_fails(self):
        self.root.with_all_historical()
        self.root.record("work/work.txt", "WORK-000999", "completed/WORK-000999-a.md")
        self.root.record("work/work.txt", "WORK-000999", "completed/WORK-000999-b.md")
        root = self.root.write()
        code, findings = run_drift(root)
        unexpected = codes(findings, "UNEXPECTED_ID_COLLISION")
        self.assertEqual(len(unexpected), 1, findings)
        self.assertEqual(unexpected[0]["level"], "FAIL")
        self.assertIn("WORK-000999", unexpected[0]["msg"])
        self.assertIn("not a declared historical collision", unexpected[0]["msg"])
        self.assertEqual(code, 1)
        # The four accepted ones are unaffected by the new failure.
        self.assertEqual(len(codes(findings, "HISTORICAL_ID_COLLISION")), 4)

    # ---- Test G -----------------------------------------------------------
    def test_g_unique_records_pass(self):
        for n, (manifest, rec) in enumerate((("work/work.txt", "completed/WORK-{}.md"),
                                             ("evidence/evidence.txt", "VER-{}.md"),
                                             ("docs/decisions/decisions.txt", "ADR-{}.md")), start=1):
            prefix = {"work/work.txt": "WORK", "evidence/evidence.txt": "VER",
                      "docs/decisions/decisions.txt": "ADR"}[manifest]
            for i in range(3):
                rid = f"{prefix}-00{n}{i:03d}"
                self.root.record(manifest, rid, rec.format(rid))
        root = self.root.write()
        code, findings = run_drift(root)
        self.assertEqual(codes(findings, "UNEXPECTED_ID_COLLISION"), [])
        self.assertEqual(codes(findings, "HISTORICAL_ID_COLLISION"), [])
        self.assertEqual(codes(findings, "MALFORMED_COLLISION_DECLARATION"), [])

    # ---- Test H -----------------------------------------------------------
    def test_h_declaration_naming_a_missing_record_cannot_suppress(self):
        """A declaration that points at a record which does not exist is
        malformed: it fails, and the duplicate it named is still reported."""
        manifest, records = HISTORICAL["WORK-000128"]
        for rec in records:
            self.root.record(manifest, "WORK-000128", rec)
        self.root.declare("WORK-000128", manifest, [records[0], "completed/WORK-000128-does-not-exist.md"])
        root = self.root.write()
        code, findings = run_drift(root)
        malformed = codes(findings, "MALFORMED_COLLISION_DECLARATION")
        self.assertEqual(len(malformed), 1, findings)
        self.assertEqual(malformed[0]["level"], "FAIL")
        self.assertIn("do not exist", malformed[0]["msg"])
        # The duplicate itself is NOT hidden by the broken declaration.
        unexpected = codes(findings, "UNEXPECTED_ID_COLLISION")
        self.assertEqual(len(unexpected), 1, findings)
        self.assertEqual(unexpected[0]["level"], "FAIL")
        self.assertIn("WORK-000128", unexpected[0]["msg"])
        self.assertEqual(codes(findings, "HISTORICAL_ID_COLLISION"), [])
        self.assertEqual(code, 1)

    def test_h2_declaration_for_an_unknown_manifest_is_malformed(self):
        manifest, records = HISTORICAL["VER-000128"]
        for rec in records:
            self.root.record(manifest, "VER-000128", rec)
        self.root.declare("VER-000128", "evidence/not-a-manifest.txt", records)
        root = self.root.write()
        code, findings = run_drift(root)
        self.assertIn("FAIL", levels_for(findings, "MALFORMED_COLLISION_DECLARATION"))
        self.assertEqual(len(codes(findings, "UNEXPECTED_ID_COLLISION")), 1)
        self.assertEqual(code, 1)

    def test_h3_declaration_of_a_single_record_is_malformed(self):
        manifest, records = HISTORICAL["ADR-000064"]
        for rec in records:
            self.root.record(manifest, "ADR-000064", rec)
        self.root.declare("ADR-000064", manifest, [records[0]])
        root = self.root.write()
        code, findings = run_drift(root)
        malformed = codes(findings, "MALFORMED_COLLISION_DECLARATION")
        self.assertEqual(len(malformed), 1, findings)
        self.assertIn("needs at least 2", malformed[0]["msg"])
        self.assertEqual(len(codes(findings, "UNEXPECTED_ID_COLLISION")), 1)
        self.assertEqual(code, 1)

    # ---- Test I -----------------------------------------------------------
    def test_i_replacing_one_side_of_a_historical_pair_is_detected(self):
        """Same id, same count, but one record swapped for a different one:
        the validator must see the mismatch, not the original collision."""
        manifest, records = HISTORICAL["ADR-000069"]
        # Both declared records still exist on disk — only the manifest row
        # changed — so the declaration itself is well formed and the mismatch
        # is the thing under test.
        (self.root.path / "docs" / "decisions" / records[1]).write_text("# historical\n")
        self.root.record(manifest, "ADR-000069", records[0])
        self.root.record(manifest, "ADR-000069", "ADR-000069-something-else-entirely.md")
        self.root.declare("ADR-000069", manifest, records)
        root = self.root.write()
        code, findings = run_drift(root)
        unexpected = codes(findings, "UNEXPECTED_ID_COLLISION")
        self.assertEqual(len(unexpected), 1, findings)
        self.assertEqual(unexpected[0]["level"], "FAIL")
        self.assertIn("does NOT match", unexpected[0]["msg"])
        self.assertIn("ADR-000069-something-else-entirely.md", unexpected[0]["msg"])
        self.assertEqual(codes(findings, "HISTORICAL_ID_COLLISION"), [])
        self.assertEqual(code, 1)

    # ---- Housekeeping -----------------------------------------------------
    def test_stale_declaration_warns_but_does_not_fail(self):
        """A declaration whose id is no longer duplicated cannot suppress
        anything, so it is a WARN telling the operator to remove it."""
        manifest, records = HISTORICAL["WORK-000128"]
        self.root.record(manifest, "WORK-000128", records[0])   # one row only
        for rec in records:                                      # both files exist
            (self.root.path / "work" / rec).parent.mkdir(parents=True, exist_ok=True)
            (self.root.path / "work" / rec).write_text("# historical\n")
        self.root.declare("WORK-000128", manifest, records)
        root = self.root.write()
        code, findings = run_drift(root)
        stale = codes(findings, "STALE_COLLISION_DECLARATION")
        self.assertEqual(len(stale), 1, findings)
        self.assertEqual(stale[0]["level"], "WARN")
        self.assertEqual(codes(findings, "UNEXPECTED_ID_COLLISION"), [])

    def test_repeated_row_for_one_record_is_not_an_identity_collision(self):
        """The same record listed on two rows under one id is a stale manifest
        row, not two records sharing an id. It must fail on its own terms and
        must not be absorbed by a declaration (HEAD carried exactly this for
        ADR-000026, ADR-000069 and ADR-000122)."""
        manifest, records = HISTORICAL["ADR-000069"]
        for rec in records:
            self.root.record(manifest, "ADR-000069", rec)
        self.root.record(manifest, "ADR-000069", records[0])   # superseded row
        self.root.declare("ADR-000069", manifest, records)
        root = self.root.write()
        code, findings = run_drift(root)
        stale_row = codes(findings, "DUPLICATE_MANIFEST_ROW")
        self.assertEqual(len(stale_row), 1, findings)
        self.assertEqual(stale_row[0]["level"], "FAIL")
        self.assertIn(records[0], stale_row[0]["msg"])
        self.assertEqual(code, 1)
        # The identity collision itself is still the declared pair, so it is
        # accepted — the two conditions are reported independently.
        accepted = codes(findings, "HISTORICAL_ID_COLLISION")
        self.assertEqual(len(accepted), 1, findings)
        self.assertEqual(accepted[0]["level"], "ACCEPTED")
        self.assertEqual(codes(findings, "UNEXPECTED_ID_COLLISION"), [])

    def test_repeated_row_alone_is_not_reported_as_a_collision(self):
        """One record, two rows, no second record: a stale row and nothing
        else — never an identity collision."""
        self.root.record("work/work.txt", "WORK-000500", "completed/WORK-000500-a.md")
        self.root.record("work/work.txt", "WORK-000500", "completed/WORK-000500-a.md")
        root = self.root.write()
        code, findings = run_drift(root)
        self.assertEqual(len(codes(findings, "DUPLICATE_MANIFEST_ROW")), 1, findings)
        self.assertEqual(codes(findings, "UNEXPECTED_ID_COLLISION"), [])
        self.assertEqual(codes(findings, "HISTORICAL_ID_COLLISION"), [])
        self.assertEqual(code, 1)

    def test_accepted_collisions_do_not_change_the_exit_code(self):
        root = self.root.with_all_historical().write()
        code, findings = run_drift(root)
        fails = [f for f in findings if f["level"] == "FAIL"]
        self.assertEqual(fails, [], fails)
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
