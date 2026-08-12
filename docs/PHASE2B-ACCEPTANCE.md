# Phase 2B Acceptance

Formal public acceptance criteria for Phase 2B. The committed suite uses
independently constructed synthetic data. Run it with `pytest tests/`.

## Phase 1 + Phase 2A regression

| Criterion | Public coverage |
|---|---|
| All Phase 1 tests still PASS | `pytest tests/` |
| All Phase 2A tests still PASS | `pytest tests/` |
| Privacy tests still PASS | `tests/unit/test_private_acceptance_runner.py`, `tools/privacy_scan.py --staged --tracked --history` |
| Migration upgrades a fresh DB to the full schema | `tests/integration/test_migration.py` (subset assertion tolerates new tables) |

## Core objects (spec section 3)

`CostRecognitionFact`, `AccrualBasisFact`, `HistoricalAccrualFact`,
`Accrual`, `AccrualReversal`, `InvoiceItemAllocation` exist as explicit
domain objects — no Generic Fact Framework. `ContractItem.source_item_key`
is unique per `(contract_id, source_item_key)` (spec section 15).

## Shared accrual semantics (spec sections 8-9)

`tests/unit/test_accrual_domain.py` covers: derived remaining
quantity/amount, the single `get_projected_accrual_status` rule
(ACTIVE / PARTIALLY_REVERSED / REVERSED), `is_open_accrual` as the one
predicate R001/R002/R003 share, and the "remaining is never a separately
mutable truth" invariant.

## Close Fact Pack import (spec sections 12-16)

`tests/integration/test_close_fact_import.py` covers: Fact Pack ->
Evidence chain (A02), Fact Pack -> ContractItem,
HistoricalAccrualFact -> Accrual (including a PARTIALLY_REVERSED go-live
state derived from reversal rows), same-SHA idempotency (0 duplicate
facts/contract items/accruals/item allocations), 0-match selector
rejection, item allocation capacity (11-B) and contract-level
confirmation requirement (11-A). The CLI smoke test
(`tests/integration/test_phase2b_cli.py`) exercises `bel import-close-facts`,
`bel accrual list`, `bel period-close preview` against a real migrated
SQLite file and asserts the preview is byte-identical across runs
(stateless recomputation).

## Period Close Preview rules

`tests/unit/test_period_close_rules.py` covers R001 (reversal on invoice),
R002 (new item-level accrual), R003 (duplicate guard), R005 (actual cost
difference), R006 (partial receipt reverses only the portion; final
reversal exact-clear), R007 (contract-level candidate), and the spec
section 37 boundary attacks: partial invoice never reverses 100%,
PARTIALLY_REVERSED + remaining > 0 never yields a new AccrualRequired,
contract-level-only match never yields a guessed reversal amount
(ITEM_MATCH_REQUIRED_FOR_REVERSAL blocker). It also covers the Codex
attack set: two open accruals competing for one allocation emit
MULTIPLE_OPEN_ACCRUALS_REQUIRE_EXPLICIT_SCOPE (never FIFO, never
proportional, never auto-settled by invoice quantity), one allocation is
never double-consumed by two accruals — including by a fully-reversed
closed sibling — and a single open accrual facing several allocations
with unclaimed quantity emits MULTIPLE_ITEM_ALLOCATIONS_REQUIRE_EXPLICIT_SCOPE
instead of picking the actual-cost source by created_at (order swap
asserted identical). SALES invoices never drive reversals nor suppress
new accruals (and PURCHASE invoices do suppress them), and a pending
(unflushed) object survives a preview run with the connection's
`total_changes` counter byte-identical (strict `no_autoflush` read-only).
MISSING_ACCRUAL_BASIS is a diagnostic blocker, not an R011 Decision.

## Golden suite (S2B-01 … S2B-08)

`tests/golden/synthetic-v1/test_period_close_baseline.py` asserts the
full preview against `period-close-baseline.json`:

| Scenario | Contract | Expected |
|---|---|---|
| S2B-01 Partial Reversal | PO-CLOSE-001 | reversal 35 / 420.00, remaining 65 / 780.00, PARTIALLY_REVERSED, difference +35.00 |
| S2B-02 Full Reversal | PO-CLOSE-002 | reversal 880.00, remaining 0.00, REVERSED, difference −40.00 |
| S2B-03 Cross-check | derived | 420.00 + 880.00 = 1300.00 |
| S2B-04 New Item Accrual | PO-CLOSE-003 | AccrualRequired CONTRACT_ITEM 624.00, 0 new Accrual rows |
| S2B-05 Contract Candidate | PO-CLOSE-004 | AccrualCandidate CONTRACT 735.00, MISSING_CONTRACT_ITEM_EVIDENCE |
| S2B-06 Duplicate Guard | PO-CLOSE-005 | 0 new AccrualRequired, 0 monetary reversal, status stays PARTIALLY_REVERSED |
| S2B-07 Item Match Blocker | PO-CLOSE-006 | 0 monetary reversal, blocker ITEM_MATCH_REQUIRED_FOR_REVERSAL |
| S2B-08 Fresh Recompute | PO-CLOSE-007 | candidate present in run 1, absent after confirmed invoice; no stale Decision rows |

The golden test also asserts the preview is a pure function: accrual,
reversal, invoice-allocation, item-allocation and business-event counts
are unchanged across a preview run, and no Voucher/AccountingEntry/TaxEntry
table exists.

## Private acceptance

`tests/private_acceptance/runner.py` adds:
`P2B_CLOSE_FACT_IMPORT`, `P2B_HISTORICAL_ACCRUAL`, `P2B_PRIOR_REVERSAL`,
`P2B_NEW_ACCRUAL`, `P2B_PERIOD_CLOSE`, `P2B_RECOMPUTE`. Each prints only
`SCENARIO_ID: PASS` or `SCENARIO_ID: FAIL` to stdout; full diagnostics —
including the reason a scenario could not run — go to
`$BEL_PRIVATE_DATA_ROOT/reports/`. When the private Close Fact Pack is
absent, the scenario prints `FAIL` and the report explains which evidence
is missing; it never reverse-engineers facts from golden answers (spec
section 33). Real amounts, counts, contracts, and counterparties never
enter the repository.
