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
| 1 | Reconstruct a contract's current execution state | **Partially implemented** — 合同360° covers a single contract; there is no cross-contract ledger, no export execution, and no sales-side execution state |
| 2 | Produce a period-close / accrual business judgment at any point in time | **Capability implemented** (Phase 2C.2 read-only preview) — first-stage coverage is not yet cutover-complete (see below) |
| 3 | Generate a deliverable accrual business data product | **Not implemented** — no export path exists |
| 4 | Give the business data needed to prepare invoicing in both directions — issuing sales invoices to external customers (向客户开票) and requesting supplier invoices (向供应商要票) | **Not implemented** |
| 5 | Expose what cannot be determined, lacks evidence, or needs a human | **Partially implemented** — deterministic exceptions and period-close blockers exist; there is no unified center, and some producer rules are not frozen |
| 6 | Export business results as an Excel / CSV Data Product | **Not implemented** |

**On capability 2 specifically:** the technical ability to answer "given
the facts confirmed so far, what would this period accrue, reverse,
differ by, and fail to judge" is implemented and shipped. That is a
statement about the capability, **not** a claim that the first-stage
Definition of Done is met: the *business coverage* of that answer is
still bounded by `ContractItem` completeness (section 2.2),
`Shipment / Export` (section 2.3), sales-side association (section 3.1),
and legacy backfill (section 7). Capability implemented; first-stage
coverage not yet cutover-complete.

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

### 2.3 `Shipment / Export` is a first-stage implementation gap

[DOMAIN.md](DOMAIN.md) freezes `Shipment / Export`'s meaning and section
3 lists `Contract ↔ Export` as a V1 match type, but **no implementation
exists**: there is no `Shipment`/`Export` domain object, no persistence
model, no adapter, and no matching pipeline. This must not be described
as "largely present, only needs a page".

It must enter Phase 2D.1 because the legacy contract ledger is organized
around export business. If BEL cannot canonically express *whether the
goods a contract covers actually shipped/exported*, it cannot replace
that ledger. A concrete anchor already exists in code: the contract
ledger carries an export-contract-number column that the importer
currently reads only to count completeness — it is never promoted to a
Fact or to any association.

Phase 2D.1 must deliver a minimal `Shipment / Export` implementation:
domain object, Evidence trace, intake path, `Contract` association, and
a Contract Business Ledger projection.

**Not frozen by this document:** `Export completed → automatically
invoice eligible`. Export execution is one *candidate* fact for future
invoicing-eligibility rules (in either direction); it does not by itself
decide invoicing eligibility. That rule is frozen separately before
Phase 2D.3 (section 5.1).

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
unknown at first — a sales scope is often first learned from a reference
carried on procurement evidence, before the sales contract document
itself has been ingested — and is supplied later from sales-side
Evidence as a normal supplementing fact. A sales-scope reference number
is **not** a customer identity, and neither is a customs-receiving party
named on shipping material. Until a customer is known, that scope cannot
support sales-invoice preparation; it produces a `Task`, never a guess.

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
never creates one automatically; a shipment implying an unestablished
link produces a `Task`. A shipment is, however, an important candidate
fact source for the quantity a future invoice preparation (in either
direction) would consider — what that rule actually is remains Phase
2D.3's to freeze (section 5.1), and nothing here establishes shipped
quantity as invoiceable quantity.

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

### 3.1 Implementation reality: the matching pipeline is purchase-side only

The frozen list above is a scope statement. What the pipeline actually
processes today is narrower:

| Match type | Status |
|---|---|
| `Contract ↔ Invoice` | Implemented for **`PURCHASE` invoices only** |
| `Contract ↔ Payment` | Implemented for **`OUT` payments only** |
| `ContractItem ↔ InvoiceItem` | Implemented (manual confirmed allocation, Phase 2C) |
| `Contract ↔ Export` | **Not implemented** (see section 2.3) |

`InvoiceDirection.SALES` and `PaymentDirection.IN` exist in the Domain
and sales invoices and incoming receipts can be imported — but the
matching pipeline never associates them with a `Contract`. So the
accurate statement of the gap is: **sales-side raw facts can exist and
be imported; the sales-side Contract association / matching pipeline is
not connected.**

This directly limits:

- the Contract Business Ledger's sales-invoice state
- the Contract Business Ledger's incoming-receipt state
- the Invoice Preparation Workbench's "does a corresponding Sales
  Invoice Fact already exist" judgment
- sales-side business execution state generally

**This is not merely a direction parameter.** Closing it required
freezing the sales-side relationship semantics first, which Phase
2D.1-R0 has now done (SCR-2D1R0-001).

The purchase side associates a supplier/seller to a contract's
`counterparty`, and that is unchanged. The sales side does **not**
associate a customer to a contract's `buyer`: business confirmation
established that the ledger's party columns describe the **procurement**
leg, where the seller is the external supplier and the buyer is **our
own trading/export entity**. `Contract.buyer` is therefore our own
entity and **must never be used as a sales-side customer key** — doing
so would attribute every sales invoice and customer receipt to our own
company.

An earlier version of this section stated the opposite. That statement
was wrong and is corrected here; see
[PHASE2D1-R0-DECISIONS.md](PHASE2D1-R0-DECISIONS.md).

The sales-side customer instead comes from an independent fact source —
`SalesContract`, built from sales-side Evidence (see section 2.5). A
sales-scope reference number carried on procurement evidence identifies
the scope but is **not** a customer identity. Reusing the purchase
side's counterparty/amount assumptions remains explicitly forbidden; the
sales-side matching *algorithm* is still tagged
`REQUIRES BUSINESS RULE FREEZE` and is implemented in Phase 2D.1-R3b.

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
   spreadsheet's dozens of raw columns. **Not implemented.** It is
   sequenced *after* `ContractItem` intake (2.2), `Shipment / Export`
   (2.3), and sales-side association (3.1), because those supply columns
   it would otherwise have to fake. **No column may display fabricated
   data for a fact BEL does not yet hold.** The Ledger drills down into
   Contract 360, never the reverse.
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
4. **开票与请票工作台 Invoice Preparation Workbench** — **not
   implemented** (Phase 2D.3-F0 establishes only the rule-neutral
   factual context — a read-only fact page, no preparation surface
   yet); see section 5.1.
5. **异常与任务中心 Exception & Task Center** — **not implemented** as a
   surface, though real unresolved work already exists; see section 5.2.

Business Fact Maintenance (section 2.1) is the operating capability
underneath these surfaces, not a sixth page — it is not inflated into a
standalone large page merely to round out a count.

### 5.1 Invoice Preparation Workbench (product shape; rules not frozen)

Product-scope clarification (frozen): the workbench covers **two
invoice-preparation directions** —

1. **SALES INVOICE PREPARATION** — our company → external sales
   customer (primary axis: `SalesContract`; the external customer comes
   only from `SalesContract.customer`).
2. **SUPPLIER INVOICE REQUEST** — supplier → our company ("how should
   the supplier invoice us?"; primary axis: procurement `Contract`;
   `Contract.buyer` is our own entity, never a customer).

The direction scope above is frozen; the business rules inside those
directions are not.

Product shape:

```
Business Facts
      ↓
Deterministic eligibility / preparation (per direction)
      ↓
Invoice Preparation Data
      ↓
actual invoicing / requesting happens (outside BEL)
      ↓
Sales Invoice / Purchase Invoice Facts enter BEL
      ↓
business state recomputed
```

From confirmed facts already in BEL, it must surface: which business has
met invoicing conditions in the relevant direction; who to bill (sales
direction) or which supplier to request an invoice from (supplier
direction); which contract/goods/business it corresponds to; quantity
and amount; what invoicing information must be prepared; which confirmed
facts support that judgment; whether a corresponding Sales Invoice Fact
(sales direction) or Purchase Invoice Fact (supplier direction) already
exists; which business does not yet qualify, and why it cannot be
judged.

V1 does not connect to the e-tax bureau, tax-control systems, or an
external invoicing API, and does not perform the legal act of issuing an
invoice — in either direction. It produces a complete, deterministic,
traceable **Invoice Preparation Data Product** (section 6).

**Prerequisites, all of which must land first:** the `ContractItem`
pipeline (2.2), `Shipment / Export` facts (2.3), sales-side association
(3.1), and an invoicing eligibility / preparation rule freeze.

**No invoicing eligibility or preparation rule (in either direction) is
frozen by this document, and none may be invented.** Before Phase 2D.3
the business must confirm what combination of facts means: *not
eligible*, *ready for invoice preparation*, *already invoiced*, and
*blocked / unresolved* — per direction. A Sales Invoice Fact existing is
**not** the same thing as an invoicing eligibility Decision; a
Shipment/Export fact does not by itself mean invoice eligibility; a
receipt/payment does not by itself mean invoice eligibility; and no
amount or quantity is apportioned across the many-to-many
ProcurementSalesLink bridge in service of any such rule. Supplier
request calculations and sales invoice calculations are **not
implemented** until those rules are frozen. Tagged `REQUIRES BUSINESS
RULE FREEZE`; no numbered rule for it exists in [RULES.md](RULES.md).

### 5.2 Exception & Task Center (infrastructure now, producers rule-by-rule)

This is BEL's formal mechanism for the "does not guess" principle (A05).
It must eventually be the single landing surface for unresolved work.

**Authoritative unresolved work already exists today** and does not wait
on any new rule freeze:

- `BusinessKeyConflict` (written by the contract-ledger importer)
- `AllocationCapacityExceeded` (written by matching)
- `MatchCase` in `HUMAN_CONFIRMATION_REQUIRED`
- Period Close blockers

The Exception Center's infrastructure can carry these immediately.

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

Today's `TaskException` models two exception types and a two-state
`OPEN`/`RESOLVED` status, with no modeled link from resolution back to
recompute. Extending the type coverage and freezing the lifecycle is a
`DEFERRED IMPLEMENTATION DECISION` for Phase 2D.4.

## 6. Data Products

V1 must support business-data export, which is what makes Excel a Data
Product rather than the System of Record. Excel/CSV is the default
first-stage format. **None of these exports exists today** — there is no
export path anywhere in the codebase. Each is assigned to a phase:

| Data Product | Phase |
|---|---|
| Contract Business Ledger export | 2D.1-R4 |
| Period Close / Accrual business data export | 2D.2 |
| Invoice Preparation Data Product | 2D.3 |
| Exception / Task export | 2D.4 |

Exact schemas and the Application-layer export boundary are
implementation decisions for the phase that builds each one.

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

**No backfill, cutover, or reconciliation capability exists today.**
Phase 2D.0 does not implement one. Phase 2D.1-R5 delivers the
*infrastructure and rehearsal*: the backfill mechanism, the Cutover
Baseline, and a reconciliation harness, first verified against the
contract-execution fact layer. The **final first-stage cutover
acceptance runs after Phase 2D.4**, because a complete switch also
depends on the 2D.2 accrual Data Product, the 2D.3 invoice preparation
rules and Data Product, and the 2D.4 exception loop.

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
