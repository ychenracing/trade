# Deployment-focused acceptance and delivery

This is the plan recorded at the acceptance freeze. Its checkboxes record that
planning snapshot; completed measurements are in `deployment-verification.json`,
and corresponding-HEAD checks and actual merge status are recorded on PR101.

Owner authorization: 2026-09-15, approve the goal of evidence-supported net return,
risk and stability improvements within an explicit use scope, with understandable
and executable decisions; revise acceptance and continue original PR101 to normal
main merge. This replaces the earlier universal historical success objective.

## Scope and decisions

The intended deployment is the existing owner-selected 17-stock technology pool,
daily close decisions and next executable open, manual decision support. The 17
leave-one-out cases test removal sensitivity. Existing prefix/add-one families
remain supporting sensitivity checks; add-one uses 5/9/13-stock bases and must not
be described as adding a new stock to the full 17-stock deployment.

Retain the existing data, costs, scenario plan, reference identities, execution,
accounting, causality and permutation correctness. No strategy parameter is changed
by this acceptance revision. Historical contracts and failed predicates stay visible.

The effective economic table is:

- Main17: net terminal wealth at least99% of the pinned incumbent, MDD at most18%
  and no worse than incumbent. At least one of main wealth or MDD must improve.
- Prefix/leave-one-out/add-one/permutation: retain18% maximum historical MDD and
  paired wealth minimum65%, P10 85%, median100%. Median improvement protects
  the distribution instead of merely allowing deterioration in every case.
- Random subsets: require paired wealth P10 at least85% and median at least100%;
  MDD severity P90, maximum, and count exceeding18% must not worsen against the
  same fixed incumbent scenarios. The uniform18% veto and65% individual wealth
  veto become diagnostics for this family. All severe losses are individually
  disclosed; these pools are outside the validated deployment claim.
- Turnover: retain complete fees, modeled slippage, fills, authorized records,
  daily peaks and buckets as diagnostics. No arbitrary total bucket veto.
- Release review must include the existing12 cross-window/cost comparisons and
  matched normal/adverse-cost main replay. In each deployed-main comparison,
  lower terminal wealth must be accompanied by lower MDD; reject simultaneous
  deterioration of both. Retain18% historical main MDD, disclose absolute losses
  and the size of every tradeoff. These are already-viewed evidence, not untouched
  holdouts, and do not establish broad generalization.
- Complete958 coverage, final source/data/config binding, engineering correctness
  and actual repository protections remain mandatory. No future return/MDD or
  broad generalization guarantee is claimed.

Rationale: preserve an actual deployment risk limit and demand measured gains,
while treating arbitrary concentrated subsets as stress information rather than
promising identical behavior for every hypothetical mandate. Existing outcomes,
including19.357897% random MDD and35.692399% paired wealth, were visible before
this owner-authorized revision. This is a disclosed policy change, not discovery
of an unseen passing strategy. Do not lower these criteria again against results.

## Implementation and verification

- [x] Preserve the previous contract bytes; revise contract.json, its hash in
  native_joint.py, and the existing production_pool.assess implementation.
- [x] Add boundary tests for deployed versus random risks, retained proximity
  wealth floors, risk distribution deterioration, main improvement, and complete
  failed-history reporting. Run RED then the affected acceptance/publication set.
- [x] Add a concise scope note to daily reports and documentation, tested in both
  simulation and account modes. Do not change trading decisions for disclosure.
- [ ] Freeze a source commit. Run final958 once on this exact source; reuse prior
  independent diagnostics only with demonstrated engine equivalence and authentic
  original identities. Complete matched costs and source qualification.
- [ ] Run the full applicable test suite, types, lint, review and repository checks.
  Save originals using file references/streaming, verify remote hashes when the
  provider permits it, and report any unresolved readback limitation.
- [ ] Publish the honestly assessed evidence, update current documentation, then
  normally merge originalPR101 and verify main. Do not introduce another strategy
  mechanism if the current candidate satisfies the approved use objective.
