# V1 Scope

This document freezes what V1 (the first stage of BEL) builds and, just
as importantly, what it does not. Anything not listed as In Scope is out
of scope for V1 by default — it does not need a separate exclusion
entry.

This is the Phase 2D.0 rebaseline. It restates V1's Definition of Done
as a product outcome and aligns every claim in this document against
**verified code reality** — see the Code Reality table in
[PHASE2D0-DECISIONS.md](PHASE2D0-DECISIONS.md), which tags each
capability with an Implementation Status and a Decision Status. Where
this document names a capability, it says plainly whether that
capability exists today.

## 0. V1 Product Definition of Done

V1 is done when BEL **replaces the manually-maintained contract business
ledger spreadsheet (合同台账 Excel) as the System of Record for business
facts and deterministic business state** — not when a fixed list of
technical phases has shipped. After the first stage, business staff no
longer hand-maintain business status in Excel.

As new business facts continuously enter BEL, BEL must be able to:

| # | Definition-of-Done capability | Status today |
|---|---|---|
| 1 | Reconstruct a contract's current execution state | **Partially implemented** — 合同360° covers a single contract; the cross-contract business ledger and its Excel export exist, and the sales-side association exists, but first-stage coverage is not yet cutover-complete |
| 2 | Produce a period-close / accrual business judgment at any point in time | **Capability implemented** (Phase 2C.2 read-only preview + Phase 2D.2 Data Product) — first-stage coverage is not yet cutover-complete (see below) |
| 3 | Generate a deliverable accrual business data product | **Implemented** (Phase 2D.2: Period Close Data Product, XLSX + CSV) — first-stage coverage is not yet cutover-complete |
| 4 | Give the business data needed to prepare invoicing in both directions — issuing sales invoices to external customers (向客户开票) and requesting supplier invoices (向供应商要票) | **Implemented** (Phase 2D.3) as a read-only Workbench + Data Product built from confirmed Facts with deterministic comparison and management advisories — see section 5.1 |
| 5 | Expose what cannot be determined, lacks evidence, or needs a human | **Implemented** (Phase 2D.4-F1) as a read-only global Center — persisted TaskExceptions, `HUMAN_CONFIRMATION_REQUIRED` MatchCases and (for a requested period) computed Period Close blockers in one neutral read projection; no generic resolve workflow, no new unresolved-work storage object, and R009–R015 remain PROPOSED |
| 6 | Export business results as an Excel / CSV Data Product | **Implemented for Contract Business Ledger (2D.1), Period Close (2D.2), Invoice Preparation (2D.3), and Exception & Task Center (2D.4-F2, CSV + XLSX)** |

**On capability 2 specifically:** the technical ability to answer "given
the facts confirmed so far, what would this period accrue, reverse,
differ by, and fail to judge" is implemented and shipped, as are its
supporting object pipelines (`ContractItem`, `Shipment / Export`, and
the sales-side association — sections 2.2, 2.3, 3.1). That is a
statement about the capability, **not** a claim that the first-stage
Definition of Done is met: the *business coverage* of that answer is
still bounded by first-stage cutover and legacy backfill (section 7).
Capability implemented; first-stage coverage not yet cutover-complete.

Excel remains legitimate as an **import format**, an **export format**,
a **cutover/backfill source**, a **downstream handoff format**, and a
**human-readable data product**. Excel is no longer the authoritative
System of Record — BEL is. See
[PHASE2D0-DECISIONS.md](PHASE2D0-DECISIONS.md) for the reasoning and
[ARCHITECTURE.md](ARCHITECTURE.md) for the frozen boundary this sits
inside.

Reaching this Definition of Done additionally requires a cutover, not
only feature completion — see section 7.

## 1. Evidence Import

V1 supports **manual upload only** of these evidence sources:

- 合同台账 Excel (contract ledger spreadsheet)
- 采购合同 (purchase contracts)
- 进项发票数据 (input/purchase invoice data)
- 销项发票数据 (output/sales invoice data)
- 银行流水 (bank statements)
- 出口/报关业务资料 (export / customs declaration material)
- 人工补充事实 (manually supplied facts)

This list is a **scope freeze, not an implementation-status claim**.
Section 2.1 states which of these have an importer today and which do
not.

### Explicitly out of scope for V1 (future Adapters)

- 邮箱 (email)
- OA (office automation system)
- 银行 API (bank API)
- 电子税务局 (e-tax bureau)
- 财务系统 (finance system)
- RebateX

These are all future **Adapters** — inbound evidence sources or outbound
consumers connected through the Adapter boundary defined in
[ARCHITECTURE.md](ARCHITECTURE.md). V1 does not build any of them, and
does not build a generic plugin framework in anticipation of them.

## 2. Business Object Management

V1 manages at least these business objects. They deliberately span
different [ARCHITECTURE.md](ARCHITECTURE.md) A02 layers — this is the
list of what V1 must manage, not a claim that every entry is a Fact:

**Business facts, and the records that attribute them**

- `Contract` — the **procurement** leg only (its buyer is our own entity)
- `ContractItem`
- `SalesContract` — the sales leg, carrying the external customer
- `ProcurementSalesLink` — the bridge between the two legs
- `Invoice`
- `Payment`
- `PaymentAllocation`
- `Shipment / Export`

**Derived business record** — computed from confirmed Facts
(`Accrual.created_from` traces to the Fact(s) that justified it), never
supplied directly by a user

- `Accrual`

**Source material** — immutable, and not a Fact by itself: content with
no Evidence behind it is still a Proposal (A02, [DOMAIN.md](DOMAIN.md))

- `Evidence`

**History** — an append-only record of what happened to the objects
above

- `BusinessEvent`

**Unresolved-work projection** — the landing object for what a rule or
Agent could not resolve (A05); never a source of business truth of its
own (see section 5.2)

- `Task / Exception`

Semantics for each are frozen in [DOMAIN.md](DOMAIN.md). The object list
itself is unchanged by Phase 2D.0; only its grouping by A02 layer is
new, and no object's frozen semantics are restated or altered here.

### 2.1 Business Fact Maintenance is a capability, not a page

Business staff must be able to continuously supply BEL with new business
facts. First-stage input paths, with their real status:

- **File import** — partially implemented. Importers exist for the
  contract ledger workbook, the purchase/sales invoice workbook, a
  bank-statement PDF, and a manually-supplied Close Fact Pack. There is
  **no** importer for 采购合同 documents or 出口/报关业务资料.
- **Direct web entry or supplementation of a business fact** — **not
  implemented**. The only human write the web layer offers today is the
  manual InvoiceItem allocation shipped in Phase 2C.

**What users maintain is a Fact, never a derived state.** Business
states such as 待暂估 (accrual pending), 待红冲 (reversal pending),
可开票 (invoiceable), 已完成 (complete), 已匹配 (matched), or
异常已解决 (exception resolved) must be derived from confirmed Facts
plus the deterministic Rules in [RULES.md](RULES.md). V1 must not add a
free-form status field a user can edit directly to represent any of
these.

What a user legitimately supplies is: new Evidence, a new Fact, a human
confirmation of a Fact, or a lawful correction of an existing Fact.

### 2.2 `ContractItem` completeness is a first-stage critical path

`ContractItem` is a first-class V1 Domain object
([DOMAIN.md](DOMAIN.md)) and it has **no ordinary business intake path
today**. The contract-ledger Excel adapter promotes only contract-header
fields to a canonical `Contract`; it structurally creates zero
`ContractItem`s. The only code path that creates a `ContractItem` is the
Close Fact Pack importer — a human-authored file.

This is a critical path, not a convenience gap, because R007 is already
frozen: where contract/supplier/amount are determinable but product and
quantity are not, only a **contract-level candidate** may be formed —
never a formal item-level `Accrual`. Downstream, all of the following
depend on `ContractItem` completeness:

- formal period-close accrual data (rather than contract-level candidates)
- partial reversal (R006 tracks matched `ContractItem` quantity)
- item-level invoice attribution
- invoice preparation quantity/product facts (both the sales and the
  supplier direction)
- the Contract Business Ledger's 商品/业务范围 dimension

`ContractItem` Fact Maintenance is therefore the first implementation
item of Phase 2D.1 (see [ROADMAP.md](../ROADMAP.md)). It is **not
ordinary CRUD**: the required semantic is

```
human-supplied / confirmed Evidence
      ↓
ContractItem Fact
      ↓
deterministic recompute
```

combined with the correction/supersession mechanism in section 2.4.

### 2.3 `Shipment / Export` — canonical Fact capability implemented

[DOMAIN.md](DOMAIN.md) freezes `Shipment / Export`'s meaning. The
**canonical Fact capability is implemented** (Phase 2D.1-R2, extended by
Phase 2D.3-F1c):

- a canonical `Shipment` domain object exists with persistence
  (`Shipment` anchor + `ShipmentRevision`);
- current/versioned Fact semantics exist (INITIAL / SUPPLEMENT /
  CORRECTION with a stable anchor, exactly like `ContractItem`);
- every `Shipment` names exactly one procurement `Contract`;
- `declared_amount` / `declared_currency` exist as nullable canonical
  Facts (Phase 2D.3-F1c) — the export/customs declaration values,
  asserted only with Evidence, never inferred, never FX-converted, never
  defaulted;
- the Contract Business Ledger can project Shipment/export context per
  procurement Contract.

This is a statement about **canonical Fact capability implemented**.
Source-document/import coverage may still be incomplete — that is a
separate question from whether the canonical object and its Facts exist.

**Customs/export is a management anchor / context Fact, NOT automatic
invoice eligibility.** Export execution does not by itself decide
invoicing eligibility; it is one management/context fact among the
confirmed Facts (Phase 2D.3 comparison is a deterministic management
control, section 5.1).

### 2.4 Fact correction / supersession semantics — frozen in Phase 2D.1-R0

`Evidence` is immutable ([DOMAIN.md](DOMAIN.md)) and remains so.
Correcting a business fact must never be implemented by overwriting
`Evidence` or by silently mutating a canonical Fact without an audit
trail.

At the time this section was first written there was **no modeled way**
to mark a Fact as corrected or superseded, and freezing those semantics
was listed here as a deferred decision. **That freeze has since been
completed in Phase 2D.1-R0** — see
[PHASE2D1-R0-DECISIONS.md](PHASE2D1-R0-DECISIONS.md) section 1, which
freezes the three-case taxonomy (supplement / correction / business
progression), the requirement that every correction rests on new
Evidence, a stable identity anchor with versioned revisions so existing
references never dangle, and the recomputation response. Implementation
is Phase 2D.1-R1.

`ContractItem` maintenance (2.2) and legacy backfill (section 7) both
depend on that freeze, which is why it was sequenced first.

### 2.5 Sales-side scope and the procurement/sales bridge

Added by SCR-2D1R0-001, after business confirmation that today's
`Contract` rows describe the **procurement** leg only.

**`SalesContract`** carries the sales-side business scope and is the
only place an external customer is expressed. Its `customer` may be
`NULL` — a sales scope is often first learned from a reference carried
on procurement evidence, before the sales contract document itself has
been ingested — and is supplied later from sales-side Evidence as a
normal supplementing fact. A sales-scope reference number is **not** a
customer identity, and neither is a customs-receiving party named on
shipping material. An unknown customer stays an explicit
missing/supplement-needed Fact state (surfaced as 客户待补充 on the
Workbench) — never a guess, never a preparation blocker, and no
persistent Task is invented merely from customer absence in Phase 2D.3
(the only customer-related Task that exists is the implemented
`sales_contract_facts` intake follow-up when a sales-contract anchor is
created without a customer). Customer presence is deliberately NOT one
of the three preparation inputs and adds no finding.

**`ProcurementSalesLink`** is the bridge: which procurement contract(s)
supply which sales scope(s). It is many-to-many, and it **expresses a
relationship only — it carries no amount and no quantity.** V1 performs
**no apportionment of amounts or quantities across the bridge**: on a
many-to-many edge there is no basis for spreading a value without an
explicit allocation fact, so any such figure would be invented.
Cross-leg apportionment requires its own confirmed business rule and is
not in V1.

`ContractItem` does not participate in the bridge, and V1 builds **no
sales-side item object**. `Shipment / Export` is **not** the bridge and
never auto-creates a `ProcurementSalesLink`; a Shipment may corroborate
an existing relationship, but an ambiguous/conflicting relationship
remains explicit for human resolution — no Task is auto-created from a
Shipment alone. A Shipment is a management/context anchor (its
`declared_amount` / `declared_currency` feed the Phase 2D.3 customs
comparison), NOT an invoiceable-quantity basis: the supplier quantity
basis (IP-P07, contract / shipped / declared precedence) remains
unresolved, and nothing here establishes shipped quantity as
invoiceable quantity.

## 3. Matching

V1 supports these match types:

- `Contract ↔ Invoice` — procurement leg (`PURCHASE` invoices)
- `Contract ↔ Payment` — procurement leg (`OUT` payments)
- `Contract ↔ Export`
- `ContractItem ↔ InvoiceItem`
- `SalesContract ↔ Invoice` — sales leg (`SALES` invoices)
- `SalesContract ↔ Payment` — sales leg (`IN` receipts)
- `Contract ↔ SalesContract` — the procurement/sales bridge, expressed
  as `ProcurementSalesLink`

No other match types are in scope for V1. The last three were added by
SCR-2D1R0-001 (Phase 2D.1-R0) once business confirmation established
that the sales leg is a separate business object; the first four are
unchanged.

### 3.1 Implementation reality: procurement and sales association both exist

The frozen list above is a scope statement. What is actually implemented
(through Phase 2D.1-R2/R3 + Phase 2D.3):

| Match type | Status |
|---|---|
| `Contract ↔ Invoice` | Implemented for **`PURCHASE` invoices only** — procurement `InvoiceAllocation` (Phase 2D.1-R3b) |
| `Contract ↔ Payment` | Implemented for **`OUT` payments only** — procurement `PaymentAllocation` (Phase 2D.1-R3b) |
| `ContractItem ↔ InvoiceItem` | Implemented (manual confirmed allocation, Phase 2C) |
| `Contract ↔ Shipment/Export` | Implemented — a `Shipment` names exactly one procurement `Contract` (Phase 2D.1-R2); the Contract Business Ledger projects Shipment/export context per contract (section 2.3) |
| `SalesContract ↔ Invoice` | Implemented — a SALES Invoice associates to a `SalesContract` via `SalesInvoiceAllocation` (Phase 2D.1-R3b) |
| `SalesContract ↔ Payment` | Implemented — an IN receipt associates to a `SalesContract` via `SalesPaymentAllocation` (Phase 2D.1-R3b) |
| `Contract ↔ SalesContract` | Implemented — the procurement/sales bridge `ProcurementSalesLink` (Phase 2D.1-R3a) |

The sales-side association is a **separate object** from the procurement
Contract association: a SALES Invoice and an IN receipt associate to a
`SalesContract` — never to a procurement `Contract` — through the
sales-side allocation objects. `SalesContract` exists (Phase 2D.1-R3a),
and the sales-side matching foundation/candidates exist as implemented
by Phase 2D.1-R3b (the sales leg's only method is the explicit
human-proposal `MANUAL_SALES_SCOPE`); automatic sales matching remains
`REQUIRES BUSINESS RULE FREEZE` and is not attempted.

The identity boundary is unchanged and frozen:

- the external customer comes **only** from `SalesContract.customer`;
- `Contract.buyer` is **our own entity** and **must never be used as a
  sales-side customer key**;
- a procurement `Contract` and a sales customer are never directly
  matched — the bridge is `ProcurementSalesLink`, a relationship only.

A sales-scope reference number carried on procurement evidence identifies
the scope but is **not** a customer identity.

## 4. Period-Close Business Engine

The close engine produces at least these outputs:

- `AccrualRequired`
- `PriorAccrualReversalRequired`
- `PurchaseCostConfirmed`
- `AccrualActualDifference`
- `PaymentUnmatched`
- `InvoiceUnmatched`
- `EvidenceMissing`
- `AmountMismatch`
- `BusinessKeyConflict`

Every output above must be produced by at least one numbered rule in
[RULES.md](RULES.md). This section, and `RULES.md` itself, are unchanged
by Phase 2D.0.

BEL does not generate accounting vouchers, debit/credit entries, finance
subject codes, postings, or tax-accounting logic anywhere in this
section — that translation belongs to a future downstream Finance
Adapter/consumer ([ARCHITECTURE.md](ARCHITECTURE.md) A04), never to the
close engine.

## 5. User-Facing Work Surfaces

V1's Definition of Done requires five core work surfaces. 业务驾驶舱
(Business Cockpit) is **not** one of them — see section 8.

1. **合同业务总账 Contract Business Ledger** — the direct product
   replacement for the Excel contract ledger. A cross-contract business
   overview with filtering: procurement contract, supplier
   (`Contract.counterparty`), our own contracting entity
   (`Contract.buyer` — **not** a customer), contract amount,
   `ContractItem` scope, the linked sales scope(s) and their external
   customer (`SalesContract.customer`), export execution,
   purchase-invoice state, sales-invoice state, outgoing payment state,
   incoming receipt state, current accrual state, invoice preparation
   state where determinable, and an
   unresolved-work/exception indicator. The Ledger's primary axis is the
   **procurement contract**, matching the legacy ledger it replaces;
   linked sales scopes are projected onto that row and are never summed
   across the bridge (see section 2.5). It does not replicate the
   spreadsheet's dozens of raw columns. **Implemented** (Phase 2D.1-R4)
   as a read-only Contract Business Ledger + Excel/CSV export; the
   first-stage cutover as a whole is still pending. It sequenced after
   `ContractItem` intake (2.2), `Shipment / Export` (2.3), and sales-side
   association (3.1), which now supply its columns. **No column displays
   fabricated data for a fact BEL does not yet hold.** The Ledger drills
   down into Contract 360, never the reverse.
2. **合同360° Contract 360** — **implemented** (Phase 2C/2C.2). Single
   contract traceability, unchanged by Phase 2D.0.
3. **月结工作台 Period-Close Workbench** — **implemented as a read-only
   preview** (Phase 2C/2C.2). Its existing semantics are preserved
   unchanged: a rehearsal that executes nothing, keeping the four-layer
   distinction between **Fact** (already in the Ledger), **Current
   State** (the balance derived from persisted Facts as of now),
   **Projected Decision** (what this period's preview would do if
   executed), and **Blocker** (the Rule Engine explicitly declining to
   decide) — see [PHASE2C2-DECISIONS.md](PHASE2C2-DECISIONS.md). The
   user must be able to ask, at period end **or at any other point in
   time**: given the business facts confirmed so far, what would need to
   be accrued, what reversed, what differences exist, and what cannot be
   judged. V1 extends this surface with a deliverable **Period Close
   Business Data Product** (section 6) expressing at least contract,
   counterparty, contract item/scope, quantity, accrual amount, reversal
   amount, actual invoice amount, difference, rule/reason, Evidence/Fact
   trace, and blocker/exception. Exact fields are an implementation
   decision for Phase 2D.2. It continues to produce no accounting
   voucher, debit/credit entry, finance subject code, posting, or
   tax-accounting logic.
4. **开票与请票工作台 Invoice Preparation Workbench** — **implemented**
   (Phase 2D.3) as a read-only Workbench + Data Product: two directions —
   SalesContract → 向客户开票, procurement Contract → 向供应商要票 — one
   read-only Workbench projection of confirmed Facts with deterministic
   comparison and management attention/advisories, and an XLSX/CSV Data
   Product. BEL performs no legal invoice issuance; see section 5.1.
5. **异常与任务中心 Exception & Task Center** — **implemented** (Phase
   2D.4-F1) as a read-only global Center over the authoritative unresolved
   work that already exists — persisted `TaskException`,
   `HUMAN_CONFIRMATION_REQUIRED` `MatchCase`, and (for a requested period)
   computed Period Close blockers — with the Exception & Task Data Product
   (F2, CSV + XLSX). No generic resolve workflow; R009–R015 still
   PROPOSED; see section 5.2.

Business Fact Maintenance (section 2.1) is the operating capability
underneath these surfaces, not a sixth page — it is not inflated into a
standalone large page merely to round out a count.

### 5.1 Invoice Preparation Workbench (implemented read-only; rules frozen in PHASE2D3-RULE-FREEZE.md)

Product-scope clarification (frozen): the workbench covers **two
invoice-preparation directions** —

1. **SALES INVOICE PREPARATION** — our company → external sales
   customer (primary axis: `SalesContract`; the external customer comes
   only from `SalesContract.customer`).
2. **SUPPLIER INVOICE REQUEST** — supplier → our company ("how should
   the supplier invoice us?"; primary axis: procurement `Contract`;
   `Contract.buyer` is our own entity, never a customer).

**Implemented (Phase 2D.3), read-only.** The Workbench is a projection of
confirmed Facts plus deterministic comparison and management advisories —
an information surface, NOT a workflow approval engine:

```
Confirmed Facts
      ↓
Deterministic comparison / management control
      ↓
Advisory / attention where appropriate
      ↓
Invoice Preparation Workbench
      ↓
Invoice Preparation Data Product
      ↓
actual issuing / requesting happens outside BEL
      ↓
Sales / Purchase Invoice Facts enter BEL
      ↓
projections recompute
```

Core semantics (superseding any earlier "eligibility / readiness /
blocked" framing, which was never frozen and has been replaced by the
accountant/business clarification):

- **Fact is truth.** Confirmed Facts are the only inputs; an
  association/record is never proof that the referenced Fact exists or
  has the right business direction.
- **Comparison is control.** Comparisons are deterministic, currency-safe
  and cardinality-safe — no FX, no blind sum, no apportionment across the
  many-to-many ProcurementSalesLink bridge.
- **Deviation is reminder/review.** Amount/product-name deviations and
  cardinality signals are ADVISORIES — never `RULE_CONFLICT`.
- **Comparison unavailable is NOT invoice prohibition.**
  `NOT_COMPARABLE_MISSING_FACT` / `NOT_COMPARABLE_AMBIGUOUS_SCOPE` mean a
  management comparison could not be made, not that invoicing is blocked.

In both directions:

- Invoice / payment / receipt have **no mandatory chronology** —
  invoice-before-receipt is common; no ordering finding is emitted.
- A missing Shipment or missing ProcurementSalesLink does **not**
  automatically mean "cannot invoice".
- An ambiguous M:N scope is `NOT_COMPARABLE_AMBIGUOUS_SCOPE`, never
  "blocked".
- A genuinely missing compared Fact may prevent ONE calculation, but it
  does not create a generic workflow eligibility status.

The frozen rule IDs and their provenance live in
[PHASE2D3-RULE-FREEZE.md](PHASE2D3-RULE-FREEZE.md). `IP-S02`
(export-sales amount consistency, three-way equality) remains
`OWNER_CONFIRMED_PROVISIONAL` — exact three-way equality was
product-owner confirmed and stays subject to later real-data review;
nothing here upgrades its provenance. The tax-classification-code rule
(`IP-P08`) is **frozen / register-only** — its implementation remains
deferred, and no guessed tax code exists anywhere.

V1 does not connect to the e-tax bureau, tax-control systems, or an
external invoicing API, and does not perform the legal act of issuing an
invoice — in either direction. It produces a complete, deterministic,
traceable **Invoice Preparation Data Product** (section 6).

### 5.2 Exception & Task Center (infrastructure now, producers rule-by-rule)

This is BEL's formal mechanism for the "does not guess" principle (A05).
It is the single landing surface for unresolved work — a read-only
neutral projection, never a generic resolve workflow.

**Authoritative unresolved work already exists today** and does not wait
on any new rule freeze. The full implemented producer inventory is
recorded in [PHASE2D4-DECISIONS.md](PHASE2D4-DECISIONS.md) §1: persisted
`TaskException` types written by the contract-ledger importer,
matching, fact maintenance (ContractItem / Shipment / SalesContract),
the ProcurementSalesLink family and cutover backfill; `MatchCase` in
`HUMAN_CONFIRMATION_REQUIRED`; and computed (period-scoped, never
persisted) Period Close blockers.

The Exception Center carries these today — as a **read projection over
distinct storage objects**, never a single mutable Task (the read-only
Center surface `GET /exceptions` in Phase 2D.4-F1, plus the F2 Exception
& Task Data Product).

**Additional exception producers require per-rule business
confirmation.** R009 `InvoiceUnmatched`, R010 `PaymentUnmatched`, R011
`EvidenceMissing`, and R012 `AmountMismatch` are still `PROPOSED` in
[RULES.md](RULES.md). They must each be frozen before becoming
authoritative exception producers — tagged
`REQUIRES BUSINESS RULE FREEZE`. It is equally wrong to say Phase 2D.4
depends entirely on R009–R012, and to treat R009–R012 as already
official system rules.

Core principle: a Task/Exception is **not** an alternate source of
business truth. Marking one "Resolved" must never paper over the
underlying business problem. The intended closed loop:

```
Exception
   ↓
Human / future Agent performs an allowed action
   ↓
New Fact enters BEL
   ↓
Rules recompute
   ↓
Exception condition disappears
   ↓
Task resolves
```

"Task resolves" in that loop is the **end-state of the recompute loop,
not a Center action** — the Center never flips a status. The loop applies
to persisted sources as a principle, but only two transitions actually
flip a row to `RESOLVED` today: a `MatchCase` `HUMAN_CONFIRMATION_REQUIRED`
→ `RESOLVED` on confirmation (`confirm_match` /
`confirm_sales_invoice_match` / `confirm_sales_payment_match`), and
`SalesContractCustomerUnresolved` → `RESOLVED` on a customer SUPPLEMENT.
Every other `TaskException` has no automatic transition: it stays OPEN as
a historical record while a human changes the underlying facts (and some,
like backfill identity tasks, stay OPEN and block reconciliation by
design). Computed conditions (Period Close blockers, Phase 2D.3
advisories) have no row to flip — they simply disappear on the next
recompute. The Center surface presents all of these distinctly, per
[PHASE2D4-DECISIONS.md](PHASE2D4-DECISIONS.md).

Today's `TaskException` carries a two-state `OPEN`/`RESOLVED` status and
a JSON `detail` whose scope keys differ per producer, and `domain.ExceptionType`
already models many implemented producer types (fact supersession,
Shipment/SalesContract identity, ProcurementSalesLink, backfill, matching
capacity, business-key conflict) — but a `TaskException` is **not** all
unresolved work: a `MatchCase` awaiting human confirmation, a computed
Period Close blocker and a Phase 2D.3 management advisory are each
separate, and `TaskException` is the only one with a persisted
`OPEN`/`RESOLVED` status. Freezing the Center's identity, lifecycle and
period semantics over these distinct sources — with no generic resolve
action — is the Phase 2D.4-F0 product freeze
([PHASE2D4-DECISIONS.md](PHASE2D4-DECISIONS.md)); extending type coverage
further stays producer-by-producer and requires a business rule freeze.

## 6. Data Products

V1 must support business-data export, which is what makes Excel a Data
Product rather than the System of Record. Excel/CSV is the default
first-stage format. Current status:

| Data Product | Status |
|---|---|
| Contract Business Ledger export | **Implemented** (Phase 2D.1-R4, CSV + XLSX) |
| Period Close / Accrual business data export | **Implemented** (Phase 2D.2, CSV + XLSX) |
| Invoice Preparation Data Product | **Implemented** (Phase 2D.3, CSV + XLSX) |
| Exception & Task Data Product | **Implemented** (Phase 2D.4-F2, CSV + XLSX) |

Exact schemas and the Application-layer export boundary are
implementation decisions for the phase that builds each one. Excel
remains a Data Product — not the System of Record.

### 6.1 Export tax rebate (退税) — canonical facts yes, rebate domain no

Tax rebate is not part of the Business Core's domain. V1 **may** maintain
the canonical business facts a downstream rebate system would consume —
`Contract`, `ContractItem`, `Shipment / Export` and their Evidence trail
are in scope precisely because they are canonical business facts, and
`Shipment / Export` records export *execution* (see
[DOMAIN.md](DOMAIN.md)).

V1 **does not** add a rebate declaration/申报 status, a rebate
calculation flow, a tax-authority interface, or any RebateX vocabulary
inside the Business Core. A future rebate consumer reads BEL's canonical
facts through the Adapter boundary
([ARCHITECTURE.md](ARCHITECTURE.md) A04), which translates BEL's
vocabulary into the consumer's — never the reverse.

## 7. Legacy Backfill and Cutover

A system that only handles business occurring after go-live is not a
System of Record — it is merely a new system alongside the old one.
Replacing the legacy ledger therefore requires an explicit cutover path:

```
Legacy contract ledger / source Evidence
      ↓
Backfill
      ↓
BEL canonical Facts
      ↓
deterministic recomputation
      ↓
Cutover Reconciliation
      ↓
BEL becomes System of Record
      ↓
Excel becomes read-only / Data Product
```

**The backfill mechanism, the Cutover Baseline, and the reconciliation
harness exist today** as the Phase 2D.1-R5 *infrastructure and rehearsal*,
first verified against the contract-execution fact layer. **The final
first-stage cutover has NOT yet happened**: BEL is not yet declared
System of Record, and no private reconciliation PASS is claimed. The
**final first-stage cutover acceptance runs after Phase 2D.4**, because a
complete switch also depends on the 2D.2 accrual Data Product, the 2D.3
invoice preparation rules and Data Product, and the 2D.4 exception loop.

### 7.1 The legacy ledger is not Golden Truth

Cutover acceptance must **not** be defined as `BEL result == current
Excel value`. The legacy spreadsheet may itself contain manual errors,
stale state, incomplete information, contradictory information, and
manually maintained results with no Evidence behind them. It cannot be
promoted to Golden Truth by being the incumbent.

The correct construction:

```
Legacy Excel
  + Source Evidence
  + Business-confirmed interpretation
      ↓
Cutover Baseline
      ↓
BEL Backfill
      ↓
BEL deterministic result
      ↓
Reconciliation
```

Reconciliation must distinguish at least three outcomes:

- `MATCH` — BEL's deterministic result agrees with the baseline
- `BEL_CORRECTED_LEGACY` — they differ, and business review confirms
  BEL's result is the correct one
- `UNRESOLVED` — the difference has not been adjudicated

BEL may be declared able to replace the legacy ledger as System of
Record only when `UNRESOLVED = 0` **and** the other first-stage
acceptance gates pass. `UNRESOLVED = 0` means every discrepancy has been
adjudicated — not that BEL agrees with Excel everywhere.

### 7.2 Cutover reconciliation respects the private-data boundary

Cutover work reads private business data and therefore obeys
[PRIVATE-DATA-POLICY.md](PRIVATE-DATA-POLICY.md) without exception. The
Cutover Baseline is private expected-results material and lives under
`$BEL_PRIVATE_DATA_ROOT/<period>/expected/`, the existing home for
exactly that (no policy change is required). Public runner output may
report only a scenario ID and PASS/FAIL, e.g.

```
P2D_CUTOVER_RECONCILIATION: PASS
```

Values, counts, names, amounts, records, and mismatch details go only to
`$BEL_PRIVATE_DATA_ROOT/reports/` — never into this repository, never to
stdout.

## 8. Business Cockpit (deferred)

业务驾驶舱 (Business Cockpit) is removed from V1's core Definition of
Done and deferred until fact completeness, the Contract Ledger, Period
Close, Invoice Preparation, exception handling, and cutover are done. It
is not cancelled, and it must not be pulled forward. See
[PHASE2D0-DECISIONS.md](PHASE2D0-DECISIONS.md).

## Non-goals for V1 (explicit, do not silently re-add)

- No Agent / LLM integration
- No MCP integration
- No accounting entries or tax-rebate logic
- No ERP concepts
- No generic workflow DSL
- No event-sourcing framework
- No microservices split "for future scale"
- No RBAC / enterprise IAM
- No direct connection to the e-tax bureau, tax-control systems, or an
  external invoicing API (section 5.1)
- No tax-rebate declaration status, calculation flow, or RebateX
  vocabulary inside the Business Core (section 6.1)

The three Phase-0-era entries about no database/UI/rule-engine
implementation are removed as stale: a database, a web UI, and a rule
engine have shipped since Phase 2A/2B/2C.
