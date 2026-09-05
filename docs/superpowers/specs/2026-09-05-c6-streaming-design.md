# C6 bounded evidence IO

Status: implementation authorized as necessary fresh-cycle execution repair. No
new economic run is permitted until factual evidence repair and P/I_B/I_S/R
freeze are complete. Historical freezes remain immutable.

## Problem and boundary

The 3,831-row Base L1 payload contains full paths. Synthetic format sizing exceeds
the trusted resume reader's 1 GiB expanded ZIP limit before many required fields
are included. Whole-file reads, JSON decoding, deepcopy and cumulative producer
lists multiply memory. Increasing the ZIP cap alone is insufficient.

GitHub documents public ubuntu-24.04 runners as 4 CPUs, 16 GB RAM and 14 GB SSD:
https://docs.github.com/en/actions/reference/runners/github-hosted-runners
These are planning constraints, not proof of available disk space on a run.

## Design

Use a small standard-library IO leaf for canonical chunk writing, strict JSON
object loading with indexed on-disk top-level record arrays, bounded file copy
and hash. Preserve sorted keys, compact separators, UTF-8, finite values, unique
keys and final LF exactly. Index entries retain file byte offsets; each record
is decoded independently. Slicing/filtering keeps offsets, not decoded paths.

Checkpoint computation appends completed records to a private local spool and
atomically publishes the existing v2 canonical checkpoint shape. Resume verifies
the exact ordered prefix and every record hash before importing it. Returned
results are immutable file-backed views. Six interdependent W rows remain one
batch. There is no parallelism/order or economic calculation change.

Large arrays stay file-backed through predicates, S counterpart comparison,
qualification, schema validation and sealed producer authentication. Small
metadata and controls remain ordinary objects. Official L4 bytes remain exact.

ZIP download/extraction uses private temporary files, streaming hashes, bounded
compressed/expanded bytes, strict member/path/type validation and no credential
forwarding. Prior-attempt verification must release older extracted data before
downloading another cumulative checkpoint; retain only exact chain metadata and
the latest child. Do not discard immutable remote evidence.

Trusted automatic resume imports only the trusted checkout's IO helper. Changes
to trusted orchestration require a separate result-independent preflight PR;
unaccepted candidate changes from #59 cannot enter main by that route. Fresh P
will explicitly freeze resource limits and dispatch branch. Numeric engineering
budgets are chosen before fresh P, not inherited as scientific thresholds.

## Verification and limits

Compare canonical bytes and SHA against the existing serializer using nested,
Unicode, floating-point and empty records. Reject duplicates, nonfinite values,
truncation, trailing data, oversize records and unsafe ZIP members. Poison completed
inputs on resume to prove no recomputation; test deterministic order and exact
prefix fencing. Synthetic large-file subprocess checks demonstrate bounded RSS
and disk use. Test streamed producer authentication against forged bytes/hash.

This work does not certify remaining order lifecycle, causal/W attribution,
indicator provenance or L2 semantics. Those remain blocking before fresh freeze.
