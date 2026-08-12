# Phase 2B Decisions

Judgment calls made while implementing Phase 2B (first deterministic
month-end close engine). Per the Phase 2B spec section 39: **no Phase 0
document was modified** — `DOMAIN.md`, `RULES.md`, `V1-SCOPE.md`,
`ARCHITECTURE.md`, and `GOLDEN-TEST.md` are untouched. R001–R007
semantics are unchanged, and R008–R015 remain `PROPOSED`. The judgment
calls below are recorded here instead. Any genuinely conflicting
specification would be a `SPEC_CHANGE_REQUEST`, not a silent deviation.

## The five required Phase 2B facts stay explicit objects

No Generic Fact Framework, no `GenericFact(type, payload_json)`.
`CostRecognitionFact`, `AccrualBasisFact`, `HistoricalAccrualFact`,
`Accrual`, `AccrualReversal`, and `InvoiceItemAllocation` are each their
own typed domain object with their own table. See spec section 3.

## CostRecognitionFact does not judge business behavior

`basis` accepts only `MANUAL_CONFIRMED`, `SALES_EXECUTION_CONFIRMED`, and
`EXPORT_EXECUTION_CONFIRMED`. The Rule Engine consumes the fact exactly as
the Fact Pack states it — Phase 2B never decides that some business action
"means" cost recognition, so no single company's accounting habit is
hard-coded into the generic system. See spec section 4.

## `Accrual.status` is a cache; the balance is the truth

`remaining_quantity` / `remaining_estimated_cost` are always DERIVED as
`original Accrual − sum(AccrualReversal)` (spec section 8). The `status`
column on `accruals` is a convenience cache that is only ever rewritten
through the shared `get_projected_accrual_status(...)` domain function,
never mutated by hand. The stored value is not an independent truth.
`is_open_accrual(...)` is the ONE predicate R001, R002, and R003 all
share (spec section 9) — nobody re-implements the "not fully reversed"
condition locally. `accrual list` / `accrual get` recompute the balance
and projected status on read for exactly this reason.

## R001 consumption tracking — shared, never double-consumed

The rule is deliberately conservative. `InvoiceItemAllocation` quantity is
a **shared resource across every Accrual on the same ContractItem**, and
Phase 2B never invents FIFO, month-proration, or proportional splitting:

- Same ContractItem, **exactly one open Accrual** → a reversal may be
  computed from the qualifying `InvoiceItemAllocation`s.
- Same ContractItem, **multiple open Accruals** and the allocation carries
  no explicit Accrual scope → **no monetary reversal at all**, and a
  `MULTIPLE_OPEN_ACCRUALS_REQUIRE_EXPLICIT_SCOPE` blocker. Even when the
  invoice quantity is large enough to cover several accruals, the engine
  does not auto-settle them — both the quantity split AND the actual-cost
  attribution are ambiguous, so a human must scope which accrual the
  invoice belongs to.

Two implementation consequences:

- **One allocation is never consumed twice.** Every reversal on a
  qualifying allocation counts against the item's shared capacity —
  including reversals owned by a fully-reversed (closed) sibling Accrual
  on the same item. An allocation is a real resource; its quantity is
  never reusable.
- When the shared capacity is fully consumed there is no contested scope,
  so no blocker is emitted (there is no reversal decision to make).

## Single open accrual + several allocations — no `created_at` guessing

Even with exactly one open Accrual, a reversal's **actual-cost
attribution** is ambiguous when more than one qualifying
`InvoiceItemAllocation` has unclaimed quantity: the allocations can carry
different unit costs, so picking the "source" of the reversed portion by
creation order would silently change the `AccrualActualDifference`. The
engine requires the reversed portion to be attributable to exactly ONE
allocation:

- exactly one qualifying allocation with unclaimed quantity → compute the
  reversal from that allocation (its unit cost and its id);
- more than one → `MULTIPLE_ITEM_ALLOCATIONS_REQUIRE_EXPLICIT_SCOPE`, no
  monetary decision. Swapping allocation creation order changes nothing:
  the outcome is the same blocker either way (covered by a permanent
  attack test).

Future resolution (not Phase 2B) would add an explicit
`InvoiceItemAllocation → Accrual` scope link, never a sort-order rule.

## Purchase invoice gate

R001 and R002 operate on 进项 (PURCHASE) invoices only. `is_purchase_in_period`
requires `Invoice.direction == PURCHASE` in addition to `issue_date <= period_end`.
A SALES invoice's item allocation never reverses an accrual, and a SALES
invoice never suppresses a new `AccrualRequired` via the section-22
"already invoiced" gate.

## Strict read-only preview

`build_period_close_preview` runs its entire computation under
`session.no_autoflush`. A preview must never write, and its reads must
never autoflush a pending (unflushed) object into the database — the
pending-object/total-changes attack would otherwise turn a "read-only"
preview into a write.

## Exact clear on the final reversal

When `reversal_quantity == remaining_quantity`, the reversal uses
`remaining_estimated_cost` exactly instead of `historical unit × qty`
rounded, so a non-terminating unit cost (e.g. qty 3 / 10.00) never leaves
a 0.01 residue. See spec section 18 and `test_r006_last_reversal_uses_exact_clear`.

## The Close Fact Pack carries an `accrual_reversals` section

Spec section 12 lists five pack sections. Go-live state can legitimately
already contain partial reversals (a company that reversed part of a
historical accrual before the system existed). To represent that state
through the same sanctioned import path — instead of a bespoke write path
— Phase 2B adds one narrow `accrual_reversals` section. It is not a
generic import extension: it references an `invoice_item_allocations`
entry and a `historical_accrual_facts` entry already in the pack and is
processed last. Reversal rows are how an Accrual legitimately becomes
`PARTIALLY_REVERSED` in the database.

## Contract-level match output is constructed directly in the S2B fixtures

The S2B partial-receipt invoices are intentionally smaller than the
contract gross, so Phase 2A's M001 exact-amount rule would never fire on
them. The golden/integration tests therefore construct the confirmed
contract-level `InvoiceAllocation` + `MatchCase` rows directly through the
repositories — representing Phase 2A's already-built M001/human-confirm
output — before importing the fact pack. The section-11-A guard then
validates item allocations against those rows exactly as it does in
production.

## `source_item_key`

An implementation-level stable reference on `ContractItem`
(`(contract_id, source_item_key)` unique) used by Fact Pack selectors.
It is NOT a global business key and NOT a SKU; Phase 0 Domain semantics
are unchanged (spec section 15).

## MISSING_ACCRUAL_BASIS is a diagnostic blocker, not a Decision

A cost-recognized contract with no `AccrualBasisFact` gets a
`CloseBlocker(type=MISSING_ACCRUAL_BASIS)`. This is deliberately NOT the
PROPOSED R011 `EvidenceMissing` Decision — R011 is not implemented and
its status is untouched. See spec section 26.

## Stateless recomputation is not R015

`bel period-close preview` is a pure read. Every Decision is recomputed
from current database facts on each run; nothing is persisted, no
Voucher/AccountingEntry/TaxEntry is produced, and no BusinessEvent is
created. When a fact changes (an invoice arrives), the next preview
naturally no longer emits the old candidate — because no stale Decision
row ever existed. This is the R015-adjacent *behavior* that spec 30 wants,
but R015 itself stays `PROPOSED` and is not claimed as implemented.

## Idempotency design

Re-importing the same bytes is fully idempotent at the EvidenceDocument
(sha256) level: zero new facts. Beyond that, contract items
(`(contract_id, source_item_key)`), accruals (`(contract_item, period)`),
item allocations (`(invoice_item, contract_item)`), facts and reversals
are skip-if-exists so a compatible re-import never doubles a fact — in
particular a repeated historical accrual can never yield a second ACTIVE
Accrual for the same item-period (spec section 16).

## Contract-level "already invoiced" gate (section 22)

`has_confirmed_invoice_in_period(contract_id)` — a confirmed
contract-level `InvoiceAllocation` whose invoice is dated by period_end —
suppresses R002/R007 for the whole contract even when item matching is
incomplete. This is why S2B-06's already-invoiced PARTIALLY_REVERSED
accrual produces zero new `AccrualRequired`; the R003 duplicate guard is
additionally verified in isolation by `test_r003_duplicate_guard_blocks_new_accrual`.

## Data file layout

See `docs/PRIVATE-DATA-POLICY.md`. The Close Fact Pack lives at
`$BEL_PRIVATE_DATA_ROOT/<period>/facts/phase2b-close-facts.json`
(private, never committed); the public mirror is
`fixtures/synthetic/phase2b_close.py` + the golden suite. When the
private facts are absent, the private scenarios print `FAIL` on stdout —
never NOT_READY, never a reverse-engineered answer — and the detailed
"what evidence is missing" explanation goes only to the private report
under `$BEL_PRIVATE_DATA_ROOT/reports/` (spec section 33).
