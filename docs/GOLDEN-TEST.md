# Golden Test Methodology

The golden test is Phase 0's most important artifact: a scenario-by-scenario
answer key that any future rule engine implementation must reproduce
before it can be trusted.

## Why this exists

Rules in [RULES.md](RULES.md) are prose today. When they become code,
the implementation is evaluated against an independently constructed
synthetic answer key, rather than merely checking that it runs. The
scenario set (G01–G06 below) is that answer key.

## Data layering

Sensitive business data must never be committed to this repository.
See `docs/PRIVATE-DATA-POLICY.md` for the full policy. The committed
golden-test layer is strictly synthetic:

```
tests/golden/synthetic-v1/         independently constructed synthetic data
                                    and answer key — committed
```

Where a scenario needs input, it belongs in `fixtures/synthetic/` as
independently constructed data, not as an anonymized derivative of a
non-public file.

## Scenarios

### G01 — 5月历史暂估 (May Prior Accrual)

May's accrual, its July reversal, and the resulting remaining
un-reversed balance (status moves `ACTIVE` → `PARTIALLY_REVERSED`;
"remaining" describes the balance, not the status). Exercises R001
(reversal-on-invoice), R003 (the remaining balance must still block a
duplicate Accrual), and R006 (partial reversal — the July reversal is
smaller than the full May accrual).

### G02 — 6月历史暂估 (June Prior Accrual)

Same shape as G01, for June's accrual. Exercises R001, R003, and R006
again against an independent period, to make sure the engine doesn't
conflate accruals from different source periods.

### G03 — 7月待暂估 (July Accrual Candidate)

The current July candidate amount, computed from the latest contract
business facts. This is a **contract-level candidate**, not a formal
item-level `Accrual` — R007 forbids generating an item-level accrual
while product/quantity detail is incomplete. The golden record must
encode this distinction explicitly (see the `classification` field
below), not just carry a bare number that a future implementation could
accidentally promote to a formal Accrual.

### G04 — 历史成本红冲 (Prior-Period Accrual Cost)

The May + June accrual amounts that were *originally estimated* in their
own historical periods (May, June) and whose *reversal transactions* land
in July, driven by invoices arriving this month. G04 does not name a new
event — it is a derived cross-check equal to G01's July reversal amount
plus G02's July reversal amount. The golden record asserts these amounts
are classified as **prior-period accrual cost**: even though the
reversal is recorded in July, the underlying cost was already recognized
against May/June, and must not be double-counted as newly confirmed July
purchase cost.

### G05 — 重复业务键 (Duplicate Business Key)

The same `contract_no` maps to different business counterparties/facts.
Exercises R004: the engine must flag a `BusinessKeyConflict`, never
silently merge the two into one contract.

### G06 — Fact变化触发重新计算 (Fact Change Triggers Recompute)

A contract that starts with `invoice_received = false` (and is therefore
a candidate under R002), then receives new Evidence showing an invoice
date. Re-running period close must cause that contract to drop out of
the "new accrual candidate" set automatically (R015) — this is asserted
as a **before/after state transition**, not a single point-in-time
number, because the behavior under test is the recompute itself.

## What "passing" means

A rule-engine implementation passes the golden test when, given the
fixture inputs for the target period, its output matches every value
and classification in the answer key exactly. Any mismatch is a
regression, not a rounding note to be waved off — amounts in the answer
key are stored as decimal strings for this reason.
