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

## Current capabilities

Phase 2B/2C currently includes:

- contract-ledger evidence import and canonical contract facts
- purchase-invoice and bank-statement adapters
- deterministic payment/invoice matching with explicit matched / ambiguous / unmatched outcomes
- confirmed ContractItem ↔ InvoiceItem allocation
- accrual, reversal and historical close facts
- a read-only period-close preview that recomputes confirmed numbered rules from current facts
- synthetic golden tests, public CI and privacy scanning
- the first two human-facing V1 workbench pages — 月结工作台 (period-close)
  and 合同360° (Contract 360) — served by `bel --db /path/to/bel.db web`
  at `http://127.0.0.1:8000`

The close preview is intentionally stateless and read-only: it writes no vouchers, accounting entries or events.

Phase 2C's pages render through the same Application Services as the CLI;
the only human write is the manual InvoiceItem allocation, which reuses
the exact CLI command's `allocate_invoice_item`. See
[docs/PHASE2C-DECISIONS.md](docs/PHASE2C-DECISIONS.md) and
[docs/PHASE2C-ACCEPTANCE.md](docs/PHASE2C-ACCEPTANCE.md).

## Getting started

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e ".[dev]"
git config core.hooksPath .githooks
.venv/bin/alembic upgrade head
.venv/bin/bel import-contract-ledger <path-to-合同台账.xlsx>
.venv/bin/bel import-invoices <path-to-发票.xlsx> --direction purchase
.venv/bin/bel import-bank <path-to-对账单.pdf> --profile cmb
.venv/bin/bel match run
.venv/bin/bel --db bel.db web        # Phase 2C workbench at http://127.0.0.1:8000
.venv/bin/pytest
```

The SQLite runtime database (`bel.db`) is a local development artifact and must stay outside the repository tree. Real business data must never be committed. Public tests run against independently constructed synthetic data under `fixtures/synthetic/`.

## Documentation

- [Architecture](docs/ARCHITECTURE.md) — frozen architecture principles
- [V1 Scope](docs/V1-SCOPE.md) — what V1 does and explicitly does not cover
- [Domain](docs/DOMAIN.md) — core business objects and semantics
- [Rules](docs/RULES.md) — numbered business rules
- [Golden Tests](docs/GOLDEN-TEST.md) — verification methodology
- [Private Data Policy](docs/PRIVATE-DATA-POLICY.md) — sensitive-data handling boundary
- [Phase 2C Decisions](docs/PHASE2C-DECISIONS.md) / [Acceptance](docs/PHASE2C-ACCEPTANCE.md)
- [Roadmap](ROADMAP.md) — planned capability progression
- [Contributing](CONTRIBUTING.md) — contribution rules and development setup

Implementation decisions and acceptance criteria for each phase are kept in `docs/PHASE*-DECISIONS.md` and `docs/PHASE*-ACCEPTANCE.md` so design changes are explicit rather than silently retrofitted to code.

## Open source

BEL is licensed under the [Apache License 2.0](LICENSE). Contributions, architecture discussions, synthetic business scenarios and adapter ideas are welcome, provided they preserve the deterministic Business Core and public-data boundary.
