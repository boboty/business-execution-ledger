# Roadmap

当前阶段顺序以 [项目再评估](docs/PROJECT-REASSESSMENT.md) 为准：先完成
可复核的第一阶段切换验收闭环，再推进最小 Application Tool Contract。
驾驶舱是按业务使用需要安排的可选投影，不是 Agent 接入的前置条件。
下文的阶段记录保留历史上下文；功能完成不代表业务已经接受切换。

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

Phase 2D.2 is a **separate phase sequenced after this one** — it is not
implemented by Phase 2D.1.

## Phase 2D.2 — Period Close Business Data Product

Turns the existing read-only preview into a deliverable business data
product, preserving the Fact / Current State / Projected Decision /
Blocker four-layer semantic. Still no voucher, no debit/credit, no
finance subject codes, no finance-system vocabulary. Chronologically the
Contract Business Ledger export (2D.1-R4) ships first; this is the first
Data Product carrying a *rule-derived business judgment* rather than a
projection of stored facts, which is why it is sequenced ahead of
invoicing and the exception centre.

**Completed — Final Gate passed.** One Application-layer path
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

Both directions are FACT CONTROL + MANAGEMENT REMINDERS, NOT a workflow
approval engine: BEL prepares invoice data, reports deterministic
comparisons and review signals, and never performs the legal act of
invoicing. No eligibility rule is invented beyond the frozen rules in
`docs/PHASE2D3-RULE-FREEZE.md`; a Shipment or receipt/payment never by
itself means invoice eligibility; no amount/quantity is apportioned
across the many-to-many ProcurementSalesLink bridge.

**F0 (rule-neutral factual context) implemented:** a read-only
Application path (`get_invoice_preparation_context`) exposing only
already-confirmed/current Facts and associations per SalesContract and
per procurement Contract — no eligibility, readiness, remaining-quantity/
amount, or cross-bridge apportionment concept; no schema change.

**F1 (deterministic invoice-preparation controls) implemented:** the two
direction rule layers (`bel.application.sales_invoice_preparation`,
`bel.application.supplier_invoice_request`) consume ONLY confirmed Facts
from F0 and emit facts + comparison results + non-blocking advisories —
never a workflow gate, never a `RULE_CONFLICT`. Comparisons are
currency-safe (explicit comparable currency only; no FX, no default, no
inference) and cardinality-safe (multiple invoices/links/shipments are
`NOT_COMPARABLE_AMBIGUOUS_SCOPE` — no sum, no apportionment, no
arbitrary selection; an absent compared Fact is a check result only,
never a preparation blocker). The export/customs declaration
(`Shipment.declared_amount` / `declared_currency`, F1c) is the preferred
management anchor for reviewing BOTH directions (IP-X01). Sales: the
three preparation inputs report fact completeness / comparison
availability, and the IP-S02 three-way amount comparison is implemented
for the unambiguous 1:1:1 scope (F1f). Supplier: the IP-P02 expected
amount (= `Contract.gross_amount` + `Contract.currency`) and the
amount/product-name consistency checks emit deviation advisories;
cardinality review signals (IP-P03 / IP-P04), the paid-but-no-invoice
follow-up (`SUPPLIER_INVOICE_FOLLOW_UP_RECOMMENDED`, IP-P09 — recomputed
from current Facts, never persisted as a Task), and payment-as-context
(IP-P01) are exposed; the tax-classification-code rule (IP-P08) is
frozen but NOT implemented — no guessed code anywhere. Missing
genuinely-required data stays a factual finding (e.g.
`MISSING_CONTRACT_GROSS_AMOUNT`), never a workflow blocker.

**F2a (integrated Workbench) implemented:** the rule-neutral F0 page
`GET /invoice-preparation` becomes the Invoice Preparation Workbench —
one read-only Application path (`get_invoice_preparation_workbench`)
composes the F0 context with both F1 reports, and the page presents two
clearly separated surfaces (向客户开票 / 向供应商要票) with three
structurally distinct blocks per scope: 已确认事实 · 核对结果 ·
提醒/待关注. Comparison/advisory outcomes are translated to
business-facing Chinese labels in the presentation layer; no eligibility
wording is shown.

**F2b (Invoice Preparation Data Product) implemented:** the Invoice
Preparation Data Product is built from the SAME neutral Workbench
projection (`get_invoice_preparation_workbench` ->
`build_invoice_preparation_data_product` -> XLSX/CSV serializer), shared
byte-for-byte by the Web (`GET /invoice-preparation/export.xlsx` /
`export.csv`) and the CLI (`bel invoice-preparation export
--format xlsx|csv`). The neutral DTO keeps the FACT / COMPARISON /
ATTENTION layers explicit (record types SALES_PREPARATION /
SALES_ATTENTION / SUPPLIER_REQUEST / SUPPLIER_ATTENTION; attention
categories UNRESOLVED_WORK / INCOMPLETE_ASSOCIATION /
MANAGEMENT_ADVISORY), preserves canonical machine-readable codes with
concise business messages, carries the confirmed-Fact boundary (a
dangling association is never counted as a confirmed Fact), and is
strictly read-only and byte-reproducible.

**Completed — Phase Final Gate passed** (Final-Gate repairs confirmed the
frozen rule semantics, the F0 preservation boundary, and the
currency/cardinality safety of the F1 comparisons).

## Phase 2D.4 — Exception & Task Center

异常与任务中心 plus the Exception/Task export.

**Status: F0 frozen/passed · F1 implemented + Slice Pre-Gate passed ·
F2 implemented + Slice Pre-Gate passed · Phase Final Gate passed.**

**F0 (semantics & product freeze) is documentation-only**: the Center is
ONE landing surface over several authoritative unresolved-work sources —
never ONE fake domain object. Storage semantics stay separate: persisted
`TaskException`, persisted `MatchCase` in `HUMAN_CONFIRMATION_REQUIRED`,
and computed (period-scoped, never persisted) Period Close blockers.
Phase 2D.3 management advisories stay advisories and are not promoted
into Tasks. The frozen inventory, source taxonomy, global identity
`(source_type, source_id)`, lifecycle (no generic RESOLVE), period
handling and `resolution_route` policy are recorded in
`docs/PHASE2D4-DECISIONS.md`; the phase-wide invariants in
`docs/PHASE2D4-ACCEPTANCE.md`.

The Center infrastructure can immediately carry the authoritative
unresolved work that already exists — the full implemented producer set
inventoried in `docs/PHASE2D4-DECISIONS.md` §1, including the Phase
2D.1-R1..R5 producers (ContractItem / Shipment fact supersession, the
Shipment and SalesContract identity types, the ProcurementSalesLink
family, backfill identity tasks) — without waiting for R009–R012.
Additional producers — R009 `InvoiceUnmatched`, R010 `PaymentUnmatched`,
R011 `EvidenceMissing`, R012 `AmountMismatch`, all still `PROPOSED` —
each require a business rule freeze before becoming authoritative. F1
(read-only Exception & Task Center) and F2 (Exception & Task Data
Product) implement the frozen semantics.

**F1 (read-only Exception & Task Center) is implemented**: one neutral
Application projection (`get_unresolved_work_center` in
`src/bel/application/unresolved_work_center.py`) aggregates persisted
`TASK_EXCEPTION` rows (the full produced type set), persisted `MATCH_CASE`
rows in `HUMAN_CONFIRMATION_REQUIRED` (both legs, candidate scopes
preserved), and — only when a `period` is supplied — the recomputed
Period Close blockers as `COMPUTED_BLOCKER` items with a deterministic,
non-persisted `source_id`. The read-only Web surface `GET /exceptions`
(异常与任务中心) renders the three sources with distinct labels, resolves
scopes through structured fields/repository lookup (never `summary`
parsing), keeps genuinely unmappable work visible, and performs zero
business-state writes. Filters: status/open-only, source_type, code,
procurement/sales contract scope, period. No generic RESOLVE, no new
storage table, no migration, no advisory→Task promotion, and no R009–R015
implementation.

**F2 (Exception & Task Data Product) is implemented**: the ONE Application
path Web and CLI share —

    get_unresolved_work_center(session, filters)
        -> build_exception_task_data_product(center)   (pure, accepts ONLY the Center)
        -> export_exception_task_xlsx() / export_exception_task_csv()

(`src/bel/application/exception_task_data_product.py`). The XLSX has
exactly four sheets (01_Summary / 02_System_Tasks / 03_Match_Confirmation /
04_Period_Close_Blockers); the CSV is one unified long table keyed by
`record_type` (== `source_type`). Every row preserves the frozen neutral
fields plus the full repeatable scope/id set as deterministic JSON (never
first-id truncated); unmappable tasks export with blank scope fields, never
dropped. Computed blockers keep the F1 deterministic `source_id` and a
blank `created_at`; 04 is present but empty without a period. Web routes
`GET /exceptions/export.xlsx` and `GET /exceptions/export.csv` accept the
same F1 filters, and `bel exceptions export` accepts `--format/--output/
--status/--open-only/--no-open-only/--source-type/--code/
--procurement-contract-id/--sales-contract-id/--period` — CLI and Web
produce byte-identical output for the same state/filters. Byte
determinism, formula-injection neutralization and the no-generated_at
rule reuse the proven Period Close / Invoice Preparation export
techniques. No schema/migration change, nothing persisted.

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
Truth. See [docs/V1-SCOPE.md](docs/V1-SCOPE.md) section 7 and
[docs/FIRST-STAGE-CUTOVER-GATE.md](docs/FIRST-STAGE-CUTOVER-GATE.md).

**The FIRST-STAGE CUTOVER GATE harness is implemented** (`bel cutover gate
--period YYYY-MM`, application seam
`bel.application.first_stage_cutover_gate`, contract in
[docs/FIRST-STAGE-CUTOVER-GATE.md](docs/FIRST-STAGE-CUTOVER-GATE.md)):
PostgreSQL-only, schema-at-head verified, canonical reconciliation with
UNRESOLVED = 0, all first-stage work surfaces and Data Products verified
(byte-deterministic), privacy-boundary enforced, and strictly read-only.
**The actual private Gate is still pending** — a PASS has NOT been
claimed, and BEL is NOT yet declared System of Record. The Gate is a
judge, never a switch: it performs no backfill, no baseline synthesis, no
discrepancy repair, no Task auto-resolution, and no System-of-Record
declaration. A Gate PASS means BEL MAY be declared System of Record; the
declaration is a separate human/business acceptance step after the REAL
private Gate PASS.

**Gate PASS 是技术就绪判断；只有后续业务负责人的明确接受与宣告，才能使
BEL 成为 System of Record，并将旧 Excel 降为参考资料 / Data Product。**

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
