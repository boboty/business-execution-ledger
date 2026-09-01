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

**Canonical Fact / intake gap (recorded, not worked around):** the
current canonical Shipment does NOT carry an export/customs declaration
amount. IP-S02 is therefore FROZEN AS A RULE but NOT YET FULLY
EVALUABLE: one of its three compared amounts has no canonical Fact
today. This is an intake gap to close with real Evidence (a declaration
amount on the Shipment/Export fact side), never by deriving or faking
the declaration amount from the SalesContract or the SALES Invoice. No
code path may substitute `SalesContract.gross_amount` or an invoice
amount for the declaration amount.

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

---

## Implementation status map (as of Phase 2D.3-F1b + F1b pre-Gate repair)

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

| Rule | Standing | Implementation |
| --- | --- | --- |
| IP-S01 | `ACCOUNTANT_CONFIRMED` | Implemented (F1a) |
| IP-S02 | `OWNER_CONFIRMED_PROVISIONAL` | Frozen as a rule; NOT fully evaluable — canonical export-declaration amount gap (IP-S02 gap above) |
| IP-S03 | `OWNER_CONFIRMED_PROVISIONAL` | Respected by F1a (receipts never consulted) |
| IP-S04 | `UNRESOLVED_SAFE_BLOCKER` | Safe blocker kept (F1a) |
| IP-P01 | `ACCOUNTANT_CONFIRMED` | Respected by F1b (payments exposed as context only) |
| IP-P02 | `ACCOUNTANT_CONFIRMED` | Implemented (F1b); amount MISMATCH → `PURCHASE_INVOICE_AMOUNT_MISMATCH` conflict; missing compared Fact/value → explicit missing-fact blocker |
| IP-P03 | `ACCOUNTANT_CONFIRMED` | Implemented as deterministic violation check (F1b) — a Contract must not be split |
| IP-P04 | `ACCOUNTANT_CONFIRMED` | Implemented as deterministic violation check (F1b) |
| IP-P05 | `ACCOUNTANT_CONFIRMED` | Implemented as exact product-name check where item facts exist (F1b); MISMATCH → `PURCHASE_INVOICE_PRODUCT_NAME_MISMATCH` conflict; missing name → explicit missing-fact blocker |
| IP-P06 | `ACCOUNTANT_CONFIRMED` | Respected by F1b (no inference; Facts only) |
| IP-P07 | `UNRESOLVED_SAFE_BLOCKER` | No quantity calculation (F1b) |
