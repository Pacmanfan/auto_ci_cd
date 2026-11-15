#!/usr/bin/env python3
"""Request type vocabulary, semantic classification and deterministic validation
(REQ-DEVOPS-045, ADR-000173, WORK-000288).

This module is the single classification/validation layer for every request
creation path (``scripts/create_request.py`` for chat, inbox, crash and
automation sources; ``scripts/preflight.py`` for records that reached the
inbox by hand). It has no dependencies beyond the standard library so it can
be imported by the process tooling and by tests.

Vocabulary
----------
``RequestType`` is the closed, canonical request-type vocabulary. Free-form
strings are never accepted: ``normalize_type`` maps case/whitespace variants
and a short list of documented aliases (``DEFECT`` -> ``BUG`` ...) onto the
canonical value and reports that it did so; anything else is rejected.

Semantics (normative text: REQ-DEVOPS-045)
------------------------------------------
* ``FEATURE``        new capability, integration, enhancement or workflow.
* ``BUG``            existing behaviour is incorrect or fails under a defined
                     condition (a deterministic test failure is a BUG).
* ``CRASH``          an actual fatal runtime failure: crash, segmentation
                     fault, abort, fatal exception, unexpected termination.
                     Generic failure language ("fails", "broken", "error",
                     "issue") never implies CRASH.
* ``REGRESSION``     previously working behaviour has become broken; needs
                     evidence of the earlier working state.
* ``PERFORMANCE``    behaviour is correct but latency / throughput / CPU /
                     GPU / memory / dropped samples / responsiveness are not.
* ``REFACTOR``       internal restructuring without a material behaviour change.
* ``BUILD``          the tree does not build / compile / link.
* ``DOCUMENTATION``  documentation is the deliverable.
* ``INVESTIGATION``  a question or analysis with no committed change.
* ``PROCESS-CHANGE`` a change to the development system itself
                     (docs/process/process-protection.md).

Classification reads the *entire* request but weighs the head (title and
opening statement) as the statement of the primary problem. Evidence for
the symptom types (CRASH, REGRESSION, PERFORMANCE, BUILD) is only counted
when the sentence reports an observed symptom: mentions that are negated
("must not crash"), conditional or handling specifications ("if the process
was killed, report ..."), capability nouns ("crash detection"), example
lists, fenced code, backticked spans and section headings are *meta*
mentions and are ignored. The classifier never invents evidence: when no
rule fires the result is ``INVESTIGATION`` with ``confidence == "low"``.

Validation
----------
``validate(text, proposed)`` is the authoritative stage between a proposed
type (an operator's or an LLM's ``--type``) and persistence:

* unknown vocabulary                       -> ``REJECTED``
* CRASH / REGRESSION / PERFORMANCE / BUILD
  without supporting evidence in the text  -> ``CORRECTED`` to the heuristic
                                              type when that is confident,
                                              otherwise ``REJECTED`` so the
                                              caller classifies again
* everything else                          -> ``PASS`` (the proposal stands;
                                              the heuristic type is recorded
                                              beside it for diagnostics)

Every outcome is a structured ``Classification`` (``to_dict()``) that the
intake writes into the request sidecar and markdown, so a developer can
always see the proposed type, the final type, the validation verdict, the
reason and the exact evidence phrases.
"""
from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field, asdict
from typing import Iterable, Optional


class RequestType(str, enum.Enum):
    FEATURE = "FEATURE"
    BUG = "BUG"
    CRASH = "CRASH"
    REGRESSION = "REGRESSION"
    PERFORMANCE = "PERFORMANCE"
    REFACTOR = "REFACTOR"
    BUILD = "BUILD"
    DOCUMENTATION = "DOCUMENTATION"
    INVESTIGATION = "INVESTIGATION"
    PROCESS_CHANGE = "PROCESS-CHANGE"

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return self.value


CANONICAL_TYPES: tuple[str, ...] = tuple(t.value for t in RequestType)

# Documented aliases. Normalisation is explicit (reported in the diagnostics),
# never silent, and nothing outside this table and the canonical values is
# accepted.
TYPE_ALIASES: dict[str, str] = {
    "DEFECT": "BUG",
    "BUGFIX": "BUG",
    "FIX": "BUG",
    "ENHANCEMENT": "FEATURE",
    "PERF": "PERFORMANCE",
    "DOC": "DOCUMENTATION",
    "DOCS": "DOCUMENTATION",
    "PROCESS": "PROCESS-CHANGE",
    "PROCESS_CHANGE": "PROCESS-CHANGE",
    "REFACTORING": "REFACTOR",
}

# Types whose selection requires evidence of that symptom in the text.
EVIDENCE_REQUIRED: frozenset[str] = frozenset({"CRASH", "REGRESSION", "PERFORMANCE", "BUILD"})

VALID_PRIORITIES: tuple[str, ...] = ("CRITICAL", "HIGH", "NORMAL", "LOW")


def normalize_type(raw: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Map ``raw`` onto the canonical vocabulary.

    Returns ``(canonical, note)``; ``canonical`` is ``None`` when the value is
    not acceptable. ``note`` describes the normalisation that was applied
    (``None`` when the value was already canonical).
    """
    if raw is None:
        return None, "no type given"
    s = str(raw).strip()
    if not s:
        return None, "empty type"
    key = re.sub(r"[\s_]+", "-", s.upper()).replace("PROCESS-CHANGE", "PROCESS-CHANGE")
    key_alias = key.replace("-", "_")
    if key in CANONICAL_TYPES:
        return key, (None if str(raw) == key else f"normalized {str(raw)!r} -> {key}")
    if key_alias in TYPE_ALIASES:
        canon = TYPE_ALIASES[key_alias]
        return canon, f"alias {s!r} -> {canon}"
    if key in TYPE_ALIASES:
        canon = TYPE_ALIASES[key]
        return canon, f"alias {s!r} -> {canon}"
    return None, (f"unknown request type {s!r}; valid: " + ", ".join(CANONICAL_TYPES)
                  + "; aliases: " + ", ".join(sorted(TYPE_ALIASES)))


def normalize_priority(raw: Optional[str]) -> Optional[str]:
    if raw is None or not str(raw).strip():
        return "NORMAL"
    s = str(raw).strip().upper()
    return s if s in VALID_PRIORITIES else None


# --------------------------------------------------------------------------- text model

_WORD = r"[A-Za-z0-9_\-/\.']+"

# Words that, when they precede a symptom term in the same sentence, mark the
# mention as negated (a robustness requirement, not an observation).
_NEGATION = re.compile(
    r"\b(?:must|should|shall|may|will|can|could|would|does|do|did|is|are|was|were|has|have)\s+(?:not|never)\b"
    r"|\b(?:mustn't|shouldn't|can't|cannot|doesn't|don't|didn't|isn't|aren't|wasn't|weren't|won't|never|without|no|not|avoid|avoids|avoiding|prevent|prevents|preventing|non)\b",
    re.I)

# Words that mark a symptom term as talked *about* (a category, an example, a
# condition to handle, a rule) rather than reported as observed.
_META = re.compile(
    r"\b(?:if|whenever|in\s+case|unless|handle|handles|handled|handling|detect|detects|detected|detection|"
    r"recover|recovers|recovery|report|reports|reported|reporting|tolerate|tolerates|survive|survives|"
    r"gracefully|graceful|robust|robustly|robustness|resilient|resilience|safe|safely|treat|treats|treated|"
    r"classify|classified|classifies|classification|classifier|means|mean|meaning|reserved|example|examples|"
    r"e\.g\.|such\s+as|including|include|includes|infer|inferred|imply|implies|implied|automatically|merely|"
    r"describe|describes|described|describing|concerns|concern|mention|mentions|mentioned|contain|contains|"
    r"containing|regarding|whether|evidence|requires|require|required|justify|justifies|assume|assumes|assumed|"
    r"potential|possible|possibly|hypothetical|scenario|scenarios|simulate|simulated|inject|injected|injection|"
    r"fixture|fixtures|expect|expected|expects|verify|verifies|verified|verification|would|could|might|"
    r"cause|causes|causing|introduce|introduces|introduced|introducing|trigger|triggers|triggered|"
    r"definition|define|defines|defined|semantics|vocabulary|category|categories|keyword|keywords|"
    r"indicator|indicators|indicate|indicates)\b",
    re.I)

# Category nouns built on a symptom word (``crash detection``) are meta.
_CRASH_NOUN_COMPOUND = re.compile(
    r"\b(?:crash|abort|segfault)[\s-]+(?:detection|detector|handling|handler|handlers|recovery|report|reports|"
    r"reporting|reporter|diagnostics|diagnostic|investigation|evidence|resilience|resilient|safe|safety|"
    r"classification|rule|rules|workflow|path|paths|artifacts|artefacts|test|tests|testing|tolerance|"
    r"protection|guard|guards|scenario|scenarios|simulation|injection|type|types|category|label|keyword|"
    r"keywords|word|words|term|terms|location|reproducibility|dump\s+availability)\b", re.I)

# Strong crash evidence: an event actually happened / a fatal signal or fault.
_CRASH_STRONG = re.compile(
    r"\b(?:crashes|crashed|crashing|segfaults|segfaulted|segfaulting|aborts|aborted|aborting|core[\s-]dumps|"
    r"core[\s-]dumped|dies|died|dying|terminates|terminated|terminating|exits|exited|exiting|quits|quit|"
    r"killed)\b"
    r"|\b(?:segmentation\s+fault|segfault|sigsegv|sigabrt|sigbus|sigill|sigfpe|sigkill|core\s+dump|"
    r"crash\s+dump|stack\s+trace|backtrace|fatal\s+(?:error|exception|signal)|assertion\s+failed|"
    r"assert(?:ion)?\s+fail(?:s|ed|ure)?|std::terminate|terminate\s+called|double\s+free|use[\s-]after[\s-]free|"
    r"heap\s+corruption|null\s+pointer\s+dereference|nullptr\s+dereference|access\s+violation|"
    r"unhandled\s+exception|application\s+crash|app\s+crash|process\s+crash|hard\s+crash|"
    r"reproducible\s+crash|crash\s+(?:on|when|while|during|after|in|at)\b|crash\s+report(?:ed)?\b|"
    r"(?:unexpected|unexpectedly)\s+(?:terminat\w+|exit\w*|die\w*|quit\w*|clos\w+|disappear\w*)|"
    r"(?:terminat\w+|exit\w*|die\w*|quit\w*|clos\w+|disappear\w*)\s+unexpectedly|"
    r"log/dump/|[a-z0-9]+_\d{8}_\d{6}_\d+\.core|\.core\s+(?:file|dump|files|dumps))",
    re.I)
# Weak crash evidence: the bare noun.
_CRASH_WEAK = re.compile(r"\b(?:crash|crashes|abort)\b", re.I)
# Words that give a crash verb an "unexpected" subject — the application, a
# process, a service — as opposed to the user quitting or a scheduled exit.
_CRASH_VERB_ONLY = re.compile(r"\b(?:exits|exited|exiting|quits|quit|dies|died|dying|terminates|terminated|"
                              r"terminating|killed|closes|closed)\b", re.I)
_CRASH_SUBJECT = re.compile(r"\b(?:application|app|process|service|daemon|gui|window|program|binary|"
                            r"executable|server|client|thread|worker|pipeline|decoder|backend|"
                            r"it)\b", re.I)
_REQUIREMENT_AFTER = re.compile(r"\s*(?:must|should|shall|are|is|will|can|need|needs|may|could|would|do|does|"
                                r"being|be|remain|remains|stay|stays|get|gets|become|becomes)\b", re.I)
_CRASH_UNEXPECTED = re.compile(r"\b(?:unexpected|unexpectedly|suddenly|silently|randomly|spontaneously|"
                               r"immediately|without\s+(?:warning|error|message|reason)|SIG[A-Z]+|"
                               r"exit\s+code\s+(?:1[0-9][0-9]|[1-9])|code\s+(?:13[0-9]|1[0-9]{2}))\b", re.I)

_REGRESSION = re.compile(
    r"\bregression\s+(?:in|of|since|after|from|between|introduced|caused|compared|versus|vs\.?|relative)\b"
    r"|\b(?:the|this|that|is\s+a|as\s+a|caused\s+a|introduced\s+a|introduces\s+a|fix(?:ing|es)?\s+(?:the|this|a)|"
    r"actual|real|genuine|observed|confirmed|reported|performance|timing|startup|rendering|behaviou?ral|functional|"
    r"visual|display|ui|gui)\s+regression\b(?![\s-]+(?:test|tests|testing|tested|suite|suites|gate|gates|check|checks|"
    r"invariant|invariants|guard|guards|fixture|fixtures|coverage|run|runs|group|groups|pass|passes|passing|risk|"
    r"protection|safety|matrix|threshold|thresholds|scenario|scenarios|artifact|artifacts|evidence|indicators?|for\b|\+))"
    r"|\bregressed\b|\bhas\s+regressed\b"
    r"|\b(?:used\s+to\s+work|previously\s+worked|worked\s+(?:before|previously|until|fine\s+(?:before|until|in)|"
    r"correctly\s+(?:before|until|in)|in\s+(?:the\s+)?(?:previous|last|earlier|old))|"
    r"no\s+longer\s+(?:works|work|shows|show|updates|update|appears|appear|renders|render|responds|respond|"
    r"loads|load|starts|start|plays|play|displays|display|opens|open|draws|draw|visible|possible|available|"
    r"correct|functional|honou?red|applied|persisted)|"
    r"stopped\s+working|stops\s+working|broke\s+(?:after|since|with|in|when|following)|"
    r"broken\s+(?:after|since|by|following)|"
    r"since\s+(?:commit|the\s+(?:recent|last|latest|previous)|work-\d+|req-|adr-|upgrading|updating|"
    r"migrating|the\s+migration|the\s+refactor|the\s+rewrite|the\s+port)|"
    r"after\s+(?:the\s+)?(?:recent|last|latest)\s+(?:change|changes|commit|commits|update|refactor|"
    r"migration|rewrite|ui\s+change|release|upgrade|merge)|"
    r"behaviou?r\s+changed\s+after|restore\s+(?:the\s+)?(?:previous|old|earlier|original)\s+"
    r"behaviou?r|worked\s+in\s+(?:the\s+)?(?:previous|last|earlier)|recently\s+broke|changed\s+recently|"
    r"was\s+working\s+(?:before|until|in)|were\s+working\s+(?:before|until|in)|"
    r"disappeared\s+after|went\s+away\s+after|lost\s+after|bad\s+vs\.?\s+good|good\s+vs\.?\s+bad)\b",
    re.I)
_REGRESSION_NEG = re.compile(r"\b(?:no|without|zero|any|avoid|avoids|prevent|prevents|introduce|introduces|"
                             r"introducing|cause|causes|causing|not|significant|unrelated|new|possible|"
                             r"potential|fps|frame[\s-]rate|performance|catch|catches|detect|detects|"
                             r"guard|guards|against|risk|treat|treated|as\s+a)\b", re.I)

_PERFORMANCE = re.compile(
    r"\b(?:slow|slowly|slower|slows|slowed|slowdown|slowdowns|slow\s+down|sluggish|lag|laggy|lagging|lags|choppy|stutter|stutters|stuttering|"
    r"excessive\s+(?:cpu|memory|gpu|ram|allocation|allocations|latency|load|time|copies|copying)|"
    r"(?:high|heavy)\s+(?:cpu|memory|gpu|ram)(?:\s+(?:usage|load|consumption|use))?|"
    r"(?:cpu|memory|gpu|ram)\s+(?:usage|consumption|footprint|pressure|spike|spikes|bound|hog|hogging)|"
    r"memory\s+leak|leaks\s+memory|leaking|leak|throughput|latency|latencies|"
    r"dropped\s+samples|drops\s+samples|dropping\s+samples|sample\s+drops|overrun|overruns|underrun|underruns|"
    r"frame\s*rate|fps|responsiveness|performance(?=\s*[:\-—]|\s+(?:problem|problems|issue|issues|is|of\s+the|remains|degrad\w+|drop\w*|impact))|"
    r"takes\s+(?:too\s+long|seconds|minutes|\d+\s*(?:s|ms|sec|seconds|minutes))|too\s+slow|too\s+long|"
    r"performance\s+(?:problem|problems|issue|issues|regression|regressions|degradation|audit|"
    r"optimi[sz]ation|optimi[sz]ations|bottleneck|bottlenecks|hot\s*spot|hot\s*spots|profile|profiling|"
    r"analysis|tuning|improvement|improvements)|"
    r"optimi[sz]e|optimi[sz]ed|optimi[sz]ation|optimi[sz]ing|speed\s+up|speed-up|faster|"
    r"startup\s+time|start-up\s+time|load\s+time|render\s+time|processing\s+rate|processing\s+time|"
    r"real[\s-]?time\s+(?:can(?:no)?t|cannot|fails|falls\s+behind)|falls\s+behind|keeps?\s+up|"
    r"backlog\s+grows|busy\s+loop|busy-loop|spinning|spins|blocks\s+the\s+(?:ui|gui|main)\s+thread|"
    r"blocking\s+the\s+(?:ui|gui|main)\s+thread|jank|janky|bogs\s+down|grinds|pegged|pegs\s+(?:a|the)\s+core|"
    r"100\s*%\s*cpu|consumes\s+(?:excessive|too\s+much|all)|eats\s+(?:cpu|memory))\b",
    re.I)
_PERFORMANCE_NEG = re.compile(r"\b(?:no|without|not|avoid|avoids|prevent|prevents|do\s+not|don't|never|"
                              r"must\s+not|should\s+not|shall\s+not|unnecessary|unnecessarily|significant|"
                              r"measure|measured|measuring|acceptable|budget|target|targets|keep|keeps|"
                              r"maintain|maintains|preserve|preserves|ensure|ensures)\b", re.I)

_BUILD = re.compile(
    r"\b(?:build\s+(?:fails|failure|failures|failed|error|errors|breaks|broke|broken|is\s+broken)|"
    r"(?:does|do|did)\s+not\s+(?:build|compile|link)|(?:doesn't|don't|didn't|won't|cannot|can't)\s+(?:build|compile|link)|"
    r"fails?\s+to\s+(?:build|compile|link)|compile\s+(?:error|errors|failure|failures|fails)|"
    r"compilation\s+(?:error|errors|failure|fails|failed)|compiler\s+error|link\s+(?:error|errors|failure|failed|fails)|"
    r"linker\s+error|linking\s+fails|undefined\s+reference|unresolved\s+external|-werror|"
    r"make\s+(?:fails|failed|error)|qmake\s+(?:fails|failed|error)|cannot\s+find\s+-l|ld\s+returned|"
    r"no\s+such\s+file\s+or\s+directory|non-compiling|not\s+compiling|tree\s+(?:does\s+not|doesn't)\s+(?:build|compile)|"
    r"broken\s+build|build\s+tree|shadow\s+build\s+(?:fails|failed|broken))\b",
    re.I)

_BUG = re.compile(
    r"\b(?:wrong|wrongly|incorrect|incorrectly|mismatch|mismatched|mismatches|mixes|mixed\s+up|inconsistent|"
    r"inconsistency|inconsistencies|fails|failing|failure|failures|failed|fails?\s+to|does\s+not|doesn't|isn't|"
    r"is\s+not|are\s+not|aren't|cannot|can't|can\s+not|unable|not\s+working|(?:doesn't|does\s+not|don't|do\s+not)\s+work|"
    r"broken|bug|bugs|defect|defects|unexpected|invalid|off\s+by\s+one|off-by-one|reversed|backwards|"
    r"stale|duplicate|duplicated|duplicates|garbage|corrupt|corrupted|corruption|ignored|ignores|overwritten|"
    r"truncated|misaligned|misclassif\w+|mis-classif\w+|misreported|mislabel\w+|misplaced|mispositioned|"
    r"misbehav\w+|malfunction\w*|never\s+(?:shows|updates|works|appears|renders|responds|loads|starts|plays|"
    r"displays|refreshes|clears|fires|arrives|matches|persists|saves)|still\s+(?:shows|displays|uses|reports|fails|"
    r"broken|wrong|missing|present|happens|occurs|visible|active|running|reports)|not\s+(?:honou?red|applied|"
    r"persisted|saved|restored|updated|refreshed|shown|displayed|rendered|reflected|propagated|respected|"
    r"cleared|reset|handled|visible|correct|sorted|filtered|selectable|clickable|editable|reproducible)|"
    r"does\s+nothing|has\s+no\s+effect|no\s+effect|flicker|flickers|flickering|glitch|glitches|hang|hangs|"
    r"hanging|hung|freeze|freezes|freezing|frozen|unresponsive|lockup|lock-up|locks\s+up|deterministic(?:ally)?\s+fail\w*|"
    r"fails?\s+deterministic\w*|intermittent\w*|flaky|race\s+condition|deadlock|deadlocks|not\s+(?:as\s+)?expected|"
    r"than\s+expected|than\s+it\s+should|(?:shows|displays|reports|returns|reads|writes|renders|draws|uses|"
    r"computes|calculates|produces|emits|prints)\s+(?:the\s+)?(?:wrong|incorrect|stale|old|garbage|empty|nothing|"
    r"zero|a\s+wrong|an\s+incorrect)|out\s+of\s+(?:sync|range|order|date)|\d+\s+(?:failures?|failing)|"
    r"failures?:\s*[1-9]|fix|fixes|fixed|fixing|repair|repairs|symptom|symptoms|reproduce|reproduces|reproducible|"
    r"repro|steps\s+to\s+reproduce|actual\s+(?:result|behaviou?r|value)|expected\s+(?:result|behaviou?r|value)|"
    r"workaround|keeps\s+(?:changing|resetting|reverting|switching|jumping|flipping|losing|showing|going)|"
    r"resets\s+to|reverts\s+to|wrong\s+(?:value|values|time|frequency|order|position|colou?r|size)|"
    r"(?:shows|displays|reports|is)\s+(?:always|constantly|permanently)\s+(?:zero|empty|blank|0|wrong))\b",
    re.I)

_FEATURE = re.compile(
    r"\b(?:add|adds|adding|added|implement|implements|implementing|implementation\s+of|introduce|introduces|"
    r"introducing|create|creates|creating|build\s+(?:a|an|the)\s+(?!tree|fails|error)|"
    r"new\s+(?:capability|capabilities|feature|features|window|windows|dialog|dialogs|page|pages|command|"
    r"commands|tool|tools|decoder|decoders|service|services|subsystem|subsystems|panel|panels|tab|tabs|"
    r"option|options|setting|settings|mode|modes|layer|layers|view|views|button|buttons|menu|menus|"
    r"export|import|format|formats|backend|backends|integration|integrations|workflow|workflows|column|"
    r"columns|control|controls|widget|widgets|api|operation|operations|pipeline|block|blocks|source|"
    r"sources|sink|sinks|plugin|plugins|module|modules|library|libraries|application|app|project|"
    r"workspace|manager|framework|engine|model|models|report|reports|action|actions|shortcut|shortcuts|"
    r"indicator|indicators|overlay|overlays|marker|markers|filter|filters|type|types|field|fields|"
    r"parameter|parameters|protocol|protocols|transport|transports|channel|channels|screen|screens|"
    r"wizard|wizards|editor|editors|viewer|viewers|browser|browsers|recorder|player|scheduler|"
    r"simulation|simulator|generator|standalone)|"
    r"support\s+for|supports?\s+(?:for|loading|reading|writing|decoding|encoding|multiple|additional|more|new|both)|"
    r"integrate|integrates|integrating|integration|integrations|enable|enables|enabling|extend|extends|extending|"
    r"expose|exposes|exposing|provide|provides|providing|capability|capabilities|feature|features|enhancement|"
    r"enhancements|enhance|enhances|allow|allows|allowing|configurable|option\s+to|ability\s+to|"
    r"design\s+and\s+implement|first-class|first\s+class|persistent\s+(?:\w+\s+){0,3}(?:controls|settings|"
    r"configuration|option|options)|framework|tooling|wire|wiring|wired|plumb|plumbing|make\s+(?:\w+\s+){0,3}"
    r"(?:available|possible|configurable|selectable|visible|persistent|start|open|show|default)|"
    r"so\s+that\s+(?:users|operators|the\s+user|an\s+operator|analysts|agents)\s+can|user\s+can|users\s+can|"
    r"operator\s+can|operators\s+can|should\s+be\s+able\s+to|be\s+able\s+to|would\s+like|i\s+want|we\s+want|"
    r"want\s+to|need\s+(?:a|an|to|support)|deliver|delivers|delivering|deliverable|deliverables|"
    r"objective|objectives|phase\s+\d|milestone|milestones|roadmap|acceptance\s+criteria|end-to-end|"
    r"(?:must|should|shall|needs?\s+to)\s+(?:always\s+|also\s+|now\s+)?(?:start|open|show|display|support|allow|"
    r"provide|persist|remember|expose|offer|include|render|draw|list|accept|load|save|export|import|be\s+able|"
    r"become|establish|use)|reuse|reusable|advance|turning\s+(?:it|the\s+\w+)\s+into|"
    r"turn\s+(?:it|the\s+\w+)\s+into|evolve|evolving|upgrade\s+(?:the|to)|"
    r"^\s*make\s+|^\s*(?:please\s+)?(?:add|implement|create|build|introduce|provide|extend|expose|integrate|"
    r"enable|allow|support|design|develop|generate|produce|wire|plumb))\b",
    re.I | re.M)

_REFACTOR = re.compile(
    r"\b(?:refactor|refactors|refactoring|refactored|restructure|restructuring|restructured|reorgani[sz]e|"
    r"reorgani[sz]ation|reorgani[sz]ing|clean\s*up|clean-up|cleanup|tidy|consolidate|consolidates|"
    r"consolidation|consolidating|deduplicate|deduplication|de-duplicate|dedup|extract\s+(?:a|an|the|into)|"
    r"split\s+(?:up|into|out)|move\s+(?:\w+\s+){0,4}(?:into|to|out\s+of)\s+(?:a|an|the|its\s+own|separate)|"
    r"de-qt|de-?qt'?d|qt-independent|qt\s+independent|qt-free|migrate\s+(?:\w+\s+){0,4}(?:to|into|out\s+of)|"
    r"migration\s+(?:of|to)|technical\s+debt|tech\s+debt|dead\s+code|simplify|simplifies|simplification|"
    r"rename|renames|renaming|reduce\s+duplication|remove\s+duplication|boundary|boundaries|"
    r"no\s+(?:behaviou?r|functional|user-visible)\s+change|behaviou?r-preserving|behaviou?r\s+preserving|"
    r"same\s+behaviou?r|modulari[sz]e|modulari[sz]ation|decouple|decoupling|decoupled|internal\s+restructur\w+|"
    r"architectural\s+(?:cleanup|clean-up|restructur\w+|consolidation)|remove\s+(?:the\s+)?(?:legacy|obsolete|"
    r"dead|unused)|retire|retires|retiring|replace\s+(?:\w+\s+){0,4}with)\b",
    re.I)

_DOCUMENTATION = re.compile(
    r"\b(?:documentation|document|documents|documented|documenting|docs|doc|readme|manual|manuals|user\s+guide|"
    r"developer\s+guide|guide|guides|help\s+page|help\s+pages|help\s+viewer|tutorial|tutorials|changelog|"
    r"change\s+log|release\s+notes|screenshots?|presentation|presentations|slides?|diagram|diagrams|"
    r"write[\s-]?up|wiki|faq|glossary|onboarding|handbook|runbook|how-to|howto|explain\s+(?:in|to)\s+the\s+(?:docs|manual))\b",
    re.I)

_INVESTIGATION = re.compile(
    r"\b(?:investigate|investigates|investigating|investigation|why\s+(?:does|is|do|did|are|was|were|can't|"
    r"cannot|doesn't|don't|isn't|aren't)|how\s+(?:does|do|is|are|can|could|should|would|would\s+we|"
    r"do\s+we|does\s+the|does\s+it)|what\s+(?:is|are|does|do|would|causes|happens|happened)|"
    r"where\s+(?:is|are|does|do)|which\s+(?:is|are|of)|audit|audits|auditing|analy[sz]e|analy[sz]es|"
    r"analy[sz]ing|analysis\s+of|determine|determines|determining|diagnose|diagnoses|diagnosing|diagnosis|"
    r"explain|explains|explaining|explanation|examine|examines|examining|understand|understanding|"
    r"root[\s-]cause|find\s+out|figure\s+out|research|researching|assess|assesses|assessment|evaluate|"
    r"evaluates|evaluation|compare|compares|comparison|review|reviews|reviewing|survey|study|feasibility|"
    r"question|questions|clarify|clarifies|clarification|is\s+it\s+possible|can\s+we|could\s+we|"
    r"should\s+we|do\s+we|does\s+the|status\s+of|report\s+on|summari[sz]e|summary\s+of|inventory|"
    r"look\s+into|check\s+whether|confirm\s+whether|tell\s+me|show\s+me|list\s+(?:the|all))\b",
    re.I)

_PROCESS_CHANGE = re.compile(
    r"\b(?:process[\s-]change|development[\s-]system|development\s+process|agent\s+constitution|agents\.md|"
    r"workflow\s+rules|lifecycle\s+rules|completion\s+gate|completion_gate|intake|request[\s-]generation|"
    r"request[\s-]creation|request\s+classification|request\s+id|request\s+ids|request\s+type|request\s+types|"
    r"drift_detect|drift\s+detection|doc_integrity|invariant_check|claim\.py|claims?\s+(?:lock|locks|ownership)|"
    r"commit_request|create_request|preflight|process_next_request|allocate_id|id\s+allocator|"
    r"state[\s-]model|traceability\s+rules|verification\s+rules|evidence\s+rules|autonomous\s+(?:agent|agents|"
    r"operating\s+model|development)|dev(?:elopment)?\s+ai|development\s+tooling|process\s+tooling|"
    r"scripts/[a-z_]+\.py|concurrent\s+agents|multi-agent|agent\s+workflow|work\s+items?|work\s+records?|"
    r"manifest|manifests|requirements\s+manifest|process\s+protection|escalation|needs-review)\b",
    re.I)

_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+")
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_FENCE = re.compile(r"^\s*(?:```|~~~)")
_META_BLOCK_START = re.compile(
    r"^\s*(?:[-*+]\s+)?(?:\*\*)?(?:examples?|strong\s+(?:\w+\s+)?indicators|(?:the\s+)?following\s+(?:do\s+not|does\s+not|"
    r"are\s+not|never|examples?|terms|words|phrases|list)|do\s+not\s+classify|never\s+classify|indicators?\s+include|"
    r"such\s+as|keywords?|vocabulary|(?:valid|canonical|known|accepted)\s+(?:types|values|labels)|"
    r"(?:handle|handles|handling|detect|detects|detection|report|reports|reporting|record|records|tolerate|"
    r"tolerates|treat|treats|surface|surfaces)\s+(?:\w+\s+){0,6}(?:when|if|for|of|the\s+following|these|"
    r"all\s+of|any\s+of|each\s+of)|(?:error|failure|fault)\s+(?:handling|modes?|conditions?|cases?|states?)|"
    r"(?:conditions|cases|states|situations|scenarios|failures|errors|faults|events|signals)\s+"
    r"(?:to\s+(?:handle|detect|report|record|cover|consider|surface|tolerate)|such\s+as|include|including|"
    r"like)|(?:must|should|shall)\s+(?:\w+\s+){0,4}(?:handle|detect|report|record|survive|tolerate|"
    r"recover\s+from)|(?:must|should|shall)\s+(?:be\s+)?(?:robust|resilient|tolerant)|"
    r"(?:mark|marks|flag|flags|classify|classifies|treat|treats)\s+(?:\w+\s+){0,4}(?:as\s+)?(?:failed|"
    r"unsuccessful|error|errors|failure|failures)\s+(?:if|when|for|on|the\s+following)).*(?::\s*$|:$|$)",
    re.I)


@dataclass
class Sentence:
    text: str
    where: str            # "head" | "body"
    meta: bool            # inside a meta block, fenced block, heading, example list
    quoted: list[tuple[int, int]] = field(default_factory=list)  # backticked spans
    bullet: bool = False


def _split_sentences(line: str) -> list[str]:
    # Sentence boundaries inside a line: '. ', '; ', ' — ', ' - ' bullets already split by lines.
    parts = re.split(r"(?<=[.;!?])\s+(?=[A-Z0-9(\"'`*])|\s+[—–]\s+|\s+\|\s+", line)
    return [p.strip() for p in parts if p and p.strip()]


def _clean_title(text: str) -> str:
    t = text.strip()
    t = re.sub(r"^\s*#\s*Request:\s*", "", t)
    return t


def _head_and_body(text: str) -> tuple[str, str]:
    """The head is the request's statement of its primary problem: the first
    heading/paragraph and the first Objective/Mission/Summary/Problem/Context
    section. The body is the whole text."""
    text = _clean_title(text)
    lines = text.splitlines()
    head_lines: list[str] = []
    # First heading + up to the first blank line after some prose.
    i = 0
    n = len(lines)
    while i < n and not lines[i].strip():
        i += 1
    # first heading(s)
    while i < n and _HEADING.match(lines[i]):
        head_lines.append(lines[i])
        i += 1
    # first paragraph(s) of prose (skip blank lines, stop after a paragraph)
    got_para = False
    while i < n:
        s = lines[i]
        if not s.strip():
            if got_para:
                break
            i += 1
            continue
        if _FENCE.match(s):
            break
        head_lines.append(s)
        got_para = True
        i += 1
    # first objective-like section
    m = re.search(r"^\s{0,3}#{1,6}\s+(?:\d+[.)]?\s*)?(?:objective|objectives|mission|goal|goals|summary|problem|"
                  r"problem\s+statement|symptom|symptoms|context|background|overview|purpose|issue|"
                  r"observed|current\s+behaviou?r|what\s+happens|description)\b.*$", text, re.I | re.M)
    if m:
        after = text[m.end():]
        sect = re.split(r"^\s{0,3}#{1,6}\s+", after, maxsplit=1, flags=re.M)[0]
        head_lines.append(sect.strip()[:1200])
    head = "\n".join(head_lines)[:2000] if head_lines else text[:600]
    return head, text


def _sentences(text: str) -> list[Sentence]:
    head, body = _head_and_body(text)
    head_sents: set[str] = set()
    hp: list[str] = []
    def _flush():
        if hp:
            for s_ in _split_sentences(" ".join(hp)):
                head_sents.add(s_)
            hp.clear()
    for line in head.splitlines():
        if not line.strip() or _FENCE.match(line):
            _flush(); continue
        if _HEADING.match(line) or _BULLET.match(line):
            _flush()
            for s_ in _split_sentences(_HEADING.sub("", _BULLET.sub("", line)).strip()):
                head_sents.add(s_)
            continue
        hp.append(line.strip())
    _flush()
    out: list[Sentence] = []
    in_fence = False
    meta_block = False
    meta_block_indent = None
    first_heading_seen = False
    meta_section = False
    # Join hard-wrapped prose into blocks first so clause context survives
    # line breaks: a heading, a bullet (with its indented continuation lines)
    # or a paragraph of consecutive plain lines is one block.
    blocks: list[tuple[str, str]] = []   # (kind, text) kind in fence|blank|heading|bullet|para
    para: list[str] = []
    for raw in body.splitlines():
        line = raw.rstrip()
        if _FENCE.match(line):
            if para:
                blocks.append(("para", " ".join(para))); para = []
            blocks.append(("fence", line)); continue
        if not line.strip():
            if para:
                blocks.append(("para", " ".join(para))); para = []
            blocks.append(("blank", "")); continue
        if _HEADING.match(line):
            if para:
                blocks.append(("para", " ".join(para))); para = []
            blocks.append(("heading", line)); continue
        if _BULLET.match(line):
            if para:
                blocks.append(("para", " ".join(para))); para = []
            blocks.append(("bullet", line)); continue
        if line.startswith((" ", "\t")) and blocks and blocks[-1][0] == "bullet" and not para:
            kind, prev = blocks[-1]
            blocks[-1] = (kind, prev + " " + line.strip()); continue
        para.append(line.strip())
    if para:
        blocks.append(("para", " ".join(para)))
    for kind, line in blocks:
        if kind == "fence":
            in_fence = not in_fence
            continue
        if kind == "blank":
            continue
        is_heading = kind == "heading"
        is_bullet = kind == "bullet"
        stripped = _HEADING.sub("", _BULLET.sub("", line)).strip()
        if is_heading and not first_heading_seen:
            first_heading_seen = True
            meta_here = in_fence
            meta_section = False
        elif is_heading:
            meta_block = False
            meta_here = True
            # "### REGRESSION" / "## CRASH rule": the section defines a category.
            name = re.sub(r"^\s*(?:\d+[.)]?\s*)?", "", stripped).strip().upper()
            name = re.sub(r"\s+(?:RULE|RULES|TYPE|TYPES|SEMANTICS|DEFINITION|TESTS?)$", "", name).strip("`* ")
            meta_section = name in CANONICAL_TYPES or name in TYPE_ALIASES
        elif meta_section:
            meta_here = True
        else:
            if _META_BLOCK_START.match(line) and (line.rstrip().endswith(":") or is_bullet or len(stripped.split()) <= 12):
                meta_block = True
                meta_block_indent = None
                meta_here = True
            elif meta_block and is_bullet:
                meta_here = True
            elif meta_block and not is_bullet:
                meta_block = False
                meta_here = in_fence
            else:
                meta_here = in_fence
        for s_ in _split_sentences(stripped):
            where = "head" if s_ in head_sents else "body"
            quoted = [(m.start(), m.end()) for m in re.finditer(r"`[^`]*`|\"[^\"]{0,80}\"|'[^']{0,60}'", s_)]
            out.append(Sentence(text=s_, where=where, meta=meta_here, quoted=quoted, bullet=is_bullet))
    return out


def _in_quoted(sent: Sentence, pos: int) -> bool:
    return any(a <= pos < b for a, b in sent.quoted)


def _window_before(text: str, pos: int, words: int = 8) -> str:
    before = text[:pos]
    toks = re.findall(_WORD, before)
    return " ".join(toks[-words:])


def _clause_before(text: str, pos: int, spill: int = 3) -> str:
    """The words of the clause that contains ``pos`` (back to the previous
    comma/semicolon/colon/parenthesis) plus ``spill`` words before that
    boundary, so a modal or negation just outside the clause still counts."""
    before = text[:pos]
    m = re.search(r"[,;:()\[\]]\s*(?=[^,;:()\[\]]*$)", before)
    if m:
        head = " ".join(re.findall(_WORD, before[:m.start()])[-spill:])
        return (head + " " + " ".join(re.findall(_WORD, before[m.end():]))).strip()
    return " ".join(re.findall(_WORD, before)[-12:])


def _window_after(text: str, pos: int, words: int = 4) -> str:
    toks = re.findall(_WORD, text[pos:])
    return " ".join(toks[:words])


def _paren_group(text: str, pos: int) -> Optional[str]:
    """The innermost parenthesised group containing ``pos``, if any."""
    depth = 0
    start = None
    for i in range(pos - 1, -1, -1):
        ch = text[i]
        if ch == ")":
            depth += 1
        elif ch == "(":
            if depth == 0:
                start = i
                break
            depth -= 1
    if start is None:
        return None
    end = text.find(")", pos)
    return text[start + 1:end if end != -1 else len(text)]


def _is_enumeration(text: str, min_parts: int = 4) -> bool:
    """A comma/semicolon-separated list of short noun phrases (a list of
    conditions, scenarios or test cases), not a report of one event."""
    parts = [p.strip() for p in re.split(r"[,;/]|\s+\|\s+", text) if p.strip()]
    if len(parts) < min_parts:
        return False
    lengths = sorted(len(p.split()) for p in parts)
    return lengths[len(lengths) // 2] <= 4


def _has_event_verb(phrase: str) -> bool:
    return bool(re.search(r"\b(?:crashes|crashed|crashing|segfaults|segfaulted|segfaulting|aborts|aborted|aborting|"
                          r"dumps|dumped|dies|died|dying|terminates|terminated|terminating|exits|exited|exiting|"
                          r"quits|quit|disappears|disappeared|killed|fault|sigsegv|sigabrt|sigbus|sigill|sigfpe|"
                          r"sigkill|trace|backtrace|failed|free|corruption|dereference|violation|exception|"
                          r"terminate|called|unexpected|unexpectedly|log/dump/|\.core)\b", phrase, re.I))


@dataclass
class Evidence:
    category: str
    where: str
    phrase: str
    strength: str      # "strong" | "weak"
    sentence: str
    note: str = ""     # "" | "enumeration" | "bare-label" | "bullet"

    def to_dict(self) -> dict:
        return {"category": self.category, "where": self.where, "phrase": self.phrase,
                "strength": self.strength, "note": self.note, "sentence": self.sentence[:200]}


def _crash_evidence(sent: Sentence) -> list[Evidence]:
    out: list[Evidence] = []
    if sent.meta:
        return out
    t = sent.text
    # A sentence that negates a crash anywhere ("decoder crashes do not crash
    # the application", "must never crash") is a robustness requirement, not a report.
    for m in _CRASH_WEAK.finditer(t):
        if _NEGATION.search(_window_before(t, m.start(), 4)):
            return out
    enumeration = _is_enumeration(t)
    short_bare = len(t.split()) <= 3
    for m in _CRASH_STRONG.finditer(t):
        if _in_quoted(sent, m.start()):
            continue
        clause = _clause_before(t, m.start())
        if _NEGATION.search(clause) or _META.search(clause):
            continue
        phrase = m.group(0)
        after = _window_after(t, m.end(), 2)
        if _CRASH_NOUN_COMPOUND.search(f"{phrase} {after}"):
            continue
        # A bare "exits"/"quits"/"killed"/"closes" verb is only a crash with an
        # application-like subject and an "unexpected" marker.
        if _CRASH_VERB_ONLY.fullmatch(phrase):
            if not _CRASH_UNEXPECTED.search(t):
                continue
        # "decoder crashes must be visible ..." — the crash word is the subject
        # of a requirement, not a reported event.
        if _REQUIREMENT_AFTER.match(t[m.end():]):
            continue
        strength, note = "strong", ""
        group = _paren_group(t, m.start())
        if enumeration or (group is not None and _is_enumeration(group, 3)):
            strength, note = "weak", "enumeration"     # an item in a list of conditions
        elif short_bare and not _has_event_verb(phrase):
            strength, note = "weak", "bare-label"
        elif sent.bullet and len(t.split()) <= 4:
            strength, note = "weak", "bullet"          # a short list item, not a report
        out.append(Evidence("CRASH", sent.where, phrase, strength, t, note))
    if not out:
        for m in _CRASH_WEAK.finditer(t):
            if _in_quoted(sent, m.start()):
                continue
            clause = _clause_before(t, m.start())
            if _NEGATION.search(clause) or _META.search(clause):
                continue
            after = _window_after(t, m.end(), 2)
            if _CRASH_NOUN_COMPOUND.search(f"{m.group(0)} {after}"):
                continue
            if _REQUIREMENT_AFTER.match(t[m.end():]):
                continue
            out.append(Evidence("CRASH", sent.where, m.group(0), "weak", t))
    return out


def _simple_evidence(sent: Sentence, category: str, rx: re.Pattern, neg: Optional[re.Pattern],
                     honour_meta: bool = True) -> list[Evidence]:
    out: list[Evidence] = []
    if honour_meta and sent.meta:
        return out
    t = sent.text
    symptom = category in EVIDENCE_REQUIRED or category == "BUG"
    enumeration = symptom and _is_enumeration(t)
    for m in rx.finditer(t):
        if _in_quoted(sent, m.start()):
            continue
        clause = _clause_before(t, m.start()) if symptom else _window_before(t, m.start(), 6)
        if neg is not None and neg.search(clause):
            continue
        if symptom and _META.search(clause):
            continue
        group = _paren_group(t, m.start()) if symptom else None
        strength, note = "strong", ""
        if enumeration or (group is not None and _is_enumeration(group, 3)):
            strength, note = "weak", "enumeration"
        elif symptom and sent.bullet and len(t.split()) <= 4:
            strength, note = "weak", "bullet"
        out.append(Evidence(category, sent.where, m.group(0), strength, t, note))
    return out


def extract_evidence(text: str) -> list[Evidence]:
    """All evidence phrases in ``text`` with their category, location and strength."""
    ev: list[Evidence] = []
    for sent in _sentences(text):
        ev.extend(_crash_evidence(sent))
        ev.extend(_simple_evidence(sent, "REGRESSION", _REGRESSION, _REGRESSION_NEG))
        ev.extend(_simple_evidence(sent, "PERFORMANCE", _PERFORMANCE, _PERFORMANCE_NEG))
        ev.extend(_simple_evidence(sent, "BUILD", _BUILD, None))
        ev.extend(_simple_evidence(sent, "BUG", _BUG, None))
        ev.extend(_simple_evidence(sent, "FEATURE", _FEATURE, None))
        ev.extend(_simple_evidence(sent, "REFACTOR", _REFACTOR, None))
        ev.extend(_simple_evidence(sent, "DOCUMENTATION", _DOCUMENTATION, None))
        ev.extend(_simple_evidence(sent, "INVESTIGATION", _INVESTIGATION, None))
        ev.extend(_simple_evidence(sent, "PROCESS-CHANGE", _PROCESS_CHANGE, None))
    return ev


# --------------------------------------------------------------------------- classification

@dataclass
class Classification:
    proposed_type: Optional[str]          # the raw proposal (``--type``), or None
    proposed_source: str                  # "override" | "heuristic"
    normalized_type: Optional[str]        # canonical form of the proposal, or None
    heuristic_type: str                   # what the text alone supports
    final_type: Optional[str]             # None when REJECTED
    confidence: str                       # "high" | "medium" | "low"
    validation: str                       # "PASS" | "CORRECTED" | "REJECTED"
    classifier_reason: str
    correction_reason: Optional[str] = None
    normalization_note: Optional[str] = None
    evidence: list[Evidence] = field(default_factory=list)
    scores: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["evidence"] = [e.to_dict() for e in self.evidence]
        return d

    def summary(self) -> str:
        s = f"proposed {self.proposed_type or '-'} ({self.proposed_source}) -> final {self.final_type or '-'} [{self.validation}]"
        if self.correction_reason:
            s += f": {self.correction_reason}"
        return s


def _count(ev: Iterable[Evidence], category: str, where: Optional[str] = None, strength: Optional[str] = None) -> int:
    return sum(1 for e in ev if e.category == category and (where is None or e.where == where)
               and (strength is None or e.strength == strength))


def _has(ev: Iterable[Evidence], category: str, **kw) -> bool:
    return _count(ev, category, **kw) > 0


def supporting_evidence(evidence: Iterable[Evidence], category: str) -> list[Evidence]:
    """Evidence that supports selecting ``category`` (the rule ``classify`` uses)."""
    ev = list(evidence)
    if category == "CRASH":
        return [e for e in ev if e.category == "CRASH" and (e.strength == "strong" or
                (e.where == "head" and not e.note and len(e.sentence.split()) <= 25))]
    if category in EVIDENCE_REQUIRED:
        return [e for e in ev if e.category == category and (e.strength == "strong" or e.where == "head")]
    return [e for e in ev if e.category == category]


def classify(text: str) -> Classification:
    """Deterministic, evidence-based classification of ``text``."""
    ev = extract_evidence(text or "")
    scores = {c: {"head": _count(ev, c, "head"), "body": _count(ev, c, "body")} for c in CANONICAL_TYPES}

    def result(t: str, conf: str, reason: str) -> Classification:
        return Classification(proposed_type=None, proposed_source="heuristic", normalized_type=None,
                              heuristic_type=t, final_type=t, confidence=conf, validation="PASS",
                              classifier_reason=reason, evidence=ev, scores=scores)

    # 1. Actual crash / fatal termination reported anywhere, or the head names one.
    crash = supporting_evidence(ev, "CRASH")
    if crash:
        return result("CRASH", "high", "reported fatal runtime failure: " + ", ".join(sorted({e.phrase for e in crash})[:4]))
    # 2. Previously working behaviour has regressed.
    reg = supporting_evidence(ev, "REGRESSION")
    if reg:
        return result("REGRESSION", "high", "evidence of previously working behaviour now broken: "
                      + ", ".join(sorted({e.phrase for e in reg})[:4]))
    # 3. Build breakage named in the head.
    if _has(ev, "BUILD", where="head"):
        return result("BUILD", "high", "build/compile/link failure in the request statement")
    # 4. Performance is the primary problem: named in the head, or the body is
    #    dominated by performance symptoms with no feature/bug framing in the head.
    head_perf = _count(ev, "PERFORMANCE", "head")
    head_feat = _count(ev, "FEATURE", "head")
    head_bug = _count(ev, "BUG", "head")
    if head_perf and head_perf >= max(head_feat, head_bug) * 0.5:
        return result("PERFORMANCE", "high", "performance/resource problem in the request statement: "
                      + ", ".join(sorted({e.phrase for e in ev if e.category == 'PERFORMANCE' and e.where == 'head'})[:4]))
    if not head_feat and not head_bug and _count(ev, "PERFORMANCE", "body") >= 2 and \
            _count(ev, "PERFORMANCE", "body") >= max(_count(ev, "FEATURE", "body"), _count(ev, "BUG", "body")):
        return result("PERFORMANCE", "medium", "performance/resource symptoms dominate the request body")
    # 5. Head framing: the statement of the primary problem decides between the
    #    non-symptom types. Ties: BUG > BUILD > PROCESS-CHANGE > REFACTOR > DOCUMENTATION > FEATURE > INVESTIGATION.
    order = ["BUG", "BUILD", "PROCESS-CHANGE", "REFACTOR", "DOCUMENTATION", "FEATURE", "INVESTIGATION"]
    weights = {"BUG": 1.0, "BUILD": 1.0, "PROCESS-CHANGE": 1.0, "REFACTOR": 1.0, "DOCUMENTATION": 0.8,
               "FEATURE": 1.0, "INVESTIGATION": 0.6}
    head_scores = {c: scores[c]["head"] * weights[c] for c in order}
    # PROCESS-CHANGE and DOCUMENTATION are only primary when they clearly lead.
    if head_scores["PROCESS-CHANGE"] and head_scores["PROCESS-CHANGE"] < max(head_scores["FEATURE"], head_scores["BUG"]):
        head_scores["PROCESS-CHANGE"] = 0
    if head_scores["DOCUMENTATION"] and head_scores["DOCUMENTATION"] <= max(head_scores["FEATURE"], head_scores["BUG"]):
        head_scores["DOCUMENTATION"] = 0
    if head_scores["INVESTIGATION"] and head_scores["INVESTIGATION"] < max(head_scores["FEATURE"], head_scores["BUG"]):
        head_scores["INVESTIGATION"] = 0
    best = max(order, key=lambda c: (head_scores[c], -order.index(c)))
    if head_scores[best] > 0:
        runner = sorted((head_scores[c] for c in order if c != best), reverse=True)[0]
        conf = "high" if head_scores[best] >= 2 and head_scores[best] > runner else "medium"
        phrases = sorted({e.phrase for e in ev if e.category == best and e.where == "head"})[:4]
        return result(best, conf, f"request statement is framed as {best.lower()}: " + ", ".join(phrases))
    # 6. Nothing in the head: fall back to the body with low confidence.
    body_scores = {c: scores[c]["body"] * weights[c] for c in order}
    best = max(order, key=lambda c: (body_scores[c], -order.index(c)))
    if body_scores[best] > 0:
        phrases = sorted({e.phrase for e in ev if e.category == best})[:4]
        return result(best, "low", f"no framing in the request statement; body suggests {best.lower()}: " + ", ".join(phrases))
    return result("INVESTIGATION", "low", "no classifiable statement; recorded for investigation")


def validate(text: str, proposed: Optional[str]) -> Classification:
    """Validate a proposed type against the text (the authoritative stage)."""
    auto = classify(text)
    if proposed is None or not str(proposed).strip():
        return auto
    norm, note = normalize_type(proposed)
    base = dict(proposed_type=str(proposed), proposed_source="override", normalized_type=norm,
                heuristic_type=auto.heuristic_type, confidence=auto.confidence, evidence=auto.evidence,
                scores=auto.scores, normalization_note=note if norm else None)
    if norm is None:
        return Classification(final_type=None, validation="REJECTED",
                              classifier_reason=auto.classifier_reason,
                              correction_reason=note, **base)
    if norm in EVIDENCE_REQUIRED and not supporting_evidence(auto.evidence, norm):
        missing = {"CRASH": "No crash/fatal-runtime evidence in the request text",
                   "REGRESSION": "No evidence of previously working behaviour that regressed",
                   "PERFORMANCE": "No performance/resource problem described in the request text",
                   "BUILD": "No build/compile/link failure described in the request text"}[norm]
        if auto.confidence in ("high", "medium") and auto.heuristic_type != norm:
            return Classification(final_type=auto.heuristic_type, validation="CORRECTED",
                                  classifier_reason=auto.classifier_reason,
                                  correction_reason=f"{missing}; corrected {norm} -> {auto.heuristic_type}", **base)
        return Classification(final_type=None, validation="REJECTED",
                              classifier_reason=auto.classifier_reason,
                              correction_reason=f"{missing}; the text alone supports only "
                                                f"{auto.heuristic_type} (confidence {auto.confidence}) — classify again "
                                                f"with a supported type or add the missing evidence", **base)
    reason = auto.classifier_reason
    if norm != auto.heuristic_type:
        reason = f"override {norm} accepted (text alone suggests {auto.heuristic_type}: {auto.classifier_reason})"
    return Classification(final_type=norm, validation="PASS", classifier_reason=reason, **base)


def stored_type_of(markdown: str) -> Optional[str]:
    m = re.search(r"^\*\*Type:\*\*\s*(.+?)\s*$", markdown, re.M)
    return m.group(1) if m else None


def original_statement_of(markdown: str) -> str:
    m = re.search(r"## Original User Statement\n\n> (.*?)\n\n## Classification", markdown, re.S)
    return m.group(1) if m else markdown


def review_record(markdown: str, sidecar: Optional[dict] = None) -> dict:
    """Review an existing request record: is its stored type in the vocabulary
    and supported by its text? Never modifies anything."""
    stored = (sidecar or {}).get("type") or stored_type_of(markdown)
    text = (sidecar or {}).get("original_text") or original_statement_of(markdown)
    norm, note = normalize_type(stored)
    auto = classify(text)
    verdict = "OK"
    detail = ""
    if norm is None:
        verdict = "INVALID-VOCABULARY"
        detail = note or ""
    elif norm in EVIDENCE_REQUIRED and not supporting_evidence(auto.evidence, norm):
        verdict = "UNSUPPORTED"
        detail = f"stored {norm} has no supporting evidence; text supports {auto.heuristic_type} ({auto.confidence})"
    elif norm != stored:
        verdict = "NON-CANONICAL"
        detail = note or ""
    elif norm != auto.heuristic_type:
        verdict = "DIFFERS"
        detail = f"stored {norm}; text alone suggests {auto.heuristic_type} ({auto.confidence}) — informational"
    return {"stored_type": stored, "normalized_type": norm, "heuristic_type": auto.heuristic_type,
            "confidence": auto.confidence, "verdict": verdict, "detail": detail,
            "classifier_reason": auto.classifier_reason,
            "evidence": [e.to_dict() for e in auto.evidence if e.category in EVIDENCE_REQUIRED]}


__all__ = ["RequestType", "CANONICAL_TYPES", "TYPE_ALIASES", "EVIDENCE_REQUIRED", "VALID_PRIORITIES",
           "normalize_type", "normalize_priority", "Classification", "Evidence", "extract_evidence",
           "supporting_evidence", "classify", "validate", "review_record", "stored_type_of",
           "original_statement_of"]
