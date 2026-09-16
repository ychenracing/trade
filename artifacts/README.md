# Artifact roles and evidence identity

`artifacts/` contains production validation, current or historical audit material, research candidates, and rejected evidence. This index separates those roles without relocating existing originals.

Physical path is not an acceptance status. An artifact is current or canonical only when its own status/provenance fields and the maintained validation contract identify it that way. Directory membership, file name, a successful workflow, or this README must never promote a rejected or invalid result.

## Production evidence

Current production/release evidence remains under `artifacts/validation/` at its existing path. The authoritative interpretation is the artifact's embedded source/status fields together with `docs/VALIDATION.md` and the tests that validate those contracts.

Examples of production-facing validation anchors include release receipts, the formal stress acceptance summary, frozen-universe backtests, and the deployment-release directory. Some validation files can still describe a candidate rather than an accepted canonical result, so consumers must read status fields rather than infer acceptance from the directory.

## Audit material

`artifacts/diagnostics/` contains diagnostic, preregistration, root-cause, recovery, and other evidence retained for audit or reproducibility. These files can explain a decision or preserve a historical investigation without becoming the current production baseline.

Audit evidence is intentionally retained at its original path. References, source revisions, hashes, manifests, and other evidence identities must remain traceable.

## Rejected and historical material

Explicitly rejected candidate originals remain retained, including `artifacts/validation/candidates/*-rejected.json` and rejected/historical research material under diagnostics. Rejected or invalid evidence must remain rejected or invalid; it is never made canonical by copying, renaming, directory cleanup, or documentation wording.

Historical diagnostics that are no longer decision-current remain useful for provenance. Their presence does not mean their thresholds, conclusions, or candidate implementations govern the current production system.

## Maintenance rules

- Do not move or rewrite frozen evidence merely to make the directory tree look cleaner.
- Before any future relocation, map every code, test, document, manifest, and evidence reference and prove that source identity and byte identity remain valid. If that cannot be proved, keep the original path.
- Do not replace large originals with summaries. An index is navigation, not evidence substitution.
- New evidence must record enough source/provenance information to distinguish its producing revision and decision status.
- Engineering validation and economic acceptance remain separate. A passing CI run does not turn a failed economic artifact into production evidence.
- `docs/VALIDATION.md` remains the human-readable entry point for current validation meaning; artifact contents and their validated provenance remain the source evidence.

The logical `production / audit / archive` distinction is therefore maintained as classification rather than a destructive directory migration. This preserves existing evidence identity while preventing current production results from being confused with diagnostics or rejected research.
