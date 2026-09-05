# C6 streaming execution plan

Execute inline, root only, under existing continuation/redo authorization.

1. Add strict bounded IO leaf with canonical compatibility and malformed-input
   tests in the existing non-economic bound-run test module.
2. Integrate checkpoint spool/views and streaming payload/digest publication;
   validate interrupted/resumed computation with poisoned completed inputs.
3. Remove full record accumulation in L1 predicates, S comparison, qualification
   and D authentication; validate existing contracts on file-backed inputs.
4. Stream remote ZIP exports and sequential history restore. Verify size/path/
   hash guards and synthetic resource use. No real scenario replay.
5. Checkpoint exact tree remotely and update #59. Prepare separate trusted resume
   preflight from main after shared helper stabilizes; retain exact branch filter.
6. Complete outstanding factual evidence repairs before fresh P/I_B/I_S/R.

At each coherent change use targeted tests. At subsystem integration include the
architecture contract and remote required checks. Do not repeat economic runs or
claim completion while a factual contract gap remains.
