# AB5 release ledger audit

This is read-only release evidence, not new strategy computation or formal
accepted/canonical publication. The owner-approved exceptions remain explicit;
original rejected artifacts have not been relabelled.

## Identity and coverage

- Candidate: `C6-Base+AB5`.
- Frozen source: `4659a2b6d265f45256777da6a2fc25d1369308bd`.
- Base producer: `34509818018`.
- Original D producer: `34572966199`, artifact `10189149456`.
- D ZIP SHA256: `786e1d9c3926bbb0ebe69d12cd6f05412332e94bd7aba08c9b8983161de670b4`.
- D file SHA256: `6afee85f4c35502816ccde2af43f5c528bd0154f1770204dfcf27e18f66dad29`.
- Fixed reference SHA256: `ffe023ae48de50225870c220d37208d1d1ee6c26ba6905a470093562153decbe`.

The five previously authenticated release-audit shards (0, 1, 2, 4, 8) cover
1,600 complete records, 310 AB5 records and 8,159 budget orders: 8,126 filled,
33 validly unfilled. Their individual ZIP/audit hashes remain in PR63's body.
They were reused, not rerun. The remaining seven native shards were independently
downloaded, authenticated and audited together:

| Shard | Artifact | Full records | AB5 records | Budget orders | Filled | Unfilled |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 3 | 10169049907 | 320 | 60 | 1476 | 1470 | 6 |
| 5 | 10167859873 | 320 | 60 | 1478 | 1471 | 7 |
| 6 | 10167876774 | 320 | 70 | 2083 | 2057 | 26 |
| 7 | 10169193921 | 320 | 70 | 2178 | 2165 | 13 |
| 9 | 10169064058 | 320 | 70 | 2059 | 2053 | 6 |
| 10 | 10168356437 | 315 | 65 | 1814 | 1813 | 1 |
| 11 | 10168917069 | 310 | 60 | 1306 | 1303 | 3 |

Combined coverage is **3,825 core records, all 765 AB5 records and 20,553 budget
orders: 20,458 filled and 95 validly unfilled**. This does not substitute for
six additional W interventions, L2 or official17/958. It is additional audit
coverage of the already-completed Base run, not incomplete Base computation.

## Reproducible validation

Each ZIP hash below matched fresh GitHub artifact metadata. Each compressed
`shard.json.gz` hash matched its native `validation.json`; source, P hash,
shard index/count, chunk size, exact partition and ordered item IDs matched the
frozen manifest. The frozen `_validate_record` recomputed each result's canonical
semantic hash. `validate_execution_facts(complete_path=True)` reconciled every
order, fill, inventory, cash/equity valuation, HWM, drawdown and trade metric.
No strategy was replayed. The existing `c6_stream._Reader` used an 8 MiB parsing
chunk; no native record bytes or tolerance changed.

| Shard | ZIP SHA256 | Detailed audit summary SHA256 |
| --- | --- | --- |
| 3 | 80ee5c6950986d35eb0e0ee56f3162dab743828b2e6176e720b5b3a25e6d8e34 | c8460916af79bece068c12d3fbb24dbaf089931d0ad1a50758089bc9edd76ea6 |
| 5 | b1e9f492ef93836b91b8de20c9a47ce4a9aa216f53cd225fa1bbd045c4ed54d9 | 833f8237b72d02bb4db89bb3463cf44f069add0a6d4150cd1e368a3b879f3777 |
| 6 | 4e2e08fe7b20566cd9597931c92fb9ffd17f88c3dbf82bf082fef274d52dc0d8 | 72965cc47a8005dc20853d135801494d06162155b7a85d3593867b080ff3bba6 |
| 7 | bdda1a2961ec5fb29a1bc276bd86fe1532e5d7dd4d6ba2305f90504dc09d6180 | 5830c0a7a6ef082d8119de1025e2719fd6971280be8d3736f44b53328ab1725b |
| 9 | 6ebff8ff18fa98256c5e820961859a8c37df02f090c5926e8cf53afcd33fac05 | 235710f7fd4f6e6c5607ad5f6ce57ad067d5985332cfc7524dae0aab34f4dc2f |
| 10 | f5299912ee3d991ed948d286388f41d2bd2db49ae2f146852d9b35d65c960667 | 4953f4ed2d09492e17eaa161840114301843f7e3e87b94121e8cdb2be3453f2d |
| 11 | 3e18e45ac0511663b106b74e4da15904ae0d1bf31b77670f544f3e7eca04ca23 | ccdfc18d93194569f9467ac5ea7c57fb58bac865f60a1ef83fb863cb400e408a |

All factual reconciliations passed. Maximum HWM and drawdown recomputation
differences were both zero. Maximum absolute cash accumulation difference in
the new batch was `2.2351741790771484e-08`, within the exact frozen account-scale
cash bound. All 62 newly audited unfilled budget orders retained the native
`deferred_sell_no_prior_adv_capacity` status; none was turned into a fill.

All actual `account_budget_trim` orders were defensive SELLs with priority 80.
Queue snapshots contained no future execution/fill. Root orders were queued at
decision close and attempted at the next recorded equity session. Carried orders
retained book/symbol/strategy/side/decision identity, had an existing acyclic
parent chain and executed after the previous attempt, never before their queue
or on/before the root decision. No violation of these checked causal conditions
was found. This is not an assertion that every order necessarily filled.

## Existing L1 predicates under the owner's accepted exceptions

The exact original D's 13 `base_l1_predicates` match P's ordered L1 manifest.
Their copied observations were re-evaluated with the release assessor from
`b25be996881350caa273d5ef4e6edd5b0ae6607c`. All 13 pass the derived release
comparison, including the four explicitly accepted envelopes. This is a
projection from the authenticated D, **not a new mechanical D or official
publication**, and does not pretend the projection is the complete Base payload.
The original D remains `QUALIFICATION_REJECTED` with no selected candidate.

Formal publication binding, any missing L2/official17-958 validation and final
full suites/five-pool regression still require their actual source-bound evidence.
The accepted historical envelope is not a future return or drawdown guarantee.
