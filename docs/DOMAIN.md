# Domain Model (Semantics)

Phase 0 freezes *meaning*, not schema. There is no DDL here, and no
storage technology is implied by field lists below — they describe what
each object must be able to express, not how it is persisted.

## Contract

```
id
contract_no
contract_type
counterparty_id
contract_date
currency
gross_amount
status
```

**Key constraint:** `contract_no` is a **business key**, not the database
primary key (`id` is). The system must tolerate `contract_no` duplicates
in the source data — it must **not** assume uniqueness — but a duplicate
`contract_no` across different counterparties or conflicting business
facts must surface as `BusinessKeyConflict` (see [RULES.md](RULES.md)),
never be silently treated as "the same contract."

## ContractItem

```
id
contract_id
sku
product_name
specification
quantity
unit
unit_price
gross_amount
tax_rate
net_amount
```

`ContractItem` is a first-class, core V1 object — a `Contract` is not
allowed to degrade into a single amount-level object. Business
correctness (accrual, matching, reconciliation) depends on item-level
quantity and product identity being tracked, not just a contract total.

## Invoice / InvoiceItem

Must support all of the following simultaneously:

- one contract with many invoices
- one invoice with many line items (goods)
- one invoice referencing multiple contracts

Consequently, `Invoice ↔ Contract` must **not** be modeled as a simple
one-to-one foreign key. The association is many-to-many and is expressed
through matching (see below and `ContractItem ↔ InvoiceItem` in
[V1-SCOPE.md](V1-SCOPE.md)), not through a single field on either object.

## Payment / PaymentAllocation

`Payment` preserves the real transaction granularity of the bank world.
For example, a single contract's payments may look like:

```
7/6   1,250.00
7/14  2,480.00
7/21    990.25
```

These must **not** be merged into one payment row for the convenience of
contract-level reporting. Each bank transaction stays its own `Payment`.

Business attribution — which contract(s) a payment belongs to — is
established separately through `PaymentAllocation`, which can split a
single `Payment` across multiple contracts, or aggregate multiple
`Payment`s toward one contract. The bank-grain fact and the business
attribution are always two different objects.

V1's attribution granularity is **contract-level**, matching the
`Contract ↔ Payment` match type in [V1-SCOPE.md](V1-SCOPE.md) — there is
no `Payment ↔ ContractItem` match in V1, so `PaymentAllocation` does not
attribute a payment to a specific item in this phase. `PaymentAllocation`
is the record of that attribution, not a separate match type of its own.

## Shipment / Export

The business execution fact that goods tied to a `Contract` (and, where
known, specific `ContractItem`s) physically left — evidenced by customs
declaration material, packing lists, or other export/logistics
documents. Its role in V1 is to support the `Contract ↔ Export` match
listed in [V1-SCOPE.md](V1-SCOPE.md): it lets the system reason about
export execution against contract terms (e.g. "did the goods this
contract covers actually ship").

`Shipment / Export` records **export execution**, not tax treatment.
It never carries a tax-rebate filing/申报 status, an amount owed to or
from the tax authority, or any other rebate-domain field — that
vocabulary belongs to a future Adapter, per A04 in
[ARCHITECTURE.md](ARCHITECTURE.md). Like every other fact object, it
links to the `Evidence` it was extracted from and, where uncertain,
surfaces as a `Task` rather than a silently-assumed match to a `Contract`.
Phase 0 does not fix its full field list beyond this; that is Phase 1
work once real 出口/报关 evidence is being ingested.

## Accrual

```
id
period
contract_item_id
quantity
estimated_cost
basis
status
created_from
```

Status values:

```
ACTIVE
PARTIALLY_REVERSED
REVERSED
```

The model must natively support **partial** invoice receipt and
**partial** reversal — an `Accrual` does not have to move atomically from
`ACTIVE` to fully `REVERSED`. `created_from` traces the Accrual back to
the confirmed Fact(s) that justified creating it — never directly to
Evidence. Those Facts are themselves traceable to their own Evidence (see
A02's `Decision → Fact → Evidence` chain in [ARCHITECTURE.md](ARCHITECTURE.md)).

A "not fully reversed" Accrual — the condition R001, R002, and R003 test
for — means `status ∈ {ACTIVE, PARTIALLY_REVERSED}` with a remaining
(un-reversed) balance greater than zero, not `status == ACTIVE` alone.

`contract_item_id` is required — see R007 in [RULES.md](RULES.md) for why
an Accrual cannot be created at the contract level alone.

## Evidence

A **CONFIRMED** Fact — anything the system treats as a trustworthy basis
for a rule to act on — **must** link to one or more `Evidence` records.
This is not optional: content extracted or claimed with no Evidence
behind it is not a Fact yet. It stays a Proposal, carrying a
`PROPOSED` / `HUMAN_CONFIRMATION_REQUIRED` confidence state (A05) and,
if actionable, a `Task`, until Evidence exists to promote it. Examples of
evidence:

- a file (合同/发票 PDF, scan, etc.)
- an Excel row
- a bank statement record
- an invoice record
- a manual human confirmation
- (future) an email, an OA document

**Evidence is immutable.** A later change in business judgment (a
different match, a corrected fact, a reversed accrual) never rewrites or
deletes the original Evidence — it produces a new Fact/Decision that
still points back to the same, unaltered Evidence trail.

This makes A02's `Decision → Fact → Evidence` chain a strict pipeline,
not a shortcut graph: a `Decision` references the `Fact`(s) it was
computed from — never Evidence directly — and each of those `Fact`s
references the Evidence that confirmed it. Nothing downstream of Evidence
is allowed to point around a missing link in that chain; a missing link
means the chain stops at a `Task`, not that the next layer reaches back
past it.

## BusinessEvent

An append-only record of things that happened to the above objects over
time (a fact was created, a match was proposed/confirmed, an accrual was
reversed, etc.). Phase 0 does not fix its schema — it is named here
because V1-SCOPE.md lists it as a managed object, and later phases must
not casually merge it into `Evidence` or `Task`, which serve different
purposes (immutable source material vs. actionable follow-up vs.
history).

## Task / Exception

The landing object for anything a rule or an Agent could not resolve with
high confidence (A05) — `BusinessKeyConflict`, `EvidenceMissing`,
low-confidence match proposals, etc. Phase 0 does not fix its schema
beyond: it must carry a reference back to the Fact/Evidence in question
and a resolvable status, so it can drive the 异常与任务中心 (Exception &
Task Center) page.
