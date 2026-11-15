#!/usr/bin/env python3
"""Request classification, validation and system-metadata tests
(REQ-DEVOPS-045, ADR-000173, WORK-000288).

Covers the defects that REQ-CHAT-20260923-004 reported:

* the old keyword scan matched "crash"/"abort"/"killed" anywhere in a 40 KB
  feature specification ("the application must not crash", "crash detection",
  "if the process was killed") and filed it as CRASH; "regression tests" filed
  requests as REGRESSION; `--type` accepted any free-form string; the request
  id/date used the UTC date, so evening requests carried tomorrow's date.

Three layers are tested:

1. the pure module (`scripts/request_classification.py`): vocabulary,
   semantics of every example in the request, negation / meta handling;
2. the real records that exposed the problem, read from `requests/` as
   regression fixtures;
3. the real CLI (`scripts/create_request.py`, `scripts/preflight.py`) in a
   throw-away repository: validation before persistence, rejection writes
   nothing, structured diagnostics in the sidecar, local-date ids, ids that
   the text cannot influence, uniqueness under concurrent creation, review
   and explicit reclassification of a hand-dropped record.

Run: python3 -m unittest scripts.tests.test_request_classification -v
     (or: python3 -m unittest discover -s scripts/tests -v)
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPTS = Path(__file__).resolve().parents[1]
REPO = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))
import agent_process as proc  # noqa: E402
import request_classification as rc  # noqa: E402

WAITER = """import os, sys, time
go = sys.argv[1]
while not os.path.exists(go):
    time.sleep(0.001)
os.execv(sys.executable, [sys.executable] + sys.argv[2:])
"""

FEATURE_SPEC = (
    "# App — External Backend Reception\n\n## Objective\n\nImplement a first-class reception "
    "capability using an external backend. Add two decoding pipelines and Project integration.\n\n"
    "## Error handling\n\nThe integration must handle:\n\n* process startup failure\n* process crash\n* nonzero exit\n"
    "* timeout\n\nThe application must not crash when the backend is unavailable. Mark a run as failed if the backend "
    "process failed, timed out, was killed, or generated incomplete output.\n\n## Regression tests\n\nRun the existing "
    "project test suites; no unrelated regressions.\n\n## Performance\n\nDo not block the Qt UI thread.\n"
)


# --------------------------------------------------------------------------- 1. module

class Vocabulary(unittest.TestCase):
    def test_canonical_values_are_the_closed_vocabulary(self):
        self.assertEqual(set(rc.CANONICAL_TYPES),
                         {"FEATURE", "BUG", "CRASH", "REGRESSION", "PERFORMANCE", "REFACTOR", "BUILD",
                          "DOCUMENTATION", "INVESTIGATION", "PROCESS-CHANGE"})
        for t in rc.CANONICAL_TYPES:
            self.assertEqual(rc.normalize_type(t), (t, None))

    def test_case_and_whitespace_variants_are_normalized_and_reported(self):
        for raw in ("crash", "Crash", "CRASH ", " crash\t"):
            canon, note = rc.normalize_type(raw)
            self.assertEqual(canon, "CRASH", raw)
            self.assertIsNotNone(note, raw)   # never silent

    def test_aliases_map_and_are_reported(self):
        self.assertEqual(rc.normalize_type("DEFECT")[0], "BUG")
        self.assertEqual(rc.normalize_type("bugfix")[0], "BUG")
        self.assertEqual(rc.normalize_type("process-change")[0], "PROCESS-CHANGE")
        self.assertEqual(rc.normalize_type("process")[0], "PROCESS-CHANGE")
        self.assertIn("alias", rc.normalize_type("DEFECT")[1])

    def test_unknown_values_are_rejected(self):
        for raw in ("CRASH?", "FAILURE", "feature | defect | question", "audit", "change", "", None, "CRASH!"):
            canon, note = rc.normalize_type(raw)
            self.assertIsNone(canon, raw)
            self.assertTrue(note)

    def test_priority_vocabulary(self):
        self.assertEqual(rc.normalize_priority("high"), "HIGH")
        self.assertEqual(rc.normalize_priority(None), "NORMAL")
        self.assertIsNone(rc.normalize_priority("URGENT"))


class Semantics(unittest.TestCase):
    """Every example the request lists, plus the shapes that fooled the old scan."""

    EXAMPLES = [
        ("Add external backend reception, two decoders, products and Project integration", "FEATURE"),
        ("Add signal simulation and generator TX integration", "FEATURE"),
        ("Add generic decoded messages and packet integration", "FEATURE"),
        ("Add application acquisition and pipeline control", "FEATURE"),
        ("Activity Time column mixes time bases between Detection and tracked-Item rows", "BUG"),
        ("Application crashes when deleting a project", "CRASH"),
        ("Segmentation fault when opening the chart", "CRASH"),
        ("Service terminates unexpectedly during startup", "CRASH"),
        ("ui_test detector test fails deterministically", "BUG"),
        ("Backend integration fails to decode the input", "BUG"),
        ("Packet radio integration has a failure", "BUG"),
        ("Chart gridlines used to work but disappeared after the recent UI change", "REGRESSION"),
        ("Add chart and plot gridline configuration", "FEATURE"),
        ("Correlation engine consumes excessive CPU after REQ-017", "PERFORMANCE"),
        ("I loaded a file and the application crashed.", "CRASH"),
        ("Add support for HDF5 data files.", "FEATURE"),
        ("The chart pans backwards in TopDown mode.", "BUG"),
        ("App.gui SIGSEGV in MainWindow::paintEvent after loading a recording", "CRASH"),
        ("The build fails with undefined reference to SettingsPage::RefreshStatus", "BUILD"),
    ]

    def test_examples(self):
        for text, expected in self.EXAMPLES:
            c = rc.classify(text)
            self.assertEqual(c.final_type, expected, f"{text!r}: {c.classifier_reason}")
            self.assertEqual(c.validation, "PASS")
            self.assertTrue(c.classifier_reason)

    def test_failure_language_never_implies_crash(self):
        for text in ("Backend integration fails to decode the input",
                     "Packet radio integration has a failure",
                     "the reception failure is reproducible",
                     "validation failure on every import; the test failure is deterministic",
                     "It doesn't work and the error is shown twice — broken, problem, issue"):
            self.assertNotEqual(rc.classify(text).final_type, "CRASH", text)
            self.assertEqual(rc.supporting_evidence(rc.extract_evidence(text), "CRASH"), [], text)

    def test_negated_conditional_and_capability_crash_mentions_are_not_evidence(self):
        for text in ("The application must not crash when the backend is missing.",
                     "The application must never crash; add a decoding backend.",
                     "Decoder crashes do not crash the application.",
                     "If the backend process failed, timed out, was killed, or produced no output, mark the run failed.",
                     "Process supervision: stdout, stderr, timeout, crash detection, restart, health status.",
                     "External decoder crashes must be visible through the Health Monitor.",
                     "Malformed packets must not crash the decoder."):
            self.assertEqual(rc.supporting_evidence(rc.extract_evidence(text), "CRASH"), [], text)
            self.assertNotEqual(rc.classify(text).final_type, "CRASH", text)

    def test_feature_specification_with_crash_and_regression_sections_is_a_feature(self):
        c = rc.classify(FEATURE_SPEC)
        self.assertEqual(c.final_type, "FEATURE", c.classifier_reason)
        self.assertEqual(rc.supporting_evidence(c.evidence, "CRASH"), [])
        self.assertEqual(rc.supporting_evidence(c.evidence, "REGRESSION"), [])
        self.assertEqual(rc.supporting_evidence(c.evidence, "PERFORMANCE"), [])

    def test_regression_needs_evidence_of_a_previous_working_state(self):
        for text in ("Add chart and plot gridline configuration. Run the existing regression tests.",
                     "Build comprehensive unit, integration, regression, and deterministic tests.",
                     "no significant chart fps regression",
                     "Modify the existing tuner so the span is configurable."):
            self.assertNotEqual(rc.classify(text).final_type, "REGRESSION", text)
        for text in ("Chart gridlines used to work but disappeared after the recent UI change",
                     "Marker labels no longer appear since commit 2bd259c",
                     "Playback worked before WORK-000280 and is broken after it; restore the previous behaviour"):
            self.assertEqual(rc.classify(text).final_type, "REGRESSION", text)

    def test_performance_needs_a_performance_problem(self):
        self.assertEqual(rc.classify("The GUI slows to 1 FPS when live detection is enabled").final_type, "PERFORMANCE")
        self.assertEqual(rc.classify("Diagnose the message table performance: severe processing slowdown").final_type,
                         "PERFORMANCE")
        # A "## Performance" section in a feature spec is not a performance problem.
        self.assertEqual(rc.supporting_evidence(rc.extract_evidence(FEATURE_SPEC), "PERFORMANCE"), [])

    def test_classification_reads_the_whole_request_not_only_the_title(self):
        text = "Chart rendering\n\nSince the last update the chart no longer renders after a refresh; it used to work."
        self.assertEqual(rc.classify(text).final_type, "REGRESSION")
        text = "Playback\n\nThe application crashed with SIGSEGV when I pressed play (log/dump/App_20260922_1.core)."
        self.assertEqual(rc.classify(text).final_type, "CRASH")

    def test_nothing_classifiable_is_investigation_with_low_confidence_not_invented_evidence(self):
        c = rc.classify("hello there")
        self.assertEqual(c.final_type, "INVESTIGATION")
        self.assertEqual(c.confidence, "low")
        self.assertEqual(c.evidence, [])

    def test_no_title_keyword_overfitting(self):
        # The fix must generalise: the same shapes with other product words.
        self.assertEqual(rc.classify("Add Foo reception, Bar/Baz products and Project integration").final_type, "FEATURE")
        self.assertEqual(rc.classify("Qux integration fails to decode Quux").final_type, "BUG")
        self.assertEqual(rc.classify("Foo terminates unexpectedly during Bar").final_type, "CRASH")


class ValidationLayer(unittest.TestCase):
    def test_unsupported_crash_is_corrected_with_a_structured_reason(self):
        c = rc.validate(FEATURE_SPEC, "CRASH")
        self.assertEqual((c.proposed_type, c.normalized_type, c.final_type), ("CRASH", "CRASH", "FEATURE"))
        self.assertEqual(c.validation, "CORRECTED")
        self.assertIn("No crash/fatal-runtime evidence", c.correction_reason)
        d = c.to_dict()
        for key in ("proposed_type", "final_type", "heuristic_type", "validation", "classifier_reason",
                    "correction_reason", "evidence", "confidence"):
            self.assertIn(key, d)

    def test_unsupported_regression_performance_and_build_are_corrected(self):
        for proposed in ("REGRESSION", "PERFORMANCE", "BUILD"):
            c = rc.validate("Add support for HDF5 data files.", proposed)
            self.assertEqual(c.validation, "CORRECTED", proposed)
            self.assertEqual(c.final_type, "FEATURE", proposed)

    def test_unknown_vocabulary_is_rejected(self):
        for raw in ("FAILURE", "CRASH?", "Crash!", "feature | defect"):
            c = rc.validate("Add support for HDF5 data files.", raw)
            self.assertEqual(c.validation, "REJECTED", raw)
            self.assertIsNone(c.final_type)

    def test_ambiguous_unsupported_symptom_is_rejected_not_guessed(self):
        c = rc.validate("hello there", "CRASH")
        self.assertEqual(c.validation, "REJECTED")
        self.assertIn("classify again", c.correction_reason)

    def test_supported_symptom_passes_and_variants_are_normalized_visibly(self):
        c = rc.validate("I loaded a file and the application crashed.", "crash")
        self.assertEqual((c.validation, c.final_type), ("PASS", "CRASH"))
        self.assertIn("normalized", c.normalization_note)
        c = rc.validate("The GUI slows to 1 FPS when live detection is enabled", "PERFORMANCE")
        self.assertEqual((c.validation, c.final_type), ("PASS", "PERFORMANCE"))

    def test_non_symptom_override_is_honoured_and_the_heuristic_is_recorded(self):
        c = rc.validate("The chart pans backwards in TopDown mode.", "FEATURE")
        self.assertEqual((c.validation, c.final_type, c.heuristic_type), ("PASS", "FEATURE", "BUG"))
        self.assertIn("override FEATURE accepted", c.classifier_reason)

    def test_heuristic_path_is_a_pass_with_reason(self):
        c = rc.validate("Add support for HDF5 data files.", None)
        self.assertEqual((c.proposed_source, c.validation, c.final_type), ("heuristic", "PASS", "FEATURE"))


# --------------------------------------------------------------------------- 2. real records

def _find_record(request_id: str):
    for p in (REPO / "requests").glob("*/*.md"):
        if p.name.lower().endswith(request_id.lower() + ".md"):
            return p
    return None


class RealRecords(unittest.TestCase):
    """The records that exposed the defect, used verbatim as fixtures.

    PROJECT-SPECIFIC: these name request records of the source project; in a
    repository that does not hold them every case skips."""

    FIXTURES = [
        ("REQ-CHAT-20260921-028", "FEATURE"),   # external backend reception spec (filed as CRASH)
        ("REQ-CHAT-20260921-026", "FEATURE"),   # generator TX tools, simulation (filed as CRASH)
        ("REQ-CHAT-20260921-025", "FEATURE"),   # generic decoded messages + packet integration (filed as CRASH)
        ("REQ-CHAT-20260922-005", "FEATURE"),   # application acquisition & pipeline control
        ("REQ-CHAT-20260922-006", "BUG"),       # Activity Time column mixes time bases (filed as DEFECT)
        ("REQ-CHAT-20260923-001", "BUG"),       # a UI test ... fails deterministically
        ("REQ-CHAT-20260921-031", "FEATURE"),   # chart gridline configuration (filed as REGRESSION)
        ("REQ-CHAT-20260921-020", "PERFORMANCE"),  # Correlation engine performance audit (filed as REGRESSION)
    ]

    def _text(self, p: Path) -> str:
        js = p.with_suffix(".json")
        if js.exists():
            try:
                t = json.loads(js.read_text(encoding="utf-8")).get("original_text")
                if t:
                    return t
            except ValueError:
                pass
        return rc.original_statement_of(p.read_text(encoding="utf-8", errors="ignore"))

    def test_fixture_records_classify_as_expected(self):
        for rid, expected in self.FIXTURES:
            p = _find_record(rid)
            if p is None:
                self.skipTest(f"{rid} not present in requests/")
            c = rc.classify(self._text(p))
            self.assertEqual(c.final_type, expected, f"{rid}: {c.classifier_reason}")

    def test_crash_filed_specifications_have_no_crash_evidence(self):
        for rid in ("REQ-CHAT-20260921-028", "REQ-CHAT-20260921-026", "REQ-CHAT-20260921-025"):
            p = _find_record(rid)
            if p is None:
                self.skipTest(f"{rid} not present in requests/")
            ev = rc.extract_evidence(self._text(p))
            self.assertEqual(rc.supporting_evidence(ev, "CRASH"), [], rid)
            self.assertEqual(rc.validate(self._text(p), "CRASH").final_type, "FEATURE", rid)

    def test_review_flags_the_stored_crash_type_as_unsupported(self):
        p = _find_record("REQ-CHAT-20260921-028")
        if p is None:
            self.skipTest("REQ-CHAT-20260921-028 not present")
        md = p.read_text(encoding="utf-8", errors="ignore")
        r = rc.review_record(md, json.loads(p.with_suffix(".json").read_text()) if p.with_suffix(".json").exists() else None)
        self.assertEqual(r["heuristic_type"], "FEATURE")
        # UNSUPPORTED while the historical CRASH label stands; OK once a human reclassifies it.
        self.assertIn(r["verdict"], ("UNSUPPORTED", "OK", "DIFFERS"))
        if r["stored_type"] == "CRASH":
            self.assertEqual(r["verdict"], "UNSUPPORTED")

    def test_the_utc_dated_record_shows_the_date_defect(self):
        # REQ-CHAT-20260923-001 was created 2026-09-22 20:32 America/New_York, i.e. 2026-09-23 00:32 UTC.
        p = _find_record("REQ-CHAT-20260923-001")
        if p is None:
            self.skipTest("REQ-CHAT-20260923-001 not present")
        data = json.loads(p.with_suffix(".json").read_text(encoding="utf-8"))
        created_utc = datetime.fromisoformat(data["created"])
        local = created_utc.astimezone(ZoneInfo("America/New_York"))
        self.assertEqual(local.strftime("%Y%m%d"), "20260922")
        self.assertIn("20260923", data["request_id"])   # the defect, preserved (ids are never renumbered)


# --------------------------------------------------------------------------- 3. CLI

class Repo:
    def __init__(self):
        tmp_base = REPO / "tmp" / "process-tests"
        tmp_base.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix="request-classification-", dir=str(tmp_base))
        self.base = Path(self.tmp.name)
        self.root = self.base / "repo"
        self.root.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=str(self.root), check=True)
        for s in proc.REQUEST_STATES:
            (self.root / "requests" / s).mkdir(parents=True, exist_ok=True)
            (self.root / "requests" / s / "README.md").write_text(f"# {s}\n")
        self.env = {k: v for k, v in os.environ.items() if k != "AGENT_ID"}
        self.env["AGENT_PROCESS_ROOT"] = str(self.root)

    def close(self):
        self.tmp.cleanup()

    def run(self, script, *args, env=None):
        e = dict(self.env)
        if env:
            e.update(env)
        return subprocess.run([sys.executable, str(SCRIPTS / script), *args], cwd=str(self.root), env=e,
                              capture_output=True, text=True, timeout=120)

    def inbox(self):
        return sorted(p for p in (self.root / "requests" / "inbox").glob("*.md") if p.name != "README.md")


class IntakeCli(unittest.TestCase):
    def setUp(self):
        self.repo = Repo()

    def tearDown(self):
        self.repo.close()

    def submit(self, text, *extra, env=None):
        cp = self.repo.run("create_request.py", "--source", "chat", "--text", text, *extra, env=env)
        return cp

    def record(self, cp):
        m = re.search(r"Request ID: (\S+)", cp.stdout)
        self.assertIsNotNone(m, cp.stdout + cp.stderr)
        rid = m.group(1)
        hits = [p for p in self.repo.inbox() if rid.lower() in p.name.lower()]
        self.assertEqual(len(hits), 1)
        md = hits[0]
        return rid, md, json.loads(md.with_suffix(".json").read_text(encoding="utf-8"))

    def test_override_crash_without_evidence_is_corrected_before_persistence(self):
        cp = self.submit(FEATURE_SPEC, "--type", "CRASH")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        rid, md, data = self.record(cp)
        self.assertEqual(data["type"], "FEATURE")
        c = data["classification"]
        self.assertEqual((c["proposed_type"], c["final_type"], c["validation"]), ("CRASH", "FEATURE", "CORRECTED"))
        self.assertIn("No crash/fatal-runtime evidence", c["correction_reason"])
        text = md.read_text(encoding="utf-8")
        self.assertIn("**Type:** FEATURE", text)
        self.assertIn("validation: CORRECTED", text)
        self.assertIn("proposed_type: CRASH", text)
        self.assertIn("CORRECTED", cp.stdout)

    def test_unknown_type_is_rejected_and_nothing_is_written(self):
        for raw in ("FAILURE", "CRASH?", "feature | defect"):
            cp = self.submit("Add support for HDF5 data files.", "--type", raw)
            self.assertEqual(cp.returncode, proc.INVALID, raw + cp.stdout)
            self.assertIn("REJECTED", cp.stderr)
            self.assertEqual(self.repo.inbox(), [], raw)
        self.assertFalse(list((self.repo.root / "requests" / "inbox").glob("*.json")))

    def test_ambiguous_override_is_rejected_and_nothing_is_written(self):
        cp = self.submit("hello there", "--type", "CRASH")
        self.assertEqual(cp.returncode, proc.INVALID, cp.stdout)
        self.assertIn("classify again", cp.stderr)
        self.assertEqual(self.repo.inbox(), [])

    def test_case_variant_of_a_supported_type_is_normalized_and_recorded(self):
        cp = self.submit("I loaded a file and the application crashed.", "--type", "crash")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        _, md, data = self.record(cp)
        self.assertEqual(data["type"], "CRASH")
        self.assertEqual(data["classification"]["validation"], "PASS")
        self.assertIn("normalized", data["classification"]["normalization_note"])
        self.assertIn("**Type:** CRASH", md.read_text(encoding="utf-8"))

    def test_heuristic_classification_records_a_reason_and_evidence(self):
        cp = self.submit("Add support for HDF5 data files.")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        _, md, data = self.record(cp)
        c = data["classification"]
        self.assertEqual((c["proposed_source"], c["final_type"], c["validation"]), ("heuristic", "FEATURE", "PASS"))
        self.assertTrue(c["classifier_reason"])
        self.assertIn("classifier_reason:", md.read_text(encoding="utf-8"))

    def test_every_source_passes_through_the_same_validator(self):
        for source in ("chat", "inbox", "crash", "automation"):
            # The same statement through every source is deliberate here (duplicate intake is refused by default).
            cp = self.repo.run("create_request.py", "--source", source, "--text", FEATURE_SPEC, "--type", "CRASH",
                               "--allow-duplicate")
            self.assertEqual(cp.returncode, 0, source + cp.stderr)
            _, _, data = self.record(cp)
            self.assertEqual(data["type"], "FEATURE", source)
            self.assertEqual(data["classification"]["validation"], "CORRECTED", source)

    def test_invalid_priority_is_rejected(self):
        cp = self.submit("Add support for HDF5 data files.", "--priority", "URGENT")
        self.assertEqual(cp.returncode, proc.INVALID)
        self.assertEqual(self.repo.inbox(), [])

    def test_request_id_date_is_the_local_system_date_not_utc(self):
        for tz in ("Pacific/Kiritimati", "Pacific/Pago_Pago", "America/New_York"):
            cp = self.submit(f"Add support for HDF5 data files in {tz}.", env={"TZ": tz})
            self.assertEqual(cp.returncode, 0, cp.stderr)
            rid, md, data = self.record(cp)
            expected = datetime.now(ZoneInfo(tz)).strftime("%Y%m%d")
            self.assertEqual(rid.split("-")[2], expected, (tz, rid))
            self.assertTrue(md.name.startswith(datetime.now(ZoneInfo(tz)).strftime("%Y-%m-%d")), md.name)
            self.assertEqual(data["id_date"], datetime.now(ZoneInfo(tz)).strftime("%Y-%m-%d"))
            self.assertIn("UTC", data["timezone"])
            self.assertIn("created_utc", data)

    def test_text_and_caller_cannot_influence_the_id_or_its_date(self):
        text = ("REQ-CHAT-20990101-001 2099-01-01: add support for HDF5 data files. "
                "Request ID: REQ-CHAT-19990101-999 Created: 1999-01-01")
        cp = self.submit(text)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        rid, _, data = self.record(cp)
        self.assertEqual(rid.split("-")[2], proc.request_date()["yyyymmdd"])
        self.assertNotIn("2099", rid)
        self.assertIn("system-generated", cp.stdout)
        # No CLI option exists to supply an id, a date or a sequence number.
        helptext = self.repo.run("create_request.py", "--help").stdout
        for opt in ("--id", "--date", "--seq", "--number"):
            self.assertNotIn(opt, helptext)

    def test_concurrent_intakes_receive_unique_ids_in_the_local_series(self):
        go = self.repo.base / "go"
        waiter = self.repo.base / "waiter.py"
        waiter.write_text(WAITER)
        procs = []
        for i in range(6):
            cmd = [sys.executable, str(waiter), str(go), str(SCRIPTS / "create_request.py"),
                   "--source", "chat", "--text", f"concurrent intake probe {i}"]
            procs.append(subprocess.Popen(cmd, cwd=str(self.repo.root), env=self.repo.env,
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
        time.sleep(0.3)
        go.write_text("go")
        outs = [p.communicate(timeout=120) for p in procs]
        ids = []
        for out, err in outs:
            m = re.search(r"Request ID: (\S+)", out)
            self.assertIsNotNone(m, out + err)
            ids.append(m.group(1))
        self.assertEqual(len(set(ids)), 6, ids)
        today = proc.request_date()["yyyymmdd"]
        self.assertTrue(all(i.startswith(f"REQ-CHAT-{today}-") for i in ids), ids)
        self.assertEqual(len(self.repo.inbox()), 6)

    def _hand_dropped_record(self):
        # A record that reached the inbox by hand (or before the validator) with an unsupported type.
        md = self.repo.root / "requests" / "inbox" / "2026-09-21-hand-dropped-req-chat-20260921-050.md"
        md.write_text("# Request: External backend reception\n\n**Request ID:** REQ-CHAT-20260921-050\n"
                      "**Source:** chat\n**Type:** CRASH\n**Priority:** NORMAL\n**Created:** 2026-09-21T21:08:56+00:00\n"
                      "**Status:** inbox\n**Attachments:** - none\n\n## Original User Statement\n\n> "
                      + FEATURE_SPEC.replace("\n", "\n> ") + "\n\n## Classification\n\n- Type: CRASH (auto-classified)\n"
                      "- Priority: NORMAL\n- Source: chat\n\n## Processing Notes\n\n- none\n", encoding="utf-8")
        md.with_suffix(".json").write_text(json.dumps({"request_id": "REQ-CHAT-20260921-050", "source": "chat",
                                                       "type": "CRASH", "priority": "NORMAL", "status": "inbox",
                                                       "original_text": FEATURE_SPEC}, indent=2), encoding="utf-8")
        return md

    def test_review_reports_an_unsupported_stored_type_without_modifying_it(self):
        md = self._hand_dropped_record()
        before = (md.read_text(encoding="utf-8"), md.with_suffix(".json").read_text(encoding="utf-8"))
        cp = self.repo.run("create_request.py", "--file", str(md))
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("UNSUPPORTED", cp.stdout)
        self.assertIn("FEATURE", cp.stdout)
        self.assertEqual((md.read_text(encoding="utf-8"), md.with_suffix(".json").read_text(encoding="utf-8")), before)
        strict = self.repo.run("create_request.py", "--file", str(md), "--strict")
        self.assertEqual(strict.returncode, proc.INVALID)
        allrun = self.repo.run("create_request.py", "--review-all")
        self.assertEqual(allrun.returncode, 0, allrun.stderr)
        self.assertIn("UNSUPPORTED", allrun.stdout)

    def test_explicit_reclassification_corrects_the_record_and_keeps_history(self):
        md = self._hand_dropped_record()
        cp = self.repo.run("create_request.py", "--file", str(md), "--reclassify")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        text = md.read_text(encoding="utf-8")
        self.assertIn("**Type:** FEATURE", text)
        self.assertNotIn("**Type:** CRASH", text)
        self.assertIn("reclassified", text)
        self.assertIn(FEATURE_SPEC.splitlines()[0], text)          # original statement untouched
        data = json.loads(md.with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(data["type"], "FEATURE")
        hist = data["classification"]["history"]
        self.assertEqual((hist[-1]["from"], hist[-1]["to"]), ("CRASH", "FEATURE"))
        # A second, explicit proposal is validated the same way.
        again = self.repo.run("create_request.py", "--file", str(md), "--reclassify", "--type", "CRASH")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("**Type:** FEATURE", md.read_text(encoding="utf-8"))
        bad = self.repo.run("create_request.py", "--file", str(md), "--reclassify", "--type", "FAILURE")
        self.assertEqual(bad.returncode, proc.INVALID)

    def test_preflight_warns_about_an_unsupported_type(self):
        md = self._hand_dropped_record()
        cp = self.repo.run("preflight.py", "--request", str(md))
        self.assertIn("has no supporting evidence", cp.stdout + cp.stderr)
        self.assertIn("CRASH", cp.stdout + cp.stderr)


if __name__ == "__main__":
    unittest.main()
