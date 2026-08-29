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
- Sales-side relationship / matching semantics (a customer/buyer to
  `Contract.buyer` association — **not** a reuse of the purchase side's
  counterparty/amount assumptions)
- `Shipment / Export` minimal semantics
- Cutover / backfill semantic rules

### 2D.1-R1 — ContractItem Fact Maintenance

Establishes the everyday business intake path for `ContractItem`, which
has none today. Not ordinary CRUD: human-supplied/confirmed Evidence →
`ContractItem` Fact → deterministic recompute, combined with the R0
correction/supersession mechanism. This is the first-stage critical path
— see [docs/V1-SCOPE.md](docs/V1-SCOPE.md) section 2.2.

### 2D.1-R2 — Shipment / Export Minimum Vertical Slice

Evidence → `Shipment / Export` Fact → `Contract` association →
query/read model. Currently zero implementation exists.

### 2D.1-R3 — Sales-side Association Foundation

`SALES` invoice → business `Contract` association, and `IN`
payment/receipt → business `Contract` association. Implemented against
the semantics frozen in R0.

### 2D.1-R4 — Contract Business Ledger

The cross-contract 合同业务总账 page, plus its Excel/CSV export. Drills
down into the existing Contract 360. No column may display fabricated
data for a fact BEL does not yet hold.

### 2D.1-R5 — Legacy Backfill + Cutover Reconciliation (infrastructure / rehearsal)

Delivers the backfill mechanism, the Cutover Baseline, and a
reconciliation harness, first verified against the contract-execution
fact layer. **This is cutover infrastructure and rehearsal — it is not
the final cutover gate**, which runs after Phase 2D.4.

## Phase 2D.2 — Period Close Business Data Product

Turns the existing read-only preview into a deliverable business data
product, preserving the Fact / Current State / Projected Decision /
Blocker four-layer semantic. Still no voucher, no debit/credit, no
finance subject codes, no finance-system vocabulary. Chronologically the
Contract Business Ledger export (2D.1-R4) ships first; this is the first
Data Product carrying a *rule-derived business judgment* rather than a
projection of stored facts, which is why it is sequenced ahead of
invoicing and the exception centre.

## Phase 2D.3 — Outbound Invoicing Workbench

对外开票工作台 plus the Outbound Invoice Preparation export.

**Prerequisites:** the ContractItem pipeline (R1), Shipment/Export facts
(R2), sales-side association (R3), and an **invoicing eligibility rule
freeze** — the business must confirm what fact combination means *not
eligible*, *ready for invoice preparation*, *already invoiced*, and
*blocked/unresolved*. No eligibility rule may be invented in advance.
BEL prepares invoice data; it never performs the legal act of invoicing.

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
Ledger, Period Close, Outbound Invoicing, exception handling, and
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
