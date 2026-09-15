# Repository working agreement

## Scope and authority

Follow the current task's scope and acceptance criteria within platform permissions. Preserve project-specific business, security, data, economic, CI and release contracts; read applicable nested `AGENTS.md` before editing that directory. Historical plans are context, not new authority. Analysis-only and approval-before-edit requests remain read-only until authorized.

## Context and methods

- Resolve current branches, SHAs, PRs and checks from GitHub; inspect local changes when a worktree exists. Continue matching work instead of creating a replacement PR or redoing verified work.
- Consult the project brief when needed to locate architecture, commands or boundaries; for continuation work, read the existing recovery state. Load only the matching PR/diff, relevant documentation and directly affected code, tests, configuration and workflows needed for the current decision; expand by uncertainty or impact. Reuse already-read unchanged context. Keep exact constraints and evidence identities intact; do not preload every skill, document or log.
- Use skills as task-specific methods, not additional task authorities. For an already authorized, bounded task, do not add repeated design approvals, execution-mode questions, mandatory full-mode workflows or ceremonial announcements. Respect platform-required skill use and genuine project approval gates. Missing optional skills are not blockers when available tools suffice.
- Prefer the smallest sufficient change and existing dependencies. Plan only material decisions, dependencies and acceptance; do not duplicate implementation code in plans. Batch related edits. Parallelize independent work only; keep one writer per shared file, runtime or evidence identity.

## Authorization and recovery

- Continue safe, clearly authorized work without asking for another "continue". Resolve factual ambiguities by reading; ask only for a material decision that cannot be resolved safely. Do not infer permission for spending, real trading, credential/permission changes, irreversible operations or unrelated external writes.
- Resume existing task branches. For new work use a feature branch unless a direct default-branch update is explicitly authorized. Preserve required reviews/checks and any PR-only policy.
- Save the first coherent result and meaningful later milestones as verified commits; push authorized checkpoints and verify remote SHA. No empty bootstrap commit, temporary bootstrap file or new Issue is required for routine work. Record ongoing state in the existing PR when applicable, not permanent instructions.
- Without explicit authorization, do not `reset`, `clean`, `rebase`, force-push, rewrite history, delete branches/worktrees, discard unknown work or overwrite unrelated changes. Never commit secrets or claim an unverified push succeeded.

## Verification and completion

- Diagnose failures at the smallest failing test, module or shard. Batch related fixes before expanding coverage; do not run the full suite after every edit. Run the complete applicable acceptance set on the stable final candidate, expanding earlier only when risk or the contract requires it.
- Treat a failed check, rejected candidate or invalidated hypothesis as feedback, not task completion. Diagnose it and continue with an evidence-supported alternative or a bounded check that distinguishes plausible causes, within the authorized scope and any task budget. If attempts add no information, reassess other authorized paths rather than repeat them. Finish when acceptance is met; if no safe authorized action remains, preserve progress and report the specific blocker or evidence gap without requiring proof that the goal is impossible. Do not weaken acceptance criteria, suppress failed evidence, or bypass safety, authorization or frozen contracts.
- Reuse evidence only while its covered behavior, inputs, dependencies, configuration and environment remain equivalent. New messages, agents or handoffs alone do not invalidate it. A new SHA still needs applicable exact-HEAD checks; never report an old CI result as the new HEAD's status.
- Behavior-neutral documentation changes need relevant link/command/format checks, not unrelated backtests. Instruction changes also need trigger, authorization and completion-boundary review. Do not weaken business gates, fixtures or thresholds to make checks pass.
- Review the complete task diff once; repeat focused review for material fixes or risk. Finish when acceptance and required checks pass with no known material correctness, security, data-integrity or economic issue. Do not add marginal optimization afterward.
- A checkpoint, PR, partial test pass or prepared handoff is not completion. Continue independent targets past a blocker. If no safe authorized action remains, preserve recoverable progress and report the exact blocked/unverified items separately from completed work. Do not promise background completion.

## trade entry points and boundaries

When architecture or command context is needed, consult `.github/PROJECT_BRIEF.md`; use `README.md`, `docs/ARCHITECTURE.md`, `docs/VALIDATION.md`, `tests/` and `.github/workflows/ci.yml` for the affected contract. `quantfusion/` is the sole implementation; scripts run as modules rather than root-level Python APIs.

This is daily-bar research and manual decision support, not broker automation. Preserve close-to-next-tradable-open causality, account/risk state across routing, fail-closed inputs and transactional publication. Diagnostic stress subsets are not canonical acceptance evidence. Do not change frozen risk thresholds, scenarios, metrics, data or evidence identities merely to obtain passing results; economic changes require the current task's explicit scope and applicable acceptance.

## Git/GitHub and original-evidence transfers

- Avoid session interruption from large inline payloads: never print or pass large file bodies, full Base64 or huge JSON through model/tool output or arguments. Do not probe payload limits, extract connector credentials, or bypass platform permissions.
- Preserve required large originals through an authorized file-reference or streaming transfer supported by the runtime/provider. Pass a path or file reference, not the bytes through the model. The owner's latest clarification permits this transport and requires preservation; it does not authorize public sharing, permission changes or unrelated uploads.
- Keep Git changes limited to explicit necessary source, tests, concise results and recovery metadata. Inspect sizes and outgoing objects; reuse existing blobs for unchanged large references. Do not indiscriminately add working directories or rewrite history.
- Record each original's local path, remote location, byte count, content hash and actual verification status. A summary/hash is not a backup. Confirm provider metadata and content checksum when available; distinguish successful upload/size verification from content readback still blocked. Preserve originals while resolving transfer failures.
- Use small serial checkpoint writes, one writer per branch, and remote SHA verification. A blocked transfer must not block independent implementation or validation; keep any unavailable required evidence explicitly unmet.
- Apply this transport boundary to current work and handoffs without rewriting historical evidence. It does not change the economic contract, release protections, task scope or scheduling permissions.
