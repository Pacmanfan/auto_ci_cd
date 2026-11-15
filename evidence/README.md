# Verification Evidence

`evidence/` contains durable records of verification runs. Evidence is proof,
not a requirement and not a substitute for a test.

Use `VER-NNNNNN` identifiers and record:

* date and repository revision/dirty state;
* requirement/work IDs;
* exact commands or manual procedure;
* environment and relevant inputs;
* result and classification (`PASS`, `EXPECTED / PRE-EXISTING`, `REGRESSION`, or `BLOCKED / ENVIRONMENTAL`);
* output/artifact paths, including crash cores when applicable;
* remaining limitations.

Historical audit evidence remains under `docs/audits/evidence/`; new durable
run records belong here unless they are specifically an audit report.
