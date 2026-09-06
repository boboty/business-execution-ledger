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

BEL has completed its first-stage cutover and is now the **System of Record for first-stage contract-execution facts and deterministic business state**. From traceable Evidence and confirmed Facts, it reconstructs contract execution state, computes period-close judgments, prepares invoicing data, exposes unresolved work, and exports governed Data Products.

Excel is no longer the System of Record. It remains supported as import and backfill material, an export and downstream handoff format, a human-readable Data Product, and historical material.

See the [First-stage System of Record Declaration](docs/FIRST-STAGE-SOR-DECLARATION.md) for the frozen milestone and the [Roadmap](ROADMAP.md) for the capability sequence.

## Current capabilities — v0.3.0

The first-stage capability sequence is complete:

- **Phase 2D.1 — Business Fact Foundation & Contract Ledger:** traceable contract-execution Facts, revision history, procurement and sales scopes, deterministic procurement matching, confirmed allocations, legacy backfill, and cutover reconciliation.
- **Phase 2D.1-P — PostgreSQL Runtime Baseline & Migration Discipline:** PostgreSQL 18 is the production/runtime persistence contract; SQLite remains a test-only convenience.
- **Phase 2D.2 — Period Close Business Data Product:** deterministic close projection and reproducible XLSX/CSV delivery.
- **Phase 2D.3 — Invoice Preparation Workbench & Data Product:** separate sales invoice preparation and supplier invoice request views, with factual comparisons and management attention signals; BEL does not perform legal invoicing.
- **Phase 2D.4 — Exception & Task Center & Data Product:** one read-only center over authoritative unresolved-work sources without collapsing their distinct semantics.
- **Real First-stage Cutover Gate and SoR declaration:** source-driven cutover, independent reconciliation, final Gate PASS, and explicit business declaration completed for `v0.3.0`.

The five first-stage work surfaces are:

- **Contract Business Ledger**
- **Contract 360**
- **Period Close**
- **Invoice Preparation**
- **Exception & Task Center**

The corresponding Data Products are:

- **Contract Ledger**
- **Period Close**
- **Invoice Preparation**
- **Exception & Task**

PostgreSQL runtime state remains rebuildable from authoritative source Evidence and approved Fact Packs. Private acceptance inputs and diagnostics remain outside the repository under `$BEL_PRIVATE_DATA_ROOT`; the public repository contains no private cutover values.

## Next up

**Minimal Application Tool Contract.**

The next capability is a small, stable Application boundary through which an Agent Runtime can read and operate BEL without direct database access or prompt-owned business rules. It will make read, proposal, validation, write, retry, uncertainty, and human-confirmation semantics explicit while remaining independent of any particular model or runtime.

The **Agent Runtime is not implemented** and comes only after this contract is defined and tested. The **Business Cockpit** remains an optional later business-facing projection; it is not a prerequisite for Agent access.

Other deliberately deferred capabilities include MCP/external-agent ecosystem contracts, downstream finance/tax/ERP/BI adapters, automatic sales-side amount matching, and procurement/sales bridge apportionment.

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
# http://127.0.0.1:8000/invoice-preparation
# http://127.0.0.1:8000/exceptions

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
- [First-stage SoR Declaration](docs/FIRST-STAGE-SOR-DECLARATION.md) — frozen `v0.3.0` System-of-Record milestone
- [First-stage Cutover Gate](docs/FIRST-STAGE-CUTOVER-GATE.md) — final technical cutover contract
- [Golden Tests](docs/GOLDEN-TEST.md) — verification methodology
- [Private Data Policy](docs/PRIVATE-DATA-POLICY.md) — sensitive-data handling boundary
- [Phase 2D.0 Decisions](docs/PHASE2D0-DECISIONS.md) / [Acceptance](docs/PHASE2D0-ACCEPTANCE.md) — V1 product rebaseline
- [Phase 2D.1 R0 Decisions](docs/PHASE2D1-R0-DECISIONS.md) / [Acceptance](docs/PHASE2D1-R0-ACCEPTANCE.md) — frozen sales, Shipment, correction and cutover semantics
- [Persistence & Migration Policy](docs/PERSISTENCE-MIGRATION-POLICY.md) — PostgreSQL runtime contract, migration immutability rules (Phase 2D.1-P)
- [Phase 2D.2 Decisions](docs/PHASE2D2-DECISIONS.md) / [Acceptance](docs/PHASE2D2-ACCEPTANCE.md) — Period Close Business Data Product
- [Phase 2D.3 Rule Freeze](docs/PHASE2D3-RULE-FREEZE.md) / [Acceptance](docs/PHASE2D3-ACCEPTANCE.md) — Invoice Preparation
- [Phase 2D.4 Decisions](docs/PHASE2D4-DECISIONS.md) / [Acceptance](docs/PHASE2D4-ACCEPTANCE.md) — Exception & Task Center
- [Contributing](CONTRIBUTING.md) — contribution rules and development setup

Implementation decisions and acceptance criteria for each phase are kept in `docs/PHASE*-DECISIONS.md` and `docs/PHASE*-ACCEPTANCE.md` so design changes are explicit rather than silently retrofitted to code.

## Open source

BEL is licensed under the [Apache License 2.0](LICENSE). Contributions, architecture discussions, synthetic business scenarios and adapter ideas are welcome, provided they preserve the deterministic Business Core and public-data boundary.
