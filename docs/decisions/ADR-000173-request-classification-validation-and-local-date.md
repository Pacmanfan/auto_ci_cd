# ADR-000173: Request types are a closed vocabulary validated deterministically against the request text; request ids carry the local system date

**Status:** ACCEPTED (process-change, human review per `docs/process/process-protection.md`)
**Date:** 2026-09-22
**Requirements:** `docs/requirements/development_operations.md` REQ-DEVOPS-045 (new), REQ-DEVOPS-014 and REQ-DEVOPS-003 amended
**Work:** WORK-000288
**Request:** REQ-CHAT-20260923-004
**Audit:** `docs/audits/request-classification-audit-2026-09-22.md`

## Context

`scripts/create_request.py` is the single intake for chat, inbox, crash and
automation requests (REQ-DEVOPS-014). Until this decision its type
assignment was a first-match keyword scan over the *whole* request text with
`CRASH` first in the table:

```python
CLASS_KEYWORDS = {"CRASH": [r"\bcrash", r"\babort", ..., r"\bkilled\b"], "REGRESSION": [r"\bregression", ...], ...}
for label, patterns in CLASS_KEYWORDS.items():
    for pat in patterns:
        if re.search(pat, lower): return label
```

A 40 KB feature specification that says "the application must not crash",
lists "crash detection" as a supervision capability, or marks a run failed
"if the backend process ... was killed" was therefore filed as `CRASH`
(REQ-CHAT-20260921-025/026/028); any specification with a "Regression
tests" section was filed as `REGRESSION` (REQ-CHAT-20260921-020/031); the
default for everything else was `INVESTIGATION` (42 records). `--type`
accepted any string, so the stores also hold `audit`, `bug`, `change`,
`FIX`, `BUGFIX`, `feature | defect | question | maintenance`. There was no
validation between the text and the type, and no diagnostic record of why a
type was chosen.

Independently, the id series and file name used `datetime.now(timezone.utc)`.
The development host runs `America/New_York`; a request filed at 20:32 local
on 2026-09-22 became `REQ-CHAT-20260923-001`. The author of the request could
not influence the date (there is no option for it), but the date was not the
authoritative local date either.

The intake is not an LLM: the agent (an LLM) calls the CLI and may pass
`--type`. Both the heuristic and the override therefore have to be
validated by deterministic code before persistence.

## Decision

1. **Closed vocabulary.** `scripts/request_classification.py` defines
   `RequestType` = `FEATURE, BUG, CRASH, REGRESSION, PERFORMANCE, REFACTOR,
   BUILD, DOCUMENTATION, INVESTIGATION, PROCESS-CHANGE` — the eight types
   `docs/process/chat-intake.md` already documented plus `REFACTOR` (asked
   for by the request) and `PROCESS-CHANGE` (the request type of the
   mechanism in `process-protection.md`). Case/whitespace variants and a
   short alias table (`DEFECT`/`BUGFIX`/`FIX` → `BUG`, `PROCESS` →
   `PROCESS-CHANGE`, `PERF`, `DOC(S)`, `ENHANCEMENT`, `REFACTORING`) are
   normalised **and reported**; anything else (`CRASH?`, `FAILURE`,
   `feature | defect`) is rejected. Priorities are the closed set
   `CRITICAL/HIGH/NORMAL/LOW`; unknown values are rejected instead of
   silently becoming `NORMAL`.

2. **Explicit semantics with evidence rules.** `CRASH`, `REGRESSION`,
   `PERFORMANCE` and `BUILD` are *symptom* types that require evidence of
   that symptom in the text. Evidence is a sentence that **reports** the
   symptom: mentions that are negated ("must not crash"), conditional or
   handling specifications ("if the process was killed, mark the run
   failed"), capability nouns ("crash detection"), subjects of a
   requirement ("decoder crashes must be visible"), items of an example or
   condition list, fenced code, quoted spans, section headings, and the
   prose of a section whose heading is a type name are *meta* mentions and
   never evidence. Generic failure language (fails, failure, broken, error,
   issue, does not work) is `BUG` evidence, never `CRASH`. `REGRESSION`
   needs a previously working state ("used to work", "no longer appears
   since commit", "broke after", "restore the previous behaviour"); the
   bare noun "regression" counts only with regression semantics ("the
   regression is caused by", "regression in X") and never as "regression
   test(s)/suite/gate/scenario/threshold". `PERFORMANCE` needs a
   performance or resource problem stated as the primary problem (head of
   the request) or dominating the body. The classifier reads the whole
   request but decides the non-symptom types (`BUG` vs `FEATURE` vs
   `REFACTOR`/`DOCUMENTATION`/`INVESTIGATION`/`PROCESS-CHANGE`) from the
   head — the title, the opening paragraph and the first
   Objective/Mission/Summary/Problem section — because that is where a
   request states its primary problem. When no rule fires the result is
   `INVESTIGATION` with `confidence: low`; evidence is never invented.

3. **Deterministic validation is authoritative.** `validate(text, proposed)`
   runs between the proposal (`--type`, whether typed by a human or an LLM)
   and persistence: unknown vocabulary → `REJECTED`; a symptom type without
   supporting evidence → `CORRECTED` to the heuristic type when that is
   confident, otherwise `REJECTED` with an instruction to classify again; any
   other proposal → `PASS` (the proposer's judgement between e.g. `FEATURE`
   and `BUG` stands, with the heuristic recorded beside it). A rejected
   request writes nothing (exit `INVALID`). Every source (`chat`, `inbox`,
   `crash`, `automation`) goes through the same function; there is exactly
   one classification path.

4. **Structured observability.** The sidecar carries
   `classification: {proposed_type, proposed_source, normalized_type,
   heuristic_type, final_type, confidence, validation, classifier_reason,
   correction_reason, normalization_note, evidence[], scores}` and the
   markdown `## Classification` block repeats the decisive fields. Corrected
   classifications say so (`validation: CORRECTED`, `correction_reason: No
   crash/fatal-runtime evidence ...`).

5. **System metadata.** The id date and file-name date come from
   `agent_process.request_date()` — the local system clock in the host's
   configured timezone — and the number from the global monotonic allocator
   (ADR-000090, unchanged; verified unique under concurrent creation). The
   sidecar records `id_date`, `timezone` (e.g. `EDT (UTC-04:00)`), `created`
   (local ISO with offset) and `created_utc`. No option supplies an id, a
   date or a sequence number, and the request text cannot influence them.

6. **Historical records are reviewed, not rewritten.** `create_request.py
   --file <record>` reports a record's stored type against its text
   (`OK`, `DIFFERS`, `NON-CANONICAL`, `INVALID-VOCABULARY`, `UNSUPPORTED`)
   without modifying it; `--review-all` reports every record under
   `requests/`; `--reclassify [--type T]` is the explicit, recorded
   migration for one record (the `**Type:**` line and the sidecar `type`
   are replaced, the previous value is appended to
   `classification.history`, the original statement is untouched).
   `scripts/preflight.py` warns at claim time when a request's stored type
   is invalid or unsupported, so hand-dropped inbox files are also checked
   before work starts. Ids are never renumbered: `REQ-CHAT-20260923-001`
   keeps its UTC-shifted date.

## Consequences

* The three feature specifications filed as `CRASH` (REQ-CHAT-20260921-025,
  -026, -028) and the two filed as `REGRESSION` (-020, -031) are reported as
  `UNSUPPORTED` by `--review-all` and left for their owners to reclassify
  with `--reclassify`; this request's own record (filed as `CRASH` by the
  old intake because its text discusses the word) is reclassified to
  `PROCESS-CHANGE` as the live demonstration.
* Existing tests that computed "today" as the UTC date
  (`test_submit_without_processing.py`, `test_process_concurrency.py`) now
  use `proc.request_date()`, the tool's own definition.
* The classifier is heuristic and evidence-based, not a keyword table for
  the known examples; `scripts/tests/test_request_classification.py` proves
  the request's examples with different product words as well.
* `AGENTS.md` and `docs/process/chat-intake.md` now state the semantics and
  that ids/dates/sequence numbers are system metadata the agent must not
  generate or guess.

## Alternatives rejected

* *Fix the keyword table* (order `FEATURE` first, drop `crash` from the
  list): still title-blind, still free-form `--type`, and it would hide the
  known examples rather than fix classification.
* *Title-only classification*: the request explicitly requires the whole
  text (a short chat body may carry the crash report after a bland title).
* *Reject every override that disagrees with the heuristic*: the heuristic
  is not better than a human at `FEATURE` vs `BUG`; the only proposals the
  validator overrules are symptom types without evidence, where the cost of
  a wrong `CRASH` (crash-investigation workflow, dump hunting) is high.
* *Keep UTC ids*: unambiguous but wrong for the people reading them; the
  request defines the authoritative date as the system date at creation.
