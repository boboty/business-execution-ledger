# Business Execution Ledger

A business fact and execution system that turns fragmented operational
evidence into traceable business facts, deterministic business states,
actionable exceptions, and downstream data products.

业务执行账是一套业务事实与执行系统：将散落的合同、商品、发票、付款、出口等证据持续组织成可信业务事实，通过确定性规则形成业务状态、异常任务和下游数据产品。

## What this system is

Based on contracts, goods, invoices, payments, exports, and other business
evidence, this system maintains trustworthy business facts, computes
business state, surfaces exceptions, and produces the business data that
other systems need.

## What this system is not

- **Not a finance/accounting system.** It does not produce accounting
  entries, ledger postings, or subject codes.
- **Not a tax-rebate system.** It does not file or track export tax
  rebates.
- **Not a BI system.** It does not own dashboards or reporting for other
  teams.

Finance, tax-rebate, and BI systems are **consumers** of this system's
output. V1 does not hard-wire to any of them; future consumption happens
through Adapters / MCP, translating this system's canonical business
vocabulary into each consumer's own vocabulary (accounting entries, rebate
filings, report schemas, etc.).

## Agent operates the system. Agent is not the system.

An Agent may read source material, extract candidate facts, call tools,
propose matches, and work tasks. But business facts, business state, and
period-close conclusions are always maintained by structured data and
deterministic rules — never by a prompt's judgment call. See
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the enforced boundary
between Agent Runtime and Business Core.

## Documentation

- [docs/V1-SCOPE.md](docs/V1-SCOPE.md) — what V1 does and explicitly does not cover
- [docs/DOMAIN.md](docs/DOMAIN.md) — core business objects and their semantics
- [docs/RULES.md](docs/RULES.md) — numbered business rules
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — frozen architecture principles
- [docs/GOLDEN-TEST.md](docs/GOLDEN-TEST.md) — golden test methodology
- [docs/PHASE1-DECISIONS.md](docs/PHASE1-DECISIONS.md) — Phase 1 implementation judgment calls
- [docs/PHASE2A-DECISIONS.md](docs/PHASE2A-DECISIONS.md) — Phase 2A implementation judgment calls
- [docs/PHASE2A-ACCEPTANCE.md](docs/PHASE2A-ACCEPTANCE.md) — Phase 2A acceptance criteria
- [docs/PRIVATE-DATA-POLICY.md](docs/PRIVATE-DATA-POLICY.md) — sensitive-data handling policy (read this first)

## Getting started

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e ".[dev]"
git config core.hooksPath .githooks   # one-time: enables the privacy pre-commit/commit-msg hooks
.venv/bin/alembic upgrade head
.venv/bin/bel import-contract-ledger <path-to-合同台账.xlsx>
.venv/bin/bel import-invoices <path-to-发票.xlsx> --direction purchase
.venv/bin/bel import-bank <path-to-对账单.pdf> --profile cmb
.venv/bin/bel match run
.venv/bin/pytest
```

Sensitive business data is never committed to this repository — see
[docs/PRIVATE-DATA-POLICY.md](docs/PRIVATE-DATA-POLICY.md). The committed
test suite (`pytest`) runs entirely against independently constructed
synthetic data under `fixtures/synthetic/`.

## Status

Phase 2A — adds Invoice/Payment/Allocation/MatchCase on top of Phase 1's
Contract import: a purchase-invoice Excel adapter, a deterministic
CMB bank-statement PDF adapter (no OCR), and the M001 deterministic
matching rule (exact counterparty + exact amount, with a
unique-candidate/ambiguous/unmatched split — never positional guessing).
No period-close, no accrual/reversal logic, no pages, no Agent
integration yet — see [docs/PHASE2A-DECISIONS.md](docs/PHASE2A-DECISIONS.md)
for what was decided along the way,
[docs/PHASE2A-ACCEPTANCE.md](docs/PHASE2A-ACCEPTANCE.md) for public
acceptance criteria, and [docs/V1-SCOPE.md](docs/V1-SCOPE.md) for what remains out
of scope.
