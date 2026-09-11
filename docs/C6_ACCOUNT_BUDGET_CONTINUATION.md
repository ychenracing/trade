# PR63 account-budget continuation — AB1

## Authority and unchanged acceptance

On 2026-09-09 the user explicitly approved continuing original PR63 toward validated merge by diagnosing account-level intervention gaps and validating a minimal risk-budget/early-reduction mechanism, and requested updating the same original Watch. This is a new economic hypothesis, not an engineering relabelling of v30. Keep PR63 open. Preserve all v30 objects, Base rejection, S qualification rejection and their lawful terminal publication. Do not run ineligible S or insert AB1 into v30 P/R.

18% MDD and its tolerance, retention/initial/promotion gates, data/seeds/scenarios/metrics, costs, continuous account/risk state, locks and close-to-next-tradable-open causality remain unchanged. No tuning, dated/symbol-specific rule, third candidate inside v30, allocator, lock relaxation/reset, live trading, paid resource or protection bypass. Existing A/B correctness recovery remains authorized. AB1 failure is feedback within this authorized direction, not permission to falsify acceptance or automatically close PR63.

Read current AGENTS, PR63 and mandatory Watch contract comment5579535549 through END. This supplement supersedes old recovery-contract wording that exhausts the entire task after Base/S rejection; it does not change old frozen formulas. Only formally accepted/canonical evidence, actual final checks/audit, protected original-PR63 merge and main verification constitute successful delivery.

## Source diagnosis before AB1

Inspected PR63 production tree6df0af04b69157c2bd702dadc1fe2bdecf2dddbf, shared by live HEAD b904b3019dc96fba884bba9761662293cb2bf7eb and authenticated source-recovery bundle. Account risk is measured after close in ensemble_orchestration.py, then global lock liquidations/tail guard/overlay are queued for the next open. universe.py uses confirmed18% for complete-reference3-6 pools, and confirmed17.5%/emergency18% for9+ pools. ensemble_allocation.py tail guard activates at18%. These are reactive lock/guard contracts, not proof of a prebreach account exposure budget. No deterministic implementation defect has yet been established; do not call AB1 a zero-drift bugfix.

Qualification artifact10100537659/run34329287766, ZIP94f5682a6baedd71c391e18f443ff7c382a3f8235a0a51a9cfbfbc0a49034ebe, has0/678 eligible residuals. The initial fallback S snapshots are not proof of missing market data. Largest first-breach groups are2025-10-14(427) and2025-09-04(152); lexicographic representatives are add-one-05-002384 and random-20260807-03-006. Dates are diagnosis labels, never strategy inputs.

## AB1 hypothesis and formula (registered before coding/comparison)

Reuse canonical ProductionReplayEngine, its account lifetime HWM and RiskAction/queue. Do not alter selection, cash allocation or lock/rearm policy. Keep AB1 disabled by default until its separate acceptance/promotion; remove rejected S from the mutable production default while retaining frozen S and explicit historical diagnostic paths. AB1 control is the same source with AB1 disabled and S disabled; new source identities must be recorded, not called I_B30.

At each decision close let E be total account equity, H the continuously maintained account lifetime HWM, G current long marked notional, and N the count of distinct live position/pending-buy books (state_index,symbol,strategy). All values must be finite, causal and internally consistent; invalid input fails closed, never entry-price/zero fallback. Reuse the canonical latest-close-on-or-before valuation; do not read future prices/volume/fillability for decisions.

Floor F = 0.82 * H. One-session stress d uses existing configured daily_loss_limit (default0.06). Two-session planning stress g = 1-(1-d)^2 represents the decision-to-execution interval and one further liquidation opportunity, not a guaranteed market bound. Cost rate c = 2*slippage + 2*commission_rate + stamp_duty; fixed reserve f = 2*N*min_commission, using the most conservative applicable sleeve costs. Remaining budget B=max(0,E-F-f); allowed gross K=min(max_total_weight*E,B/(g+c)). Do not fit these inputs to the observed failures.

When G>K, request the same proportional risk reduction across existing books; round required partial sells upward to100-share lots and cap at actual position shares (full odd-lot exit is legal). Preserve stronger existing liquidation instructions and every account/sleeve lock. Only actual canonical fills change holdings/cash. A blocked/unfilled sell is not risk already removed.

Pending buys/additions may use at most max(0,K-G), without crediting queued sells. Apply a common downward share scale to the entire existing pending-buy batch using its causal signal prices, then floor buys to lots. This is veto/reduce of already generated intents, not alpha ranking, capital reallocation or a new candidate selector. It permits bounded re-entry when cash has genuine budget rather than permanently vetoing all buys below a historical peak. Invalid or nonpositive buy prices fail closed. Existing execution sizing may further reduce, never enlarge, the close-authorized target. Canonical next-open costs/T+1/ADV/limit/suspension checks remain authoritative; this planning envelope cannot guarantee a future18% cap under arbitrary gaps or illiquidity.

Each enabled close records inputs, planned cap/reduction/buy scale and planned-not-filled semantics in risk audit events. Fixed AB1 source/config/evidence are distinct from v30; historical C6 frozen runners must not accept this new flag as unchanged Base/S evidence.

## Bounded validation and continuation

Synthetic tests first: high-water bull-silence, pressure before18%, exact formula/cost/min-fee input, finite/fail-closed input, proportional lot rounding, sibling-book isolation, stronger liquidation preserved, buy batch cap/no credit for pending sells, no lock/HWM/cash mutation, next-open execution and partial/blocked sells. Add only the exact new synthetic test file to the prefreeze CI allowlist; do not enable unrelated full economics per edit.

Fixed diagnostic set, registered before economic comparison: prefix-05,prefix-09,prefix-10,prefix-13,prefix-17,add-one-13-601869,add-one-05-002384,random-20260807-03-006. These cover retained winner/discontinuity/expansion and the two largest first-breach groups. Same frozen2025-04-01..2026-07-20 window, initial2000000, data/fees/scenario definitions and runtime for control/AB1. Reuse exact valid control evidence when actually equivalent; otherwise compute each selected control once. Record MDD, wealth ratio, order buckets, action→fill timing, terminal locks and equity/cost reconciliation. A subset is diagnostic/noncanonical only. No new full matrix until the mechanism's causal benefit and retention tradeoff are understood. Failed rows remain visible and drive root-cause classification rather than parameter search or repeated blind full runs.

A stable candidate still requires separately bound complete applicable L1/L2 and official17/958, audit, removal of temporary C6_PREFREEZE embargo, actual exact-HEAD checks/reviews/protection, selected-source integration, protected squash merge of original PR63 and main verification. New behavior invalidates dependent economic evidence. Do not claim completion at preregistration, synthetic tests, checkpoint or partial CI.

## AB1 fixed-diagnostic result and rejection

Run 34362249425 at builder source
`7e182ec23b84930b271b496261c7fdcf9692a9aa` completed the 64 locked
native tests and the registered eight-scenario comparison.  Unique artifact
10110777329 has ZIP SHA-256
`d6e7448a85a53d193e0bd601dab0493a6b6a6e863be0f231a4bd46ff5b984afa`;
the receipt binds prospective tree
`45a16227246073ce7b2f0c3c412db44b21b5776a`, the frozen OCI runtime,
dependency lock and data tree.  It is diagnostic/noncanonical and dispatched
no formal economics.

AB1 put all eight sampled paths within 18% MDD, but already supplies three
deterministic formal-gate counterexamples.  `prefix-05` retained only
0.9200186 of control wealth, below the frozen 0.99 initial gate.  The
other-prefix minimum was 0.8798962 on `prefix-10`, below 0.95.
`add-one-05-002384` produced 238 date/symbol/side buckets, above the absolute
200 maximum.  Candidate buckets were 107--238 versus 21--48 for controls, and
most paths recorded 119--199 budget fills.  AB1 is rejected for
`RETURN_IMPAIRMENT` and excessive action churn.  Workflow success and eight
MDD screens do not make it accepted or canonical; do not run AB1 full L1/L2.

## AB2 preregistration before implementation or results

The common observed defect is action allocation, not a fitted cap threshold:
AB1 applies the same proportional reduction independently to every live book,
rounds each book upward by a board lot and repeatedly cuts strong and unrelated
books.  AB2 keeps AB1's registered HWM/equity, 0.82 floor, two configured
daily-loss sessions, existing cost/min-fee reserve, maximum total weight,
pending-buy envelope, locks, close-known inputs and canonical next-open
execution exactly unchanged.

Only the sell planner changes.  Let required notional relief be
`max(0, G-K)`.  Rank existing books weakest first using the already canonical
mean allocation score; preserve deterministic tie order by score, symbol,
state index and strategy.  Emit whole-book reductions until the residual
relief fits one book, then emit the minimum remaining shares rounded upward to
one 100-share lot and capped by that book's actual shares.  A full odd-lot exit
remains legal.  Stronger pending sell instructions remain authoritative through
the existing RiskAction consolidation.  AB2 adds no alpha signal, selector,
allocator, threshold, cooldown, date/symbol rule or result-derived parameter.

Synthetic tests must first prove minimum sufficient notional, weakest-first
ordering, stable ties, one partial final book, stronger-sell preservation,
unchanged buy-envelope behavior, no HWM/cash mutation and next-open
fill/block semantics.  Then rerun the exact same eight registered scenario IDs
against the equivalent control.  Required diagnostic screens are the actual
frozen witnesses already exposed by AB1: every MDD at most 18%+tolerance,
`prefix-05` wealth ratio at least 0.99, every other sampled prefix at least
0.95, all sampled date/symbol/side buckets at most 200, and materially fewer
budget actions than AB1.  These remain necessary diagnostic screens, not
sufficient formal acceptance.  A failed AB2 remains visible and must not be
expanded to a full matrix; a stable AB2 requires a new source/binding and all
applicable formal L1/L2 and official17/958 gates.


## AB2 fixed-diagnostic result and rejection

Run 34370628906 at builder source
`9e42675196696c8020879795b558c1f022715f5e` completed every locked step.
Unique artifact 10119067325 has ZIP SHA-256
`f896553c2a52904e677a9322f8fa7d553f8accf25b107e802785ece60e7ba304`;
its receipt binds prospective tree
`8f79b26bd39a7fce0acbc1356723e1cc003c2c42`, the same frozen OCI,
dependency lock and data tree, and reports no branch write or formal economic
dispatch. The artifact was independently downloaded and its GitHub digest,
receipt and complete sixteen rows were checked.

AB2 materially fixed AB1's sell-allocation churn. All eight candidate MDDs
were between 17.41% and 17.90%, every date/symbol/side bucket was at most 199,
and budget fills fell to 42--60 from AB1's 119--199. But four required prefix
retention witnesses still failed: prefix-09 `0.8935881`, prefix-10
`0.8799609`, prefix-13 `0.9350364` and prefix-17 `0.9117807`, each below
0.95. AB2 is therefore rejected/noncanonical for `RETURN_IMPAIRMENT`; its
workflow success and risk screens do not authorize a full matrix.

The common causal defect is now narrower than AB1's sell planner. Replaying
the exact AB2 source on prefix-09 showed 118 buy-clipping days and 709 clipped
buy-order records, including 78 clipping days before the first budget sell
action on 2025-09-02. The first clipping occurred on 2025-04-24 while the
budget cap still equalled the ordinary `max_total_weight * equity` ceiling.
Across the run 3,280,800 requested shares were removed and 310 buy intents
were rounded to zero. These counts are a noncanonical causal diagnostic, not
new acceptance evidence. The canonical execution path already enforces the
same ordinary total-exposure ceiling and cash at the actual next open, so AB2
duplicated normal buy allocation before the risk budget was binding.

## AB3 preregistration before implementation or results

AB3 keeps AB2's exact HWM/equity inputs, 0.82 floor, two configured
daily-loss sessions, costs/minimum-fee reserve, weakest-first minimum-sufficient
sell planner, locks, causal timestamps and next-open execution. It changes
only how the already registered pending-buy envelope distinguishes the normal
portfolio ceiling from a binding loss budget.

Let `ordinary_cap = max_total_weight * E` and keep AB2's `K` unchanged.
When `K` equals `ordinary_cap`, leave pending buys byte-for-byte unchanged
and let the existing canonical execution-day exposure/cash/ADV/limit checks
handle them. When `K < ordinary_cap`, the loss budget is binding: cancel all
pending buys/additions for that close, without credit for queued sells, while
still emitting AB2's required weakest-first reductions when `G > K`.
Comparison uses the existing finite-value tolerance only; it adds no fitted
threshold, ranking, allocator, cooldown, date/symbol exception or parameter
search. Invalid inputs still fail closed and stronger existing exits remain
dominant.

Synthetic tests must first show that a nonbinding envelope does not rewrite a
pending buy batch, a binding envelope blocks every pending buy without credit
for queued sells, the transition is derived from the unchanged formula, and
all AB2 sell/HWM/cash/fill invariants remain true. Then run the exact same
registered eight scenarios and controls once. The same diagnostic screens
apply: every MDD at most 18% plus tolerance, prefix-05 retention at least 0.99,
other sampled prefixes at least 0.95, every bucket at most 200, and no return
to AB1-level action churn. Failure remains visible and must not be expanded;
only a stable AB3 may receive a new formal identity and full applicable
L1/L2/official17-958 validation.


## AB3 deterministic rejection before integration

Focused review 5158548491 found a correctness counterexample in the preregistered
all-buy veto. At `E=90000,H=100000,G=0`, using the actual unchanged capacity
function, default costs and one pending-buy book gives
`K=66917.92294807367` while `ordinary_cap=90000`. The real
`RecoverableDrawdownRiskManager` remains unlocked for five consecutive closes
at 10% drawdown, but AB3 cancels every buy because the budget is binding. With
no holdings or fills, equity cannot recover and the binding condition persists.
This is an absorbing cash state outside the account lock contract.

AB3 is therefore not eligible for source integration or formal expansion even
if its already-dispatched fixed-eight diagnostic passes. Run 34392403676 remains
valid historical diagnostic work and must not be cancelled or relabelled, but
its result cannot override this synthetic correctness failure.

## AB4 preregistration before implementation or results

AB4 keeps AB3's nonbinding behavior, AB2's weakest-first minimum-sufficient
sell planner, and the exact AB1 capacity formula, HWM/equity inputs, 0.82 floor,
two configured daily-loss sessions, costs/minimum-fee reserve, locks, causal
timestamps and next-open execution.

Only binding-time buy admission changes. Let
`headroom=max(0,K-G)`, computed from current marked gross before any queued
sell. If `K == ordinary_cap`, leave the pending-buy batch unchanged for the
existing canonical execution path. If `K < ordinary_cap`, apply the original
common downward share scale
`min(1, headroom / requested_pending_buy_notional)` to the entire already
generated buy batch, then floor each retained buy to the A-share board lot.
When `headroom=0`, all buys are vetoed naturally; when it is positive, a
bounded buy may remain, preventing AB3's cash trap. Queued sells receive zero
buying credit.

This restores no old AB1 sell behavior and adds no selector, ranking,
allocator, threshold, cooldown, lock relaxation/reset, date/symbol exception
or parameter search. Existing canonical cash, total/symbol/group exposure,
ADV, limit and suspension checks may only reduce the close-authorized batch
further. Invalid values still fail closed; stronger sell instructions and
actual account locks remain authoritative.

Synthetic tests must first demonstrate: nonbinding buys remain unchanged;
binding positive headroom retains a bounded lot-rounded batch; zero headroom
vetoes buys without crediting queued sells; an unlocked zero-gross account at
10% drawdown can admit positive risk; and AB2 sell/HWM/cash/next-open invariants
remain intact. Then run the exact same fixed eight scenarios once after the
already-running AB3 attempt terminates. The unchanged screens remain necessary:
all MDD at most 18% plus tolerance, prefix-05 retention at least 0.99, other
prefix retention at least 0.95, every order bucket at most 200, and materially
less action churn than AB1. Only a stable locked AB4 may receive a new formal
identity and complete L1/L2/official17-958 validation.


## AB4 local preview and bounded rejection before locked dispatch

Tests were changed before implementation. The positive-binding-headroom assertion
failed under AB3 while `K=66917.92294807367` and then passed under the minimum
AB4 common-scale implementation. Nonbinding, zero-headroom, unlocked-empty-account
re-entry and existing sell-path assertions also passed as direct synthetic
checks. Python compileall and diff whitespace validation passed; this local
environment does not contain pytest or ruff, so no locked/native-suite claim is
made.

A noncanonical local replay of the exact registered eight paths passed the
registered screens: MDD ranged from 17.2793% to 17.9832%, every listed wealth
ratio was at least 1.0075, the largest date/symbol/side bucket count was 176,
and budget fills were 25--68. The eighth registered row is
`random-20260807-03-006`; an initial scratch invocation accidentally used
`random-20260807-03-004` and is not counted as the registered-eight result.

That accidental row nevertheless exposed a deterministic full-plan witness
which cannot be hidden. Under AB4, `random-20260807-03-004` reached
18.1223663541% MDD, above the unchanged 18% hard gate. The first budget sell
decision was 2025-09-04 and filled 2025-09-05. At the 2025-09-05 close the
loss budget was binding, existing gross was 2,724,624 and `K` was
5,070,915, so AB4 admitted the complete 1,619,352 pending buy batch in 688498.
Those buys filled on 2025-09-08; the position then fell from the 254.98473
fill to the 219.99978 next open and the account first breached 18% on
2025-09-09 before the close-generated reductions could fill. Equity-return and
MDD recomputation reconciled exactly.

This classifies AB4 as `EXECUTION_GAP_RISK / ACTION_INSUFFICIENT`, not a
threshold or scenario problem. Its local evidence is not frozen acceptance,
but the known formal scenario counterexample blocks full-matrix expansion and
must be retained. The still-valid AB3 run 34392403676 remains untouched; no
AB4 locked run may be duplicated ahead of it.

## AB5 preregistration before implementation or results

AB5 keeps AB4's HWM/equity, 0.82 floor, remaining-budget `B`, two-session
gross cap `K`, weakest-first minimum-sufficient sells, nonbinding buy
behavior, account/sleeve locks, costs, timestamps and canonical next-open
execution. It adds no alpha selector, ranking, allocator, fitted threshold,
cooldown or date/symbol exception.

Only binding-time pending-buy admission gains a second, execution-gap loss
ledger using the engine's already canonical `limit_pct_for_code` board-limit
classification. At a binding close, debit current marked books from `B` by
`marked_notional * (limit_pct_for_code(symbol) + variable_exit_cost_rate)`.
Queued sells receive no credit. Debit each pending buy at its close-known
signal notional times the same symbol-specific factor. Compute a common batch
scale as the minimum of AB4's gross-headroom scale and
`max(0, B-current_gap_debit) / requested_buy_gap_debit`, capped at one, then
floor every retained order to the existing A-share lot. A zero debit headroom
vetoes buys naturally; an unlocked empty account with positive `B` can still
admit a bounded lot batch. Existing execution checks may only reduce it.

The statutory board-limit mapping is existing execution-domain data, not a
searched parameter. This ledger is a conservative planning witness, not a
promise that every future gap is bounded. Synthetic tests must first prove
board-limit classification reuse, simultaneous current-book debit, no
queued-sell credit, common scaling/lot rounding, positive empty-account
re-entry, and unchanged AB2/AB4 sell/HWM/cash/next-open behavior.

After the sole AB3 attempt is terminal, locked validation may run once on the
original registered eight plus the newly mandatory
`random-20260807-03-004` regression witness. All original screens remain,
and every one of the nine MDDs must be at most 18% plus tolerance. Failure is
retained and not expanded; only a stable locked AB5 can receive a new formal
identity and complete applicable L1/L2 and official17/958 validation.


## AB5 local preview; locked evidence still pending

The new board-gap-debit assertion was written first and failed under AB4 because
the receipt had no current gap debit. The minimum AB5 implementation then made
that assertion pass: in the synthetic binding case current gap debit was
4,060, remaining gap headroom was 3,930, retained buy debit was 3,857 and the
common buy scale was 0.193596. Compileall and diff validation passed. Pytest,
ruff and the frozen Python runtime are not installed in this local environment,
so their success is not claimed.

A noncanonical local replay then evaluated the original eight plus the new
`random-20260807-03-004` witness exactly once. All nine reconciled terminal
equity/return and recomputed MDD. MDD ranged from 15.8683% to 17.9906%; the
new witness fell from AB4's 18.1224% to 17.7965%. The lowest wealth ratio was
1.007414 on `random-20260807-03-006`; the largest bucket count was 185 and
budget fills were 10--51. Thus AB5 passes the preregistered local screen without
changing the 18% or retention gates.

These are direction-selection results, not accepted/canonical or locked
evidence. Do not integrate AB5 into PR63 or start formal economics from them.
The next authorized expensive action is one frozen native/synthetic and
nine-scenario AB5 run after AB3 run 34392403676 reaches a terminal state. Its
exact source, workflow receipt and artifact must be authenticated before any
new formal identity or L1/L2 expansion.


## AB5 locked diagnostic authentication and promotion to formal candidate

Run [34410163617](https://github.com/ychenracing/trade/actions/runs/34410163617)
completed at exact builder `acacb4eaaefac20533f1728194dd167070ac9e6f`.
Its sole artifact `10127361435` has GitHub and independently recomputed ZIP
SHA-256 `56d789e8b2e9508ca06a8e7ea8be134f0c96e2576e0f1a7324d83b11f765165b`.
The receipt binds PR base `6619d71abf7317acdc7fba0236ee74291e9ae074`,
prospective tree `8a9651c741bcbc9efc66dffabcfc493eae769bf3`, frozen Python
3.12.14/runtime `sha256:581429e3df12d76e6af4be5ab7d0e7fc2013eb57dc23d2de691411c8efdbb970`,
unchanged dependency lock `22d95d1f81d2cac0a0e164ff3d79717904509135b6a0974ae1f679c8a00cd434`
and data tree `85680349e9026a013042752dd7e4a2d54cf89059`. All nine
published source blobs resolve; the run made zero branch writes and zero formal
economic dispatches.

All 69 affected native tests passed. The artifact contains the exact nine
controls and nine AB5 rows; every terminal-equity and MDD recomputation agrees.
Candidate absolute MDD ranged from 15.868264% to 17.990578%. The lowest wealth
ratio was 1.007414 on `random-20260807-03-006`, `prefix-05` was 2.315090,
and every other registered comparison exceeded 1.07. The largest
date/symbol/side bucket count was 185 and budget fill records ranged from 10 to
51, materially below AB1's 119--199. The mandatory AB4 counterexample
`random-20260807-03-004` measured 17.796520%. AB5 therefore passes every
preregistered locked diagnostic screen without changing a gate.

This promotes the exact AB5 source blobs to a new formal candidate, not to
accepted/canonical status. Integrating these blobs invalidates prior dependent
economic evidence. The next required evidence is a separately bound complete
applicable L1/L2 and official17/958 run using this exact economic source and the
unchanged gates, followed by cash/HWM audit, final non-embargo checks and
protected merge. The locked nine-scenario diagnostic remains noncanonical and
cannot substitute for those stages.

## Watch

Original ID6a9e3c8f7fd48191acc3b5e6b5c7acf3. Maintain mandatory contract5579535549 and single heartbeat5579901968, with truthful trigger/executor and STARTED/YIELDED phases. Preserve hourly/Asia-Tokyo cadence. This manual session has no callable timer-management actions after bounded discovery despite installed Task Tool; updating GitHub's mandatory instruction is not a timer-settings readback or scheduled-consumption proof. The next manager-capable invocation must update/read back the same task prompt to this direction without creating a replacement. Missing timer management does not block independent GitHub engineering. Preserve effective single-writer ownership; no competing AB1 or duplicate economic attempt.


## v37 formal-path integration rejection and AB5-FORMAL-1 preregistration

### Authenticated v37 evidence and classification

Run [34441331412](https://github.com/ychenracing/trade/actions/runs/34441331412) completed successfully at 2026-09-10T09:51:05Z. All 12 fixed OCI shard ZIPs were independently downloaded and their bytes matched the 12 GitHub SHA-256 digests. Their semantic attestations bind source/validator revision I_B36 `5cb1fb8a1208c96cfee075ef38e0d5f115929613`, P36, the fixed 12-way chunking, and the same preregistration digest. The shards contain exactly 3825 evaluation records (765 scenarios times five variants).

The assembled artifact is `10146274233`, `c6-bound-c6-v37-base-l1-a0`, 642301990 bytes, GitHub digest `sha256:b51aa3f7d6139d24dff18338b6dd279b0766e0dc8d23376e851c9bb0eda39617`. The current connector cannot download a single artifact above 512 MiB. The central job log nevertheless proves that it verified the twelve shard digests, assembled and internally verified the sealed export, and uploaded that exact digest. This size limitation does not convert the run to failure, but it is retained as an independent-wrapper-download limitation.

Independent recomputation over the authenticated shard records found that the formal `C6-Base` row breached the unchanged 18% hard-MDD boundary in 678 of 765 scenarios. Worst was `add-one-13-601869` at 24.424813929407263% MDD. `random-20260807-03-004` was 23.406655496167797%, whereas the locked AB5 diagnostic was 17.796520%. Every formal `C6-Base` record had zero `account_budget_events` and zero `account_budget_orders`; for `...004`, `C6-Base`, `F0+F1`, and `F0-only` were identical.

This is `IMPLEMENTATION_INTEGRATION_ERROR / FORMAL_CANDIDATE_IDENTITY_NOT_BOUND`, not an AB5 economic rejection. The exact source explains the observation:

- `account_risk_budget_enabled` is optional and defaults false.
- the account-budget action executes only when that flag is true;
- the diagnostic guard rejects enabling it while claiming frozen `C6-Base` or `C6-Base+S`;
- R37 bound `c6.base.l1` to candidate `C6-Base`.

Thus v37 is valid evidence for the unchanged old Base path, but inadmissible as formal AB5 evidence. It must not feed S qualification, D selection, L2, official17/958, acceptance or canonical publication for AB5. No such successor is authorized from this result.

### AB5-FORMAL-1 frozen correction

The next correction is integration-only: bind the already locked AB5 mechanism to a distinct candidate identity (working name `C6-Base+AB5`) that cannot masquerade as `C6-Base` or `C6-Base+S`. It does not change AB5 equations, risk thresholds, 18% MDD+tolerance, retention/initial/promotion gates, data, seeds, scenario membership/order, metrics, transaction costs, account-lock rules, execution causality, or allocator behavior.

Before implementation, the following tests are required and must fail on I_B36:

1. a formal AB5 binding selects a candidate identity distinct from both frozen Base/S identities and maps to the exact AB5 implementation source;
2. only that identity enables `account_risk_budget_enabled`; frozen `C6-Base` and `C6-Base+S` remain byte/behavior compatible with the flag absent;
3. formal AB5 `random-20260807-03-004` emits account-budget envelope/order evidence and reproduces the locked AB5 path rather than the old Base row;
4. L1 task/item manifests, predicates, checkpoints, seals, selection and downstream source resolution consume the distinct AB5 identity without changing the 765-scenario manifest or frozen gates;
5. any attempt to label an enabled-budget run as `C6-Base` or `C6-Base+S` fails closed.

The implementation must be the smallest coherent identity/config plumbing needed to satisfy those tests. Because P/R and candidate binding semantics change, all affected formal evidence is invalidated and must use a new P/I/R identity before one fresh Base L1. v37 and every earlier rejection remain immutable. No economic dispatch is allowed until the new frozen-source receipt and evidence-only R both authenticate the exact formal AB5 path.

## AB6 preregistration: stock-book execution-gap debit

Authenticated v42 Base L1 and its sealed D retain `C6-Base+AB5` as
`QUALIFICATION_REJECTED`: 152 of 765 scenarios exceed the unchanged 18% MDD
gate and none qualifies for S.  Read-only run 34576152841 projected six fixed
representatives from that exact Base artifact.  All six had a close-known AB5
action before the first breach, complete next-open fills and no execution
obstruction, yet still breached.  Their last pre-breach `current_gap_debit`
was 1.62--1.99 times `remaining_loss_budget`.  The common class is therefore
`ACTION_INSUFFICIENT`, not late signal or blocked execution.

AB6 keeps AB5's account HWM/equity, 0.82 floor, two-session stress cap, cost
reserve, ordinary exposure cap, pending-buy gap debit, weakest-first ordering,
locks, timestamps and canonical next-open queue.  It adds no threshold,
selector, allocator, cooldown, date/symbol exception or searched parameter.
Only the already-computed stock-book execution-gap ledger becomes a sell-side
constraint: at a binding close, plan the minimum lot-rounded weakest-first
reductions needed for both `gross <= gross_cap` and
`remaining_current_gap_debit <= remaining_loss_budget`.  Each reduced book
releases its marked notional times its existing
`limit_pct_for_code(symbol) + variable_exit_cost_rate`; pending or queued sells
receive no advance credit.  Stronger existing exits remain authoritative.

Tests must fail first under AB5 and then prove: the stock-gap constraint adds
only the minimum required lot relief when it is tighter than the two-session
cap; different board-limit classes release their own debit; a nonbinding
account remains unchanged; pending sells do not fund buys or erase current
inventory debit; stronger locks are not weakened; invalid inputs fail before
queue mutation; and generated actions still use the existing T+1 execution
path.  Receipts must expose pre-plan and post-plan stock-gap debit and whether
that constraint bound.

Only after those tests pass may one noncanonical fixed diagnostic compare AB5
and AB6 on the original nine registered AB5 rows plus these six v42 residual
witnesses: `random-20260817-03-027`, `random-20260807-12-040`,
`random-20260817-08-023`, `random-20260807-05-040`,
`leave-one-out-300308`, and `random-20260807-08-046`.  Existing control
evidence may be reused only where identity and inputs are equivalent.  The
unchanged screens are MDD at most 18% plus tolerance, prefix-05 retention at
least 0.99, every other prefix at least 0.95, every date/symbol/side bucket at
most 200, exact cash/equity/MDD reconciliation, and no new terminal lock or
execution-boundary violation.  Failure is retained and classified; it is not
expanded to a full matrix.  A stable AB6 must receive a new formal identity
and invalidate every AB5-dependent economic result before any L1 expansion.

## AB6 fixed diagnostic rejection and AB7 preregistration

The exact preregistered 15-row local comparison completed once on the AB6
diagnostic source.  AB6 improved four of the six v42 residual witnesses below
18%, but it is rejected/noncanonical.  `prefix-05`, `prefix-09`, `prefix-10`
and `prefix-17` worsened to 19.35%--19.50% MDD; prefix-05 and prefix-17 wealth
ratios were 0.4695 and 0.4573.  Prefix-13 and add-one-13-601869 produced 221
and 245 date/symbol/side orders.  Cash/equity and MDD recomputation reconciled
and no terminal lock appeared, so this is `RETURN_IMPAIRMENT / ACTION_CHURN`,
not an accounting or execution error.  The rejection remains visible and AB6
must not receive a formal identity or full matrix.

The minimal causal discriminator on prefix-05 found 20 stock-gap binding
dates, including eight dates on which `gross <= gross_cap` and AB5 would not
have emitted any sell.  AB6 had therefore expanded the intervention from
"make an existing AB5 reduction sufficient" to "create a new reduction on
any binding-budget date".  That expansion is not required by the six residual
witnesses, all of which already had AB5 reduction orders before breach.

AB7 keeps AB6's exact debit arithmetic, lot rounding, weakest-first order and
all AB5 boundaries, but applies the stock-book gap constraint only when the
unchanged AB5 sell predicate already holds: `gross > gross_cap` using the
existing finite-value tolerance.  When AB5 would not create a sell, AB7 must
not create one solely from the stock-gap ledger.  Pending-buy gap debit remains
the unchanged AB5 rule.  This introduces no fitted threshold, cooldown,
selector, allocator, date/symbol exception or parameter search.

Tests must first fail under AB6 and then prove the exact non-expansion case,
the stronger amount on an existing AB5 sell date, per-board debit, AB5 identity
compatibility, lock priority, fail-closed inputs and T+1 execution.  Then run
the same 15-row AB5/AB7 comparison once with the same screens.  Failure stays
noncanonical and drives the next causal split; only a stable AB7 may receive a
new formal candidate identity.

## AB7 fixed diagnostic rejection and AB8 preregistration

The same 15-row comparison completed once for AB7.  It reduced prefix-13's
order bucket from AB6's 221 to 211 and restored its wealth ratio to 0.9827,
but the candidate remains rejected/noncanonical: prefix-05/09/10/17 still
reached 19.35%--19.50% MDD, prefix-05 and prefix-17 wealth ratios remained
0.4719 and 0.4573, and prefix-13 still exceeded the 200-order screen.  Four of
six v42 residual witnesses passed 18%; `leave-one-out-300308` worsened to
19.40%.  Accounting, MDD and terminal-lock checks remained valid.  Therefore
the return injury comes mainly from the full stock-gap liquidation amount on
otherwise-valid AB5 sell dates, not only from AB6's extra sell dates.

At 2025-09-02, harmful prefix-05/17 paths had only 0.58%--0.63% equity of AB5
gross-cap excess but AB7 expanded directly to the full board-gap debit.  The
worst v42 residual had 14.34% gross-cap excess on that same date.  AB8 does not
turn that observation into a fitted cutoff.  Instead it uses the magnitude of
the existing AB5 action itself as the causal, parameter-free bound: when the
AB7 predicate holds, add at most one further gross-notional tranche equal to
the minimum AB5 gross relief.  Thus total planned gross relief is
`min(current_gross, 2 * (current_gross - gross_cap))`; the stock-gap ledger
only activates the second tranche and per-book debit remains recorded.  There
is no action when AB5 has none, no fraction search, and no changed risk or
execution threshold.

Tests must fail first and then prove: the extra tranche is neither smaller
than the requested one-for-one reinforcement nor larger by more than existing
lot rounding; the AB7 no-new-date rule remains; AB5/AB6/AB7 diagnostic
identities remain unchanged; and all prior lock, board classification,
fail-closed and T+1 tests pass.  Then execute the same fixed 15-row comparison
once.  AB8 is rejected without formal expansion if any existing risk,
retention, order, reconciliation or lock screen fails.

## AB8 fixed diagnostic rejection and AB9 preregistration

The same 15-row comparison completed once for AB8.  It materially reduced
AB6/AB7 churn and kept 12 rows under 18%, but remains rejected/noncanonical.
`random-20260817-08-023`, `random-20260807-05-040` and
`leave-one-out-300308` still reached 21.33%, 21.01% and 18.46% MDD.
Prefix-05/17 wealth retention was only 0.7099/0.6389 and prefix-17 produced
209 date/symbol/side orders.  All cash/equity and MDD recomputations remained
exact within floating tolerance and no terminal lock appeared.

A focused AB5/AB8 comparison on prefix-05 and prefix-17 shows the structural
return injury: the stronger account sells fully remove multiple strategy books
and those exact book identities receive no later filled buy.  Cash days rise
from 33 to 56 on prefix-05 and total fills rise from 264 to 279; on prefix-17
fills rise from 438 to 529.  The mechanism is therefore changing the long-term
holding/re-entry path, not merely paying a one-day defensive cost.  Changing
the reinforcement multiple again would be parameter search and is forbidden.

AB9 keeps AB8's one-tranche bound and every existing account/execution rule.
It changes only how the *additional* stock-gap tranche consumes the existing
weakest-first book order.  First plan the unchanged minimum AB5 gross-cap
relief.  Then traverse the post-AB5 remaining books in the same stable order,
but the additional tranche may not remove the final existing A-share board lot
from any book.  This preserves book/strategy identity for canonical re-entry
and additions; it is not a new ranker or allocator.  If insufficient removable
inventory exists, report the residual gap rather than weakening a lock,
inventing credit or forcing a full exit.  Stronger pre-existing exits and the
base AB5 action remain authoritative and may still liquidate a book.

Tests must fail first and prove the two-pass boundary, final-lot retention only
for the extra tranche, exact AB5 base quantity, stable weakest-first order,
truthful residual receipt, stronger-exit priority, and unchanged board/cash/T+1
contracts.  Then run the same fixed 15-row AB5/AB9 comparison once.  Any MDD,
retention, order, reconciliation or lock failure rejects AB9 without full
matrix expansion.

## AB9 fixed diagnostic rejection and selected-book capacity gate

The same fixed 15-row comparison completed once for AB9.  It remains rejected
and noncanonical.  Prefix-05 improved to 16.60% MDD, 0.7655 wealth retention
and 109 date/symbol/side orders, but prefix-17 retained only 0.7439 of AB5
wealth and produced 212 orders.  Add-one-13-601869 and add-one-05-002384
retained only 0.6397 and 0.7217.  Three fixed v42 residual witnesses still
failed the hard MDD gate: `random-20260817-08-023` at 21.36%,
`random-20260807-05-040` at 21.08%, and `leave-one-out-300308` at 18.42%.
Cash/equity and MDD recomputation reconciled and no terminal lock appeared.
AB9 therefore does not receive a formal identity or full matrix.

Before implementing another sell-topology variant, a read-only capacity gate
used the authenticated six-witness gap projection.  On the final decision date
before each first breach, it subtracted both the existing AB5 planned release
and the maximum possible release from fully liquidating only those books AB5
had already selected.  All six witnesses still exceeded their remaining loss
budget.  The post-completion debit versus budget ranged from 648,571.85 versus
434,606.61 to 1,421,850.98 versus 865,425.41.  Consequently an intervention
restricted to already-selected AB5 book identities is structurally incapable
of covering the observed execution gap; it is rejected analytically without
implementation or another economic run.

Do not tune the reinforcement multiple, trigger, or retained lot count after
these results.  A successor, if any, must be based on a different causal signal
or an existing canonical risk path, separately preregistered and falsified on
the fixed diagnostics.  Automatic restoration/rebuy, a new selector, or a new
allocator remains out of bounds.
