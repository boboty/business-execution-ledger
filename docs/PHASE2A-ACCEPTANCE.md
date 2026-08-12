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
eligibility-scoping fix this guards against. Ambiguous same-amount
clusters are asserted to have zero Allocations at that amount.

## No sequence guessing (spec section 20)

`tests/unit/test_matching_engine.py::test_no_sequence_guessing_two_contracts_two_invoices_same_amount`
constructs the forbidden-shortcut shape (two same-amount contracts and
two same-amount/counterparty invoices) and asserts both invoices land in
`HUMAN_CONFIRMATION_REQUIRED` with *both* contracts as candidates, and
zero allocations — never positionally paired. The same shape recurs in
the synthetic suite.

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
