# Work Management

`work/` is the canonical active work system.

* `work/work.txt` is the stable-ID manifest.
* `work/queued/` contains eligible work not yet claimed.
* `work/active/` contains claimed work.
* `work/blocked/` contains work with an explicit blocker.
* `work/completed/` contains completed work records and links to evidence.
* `work/cancelled/` contains intentionally abandoned work records.
* `work/tasks/` retains active or migrated subsystem work documents referenced by the manifest.
* `work/backlog.md` is the triaged backlog and request conversion queue; it is not normative requirements authority.

Use `WORK-NNNNNN` identifiers. Each work record must link to at least one
requirement or explicitly state why it is a mechanical/documentation task.

Source-local task notes (if the project keeps any) are orientation and historical
implementation material. They are not a second active work authority.
