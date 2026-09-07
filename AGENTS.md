# Repository working agreement

## Entry and authority

- Follow the current task's scope and acceptance criteria; read applicable nested `AGENTS.md` before editing that directory.
- For continuation work, read `PROJECT_STATE.md` after this file, resolve mutable branch/PR/SHA/CI/artifact facts from GitHub, then load only the matching PR/diff and affected code, tests, configuration and workflows. Read `.github/CHATGPT_PROJECT_BRIEF.md` only for stable architecture, commands or boundaries. Historical plans are context, not new authority; do not preload unrelated history, skills or logs.
- Skills provide methods, not additional authority or approval gates. Continue already-authorized bounded work unless a platform/safety limit or a material decision not resolvable from repository state blocks it.

## Git, recovery and verification

- Resume the matching branch/PR. New work uses a feature branch unless a direct default-branch update is explicitly authorized; preserve required reviews, checks and branch protection.
- Save coherent recoverable milestones as commits, push when authorized, verify the remote SHA, and keep mutable task state in the existing PR.
- Without explicit authorization, do not reset, clean, rebase, force-push, rewrite history, discard unknown work, or commit secrets.
- Verify the smallest affected scope first and expand by impact. Run the complete applicable acceptance on the stable final candidate or when the active contract requires it earlier. Reuse evidence only while its covered behavior, inputs, dependencies, configuration and environment remain equivalent; new messages/handoffs alone do not invalidate it, while a new SHA still needs applicable exact-HEAD checks.
- Behavior-neutral documentation changes need relevant link/command/governance checks, not unrelated backtests once neutrality is established. Never weaken business gates, fixtures or frozen evidence to make checks pass.

## trade boundaries

Use `README.md`, `docs/ARCHITECTURE.md`, `docs/VALIDATION.md`, `tests/` and `.github/workflows/ci.yml` for the affected contract. `quantfusion/` is the sole implementation; scripts run as modules rather than root-level Python APIs.

This is daily-bar research and manual decision support, not broker automation. Preserve close-to-next-tradable-open causality, account/risk state across routing, fail-closed inputs and transactional publication. Diagnostic stress subsets are not canonical acceptance evidence. Do not change frozen risk thresholds, scenarios, metrics, data or evidence identities merely to obtain passing results; economic changes require the current task's explicit scope and applicable acceptance.
