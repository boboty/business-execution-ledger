# Phase 2D.3 — Invoice Preparation Rule Freeze & Provenance Registry

This document registers the Phase 2D.3-scoped invoice-preparation rule
IDs and, for each, its **provenance** — who confirmed it and under what
standing. It is a Phase-2D.3-scoped registry: these rule IDs are NOT yet
promoted into `docs/RULES.md` (that promotion is a later, deliberate
step), and nothing here changes any frozen global rule.

Two directions are covered:

- **IP-S** — Sales Invoice Preparation (our company → external sales
  customer; primary axis `SalesContract`).
- **IP-P** — Supplier/Purchase Invoice Request (supplier → our company,
  "how should the supplier invoice us?"; primary axis procurement
  `Contract`, `Contract.buyer` being our own entity).

## Provenance / status values

| Value | Meaning |
| --- | --- |
| `ACCOUNTANT_CONFIRMED` | Confirmed by the accountant. Business-confirmed rule; implement deterministically as stated. |
| `OWNER_CONFIRMED_PROVISIONAL` | Deliberately accepted by the product owner so implementation can proceed. It is deterministic for the current implementation, MUST be specifically re-reviewed during later real-data acceptance / first-stage cutover, and may be revised if later business Evidence contradicts it. Provisional is NOT unconfirmed: it is a recorded, owned decision. |
| `UNRESOLVED_SAFE_BLOCKER` | Not frozen. No rule is invented; the system keeps an explicit safe blocker / refuses to judge. A later freeze is required before any judgment exists. |

No rule in this registry carries any private-derived value. The
rule-discovery conversations themselves are external/private; only the
rule statements and their provenance class are recorded here.

---

## Sales direction (IP-S)

### IP-S01 — Three simultaneous required inputs

Sales invoice preparation requires ALL THREE of the following
simultaneously:

1. `SalesContract`
2. the current linked procurement Contract (via a CURRENT
   `ProcurementSalesLink`)
3. a confirmed Shipment/Export Fact

**Source: `ACCOUNTANT_CONFIRMED`.**

Implemented in Phase 2D.3-F1a
(`bel.application.sales_invoice_preparation`): missing inputs produce
explicit blocker outcomes; `INPUTS_PRESENT` states required-input fact
completeness only and is not an eligibility or readiness Decision.

### IP-S02 — Export-sales amount consistency (three-way equality)

For export-sales business, amount consistency means:

```
SalesContract gross amount
  == export/customs declaration amount
  == final SALES Invoice gross amount
```

ONLY amount equality is required by this rule. It does NOT require — and
must never be read as requiring — that customer, product, specification,
or quantity be identical across the three documents. Those are not part
of IP-S02.

**Source: `OWNER_CONFIRMED_PROVISIONAL`.**

**Canonical declaration amount/currency (closed for F1c):** the
canonical Shipment/Export Fact now carries `declared_amount` and
`declared_currency` (Phase 2D.3-F1c) — the amount and currency
explicitly stated by the confirmed export/customs declaration Evidence,
asserted only with Evidence, never inferred, never FX-converted, and
never defaulted to CNY/USD. An amount known without its currency remains
a representable incomplete Fact.

**Remaining limitation (recorded structurally, not worked around):**
IP-S02 is FROZEN AS A RULE but its full three-way consistency
evaluation is still PENDING. The current Invoice Fact has no explicit
currency field, so comparing declaration amount/currency against the
final SALES Invoice gross amount must not introduce an implicit currency
assumption merely to make the rule appear complete — any downstream
three-way comparison must refuse cross-currency comparison, and none is
implemented yet. No code path may substitute `SalesContract.gross_amount`
or an invoice amount for the declaration amount, and no legacy sales
amount is backfilled as a customs-declaration amount: real declaration
Evidence or an explicit human-confirmed Fact is required.

### IP-S03 — Receipt is not a hard prerequisite

An incoming receipt/payment is NOT a hard eligibility prerequisite for
sales invoice preparation in the current implementation. "收多少开多少"
(invoice exactly as much as has been received) is NOT implemented.

**Source: `OWNER_CONFIRMED_PROVISIONAL`.**

### IP-S04 — M:N shipment aggregation semantics

The shipment aggregation semantics for M:N `ProcurementSalesLink`
(whether "any linked contract has a shipment" or "all linked contracts
have shipments" would satisfy input 3) are NOT frozen. The current safe
blocker is kept; neither ANY nor ALL is chosen.

**Source: `UNRESOLVED_SAFE_BLOCKER`.**

Implemented in Phase 2D.3-F1a as
`SHIPMENT_JUDGMENT_DEFERRED_MULTIPLE_LINKS` — always emitted under
multiple current links, regardless of shipment presence.

---

## Supplier / procurement direction (IP-P)

### IP-P01 — OUT payment is context, not a gate

A supplier invoice request commonly happens after the OUT payment, but
payment has exceptions and is NOT a hard gate. Existing OUT payment
Facts are exposed as context; they never gate, enable, or score a
request.

**Source: `ACCOUNTANT_CONFIRMED`.**

### IP-P02 — Expected purchase invoice gross amount

The expected supplier PURCHASE invoice gross amount is the procurement
Contract gross amount. This is a preparation amount — not an accounting
value and not a tax calculation.

**Source: `ACCOUNTANT_CONFIRMED`.**

### IP-P03 — One procurement Contract → one PURCHASE invoice

One procurement Contract must not be split across multiple PURCHASE
invoices.

**Source: `ACCOUNTANT_CONFIRMED`.**

### IP-P04 — One PURCHASE invoice → one procurement Contract

One supplier PURCHASE invoice must not cover multiple procurement
Contracts.

**Source: `ACCOUNTANT_CONFIRMED`.**

### IP-P05 — Product naming consistency

Where item facts exist, supplier invoice product naming must match the
confirmed procurement/export product naming.

**Source: `ACCOUNTANT_CONFIRMED`.**

### IP-P06 — Tax rate comes from Evidence, never inference

The tax rate comes from the actual invoice / tax evidence. BEL must not
infer a supplier invoice tax rate. An actual PURCHASE InvoiceItem's
`tax_rate` may be displayed as an existing Fact; no tax-rate
recommendation or inference exists anywhere.

**Source: `ACCOUNTANT_CONFIRMED`.**

### IP-P07 — Quantity basis unresolved

The supplier invoice request quantity basis is not yet frozen: the
precedence between contract quantity / shipped quantity / declared
quantity is not established. No quantity calculation may be invented.

**Source: `UNRESOLVED_SAFE_BLOCKER`.**

### IP-P08 — Tax classification code comes from Evidence, never inference

The goods/services tax classification code (税收分类编码) on a supplier
PURCHASE invoice line follows confirmed Evidence only:

- an already-confirmed code the system holds for that product category
  is **directly reused** (直接沿用) — never re-derived or re-asked;
- a new product category, or a product with no confirmed code, requires
  the **supplier to confirm** it — the outcome is
  `HUMAN_CONFIRMATION_REQUIRED`, never a guessed code;
- BEL **never guesses a tax classification code**: no inference from
  product name, tax rate, quantity, or any other existing value, and no
  mapping/synthesis from another field. A product with no confirmed code
  remains representable as "code unknown" while the system records the
  need for supplier confirmation.

**Source: `ACCOUNTANT_CONFIRMED`.**

---

## Implementation status map (as of Phase 2D.3-F1d)

Semantics of the frozen-rule conflicts implemented by F1b: a MISMATCH
of an IP-P02 amount comparison or an IP-P05 product-name comparison is
a frozen-rule conflict (`PURCHASE_INVOICE_AMOUNT_MISMATCH` /
`PURCHASE_INVOICE_PRODUCT_NAME_MISMATCH`) and makes the scope decision
`RULE_CONFLICT` — never worded as "unpaid"/"outstanding"/"overdue". A
comparison required by an existing association that cannot be performed
because the compared Fact/value is absent is NOT a rule conflict: it
emits an explicit missing-fact blocker and makes the scope decision at
least `INSUFFICIENT_FACTS`. Status precedence: `RULE_CONFLICT` >
`INSUFFICIENT_FACTS` > `PREPARATION_AMOUNT_DETERMINABLE`.

Advisory / blocker separation (Phase 2D.3-F1d): every implemented rule
is re-leveled into a **finding level** — the outcome class its findings
produce on the SUPPLIER_INVOICE_REQUEST Decision:

- `BLOCKER` — hard findings (rule conflicts / missing compared Facts).
  The decision status is derived from these ALONE
  (`RULE_CONFLICT` > `INSUFFICIENT_FACTS` > `PREPARATION_AMOUNT_DETERMINABLE`).
- `ADVISORY` — explicit NON-BLOCKING findings (F1d advisory channel).
  They record a frozen accountant-confirmed rule consequence that is
  factual context and never a gate, and they NEVER affect the decision
  status: a scope with advisories and no blockers is still
  `PREPARATION_AMOUNT_DETERMINABLE`, and an advisory coexisting with a
  blocker leaves the blocker's status intact.
- `CONTEXT` — Facts exposed on the decision; no finding is emitted by
  this rule today.
- `NO-FINDING` — the rule emits nothing yet (guard-only or pending).

Provenance standings are unchanged by this re-leveling; the column only
states the outcome class each rule's findings map onto.

| Rule | Standing | Finding level | Implementation |
| --- | --- | --- | --- |
| IP-S01 | `ACCOUNTANT_CONFIRMED` | `BLOCKER` | Implemented (F1a); missing required input → explicit blocker / `INSUFFICIENT_FACTS` |
| IP-S02 | `OWNER_CONFIRMED_PROVISIONAL` | `CONTEXT` | Frozen as a rule; canonical declaration amount/currency supported after F1c and exposed as Shipment Facts, but full three-way consistency evaluation still pending (IP-S02 limitation above) — no finding emitted |
| IP-S03 | `OWNER_CONFIRMED_PROVISIONAL` | `CONTEXT` | Respected by F1a (receipts never consulted); no finding emitted |
| IP-S04 | `UNRESOLVED_SAFE_BLOCKER` | `BLOCKER` | Safe blocker kept (F1a) |
| IP-P01 | `ACCOUNTANT_CONFIRMED` | `ADVISORY` | F1b exposes OUT payment Facts as context; F1d adds the `OUT_PAYMENT_PRESENT_CONTEXT_ONLY` advisory — payment never gates, never affects status |
| IP-P02 | `ACCOUNTANT_CONFIRMED` | `BLOCKER` | Implemented (F1b); amount MISMATCH → `PURCHASE_INVOICE_AMOUNT_MISMATCH` conflict; missing compared Fact/value → explicit missing-fact blocker |
| IP-P03 | `ACCOUNTANT_CONFIRMED` | `BLOCKER` | Implemented as deterministic violation check (F1b) — a Contract must not be split |
| IP-P04 | `ACCOUNTANT_CONFIRMED` | `BLOCKER` | Implemented as deterministic violation check (F1b) |
| IP-P05 | `ACCOUNTANT_CONFIRMED` | `BLOCKER` | Implemented as exact product-name check where item facts exist (F1b); MISMATCH → `PURCHASE_INVOICE_PRODUCT_NAME_MISMATCH` conflict; missing name → explicit missing-fact blocker |
| IP-P06 | `ACCOUNTANT_CONFIRMED` | `ADVISORY` | F1b exposes an existing InvoiceItem `tax_rate` as the Fact it is; F1d adds the `EXISTING_INVOICE_ITEM_TAX_RATE_FACT` advisory — display only, never an inference/recommendation, never a status change |
| IP-P07 | `UNRESOLVED_SAFE_BLOCKER` | `NO-FINDING` | No quantity calculation (F1b); guard only |
| IP-P08 | `ACCOUNTANT_CONFIRMED` | `NO-FINDING` | Frozen as a rule; NO tax-classification-code path exists anywhere yet (no field, no confirmation flow) — register-only; eventual outcome class `HUMAN_CONFIRMATION_REQUIRED`, implementation pending a later stage |
