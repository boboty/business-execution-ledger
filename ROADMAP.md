# Roadmap

Business Execution Ledger (BEL) is being built as a deterministic
business execution layer for agentic systems. The roadmap is
intentionally capability-driven rather than feature-volume-driven: each
phase should make more business work safely delegable without moving
authoritative business state into prompts.

As of Phase 2D.0, V1's Definition of Done is a product outcome —
replacing the manually-maintained contract ledger spreadsheet as the
System of Record — rather than a checklist of technical phases. Reaching
it requires a cutover, not only feature completion. See
[docs/V1-SCOPE.md](docs/V1-SCOPE.md) and
[docs/PHASE2D0-DECISIONS.md](docs/PHASE2D0-DECISIONS.md).

## Done — v0.1.1 / Phase 2C.2 (Human Workbench)

- Traceable Evidence → Fact → Decision chain
- Contract, invoice, payment, allocation and accrual domain objects
- Deterministic matching with explicit ambiguous/unmatched outcomes —
  **purchase-side only** (`PURCHASE` invoices, `OUT` payments)
- Read-only period-close preview with numbered business rules, rendered
  through the 月结工作台 and 合同360° workbench pages
- Synthetic golden tests and privacy guards for a public repository

## Phase 2D.0 — V1 Product Rebaseline

**Documentation / product freeze.** Re-freezes the V1 Definition of
Done, the five core work surfaces, the code-reality baseline, and the
sequence below. No Domain, Rule Engine, schema, or application code
change.

## Phase 2D.1 — Business Fact Foundation & Contract Ledger

The foundation round. Its sub-rounds are ordered by dependency, not
convenience: the Contract Business Ledger comes last because the facts
it displays must exist first.

### 2D.1-R0 — Business Semantics Freeze

Design and rule-freeze round. At minimum:

- Fact correction / supersession semantics
- Sales-side relationship / matching semantics — **not** a reuse of the
  purchase side's counterparty/amount assumptions
- `Shipment` minimal semantics
- Cutover / backfill semantic rules

Frozen in [docs/PHASE2D1-R0-DECISIONS.md](docs/PHASE2D1-R0-DECISIONS.md),
verified against [docs/PHASE2D1-R0-ACCEPTANCE.md](docs/PHASE2D1-R0-ACCEPTANCE.md).

The party-role question is answered: 卖方 is the domestic supplier and
买方 is our own trading/export entity. `Contract` therefore represents
the **procurement leg only**, and `Contract.buyer` may **never** serve
as a sales-side customer key.

That answer required a spec change, approved as **SCR-2D1R0-001**: the
sales leg is a separate object, `SalesContract`, carrying the external
customer, bridged to the procurement contract by `ProcurementSalesLink`.
Both are now frozen in [docs/DOMAIN.md](docs/DOMAIN.md) and
[docs/V1-SCOPE.md](docs/V1-SCOPE.md).

R0 leaves **R1, R2, R3a and R3b ready to start**. R5's design is ready;
its implementation additionally needs a source-account field on
`Payment` before its business identity is sound.

### 2D.1-R1 — ContractItem Fact Maintenance

Establishes the everyday business intake path for `ContractItem`, which
has none today. Not ordinary CRUD: human-supplied/confirmed Evidence →
`ContractItem` Fact → deterministic recompute, combined with the R0
correction/supersession mechanism. This is the first-stage critical path
— see [docs/V1-SCOPE.md](docs/V1-SCOPE.md) section 2.2.

### 2D.1-R2 — Shipment Minimum Vertical Slice

Evidence → `Shipment` Fact → `Contract` association → query/read model.
Currently zero implementation exists. R2 also adds the explicit
`shipment_id` provenance link that lets a `CostRecognitionFact` name the
shipment that evidenced it — an intake change; the Rule Engine is not
touched, and a shipment never auto-derives cost recognition.

### 2D.1-R3a — Sales Scope & Procurement-Sales Bridge

Establishes the sales leg, which BEL has no representation of today:
`SalesContract` intake from sales-side Evidence; the external customer
supplied as a supplementing fact (a scope may legitimately exist before
its customer is known); `ProcurementSalesLink` as the many-to-many
bridge; an optional sales-side reference on `Shipment`; and the Tasks
for an unresolved sales scope or an unconfirmed link.

The bridge carries **no amount and no quantity**, and V1 performs no
apportionment across it.

### 2D.1-R3b — Sales-side Allocation

`SALES` invoice → `SalesContract`, and `IN` receipt → `SalesContract`,
through **their own allocation objects** — `SalesInvoiceAllocation` and
`SalesPaymentAllocation`. The procurement `InvoiceAllocation`,
`PaymentAllocation` and `MatchCandidate` keep their hard procurement
foreign keys and are not generalised; `MatchCase` is reused with a
separate `SalesMatchCandidate`.

The first version may be **manual / human-confirmed only**.
**No automatic amount matching** — the sales-side algorithm still
requires a business rule freeze, and the procurement `M001` condition
may not be reused.

### 2D.1-R4 — Contract Business Ledger

The cross-contract 合同业务总账 page, plus its Excel/CSV export. Drills
down into the existing Contract 360. No column may display fabricated
data for a fact BEL does not yet hold.

### 2D.1-R5 — Legacy Backfill + Cutover Reconciliation (infrastructure / rehearsal)

Delivers the backfill mechanism, the Cutover Baseline, and a
reconciliation harness, first verified against the contract-execution
fact layer. **This is cutover infrastructure and rehearsal — it is not
the final cutover gate**, which runs after Phase 2D.4.

## Phase 2D.1-P — PostgreSQL Runtime Baseline & Migration Discipline

**Infrastructure only — no V1 business-scope expansion.** Inserted
between Phase 2D.1 and Phase 2D.2, completed before BEL begins carrying
authoritative (non-disposable) business data.

Moves BEL's runtime persistence from SQLite to PostgreSQL: PostgreSQL is
now the production/runtime contract for Web and CLI; SQLite remains only
as an explicit test-only convenience with no active Alembic chain and no
concurrent-Web guarantee. Every persistence invariant closed in prior
Phase 2D.1 rounds (one-current/one-initial revisions, the
ProcurementSalesLink one-current relationship, correction lineage,
whole-fact supersession, CAS writer races) is preserved under
PostgreSQL's weaker default isolation via a shared advisory-lock
serialization boundary.

A pre-existing SQLite-only trigger statement in the frozen migration
chain could not be replayed on PostgreSQL at all (a hard syntax error,
not a portability nuance) — resolved as a one-time, explicitly-approved
migration rebaseline: the old chain (`migrations/versions/`) is frozen
byte-for-byte as historical reference, and a new PostgreSQL-only chain
(`migrations/postgresql_versions/`) becomes the active migration
history, with mechanically-enforced immutability (`tools/check_migration_immutability.py`,
pre-commit and CI). See
[docs/PERSISTENCE-MIGRATION-POLICY.md](docs/PERSISTENCE-MIGRATION-POLICY.md)
for the full rationale and the frozen M1-M10 migration rules.

No SQLite → PostgreSQL data migrator exists or is planned — `bel.db` was
always disposable; a fresh PostgreSQL database is rebuilt from source
Excel/Evidence.

Phase 2D.2 is **not yet started** by this phase.

## Phase 2D.2 — Period Close Business Data Product

Turns the existing read-only preview into a deliverable business data
product, preserving the Fact / Current State / Projected Decision /
Blocker four-layer semantic. Still no voucher, no debit/credit, no
finance subject codes, no finance-system vocabulary. Chronologically the
Contract Business Ledger export (2D.1-R4) ships first; this is the first
Data Product carrying a *rule-derived business judgment* rather than a
projection of stored facts, which is why it is sequenced ahead of
invoicing and the exception centre.

**Implementation-complete** (pending pre-Gate/Gate review): one
Application-layer path
(`get_period_close_workbench` -> `build_period_close_data_product` ->
XLSX/CSV serializer) shared by Web (`GET /period-close/export.xlsx`,
`.../export.csv`) and CLI (`bel period-close export`). No new close
rule, no persisted close result/snapshot, no schema change. See
[docs/PHASE2D2-DECISIONS.md](docs/PHASE2D2-DECISIONS.md) and
[docs/PHASE2D2-ACCEPTANCE.md](docs/PHASE2D2-ACCEPTANCE.md).

## Phase 2D.3 — Invoice Preparation Workbench (开票与请票工作台)

The Invoice Preparation Workbench (renamed from "Outbound Invoicing
Workbench" 对外开票工作台 by a frozen product-scope clarification) plus
the Invoice Preparation Data Product. Phase 2D.3 covers TWO
invoice-preparation directions:

1. **SALES INVOICE PREPARATION** — our company → external sales
   customer (primary axis: `SalesContract`; the external customer comes
   only from `SalesContract.customer`).
2. **SUPPLIER INVOICE REQUEST** — supplier → our company, i.e. "how
   should the supplier invoice us?" (primary axis: procurement
   `Contract`; `Contract.buyer` is our own entity, never a customer).

The product-scope clarification above is frozen; the BUSINESS RULES
inside those directions are not.

**Prerequisites:** the ContractItem pipeline (R1), Shipment/Export facts
(R2), sales-side association (R3), and an **invoicing eligibility /
preparation rule freeze** — the business must confirm what fact
combination means *not eligible*, *ready for invoice preparation*,
*already invoiced*, and *blocked/unresolved* **in each direction**. No
eligibility rule may be invented in advance. A Shipment does not by
itself mean invoice eligibility; a receipt/payment does not by itself
mean invoice eligibility; no amount/quantity is apportioned across the
many-to-many ProcurementSalesLink bridge. BEL prepares invoice data; it
never performs the legal act of invoicing. Neither direction's request /
preparation calculations are implemented until the rules freeze.

**F0 (rule-neutral factual context) implemented:** a read-only
Application path (`get_invoice_preparation_context`) plus the Web fact
context page `GET /invoice-preparation` (向客户开票 / 向供应商要票),
exposing only already-confirmed/current Facts and associations per
SalesContract and per procurement Contract — no eligibility, readiness,
remaining-quantity/amount, or cross-bridge apportionment concept, no
schema change. The Phase 2D.3 business-rule discovery itself is
external/private; eligibility/preparation rules remain unfrozen and
unimplemented, and no final export Data Product is built in this round.

**F1a (sales-direction rule foundation) implemented:** the
`SALES_INVOICE_PREPARATION` rule layer
(`bel.application.sales_invoice_preparation`) evaluates the THREE frozen
required inputs per SalesContract scope — SalesContract, at least one
CURRENT linked procurement Contract, and a Shipment/Export Fact on the
linked contract — and emits an explicit blocker / insufficient-fact
outcome when one is missing (`NO_CURRENT_PROCUREMENT_LINK`,
`NO_SHIPMENT_FACT_ON_LINKED_CONTRACT`). Under MULTIPLE current links the
any/all shipment judgment is deliberately NOT made
(`SHIPMENT_JUDGMENT_DEFERRED_MULTIPLE_LINKS`) — that rule is not frozen
and the system does not guess. `INPUTS_PRESENT` states required-input
fact completeness only: it is not readiness, not an eligibility
Decision, and no should-invoice amount/quantity, receipt-triggered
invoicing, or supplier-direction calculation exists. `customer` comes
only from `SalesContract.customer` (never judged by this rule); the
Fact -> Decision layering is preserved (pure function over the F0 fact
context, strictly read-only), and a deliberately-empty Application-layer
seam is reserved for the future 一致性校验 whose compared field set is
not frozen.

## Phase 2D.4 — Exception & Task Center

异常与任务中心 plus the Exception/Task export.

The infrastructure can immediately carry the authoritative unresolved
work that already exists (`BusinessKeyConflict`,
`AllocationCapacityExceeded`, `MatchCase` in
`HUMAN_CONFIRMATION_REQUIRED`, period-close blockers). Additional
producers — R009 `InvoiceUnmatched`, R010 `PaymentUnmatched`, R011
`EvidenceMissing`, R012 `AmountMismatch`, all still `PROPOSED` — each
require a business rule freeze before becoming authoritative.

## FIRST-STAGE CUTOVER GATE

The point at which BEL may be declared the System of Record. Requires at
minimum:

- required business fact flows operational
- first-stage work surfaces operational
- Data Products operational
- backfill complete
- private cutover reconciliation PASS
- unresolved cutover discrepancy = 0

Reconciliation is against a business-confirmed **Cutover Baseline**, not
against the raw legacy spreadsheet — the legacy ledger is not Golden
Truth. See [docs/V1-SCOPE.md](docs/V1-SCOPE.md) section 7.

**Passing this gate is what makes BEL the System of Record and demotes
the legacy Excel to a read-only reference / Data Product.**

## Post first-stage — Business Cockpit

业务驾驶舱 returns to scope only after fact completeness, the Contract
Ledger, Period Close, Invoice Preparation, exception handling, and
cutover are complete. Deferred, not cancelled, and not to be pulled
forward.

## Then — Agent Runtime

The frozen order within this stage is: Application Tool Contract first,
then the first Agent Runtime behind it, then runtime substitutability,
then MCP/ecosystem.

- Define the Application API / Tool Contract boundary an Agent Runtime
  would sit behind — this comes before any runtime is introduced
- Introduce the first Agent Runtime through that boundary
- Keep the Business Core independent of any specific model or agent
  framework
- Add automated architecture checks that fail if the Business Core
  imports an agent runtime
- Prove runtime substitutability with contract tests against more than
  one runtime

## Ecosystem

- Define stable Tool / MCP contracts for external agent runtimes
- Add adapters for downstream finance, tax, ERP and analytics consumers
  without leaking their vocabulary into the Business Core
- Publish reusable synthetic business scenarios for testing agentic
  business systems
- Improve contributor documentation and issue templates as outside
  contributors arrive

## What will not change

The core boundary is deliberate: agents may understand evidence,
propose associations, explain exceptions and operate tools;
authoritative business facts, business states and close decisions
remain governed by structured data and deterministic rules.

See `docs/ARCHITECTURE.md` for the frozen architecture principles.
