# Instance-file templates

`scripts/devsys.py install` copies the framework into a project, but these
files belong to each instance once it exists — a project registers its own
suites, requirements, decisions, audit settings and configuration in them.
`install` therefore writes them from these templates (when the target has
none, or with `--force`), never from the instance it runs in, so installing
from an already configured project does not carry that project's
configuration, suites, requirement rows or ADRs into the new one.

| Template | Instance file |
|---|---|
| `devsys.json` | `devsys.json` |
| `test_manifest.json` | `scripts/test_manifest.json` |
| `test_quarantine.json` | `scripts/test_quarantine.json` |
| `audit_manifest.json` | `.audit/manifest.json` |
| `requirements.txt` | `docs/requirements/requirements.txt` |
| `decisions.txt` | `docs/decisions/decisions.txt` (and it names the framework ADRs `install` copies) |

In the framework distribution the instance files equal their templates
(`scripts/tests/test_devsys_tooling.py` `Templates`); change both together.
