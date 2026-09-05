# Phase 2A Acceptance

Formal public acceptance criteria for Phase 2A. The committed suite
uses independently constructed synthetic data. Run it with
`pytest tests/`.

## Phase 1 regression (spec section 3)

| Criterion | Public coverage |
|---|---|
| Contract import follows the synthetic baseline | Synthetic golden tests |
| `ContractItem = 0` | Phase 2A does not modify `ContractItemModel` |
| Duplicate business key produces one `BusinessKeyConflict` | Integration tests |
| Every Contract is traceable to Evidence | Integration tests |
| Phase 1 behavior remains covered | Public test suite |
| InvoiceItem is never treated as ContractItem | `import_invoices` never writes `ContractItemModel`; no code path connects the two |

## Invoice import (spec section 8)

The synthetic suite covers invoice and item counts plus net/tax/gross
totals against its synthetic baseline. It includes 普票/专票/通行费/铁路电子客票
invoice types, negative-amount (红票) rows, and blank tax-rate rows.
Re-import is idempotent (0 additional records). The suite includes a
multi-item invoice and a negative/red invoice.

## Bank import (spec section 13)

The synthetic suite covers payment count and opening/IN/OUT/closing
balances against its synthetic baseline. `opening + IN − OUT == closing`
holds exactly. It checks the full running-balance chain
transaction-by-transaction, not only the aggregate identity. Re-import
is idempotent (0 additional records).

## Contract-eligible Invoice/Payment sets (spec sections 9/14)

Covered by aggregate eligibility counts in
`tests/golden/synthetic-v1/matching-baseline.json`.

## M001 matching (spec sections 21/22)

Eligibility (counterparty membership in the contract set, checked
*before* candidate computation — never amount) is an explicit gate in
`_run_match_pass`, not a derived-after-the-fact label. Out-of-scope
subjects (invoices/payments whose counterparty was never a contract
party) never become a `MatchCase` at all — checked by asserting the
persisted `MatchCase` count per subject_type equals `eligible_total`
exactly, not `eligible_total + out_of_scope`.

`tests/golden/synthetic-v1/test_matching_baseline.py` asserts
`unmatched` and `eligible_total` directly against its synthetic
baseline, not inferred from `auto + human` summing to the expected
total — a version with no eligibility scoping could pass that weaker
check by coincidence; see docs/PHASE2A-DECISIONS.md for the
eligibility-scoping fix this guards against. In the synthetic golden
scenario the equivalent same-amount cluster's Contracts carry no
`contract_date` (the ledger has no date column), so chronological
fallback is genuinely unavailable there: those cases remain
`HUMAN_CONFIRMATION_REQUIRED` with zero Allocations at that amount.
Deterministic chronological allocation of *dated* equivalent candidates
is asserted directly by the unit tests named below.

## Procurement matching decision (supersedes "no sequence guessing", spec section 20)

The Phase 2A-era rule that two equivalent (same-counterparty /
same-amount) candidates must always become `HUMAN_CONFIRMATION_REQUIRED`
was superseded by later business-owner confirmation — see the SUPERSEDED
section of docs/PHASE2A-DECISIONS.md. Active procurement matching is
**explicit → chronological → human**:

- explicit/authoritative decisions are preserved (never reassigned);
- otherwise subjects are matched in business chronological order and each
  subject allocates to the earliest candidate Contract with sufficient
  remaining capacity (`AUTO_CONFIRMED` with
  `EXACT_COUNTERPARTY_AMOUNT_CHRONOLOGICAL`) — but chronology is only used
  when it is actually needed: a unique/effectively-unique candidate
  allocates regardless of missing dates, and a multi-candidate decision
  that would need chronology stays `HUMAN_CONFIRMATION_REQUIRED` whenever
  a competing Contract has no `contract_date` or an Invoice needing
  subject chronology has no `issue_date`.
- crucially, "effective uniqueness" must be independent of any
  chronological allocation created in the SAME unresolved cohort this
  run: unresolved subjects sharing a normalized counterparty + exact
  amount share the same static candidate Contract pool, so a cohort of
  2+ such subjects requires EVERY member to have a real date (and every
  Contract it could still reach, pre-run, a real `contract_date`) before
  any of them are chronologically allocated — a dated member is never
  processed first merely to consume capacity and narrow what an undated
  sibling sees. See docs/PHASE2A-DECISIONS.md.

Covered by
`tests/unit/test_matching_engine.py::test_two_contracts_two_invoices_chronological_allocation`,
`test_equivalent_candidates_allocate_chronologically`,
`test_three_contracts_three_payments_chronological`,
`test_missing_contract_date_among_multiple_candidates_is_hcr`,
`test_unique_candidate_with_missing_contract_date_auto_confirms`,
`test_missing_invoice_date_with_multiple_candidates_is_hcr`,
`test_missing_invoice_date_effectively_unique_allocates`,
`test_same_contract_date_tie_business_key_beats_uuid`,
`test_same_invoice_issue_date_tie_business_key_beats_uuid`,
`test_mixed_dated_undated_invoices_multiple_contracts_is_hcr`,
`test_mixed_dated_undated_invoices_single_contract_capacity_is_hcr`,
`test_all_dated_cohort_allocates_chronologically`,
`test_single_undated_invoice_unique_contract_auto_confirms`,
`test_single_undated_invoice_preexisting_capacity_makes_one_contract_viable`, and
`test_undated_contract_in_competing_set_is_hcr`.
Same-date ties use real business/source keys (`contract_no`,
`external_invoice_key`, `bank_reference`) before UUID.

## Allocation capacity safety (spec section 24)

`tests/unit/test_matching_engine.py::test_allocation_capacity_exceeded_blocks_second_unique_match`
verifies a second unique-candidate match against an already-fully-allocated
contract does not auto-confirm, and produces an
`ALLOCATION_CAPACITY_EXCEEDED` `TaskException` instead. See
docs/PHASE2A-DECISIONS.md for why this path being untested at the
acceptance level is expected, not a gap.

## Idempotency (spec section 29)

Every import (contract ledger, invoices, bank statement) and every
match run is idempotent: a second run against the same file/state
creates zero additional facts (`is_reimport=True` /
`already_matched_skipped`). Covered by the public synthetic suite.

## Traceability (spec section 27)

`bel invoice get`, `bel payment get`, and `bel contract get` all print
the full chain down to raw Evidence — exercised via CLI, not just unit
tests.

## Full test suite

Public synthetic suite: unit, integration, and golden (synthetic-v1)
layers covering Phase 1 and Phase 2A behavior. Run `pytest tests/` for
the current result.
