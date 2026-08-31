# Business Execution Ledger

**A deterministic business execution layer for agentic systems.**

BEL turns fragmented operational evidence into traceable business facts, deterministic business states, actionable exceptions, and downstream data products. It explores a simple boundary for the agentic era: **agents may understand evidence and operate the system; authoritative business state must remain explainable, testable, and governed by structured data and deterministic rules.**

业务执行账是一套面向 Agent 时代的业务事实与执行系统：将散落的合同、商品、发票、付款、出口等证据持续组织成可信业务事实，通过确定性规则形成业务状态、异常任务和下游数据产品。

## Why BEL exists

AI agents are increasingly capable of reading documents, calling tools and completing multi-step business work. The harder problem is not giving an agent more tools; it is deciding **what an agent is allowed to decide**.

BEL separates four responsibilities:

- **Evidence** preserves what source systems and documents actually said.
- **Facts** promote trustworthy business information with traceability back to evidence.
- **Rules** compute authoritative business state deterministically.
- **Agents** interpret, propose, explain and operate through explicit application/tool contracts.

This makes agentic business automation auditable without reducing the agent to a chatbot or turning prompts into an unofficial rule engine.

## Core architecture boundary

> **Agent operates the system. Agent is not the system.**

An Agent may read source material, extract candidate facts, call tools, propose matches, and work tasks. But business facts, business state, and period-close conclusions are maintained by structured data and deterministic rules — never by a prompt's judgment call.

The architecture enforces:

- `Decision → Fact → Evidence` traceability
- deterministic rules for authoritative business state
- explicit `Task / Exception` creation when the system is uncertain
- a runtime-agnostic Business Core that does not depend on Pi, PydanticAI, OpenAI Agents SDK, or another agent framework

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the frozen principles.

## What this system is

Based on contracts, goods, invoices, payments, exports, and other business evidence, BEL maintains trustworthy business facts, computes business state, surfaces exceptions, and produces business data that other systems can consume.

## What this system is not

- **Not a finance/accounting system.** It does not produce accounting entries, ledger postings, or subject codes.
- **Not a tax-rebate system.** It does not file or track export tax rebates.
- **Not a BI system.** It does not own dashboards or reporting for other teams.

Finance, tax-rebate, ERP and BI systems are consumers of BEL output. Future integrations happen through Adapters / MCP, translating BEL's canonical business vocabulary into each consumer's own vocabulary without leaking those external concepts into the Business Core.

## Product goal

BEL's first-stage (V1) Definition of Done is to **replace the manually-maintained contract business ledger spreadsheet as the System of Record for business facts and deterministic business state**. Business staff should stop hand-maintaining business status in Excel; from the facts it continuously receives, BEL should reconstruct contract execution state, produce period-close/accrual judgments at any point in time, give the data needed to prepare outbound invoicing, expose what it cannot determine, and export business results as Data Products.

Excel remains supported as an import format, an export format, a cutover/backfill source, a downstream handoff format, and a human-readable data product. **It is demoted from System of Record only after the first-stage cutover gate passes.** Feature completion alone is not enough: legacy data and source Evidence must be backfilled, reconciled against a business-confirmed Cutover Baseline, and every cutover discrepancy must be adjudicated.

See [docs/V1-SCOPE.md](docs/V1-SCOPE.md) for the frozen V1 boundary and [ROADMAP.md](ROADMAP.md) for the capability sequence.

## Current capabilities — through Phase 2D.2

Phase 2D.1, **Business Fact Foundation & Contract Ledger**, is implementation-complete. The current system includes:

- traceable `Evidence → Fact → Decision` chains and deterministic purchase-side matching
- procurement-side `Contract` facts with stable identity and revision history
- everyday `ContractItem` fact maintenance with supplement/correction semantics
- `Shipment` / export execution facts associated with procurement contracts
- a separate sales-side `SalesContract` scope carrying the external customer
- many-to-many `ProcurementSalesLink` relationships between procurement and sales scopes, with no invented amount/quantity apportionment
- purchase-side `InvoiceAllocation` / `PaymentAllocation` and human-confirmed sales-side `SalesInvoiceAllocation` / `SalesPaymentAllocation`
- `ContractItem ↔ InvoiceItem` confirmed allocation
- accrual, reversal, historical close facts, and current whole-fact supersession semantics where required for cutover
- a read-only period-close preview that recomputes numbered rules from current facts
- three human-facing work surfaces:
  - **合同业务总账 / Contract Business Ledger** — cross-contract current business-fact projection with filters, CSV export and XLSX export
  - **合同360° / Contract 360** — drill-down view for one procurement contract and its current related facts
  - **月结工作台 / Period Close Workbench** — read-only projected close judgment and blockers, with an exportable **Period Close Business Data Product** (XLSX: six logical sheets; CSV: one unified long table) via both the Web workbench and `bel period-close export`
- legacy backfill infrastructure with business-identity-aware replay handling
- a closed, human-confirmed Cutover Fact path for the explicitly allowed fact types
- private Cutover Baseline reconciliation with `MATCH / BEL_CORRECTED_LEGACY / UNRESOLVED` outcomes
- persistent Tasks for incomplete, ambiguous or conflicting backfill identities
- synthetic golden tests, migration tests, privacy scanning, and private-data acceptance boundaries suitable for a public repository

The Phase 2D.1 cutover work is **infrastructure and rehearsal only**. BEL has **not** yet passed the first-stage cutover gate and must not yet be declared the System of Record.

**Phase 2D.1-P — PostgreSQL Runtime Baseline & Migration Discipline** followed as an infrastructure-only phase: PostgreSQL 18 is now the production/runtime persistence contract (SQLite remains a test-only convenience), with mechanically-enforced migration immutability. No V1 business scope changed. See [docs/PERSISTENCE-MIGRATION-POLICY.md](docs/PERSISTENCE-MIGRATION-POLICY.md).

PostgreSQL is BEL's runtime persistence contract, and runtime data is always rebuildable from authoritative source Evidence and approved Fact Packs via the documented rebuild path (`docs/PERSISTENCE-MIGRATION-POLICY.md`) — local database files are never a source of truth. Private acceptance evidence and results remain outside the repository, under `$BEL_PRIVATE_DATA_ROOT/reports/`. **This does not replace the later business-confirmed first-stage cutover gate.**

**Phase 2D.2 — Period Close Business Data Product** is implementation-complete (pending pre-Gate/Gate review): the existing read-only period-close preview/workbench is now also exportable as a deliverable, traceable, reproducible Data Product (XLSX and CSV), through the same Application-layer path for both Web and CLI. It introduces no new close rule, no persisted close result, and no finance-system vocabulary — see [docs/PHASE2D2-DECISIONS.md](docs/PHASE2D2-DECISIONS.md) and [docs/PHASE2D2-ACCEPTANCE.md](docs/PHASE2D2-ACCEPTANCE.md).

## Not built yet

The remaining V1 critical path is intentionally narrow:

- **Phase 2D.3 — Outbound Invoicing Workbench**: freeze invoicing eligibility semantics, provide invoice-preparation data, and export the preparation data product; BEL prepares data but does not perform legal invoicing
- **Phase 2D.4 — Exception & Task Center**: one human-facing center and data product for authoritative unresolved work already produced by BEL
- **FIRST-STAGE CUTOVER GATE**: complete backfill, private reconciliation, unresolved cutover discrepancy = 0, and explicit System-of-Record switch

Also deliberately not built yet:

- Business Cockpit
- Agent Runtime
- MCP / external agent tool ecosystem
- downstream finance, tax, ERP or BI adapters
- automatic sales-side amount matching
- procurement/sales bridge apportionment

## Next up

**Phase 2D.3 — Outbound Invoicing Workbench.**

Freezes invoicing eligibility semantics against the business, then provides invoice-preparation data and its export; BEL prepares data but never performs the legal act of invoicing.

After 2D.3, V1 proceeds to the Exception & Task Center and then the first-stage cutover gate. See [ROADMAP.md](ROADMAP.md).

## Getting started

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e ".[dev]"
git config core.hooksPath .githooks

# PostgreSQL 18 is BEL's V1 runtime target. For a source checkout,
# repo-root .env is a development convenience automatically loaded by
# both `bel` and `alembic`; packaged/production deployments must inject
# BEL_DATABASE_URL explicitly from their deployment environment.
createdb bel
cp .env.example .env
# Edit .env and set, for example:
# BEL_DATABASE_URL=postgresql+psycopg://USER:PASSWORD@HOST:5432/bel
.venv/bin/alembic upgrade head

# Import deterministic source evidence
.venv/bin/bel import-contract-ledger <path-to-合同台账.xlsx>
.venv/bin/bel import-invoices <path-to-发票.xlsx> --direction purchase
.venv/bin/bel import-bank <path-to-对账单.pdf> --profile cmb --source-account-id <stable-bank-account-id>
.venv/bin/bel match run

# Import an approved Close Fact Pack when the period needs facts that
# are not deterministically derivable from source evidence alone.
.venv/bin/bel import-close-facts <path-to-close-facts.json>
.venv/bin/bel period-close preview <YYYY-MM>

# Human workbench
.venv/bin/bel web
# http://127.0.0.1:8000/contract-ledger
# http://127.0.0.1:8000/period-close

# Verification
.venv/bin/pytest
```

Real business data must never be committed. Public tests run against independently constructed synthetic data under `fixtures/synthetic/`.

SQLite remains available as an explicit test-only convenience (`sqlite:///path` or in-memory `sqlite://`) — it has no active Alembic chain and no concurrent-Web guarantee, so it is never the runtime for `bel web` or a shared development database. The historical local runtime file `bel.db` is retired and is not a supported runtime or migration source. PostgreSQL development databases are rebuilt from source Excel / PDF Evidence and approved Fact Packs rather than copied from the legacy SQLite database. See [docs/PERSISTENCE-MIGRATION-POLICY.md](docs/PERSISTENCE-MIGRATION-POLICY.md) for the full runtime contract and migration immutability rules.

Cutover/backfill acceptance uses a private data root outside the repository. Expected Cutover Baseline material is reconciliation input only; it is never a source of canonical Facts. A Cutover Baseline must be independently business-confirmed — it must not be generated from current BEL state merely to make reconciliation pass. If no such baseline exists yet, cutover reconciliation is simply **not run**; that is not a business-result mismatch.

## Documentation

- [Architecture](docs/ARCHITECTURE.md) — frozen architecture principles
- [V1 Scope](docs/V1-SCOPE.md) — V1 product boundary and Definition of Done
- [Domain](docs/DOMAIN.md) — canonical business objects and semantics
- [Rules](docs/RULES.md) — numbered deterministic business rules
- [Roadmap](ROADMAP.md) — capability sequence through first-stage cutover and beyond
- [Golden Tests](docs/GOLDEN-TEST.md) — verification methodology
- [Private Data Policy](docs/PRIVATE-DATA-POLICY.md) — sensitive-data handling boundary
- [Phase 2D.0 Decisions](docs/PHASE2D0-DECISIONS.md) / [Acceptance](docs/PHASE2D0-ACCEPTANCE.md) — V1 product rebaseline
- [Phase 2D.1 R0 Decisions](docs/PHASE2D1-R0-DECISIONS.md) / [Acceptance](docs/PHASE2D1-R0-ACCEPTANCE.md) — frozen sales, Shipment, correction and cutover semantics
- [Persistence & Migration Policy](docs/PERSISTENCE-MIGRATION-POLICY.md) — PostgreSQL runtime contract, migration immutability rules (Phase 2D.1-P)
- [Phase 2D.2 Decisions](docs/PHASE2D2-DECISIONS.md) / [Acceptance](docs/PHASE2D2-ACCEPTANCE.md) — Period Close Business Data Product
- [Contributing](CONTRIBUTING.md) — contribution rules and development setup

Implementation decisions and acceptance criteria for each phase are kept in `docs/PHASE*-DECISIONS.md` and `docs/PHASE*-ACCEPTANCE.md` so design changes are explicit rather than silently retrofitted to code.

## Open source

BEL is licensed under the [Apache License 2.0](LICENSE). Contributions, architecture discussions, synthetic business scenarios and adapter ideas are welcome, provided they preserve the deterministic Business Core and public-data boundary.
