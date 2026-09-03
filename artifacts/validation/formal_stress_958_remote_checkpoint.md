# Exact 958-scenario acceptance remote checkpoint

This connector-originated commit intentionally follows the workflow-produced, remotely persisted exact 17-symbol / 958-scenario evidence.

Ordinary pull-request CI must validate this exact branch head before merge. The preceding evidence commit is required to contain:

- 958/958 completed scenarios and 958 unique IDs;
- 17 prefix, 17 leave-one-out, 24 add-one, 750 random-subset, and 150 permutation results;
- finite production-replay metrics and source/data/scenario/run fingerprints;
- the truthful accepted/rejected and canonical/non-canonical state;
- an immutable historical 22-symbol/983 candidate;
- synchronized README, architecture, validation documentation, code comments, and tests;
- no alpha, risk-economic, threshold, universe-order, seed, data, fee, slippage, capacity, T+1, or matching changes.

This file is permanent audit metadata, not a substitute for the result artifact or CI.
