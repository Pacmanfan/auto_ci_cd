# Normative Requirements

`docs/requirements/` is the canonical behavioral authority for <PROJECT>.
The directory is indexed by `requirements.txt`; each domain document owns a
coherent set of observable requirements and acceptance/verification guidance.

Requirements are not requests, implementation plans, tests, evidence, or
architecture explanations. A request may extend an existing requirement, but
must not create a duplicate owner. Existing domain identifiers remain stable;
new repository-process requirements use `REQ-DEVOPS-*`.

## Authority rules

* `docs/requirements/` defines what behavior must exist.
* `docs/decisions/` explains significant design choices.
* `work/` records implementation activity.
* Tests prove requirements; `evidence/` records executed proof.
* `docs/history/` is historical and cannot silently override current text.

Start with `requirements.txt`, then read the owning domain document.
