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
| `UNRESOLVED` | Not frozen. The system records the unresolved question and defers all judgment — it never invents a rule and never blocks preparation on it. A later freeze is required before any judgment exists. |
| `UNRESOLVED_SAFE_BLOCKER` | Historical standing (Phase 2D.3-F1a/F1b) superseded by the F1d re-leveling: the old safe blocker implementations — an explicit blocker / refusal to judge — are now recorded as `UNRESOLVED` (safe deferral, never a preparation blocker). Kept only for the historical registry text; no current rule uses it. |

Provenance (who confirmed the rule, under what standing) and enforcement
(what outcome class the rule's findings produce) are independent
dimensions: an `ACCOUNTANT_CONFIRMED` rule may enforce at any finding
level, and an `UNRESOLVED` standing means the rule's comparison is
deferred, not that it blocks.

No rule in this registry carries any private-derived value. The
rule-discovery conversations themselves are external/private; only the
rule statements and their provenance class are recorded here.

---

## Sales direction (IP-S)

### IP-S01 — The three preparation inputs (fact completeness, not a gate)

Sales invoice preparation reports three inputs for every `SalesContract`
scope, in this order:

1. `SalesContract`
2. the current linked procurement Contract (via a CURRENT
   `ProcurementSalesLink`)
3. a confirmed Shipment/Export Fact

**Source: `ACCOUNTANT_CONFIRMED`.**

**Finding level: `CONTEXT`** — fact completeness / comparison
availability, NOT invoice eligibility. Re-leveled in Phase 2D.3-F1d:

- `SalesContract` is the genuinely-required sales-scope data. A sales
  scope exists only for an existing `SalesContract` anchor, so it is
  present by construction; missing genuinely-required sales-scope data
  would be `INSUFFICIENT_FACTS` (where preparation data cannot be
  built), which the F0 construction makes unreachable today.
- the ProcurementSalesLink is a management/context linkage. A missing
  link only makes procurement-side comparison unavailable — it is NOT an
  eligibility blocker and never gates invoice preparation.
- the Shipment/Export Fact is an export-management anchor. A missing
  Shipment makes the export/customs comparison unavailable — NOT "may
  not issue invoice", and never an eligibility blocker.

Implemented in Phase 2D.3-F1a and re-leveled in Phase 2D.3-F1d
(`bel.application.sales_invoice_preparation`): the three `required_inputs`
report comparison availability only; no blocker is emitted by the sales
rule set, and `INPUTS_PRESENT` is never an eligibility or readiness
Decision.

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

**Finding level: `ADVISORY` (management control)** — the three-way
comparison is IMPLEMENTED for the unambiguous scope (Phase 2D.3-F1f). It
is evaluated as a MANAGEMENT control, NEVER a workflow gate, NEVER a
`RULE_CONFLICT`, and never a preparation blocker. Only the unambiguous
1:1:1 comparison scope is compared: exactly ONE confirmed SALES Invoice
Fact (a `SalesInvoiceAllocation` whose Invoice Fact is missing or not
direction SALES is NOT a confirmed Invoice Fact), exactly ONE current
`ProcurementSalesLink`, and exactly ONE current Shipment on the linked
Contract. Outcome vocabulary:

- `MATCH` — all three currencies explicit and equal, all three amounts
  exactly equal;
- `DEVIATION` (amount) — same explicit currency, amounts not all equal ->
  `SALES_INVOICE_AMOUNT_DEVIATION` ADVISORY;
- `NOT_COMPARABLE_CURRENCY_MISMATCH` — the relevant explicit currencies
  are not all equal -> `SALES_INVOICE_CURRENCY_DEVIATION` ADVISORY; no
  amount comparison is attempted, no FX;
- `NOT_COMPARABLE_MISSING_FACT` — any compared amount/currency Fact
  absent (including a scope with no confirmed SALES Invoice Fact or no
  Shipment/Export Fact); a check result ONLY, never a blocker;
- `NOT_COMPARABLE_AMBIGUOUS_SCOPE` — invoice/declaration scope ambiguous
  by cardinality (multiple confirmed SALES invoices, multiple current
  links, or multiple Shipment/Export declaration candidates): no sum, no
  apportionment, no arbitrary selection — cardinality ambiguity takes
  precedence over selecting arbitrary facts.

The comparison is currency-safe by construction: amounts are compared
ONLY when all three amounts AND all three currencies exist and the three
currencies are explicitly equal — no FX, no default currency, no
implicit same-currency assumption. The comparison never blocks invoice
preparation, never changes the decision `status`, and receipt chronology
never affects it.

**Remaining limitation (recorded structurally, not worked around):** the
comparison is implemented ONLY for the unambiguous 1:1:1 scope. Any scope
with multiple confirmed SALES invoices, multiple current links, or
multiple Shipment/Export declaration candidates is
`NOT_COMPARABLE_AMBIGUOUS_SCOPE` — never summed, never apportioned, never
compared against an arbitrarily chosen candidate. No code path may
substitute `SalesContract.gross_amount` or an invoice amount for the
declaration amount, and no legacy sales amount is backfilled as a
customs-declaration amount: real declaration Evidence or an explicit
human-confirmed Fact is required. Canonical currency support on all
three legs — `SalesContract.currency`, Shipment `declared_currency`
(F1c), and `Invoice.currency` (F1e) — is what makes the equal-currency
comparison safe; a missing currency is never defaulted or inferred to
make the rule appear complete.

### IP-S03 — Receipt is not a hard prerequisite

An incoming receipt/payment is NOT a hard eligibility prerequisite for
sales invoice preparation in the current implementation. "收多少开多少"
(invoice exactly as much as has been received) is NOT implemented.

**Source: `ACCOUNTANT_CONFIRMED`.**

**Finding level: `CONTEXT`.** No required chronology exists: an invoice
before or after its receipt/payment is equally fine (invoice-before-
receipt is common), and no ordering finding is emitted. Invoice
preparation and receipt/payment are independent facts.

### IP-S04 — M:N shipment aggregation semantics

The shipment aggregation semantics for M:N `ProcurementSalesLink`
(whether "any linked contract has a shipment" or "all linked contracts
have shipments" would satisfy the shipment input) are NOT frozen. Neither
ANY nor ALL is chosen; the system does not guess.

**Source: `UNRESOLVED`.**

**Finding level: `CONTEXT`** — an unresolved comparison, NEVER a
preparation blocker. Implemented in Phase 2D.3-F1a as the shipment input
being not judged, re-leveled in Phase 2D.3-F1d to
`NOT_JUDGED_UNDER_MN_UNRESOLVED` (no blocker, no status change): under
multiple current links the shipment input is recorded as not judged
regardless of shipment presence, the M:N linked-contract facts stay
visible on the decision, no cross-bridge aggregation is performed, and
any future comparison over M:N is `NOT_COMPARABLE` / `UNRESOLVED` —
invoice preparation is never blocked by it.

---

## Supplier / procurement direction (IP-P)

### IP-P01 — OUT payment is context, not a gate

A supplier invoice request commonly happens after the OUT payment, but
payment has exceptions and is NOT a hard gate. Existing OUT payment
Facts are exposed as context; they never gate, enable, or score a
request.

**Source: `ACCOUNTANT_CONFIRMED`.**

**Finding level: `CONTEXT`** — payment ordering produces no status and
no advisory (Phase 2D.3-F1d removed the earlier
`OUT_PAYMENT_PRESENT_CONTEXT_ONLY` advisory). The only payment-derived
finding is the IP-P09 follow-up below (paid + no PURCHASE invoice), and
it is a management reminder, never a payment-state signal.

### IP-P02 — Expected purchase invoice gross amount

The expected supplier PURCHASE invoice gross amount is the procurement
Contract gross amount, with the Contract's own currency as the expected
currency. This is a preparation amount — not an accounting value and
not a tax calculation.

**Source: `ACCOUNTANT_CONFIRMED`.**

**Finding level: `ADVISORY`** (reference + deviation). The Contract gross
amount is the reference for a deterministic comparison (exact `Decimal`
equality). The amount comparison is valid ONLY when both explicit
currencies are present and exactly comparable — there is NO implicit
same-currency assumption (Phase 2D.3-F1e). Semantics:

- `MATCH` — same explicit currency, exact gross amount equality.
- `DEVIATION` (amount) — same explicit currency, gross amount
  inequality -> `PURCHASE_INVOICE_AMOUNT_DEVIATION` ADVISORY; the
  invoice Fact remains valid and nothing is a rule conflict.
- `NOT_COMPARABLE_CURRENCY_MISMATCH` — both currencies explicit but
  different -> `PURCHASE_INVOICE_CURRENCY_DEVIATION` ADVISORY; no
  amount comparison is attempted (no FX, no amount deviation implied).
- `NOT_COMPARABLE_MISSING_FACT` — any required amount/currency Fact
  absent (including a missing `Invoice.currency`): a check result ONLY,
  never a blocker — the optional management comparison simply cannot be
  made, and it never makes the Decision `INSUFFICIENT_FACTS`. The
  preparation reference stays determinable from
  `Contract.gross_amount` + `Contract.currency`.

A missing Contract gross amount stays `INSUFFICIENT_FACTS` (genuine
data incompleteness — the primary preparation value cannot be built).

### IP-P03 — One procurement Contract → one PURCHASE invoice

One procurement Contract is not expected to be split across multiple
PURCHASE invoices.

**Source: `ACCOUNTANT_CONFIRMED`.**

**Finding level: `ADVISORY`** (Phase 2D.3-F1d, re-leveled from a
conflict). More than one currently-allocated PURCHASE invoice emits
`MULTIPLE_PURCHASE_INVOICES_ON_CONTRACT` — a management review signal,
not a violation. The split is legitimate business state and every Fact
stays preserved.

### IP-P04 — One PURCHASE invoice → one procurement Contract

One supplier PURCHASE invoice is not expected to cover multiple
procurement Contracts.

**Source: `ACCOUNTANT_CONFIRMED`.**

**Finding level: `ADVISORY`** (Phase 2D.3-F1d, re-leveled from a
conflict). An M:N association emits `PURCHASE_INVOICE_SPANS_MULTIPLE_CONTRACTS`
— the M:N relationship is not a business error. The invoice is never
silently apportioned.

### IP-P05 — Product naming consistency

Where item facts exist, supplier invoice product naming should match the
confirmed procurement/export product naming.

**Source: `ACCOUNTANT_CONFIRMED`.**

**Finding level: `ADVISORY`** (Phase 2D.3-F1d, re-leveled from a
conflict). An unequal pair of confirmed names emits
`PURCHASE_INVOICE_PRODUCT_NAME_DEVIATION` — a management review signal,
never a violation. A missing product name is `NOT_COMPARABLE_MISSING_FACT`
(a check result only) and MUST NOT make the whole Decision
`INSUFFICIENT_FACTS`: the comparison is an optional management one.

### IP-P06 — Tax rate comes from Evidence, never inference

The tax rate comes from the actual invoice / tax evidence. BEL must not
infer a supplier invoice tax rate. An actual PURCHASE InvoiceItem's
`tax_rate` may be displayed as an existing Fact; no tax-rate
recommendation or inference exists anywhere.

**Source: `ACCOUNTANT_CONFIRMED`.**

**Finding level: `CONTEXT`** — an existing InvoiceItem `tax_rate` is the
Fact it is (Phase 2D.3-F1d removed the earlier
`EXISTING_INVOICE_ITEM_TAX_RATE_FACT` advisory); no finding is emitted
for its presence.

### IP-P07 — Quantity basis unresolved

The supplier invoice request quantity basis is not yet frozen: the
precedence between contract quantity / shipped quantity / declared
quantity is not established. No quantity calculation may be invented.

**Source: `UNRESOLVED`.**

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

### IP-P09 — Paid but no PURCHASE invoice is a management follow-up

When at least one confirmed OUT Payment is currently allocated to a
procurement Contract and NO PURCHASE Invoice Fact is associated yet, the
decision emits the `SUPPLIER_INVOICE_FOLLOW_UP_RECOMMENDED` advisory —
已付款，尚未收到对应进项发票，建议催供应商开票 ("paid, invoice not yet
received — recommend asking the supplier to issue it").

This is a management reminder only. It is NOT overdue, NOT a rule
conflict, NOT payment-required, NOT an eligibility gate, and NOT a
mandatory chronology. It disappears on recomputation as soon as a
PURCHASE invoice is associated, and it does NOT persist a Task (a later
stage — Phase 2D.4 — may promote this to a Task workflow).

**Source: `ACCOUNTANT_CONFIRMED`.**

**Finding level: `ADVISORY`** (Phase 2D.3-F1d).

---

## Cross-direction management anchor (IP-X)

### IP-X01 — Customs declaration is the invoice-preparation management anchor

A Shipment/Export Fact's `declared_amount` / `declared_currency`
(Phase 2D.3-F1c, asserted only with Evidence, never inferred, never
FX-converted, never defaulted) is the preferred management anchor for
reviewing BOTH the PURCHASE and the SALES invoice-preparation comparison.

This is a management control, NOT proof of a mathematical match: the
declaration amount is the reference management expects the invoices to
align with, and any deviation is a review signal. No apportionment, no
FX conversion, and no implicit currency is introduced.

**Source: `ACCOUNTANT_CONFIRMED`.**

**Finding level: `ADVISORY` when a frozen same-scope comparison detects
deviation; otherwise `CONTEXT`.** The declared Facts are exposed on the
Shipment (F1c) and are the declaration leg of the FIRST executable
SALES-side comparison using this anchor — the IP-S02 three-way check
(Phase 2D.3-F1f, unambiguous 1:1:1 scope). The supplier-side amount
comparison uses `Contract.gross_amount` as its reference. IP-X01 is a
cross-direction MANAGEMENT anchor, NOT a universal cross-leg equality
rule: F1f introduces no PURCHASE-vs-customs equality rule.

---

## Implementation status map (as of Phase 2D.3-F1f)

The F1d PRE-GATE REPAIR re-levels every implemented rule into a
**finding level** — the outcome class its findings produce — with
provenance and enforcement as INDEPENDENT dimensions:

- `BLOCKER` — hard findings: genuinely-required data absent. The
  decision `status` is derived from these ALONE. On the supplier side
  exactly one blocker code exists today (`MISSING_CONTRACT_GROSS_AMOUNT`),
  giving `INSUFFICIENT_FACTS`; otherwise
  `PREPARATION_AMOUNT_DETERMINABLE`. `RULE_CONFLICT` has been REMOVED
  from the decision vocabulary: a legitimate business state that departs
  from the preferred management pattern is NEVER a conflict.
- `ADVISORY` — explicit NON-BLOCKING management reminders / review
  signals (F1d channel). They record a frozen accountant-confirmed rule
  consequence that is legitimate business state worth a review or
  follow-up, recomputed from current Facts on every evaluation, and they
  NEVER affect the decision `status`: a scope with advisories and no
  blockers is still `PREPARATION_AMOUNT_DETERMINABLE`, and an advisory
  coexisting with a blocker leaves the blocker's status intact.
- `CONTEXT` — Facts exposed on the decision; no finding is emitted by
  this rule today.
- `NO-FINDING` — the rule emits nothing yet (guard-only or pending).

Check-result vocabulary (Phase 2D.3-F1e adds `NOT_COMPARABLE_CURRENCY_MISMATCH`;
Phase 2D.3-F1f adds `NOT_COMPARABLE_AMBIGUOUS_SCOPE`): `MATCH` /
`DEVIATION` / `NOT_COMPARABLE_MISSING_FACT` /
`NOT_COMPARABLE_CURRENCY_MISMATCH` / `NOT_COMPARABLE_AMBIGUOUS_SCOPE`.
`NOT_COMPARABLE_CURRENCY_MISMATCH` is emitted when the compared explicit
currencies are present but not all equal: no amount comparison is
attempted and no amount deviation is implied — the IP-P02 comparison is
currency-safe by construction (explicit comparable currency required;
no FX, no default, no inference). `NOT_COMPARABLE_AMBIGUOUS_SCOPE` is
the SALES-side (IP-S02) outcome for a scope whose invoice/declaration
cardinality is ambiguous (multiple confirmed SALES invoices, multiple
current links, or multiple Shipment/Export declaration candidates): no
sum, no apportionment, no arbitrary selection.

The F1b conflict semantics they supersede: the old amount / product-name
`MISMATCH` blockers (`PURCHASE_INVOICE_AMOUNT_MISMATCH`,
`PURCHASE_INVOICE_PRODUCT_NAME_MISMATCH`), the IP-P03 / IP-P04
cardinality conflict blockers, the missing compared-Fact blockers
(`MISSING_PURCHASE_INVOICE_FACT`, `MISSING_CONTRACT_ITEM_PRODUCT_NAME`,
`MISSING_INVOICE_ITEM_PRODUCT_NAME`), the `RULE_CONFLICT` status, the
IP-P01 / IP-P06 advisories, and the three sales eligibility blockers
(`NO_CURRENT_PROCUREMENT_LINK`, `NO_SHIPMENT_FACT_ON_LINKED_CONTRACT`,
`SHIPMENT_JUDGMENT_DEFERRED_MULTIPLE_LINKS`) are all removed; the
reclassified outcomes appear in the table below.

| Rule | Provenance | Finding level | Implementation |
| --- | --- | --- | --- |
| IP-S01 | `ACCOUNTANT_CONFIRMED` | `CONTEXT` | Re-leveled (F1a/F1d): three inputs report fact completeness / comparison availability only — link = management linkage, shipment = export-management anchor, no eligibility blocker; `INSUFFICIENT_FACTS` reserved for genuinely-required sales-scope data (unreachable by construction) |
| IP-S02 | `OWNER_CONFIRMED_PROVISIONAL` | `ADVISORY` (management control; implemented for the unambiguous 1:1:1 scope in Phase 2D.3-F1f — `MATCH`/`DEVIATION`/`NOT_COMPARABLE_MISSING_FACT`/`NOT_COMPARABLE_CURRENCY_MISMATCH`/`NOT_COMPARABLE_AMBIGUOUS_SCOPE`; never conflict, never a preparation blocker) | Canonical currency on all three legs — Shipment `declared_amount`/`declared_currency` (F1c) and `Invoice.currency` (F1e). Three-way comparison implemented for the unambiguous scope (F1f): exactly one confirmed SALES Invoice Fact (a dangling allocation is NOT a confirmed Fact), exactly one current link, exactly one current Shipment; amounts compared only with all three amounts + currencies explicit and equal (no FX, no default, no inference); same-currency inequality → `SALES_INVOICE_AMOUNT_DEVIATION`, explicit currency mismatch → `SALES_INVOICE_CURRENCY_DEVIATION`; multiple invoices / links / declaration candidates → `NOT_COMPARABLE_AMBIGUOUS_SCOPE` — never sum/apportion/choose one; the comparison never blocks preparation |
| IP-S03 | `ACCOUNTANT_CONFIRMED` | `CONTEXT` | Respected by F1a/F1d (receipts never consulted, no chronology finding); invoice-before-receipt is common |
| IP-S04 | `UNRESOLVED` | `CONTEXT` (unresolved comparison, never a blocker) | Shipment input recorded `NOT_JUDGED_UNDER_MN_UNRESOLVED` (F1a boundary, re-leveled F1d); M:N facts stay visible; future comparison → `NOT_COMPARABLE` / `UNRESOLVED`; never blocks invoice preparation |
| IP-P01 | `ACCOUNTANT_CONFIRMED` | `CONTEXT` | Payment exposed as context only (F1b); no status/advisory from payment ordering (F1d removed the `OUT_PAYMENT_PRESENT_CONTEXT_ONLY` advisory) |
| IP-P02 | `ACCOUNTANT_CONFIRMED` | `ADVISORY` (reference + deviation) | Expected amount = `Contract.gross_amount` + expected currency = `Contract.currency` (F1b/F1e); amount comparison valid only with explicit comparable currency (F1e) — same currency + amount mismatch → `PURCHASE_INVOICE_AMOUNT_DEVIATION`, different explicit currencies → `PURCHASE_INVOICE_CURRENCY_DEVIATION`, missing currency/amount → `NOT_COMPARABLE_MISSING_FACT` (check result only, never blocks); all advisories (F1d); missing Contract amount → `INSUFFICIENT_FACTS` (genuine data incompleteness) |
| IP-P03 | `ACCOUNTANT_CONFIRMED` | `ADVISORY` | `MULTIPLE_PURCHASE_INVOICES_ON_CONTRACT` advisory (F1d, re-leveled from a conflict) — split is legitimate business state; all Facts preserved |
| IP-P04 | `ACCOUNTANT_CONFIRMED` | `ADVISORY` | `PURCHASE_INVOICE_SPANS_MULTIPLE_CONTRACTS` advisory (F1d, re-leveled from a conflict) — never apportioned, M:N is not an error |
| IP-P05 | `ACCOUNTANT_CONFIRMED` | `ADVISORY` | DEVIATION → `PURCHASE_INVOICE_PRODUCT_NAME_DEVIATION` advisory (F1d, re-leveled from a conflict); missing name → `NOT_COMPARABLE_MISSING_FACT` check result only (never blocks preparation) |
| IP-P06 | `ACCOUNTANT_CONFIRMED` | `CONTEXT` | Existing InvoiceItem `tax_rate` exposed as the Fact it is (F1b); no advisory (F1d removed `EXISTING_INVOICE_ITEM_TAX_RATE_FACT`) |
| IP-P07 | `UNRESOLVED` | `NO-FINDING` | No quantity calculation (F1b); guard only |
| IP-P08 | `ACCOUNTANT_CONFIRMED` | `NO-FINDING` (register-only; eventual outcome class `HUMAN_CONFIRMATION_REQUIRED`) | Frozen as a rule; NO tax-classification-code path exists anywhere yet (no field, no confirmation flow) — register-only; implementation pending a later stage |
| IP-P09 | `ACCOUNTANT_CONFIRMED` | `ADVISORY` | `SUPPLIER_INVOICE_FOLLOW_UP_RECOMMENDED` (F1d): paid + no PURCHASE invoice → management follow-up; gone on recomputation once an invoice exists; no Task persisted |
| IP-X01 | `ACCOUNTANT_CONFIRMED` | `ADVISORY` when a frozen same-scope comparison detects deviation; otherwise `CONTEXT` | Customs declaration (`declared_amount`/`declared_currency`, F1c) is the management anchor for reviewing PURCHASE and SALES prep; F1f implements the IP-S02 SALES-side three-way comparison for the unambiguous 1:1:1 scope — same-currency amount deviation → `SALES_INVOICE_AMOUNT_DEVIATION`, explicit currency mismatch → `SALES_INVOICE_CURRENCY_DEVIATION` (both ADVISORY, never a conflict); ambiguous/missing scope stays `NOT_COMPARABLE_AMBIGUOUS_SCOPE`/`NOT_COMPARABLE_MISSING_FACT`, never a preparation blocker; no sum / no apportionment / no arbitrary selection; management control, never workflow eligibility |
