# Business Rules (Frozen Definitions — Not Implemented)

Phase 0 freezes rule **numbers, intent, and trigger conditions**. No rule
is implemented as code in this phase. Rule logic below is written in
plain-language pseudo-condition form, not in any target language.

Each rule is tagged:

- **CONFIRMED** — the business owner specified this rule's intent and
  trigger conditions directly, including the following confirmed
  principle governing R001/R002/R003: a `PARTIALLY_REVERSED` Accrual
  with `remaining balance > 0` is still an open, unsettled accrual — it
  must keep participating in later reversal-on-invoice matching (R001)
  and must still block a duplicate Accrual being created for the same
  business scope (R003), exactly as an `ACTIVE` Accrual would; R002 is
  bound by the same "not fully reversed" test. R001/R002/R003 below
  state this directly rather than testing `status == ACTIVE` alone.
- **PROPOSED** — drafted in Phase 0 to make sure every
  [V1-SCOPE.md](V1-SCOPE.md) close-engine output has at least one
  producing rule (see the coverage table at the bottom). R008–R015 are
  all PROPOSED: they have not been confirmed by the business owner and
  must not be treated as settled until reviewed, the same way R001–R007
  were.

Every rule that produces a close-engine `Decision` must remain traceable
to the `Fact`(s) it read — never to Evidence directly — per A02 in
[ARCHITECTURE.md](ARCHITECTURE.md).

---

## R001 — 历史暂估到票 (Prior Accrual Now Invoiced) — CONFIRMED

```
存在未完全红冲的 Accrual（status ∈ {ACTIVE, PARTIALLY_REVERSED} 且未红冲余额 > 0）
AND 对应采购业务已取得有效进项发票
→ PriorAccrualReversalRequired
```

A `PARTIALLY_REVERSED` Accrual with remaining balance is still open —
it must keep triggering further reversals as later invoices arrive, not
just an `ACTIVE` Accrual (see the confirmed principle above, and
[DOMAIN.md](DOMAIN.md)'s requirement that partial reversal be natively,
repeatedly supported).

## R002 — 新增暂估候选 (New Accrual Candidate) — CONFIRMED

```
业务已满足当期成本确认条件
AND period_end 前无有效采购发票
AND 不存在对应未完全红冲的 Accrual（status ∈ {ACTIVE, PARTIALLY_REVERSED} 且未红冲余额 > 0）
→ AccrualRequired
```

Same confirmed principle as R001/R003: a business scope holding a
`PARTIALLY_REVERSED` Accrual with remaining balance is not eligible for a
new `AccrualRequired` — R002, R001, and R003 all test the same "not
fully reversed" condition, so they never produce opposite conclusions
from the same facts.

## R003 — 防止重复暂估 (Prevent Duplicate Accrual) — CONFIRMED

```
同一业务范围存在未完全红冲的 Accrual（status ∈ {ACTIVE, PARTIALLY_REVERSED} 且未红冲余额 > 0）
→ 不允许再次创建同范围 Accrual
```

Same confirmed principle as R001: a `PARTIALLY_REVERSED` Accrual still
has an outstanding balance for the same business scope and must still
block a duplicate — the guard does not lapse just because a first
partial reversal already happened.

This is a guard rule: it constrains R002, it does not itself emit a new
close-engine output type.

## R004 — 合同业务键冲突 (Contract Business-Key Conflict) — CONFIRMED

```
contract_no 相同
AND 关键主体/业务事实冲突
→ BusinessKeyConflict
```

Automatic merge is forbidden — see the `contract_no` constraint in
[DOMAIN.md](DOMAIN.md).

## R005 — 发票金额差异 (Invoice vs. Accrual Amount Difference) — CONFIRMED

```
invoice matched
AND 正式采购成本 != 先前暂估成本
→ AccrualActualDifference
```

## R006 — 部分到票 (Partial Invoice Receipt) — CONFIRMED

Partial goods or partial quantity within a contract obtaining a formal
invoice only reverses the corresponding **portion** of the historical
accrual. Obtaining one invoice against a multi-item contract must never
cause the entire contract's accrual to be reversed. This scopes R001: the
reversal amount tracks matched `ContractItem` quantity, not the contract
total.

## R007 — 商品明细不足 (Insufficient Item-Level Detail) — CONFIRMED

```
可以确定：合同 / 供应商 / 金额
不能确定：商品 / 数量
→ 允许输出合同级 AccrualCandidate
→ 禁止生成 ContractItem 级正式暂估结果
```

This is a hard rule (see [DOMAIN.md](DOMAIN.md) — `Accrual.contract_item_id`
is required). A contract-level candidate is a weaker, explicitly
lower-confidence object than a formal `Accrual`, and must be visibly
distinguishable from one downstream.

---

## R008 — 采购成本确认 (Purchase Cost Confirmed) — PROPOSED

```
Invoice matched to ContractItem (via ContractItem ↔ InvoiceItem)
AND matched quantity/amount within tolerance of contract terms
AND no conflicting unreversed Accrual (ACTIVE or PARTIALLY_REVERSED) dispute
→ PurchaseCostConfirmed
```

**Pending confirmation notice:** R008 remains PROPOSED and requires
business review before implementation, the same as R009–R015.

## R009 — 发票未匹配 (Invoice Unmatched) — PROPOSED

```
Invoice exists
AND no CONFIRMED or PROPOSED match to any Contract/ContractItem by period_end
→ InvoiceUnmatched
```

## R010 — 付款未匹配 (Payment Unmatched) — PROPOSED

```
Payment exists
AND no PaymentAllocation exists for it by period_end
→ PaymentUnmatched
```

## R011 — 证据缺失 (Evidence Missing) — PROPOSED

```
A business fact required for period close (e.g. a matching contract,
invoice, or bank record) is expected but has no supporting Evidence
by period_end
→ EvidenceMissing
```

Phase 0 freezes only that this output must be rule-produced; the exact
catalogue of "expected but missing" evidence types is Phase 1 work.

## R012 — 金额不一致 (Amount Mismatch) — PROPOSED

```
Amounts between matched objects (e.g. Contract vs Invoice, Invoice vs
Payment) differ beyond tolerance
AND the difference is not already explained by another rule
(e.g. R005's accrual-vs-actual difference, R006's partial receipt)
→ AmountMismatch
```

## R013 — 低置信度匹配需人工确认 (Low-Confidence Match Requires Confirmation) — PROPOSED

Operationalizes A05.

```
Agent-proposed match or fact has confidence below the auto-confirm
threshold
→ status = HUMAN_CONFIRMATION_REQUIRED, a Task is generated
→ the proposal is never written as a confirmed Fact automatically
```

## R014 — 证据冲突需生成任务 (Conflicting Evidence Requires a Task) — PROPOSED

Operationalizes A02 (Evidence is immutable) together with A05.

```
New Evidence contradicts (rather than supplements) an already-confirmed
Fact
→ the confirmed Fact is not silently overwritten; a Task is generated;
   both the old Fact and the new Evidence remain intact and traceable
```

## R015 — 事实变化触发重新计算 (Fact Change Triggers Recompute) — PROPOSED

```
A Fact that a prior Decision depended on changes
(e.g. invoice_received flips from false to true)
→ on the next Period Close run, the dependent Decision (e.g. an
   AccrualRequired candidate) is automatically recomputed and may drop
   out of the candidate set
→ this must never require a human to manually delete a stale result
```

This rule is what G06 in [GOLDEN-TEST.md](GOLDEN-TEST.md) exercises.

---

## Coverage: every V1-SCOPE close output has a producing rule

| Close-engine output | Producing rule(s) |
|---|---|
| `AccrualRequired` | R002, scoped by R007 |
| `PriorAccrualReversalRequired` | R001, scoped by R006 |
| `PurchaseCostConfirmed` | R008 |
| `AccrualActualDifference` | R005 |
| `PaymentUnmatched` | R010 |
| `InvoiceUnmatched` | R009 |
| `EvidenceMissing` | R011 |
| `AmountMismatch` | R012 |
| `BusinessKeyConflict` | R004 |
