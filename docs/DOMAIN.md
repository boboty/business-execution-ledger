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

## SalesContract

The sales-leg counterpart of `Contract`. Added by SCR-2D1R0-001 after
business confirmation that the contract ledger's party columns record
the **procurement** leg: its seller is the external supplier and its
buyer is our own trading/export entity. `Contract` therefore represents
a **procurement contract only**, and it carries no external customer.

`SalesContract` is the object that carries the sales-side business
scope — the 外销合同 under which goods are sold to an external customer.

```
id
our_entity
sales_contract_no
customer
currency
gross_amount
contract_date
status
```

**`customer` is the external buying party**, and it is the only place in
the domain where an external sales customer is expressed. It must never
be inferred from `Contract`'s own party fields: our own entity appears
there as the procurement contract's buyer, so reusing it would attribute
sales business to ourselves.

`customer` may legitimately be **unknown at first**. A sales scope is
often first learned from a reference on procurement evidence, before the
sales contract document itself has been ingested. Such a `SalesContract`
exists with no `customer` and is not yet capable of supporting outbound
invoicing; the customer is supplied later from sales-side Evidence as a
normal supplementing fact, never guessed. A sales-scope reference number
identifies the scope — it is **not** a customer identity, and neither is
a customs-receiving party named on shipping material.

`our_entity` is our own contracting party on the sales leg — the same
kind of party that appears as the buyer of a procurement `Contract`. It
is part of the scope's business identity, and it must come from Evidence
(the sales contract itself, or the procurement evidence that asserts
both our entity and the sales-scope reference on one record). It is
never guessed.

`sales_contract_no` is a **business key**, not a database primary key,
and it is a different namespace from `Contract.contract_no` — the two
never share uniqueness. Following the same discipline `Contract` already
applies, a sales contract number is **not assumed globally unique**: the
business identity of a sales scope is the pair
`(our_entity, sales_contract_no)`. `customer` deliberately takes no part
in identity, because it is allowed to be unknown when the scope is first
learned and supplied later. Duplicates must not be silently merged — the
same identity presenting conflicting business facts surfaces as
`BusinessKeyConflict` (see [RULES.md](RULES.md) R004), never as one
scope.

Sales-side invoices and receipts are attributed to a `SalesContract`
through **their own allocation objects**, physically separate from the
procurement ones. A procurement allocation always attributes to a
procurement `Contract`, and a sales allocation always attributes to a
`SalesContract`; neither can express the other's relationship. A sales
invoice is therefore never attributable to a procurement contract, and a
purchase invoice is never attributable to a sales scope — the separation
is structural rather than a matter of discipline.

Like every other fact object, a `SalesContract` links to the `Evidence`
it was extracted from, and an uncertain case surfaces as a `Task` rather
than a silently assumed customer.

## ProcurementSalesLink

The canonical bridge between the two legs: which procurement
`Contract`(s) supply which `SalesContract`(s). It exists because the two
legs are genuinely separate business objects, and without an explicit
bridge there would be no defined way to relate purchase execution to
sales execution for the same underlying business.

```
id
procurement_contract_id
sales_contract_id
source_fragment_id
confirmation_type
created_at
```

**The link expresses a relationship and nothing else. It deliberately
carries no amount and no quantity.** The relationship between the legs
is many-to-many — one sales scope may be supplied by several procurement
contracts, and one procurement contract may supply several sales scopes
— and on a many-to-many edge there is no basis for apportioning a value
without an explicit allocation fact. Any figure produced by spreading an
amount or a quantity across this bridge would be invented. Cross-leg
apportionment is therefore not part of the bridge and requires its own
confirmed business rule before it can exist.

`ContractItem` does not participate in the bridge. Which purchased item
fulfils which sold line is a separate business question with no evidence
source at this stage, and nothing in the first stage requires it.

`Shipment / Export` is **not** the bridge and never creates one. A
shipment is an execution fact that may *corroborate* a link; a shipment
implying a link that has not been established surfaces as a `Task`, not
as a silently created relationship.

**A link exists only for a relationship that has been confirmed.**
`confirmation_type` records how — deterministically from evidence, or by
a human. Evidence that merely suggests a possible pairing does **not**
create a link: it produces a candidate for human confirmation and, where
it cannot be resolved, a `Task`. Because an unconfirmed relationship is
never written, the link itself needs no open/resolved workflow state of
its own.

`source_fragment_id` is the direct `Evidence` trace for that confirmed
relationship — commonly the procurement record carrying a sales-scope
reference. A human confirmation is not exempt: it must supply its own
manual `Evidence`, so that no link exists whose only justification is
that somebody clicked a button.

Identity has two layers. The **relationship business key** is the pair
`(procurement_contract_id, sales_contract_id)`, and neither end may be
empty — it says *which* business relationship this is. A
`ProcurementSalesLink` record is one **assertion episode**: one occasion
on which that relationship was confirmed to hold. A business key may
therefore accumulate several episodes over time, but **at most one of
them is current** at any moment; the rest are permanently retired.

Within an episode the relationship is idempotent: the same supporting
Evidence arriving again is the same assertion, not a new one, and no
second record is created however many independent pieces of Evidence
come to support it. Re-processing Evidence for a relationship that has
since been retired never makes it current again. Evidence that
conflicts with an existing confirmed pair never overwrites it and never
silently re-points it — it surfaces as a `Task`, and the current link is
unchanged until a human confirms what the conflict means.

Confirming a link asserts that a relationship exists, and such an
assertion can later be shown wrong. A confirmed assertion is therefore
**superseded or invalidated, never deleted and never re-pointed**: the
retired episode keeps both its endpoints and its original Evidence
permanently, a replacement relationship becomes current where one
exists, and where none exists the relationship is simply recorded as no
longer holding. Only a retired episode's successor can be current; a
retired episode is final and is never corrected a second time, and an
episode may be superseded at most once, so a correction history never
branches. Only current episodes take part in business projection and
judgment; retired ones remain auditable.

A relationship that was retired may later be **re-established** — with
new Evidence and an explicit human confirmation, never automatically.
That writes a *new* assertion episode; it does not revive the retired
one, which stays retired permanently. Because the bridge is
many-to-many, a further relationship for the same procurement contract
is a legitimate *addition* rather than a correction — which of the two a
new piece of Evidence means is a human determination, never an
inference. Ambiguity (most notably
one procurement contract supplying several sales scopes, where cost
attribution becomes undecidable) likewise surfaces as a `Task`; the
system does not choose an attribution.

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
