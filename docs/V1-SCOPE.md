# V1 Scope

This document freezes what V1 builds and, just as importantly, what it
does not. Anything not listed as In Scope is out of scope for V1 by
default — it does not need a separate exclusion entry.

## 1. Evidence Import

V1 supports **manual upload only** of these evidence sources:

- 合同台账 Excel (contract ledger spreadsheet)
- 采购合同 (purchase contracts)
- 进项发票数据 (input/purchase invoice data)
- 销项发票数据 (output/sales invoice data)
- 银行流水 (bank statements)
- 出口/报关业务资料 (export / customs declaration material)
- 人工补充事实 (manually supplied facts)

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

## 2. Business Fact Management

V1 manages at least these fact objects:

- `Contract`
- `ContractItem`
- `Invoice`
- `Payment`
- `PaymentAllocation`
- `Shipment / Export`
- `Accrual`
- `Evidence`
- `BusinessEvent`
- `Task / Exception`

Semantics for each are frozen in [DOMAIN.md](DOMAIN.md).

## 3. Matching

V1 supports these match types:

- `Contract ↔ Invoice`
- `Contract ↔ Payment`
- `Contract ↔ Export`
- `ContractItem ↔ InvoiceItem`

No other match types are in scope for V1.

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
[RULES.md](RULES.md).

## 5. User-Facing Pages

V1 ships exactly five working pages:

1. 业务驾驶舱 (Business Cockpit)
2. 合同业务总账 (Contract Business Ledger)
3. 合同360° (Contract 360)
4. 异常与任务中心 (Exception & Task Center)
5. 月结工作台 (Period-Close Workbench)

No page implementation happens in Phase 0. This is a scope freeze, not a
design.

## Non-goals for V1 (explicit, do not silently re-add)

- No database implementation
- No UI implementation
- No rule engine implementation
- No Agent / LLM integration
- No MCP integration
- No accounting entries or tax-rebate logic
- No ERP concepts
- No generic workflow DSL
- No event-sourcing framework
- No microservices split "for future scale"
