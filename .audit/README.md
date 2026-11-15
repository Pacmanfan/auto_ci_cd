# .audit — architecture / conformance audit

* `manifest.json`: the audit's configuration (tracked). It lists canonical sources, the module map,
  profiles, cost classes, retention and every check with its rule and source document.
* `runs/AUDIT-YYYYMMDD-NNN/`: one directory per audit run (git-ignored), holding `audit.json`,
  `audit.md`, `audit-packet.md` and `evidence/`. Retention keeps the newest 30 runs and every
  baseline run.

Run `./tools/audit [quick|standard|baseline]`. Documentation: `docs/verification/architecture-audit.md`
(ARCH-AUDIT-*, ADR-000195). Baselines that must outlive the checkout are copied into `evidence/VER-*`.
