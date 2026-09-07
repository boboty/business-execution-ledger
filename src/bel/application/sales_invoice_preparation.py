"""SALES_INVOICE_PREPARATION rule foundation (Phase 2D.3-F1a, re-leveled
in Phase 2D.3-F1d).

Formally establishes the sales-direction preparation rule layer on top
of the F0 fact context. The rule layer consumes ONLY already-confirmed /
current Facts (via ``get_invoice_preparation_context``) and produces a
Decision per SalesContract scope — the Fact -> Decision layering is
preserved: this module never writes, never mutates Facts, and never
re-derives "current" semantics.

The Invoice Preparation Workbench is FACT CONTROL + MANAGEMENT
REMINDERS, NOT a workflow approval engine. SALES_INVOICE_PREPARATION is
NOT a process gate: it reports fact completeness and comparison
availability per scope; it never decides whether an invoice may be
issued.

Core Completion supersedes the historical F1f three-way numerical
equality with TWO INDEPENDENT management comparisons (a SalesContract in
USD and a SALES invoice in CNY are not equal amounts in the same
currency, so a single three-way equality is no longer meaningful):

- ``amount_check`` (``SalesInvoiceAmountCheck``) — the expected CNY
  invoice amount is ``SalesContract.gross_amount`` (USD) times the
  applicable SAFE USD/CNY rate for the explicit ``invoice_month``
  (latest confirmed publication on or before the month's natural first
  day; see ``bel.application.fx_rate_evidence``), compared against the
  confirmed SALES Invoice's own CNY amount when its currency is
  explicitly CNY. Outcomes: MATCH / DEVIATION /
  NOT_COMPARABLE_MISSING_FACT / NOT_COMPARABLE_CURRENCY_MISMATCH /
  NOT_COMPARABLE_AMBIGUOUS_SCOPE / NOT_COMPARABLE_FX_MISSING /
  NOT_COMPARABLE_FX_AMBIGUOUS.
- ``customs_check`` (``SalesCustomsAmountCheck``) — a SEPARATE,
  currency-safe comparison of ``SalesContract.gross_amount``/currency
  against the Shipment/Export ``declared_amount``/``declared_currency``
  on the current, unambiguous linked procurement Contract (IP-X01: the
  customs declaration remains a comparable management fact, never
  folded into the FX conversion). Outcomes: MATCH / DEVIATION /
  NOT_COMPARABLE_MISSING_FACT / NOT_COMPARABLE_CURRENCY_MISMATCH /
  NOT_COMPARABLE_AMBIGUOUS_SCOPE.

Neither check ever blocks invoice preparation or changes ``status``; a
DEVIATION on either emits its own NON-BLOCKING advisory
(``SALES_INVOICE_AMOUNT_DEVIATION`` / ``SALES_CONTRACT_CUSTOMS_AMOUNT_DEVIATION``),
and an explicit currency mismatch on either emits its own
(``SALES_INVOICE_CURRENCY_DEVIATION`` / ``SALES_CONTRACT_CUSTOMS_CURRENCY_DEVIATION``).
Multiple confirmed SALES invoices, multiple current ProcurementSalesLinks,
or multiple Shipment/Export declaration candidates make the relevant
check's scope ambiguous — no sum, no apportionment, no arbitrary
selection — and invoice preparation stays open regardless.

``invoice_quantity_check`` (``SalesInvoiceQuantityCheck``) is a third,
independent comparison: the confirmed SALES invoice's own item
quantity/unit against ``SalesContract.quantity``/``unit`` — never
summed across multiple invoices or multiple item lines, never mixing
units, and never substituting Shipment/procurement quantity. Expected
quantity always stays ``SalesContract.quantity`` regardless of this
check's outcome.

The three inputs of SALES_INVOICE_PREPARATION, in this order, are a
FACT-COMPLETENESS / COMPARISON-AVAILABILITY report — not eligibility
inputs:

1. ``SALES_CONTRACT``              — the scope's own SalesContract Fact.
   This is the genuinely-required sales-scope data. A sales scope exists
   only for an existing SalesContract anchor, so this input is present
   by construction; missing genuinely-required sales-scope data would be
   ``INSUFFICIENT_FACTS`` (where preparation data cannot be built),
   which the F0 construction makes unreachable today.
2. ``LINKED_PROCUREMENT_CONTRACT`` — the current linked procurement
   Contract(s) (via CURRENT ``ProcurementSalesLink``). This is a
   management/context linkage: the bridge is exposed as a fact, and a
   missing link only makes procurement-side comparison unavailable. It
   is NOT an eligibility blocker and never gates invoice preparation.
3. ``SHIPMENT_EXPORT_FACT``        — a Shipment/Export Fact on the
   linked procurement Contract (single-link case only). This is an
   export-management anchor: a missing Shipment makes the export/customs
   comparison unavailable (NOT "may not issue invoice"), and it is never
   an eligibility blocker.

Deliberately NOT implemented (each requires its own rule freeze):

- M:N shipment judgment — when a scope has MULTIPLE current links, no
  "any shipment" / "all linked contracts have shipments" decision is
  made: the shipment input is recorded ``NOT_JUDGED_UNDER_MN_UNRESOLVED``
  (an unresolved comparison, never a blocker). The system does not
  guess, and the M:N linked-contract facts stay visible.
- Aggregation across an ambiguous scope — under multiple confirmed
  SALES invoices, multiple current links, multiple Shipment/Export
  declaration candidates, or multiple item lines on a single confirmed
  invoice, none of ``amount_check`` / ``customs_check`` /
  ``invoice_quantity_check`` is performed: the scope is
  ``NOT_COMPARABLE_AMBIGUOUS_SCOPE`` (never a sum, never an
  apportionment, never an arbitrary selection).
- 应开金额 / 应开数量 — no should-invoice amount or quantity exists
  anywhere in this module.
- Receipt/payment triggering — no "receipt triggers invoice" or
  "已收多少开多少" logic. Invoice may precede or follow receipt/payment:
  both orderings are fine, and no chronology finding is emitted.
- The exact field set of the future 一致性校验 (consistency validation,
  "完全一致") — reserved, not defined (see below).
- Supplier-side preparation calculation — this module is
  sales-direction only.

``customer`` on every decision comes only from
``SalesContract.customer`` (the Domain's only expression of an external
customer). Whether a customer is known is surfaced as a fact, never
judged: customer presence is deliberately NOT one of the three inputs
and adds no finding here.

Consistency-validation reservation: ``SalesPreparationConsistencyCheckResult``
and ``SALES_INVOICE_CONSISTENCY_CHECK_NAMES`` reserve a pure
Application-layer seam for the future 一致性校验. The seam is deliberately
EMPTY — the exact compared field set is not frozen, and populating it is
a Phase 2D.3 rule freeze, not an implementation decision. No code path
produces a consistency-check result today — the IP-S02 amount
comparisons (``amount_check`` / ``customs_check``) are SEPARATE frozen
checks and do not use this seam.

Strictly read-only: evaluation is a pure function of the F0 context.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy.orm import Session

from bel.application.invoice_preparation import (
    ApplicableFxRateEvidence,
    InvoicePreparationContext,
    SalesScopeContext,
    SupplierScopeContext,
    get_invoice_preparation_context,
)
from bel.domain.invoice import InvoiceDirection

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


class SalesPreparationRequiredInput:
    """The THREE inputs of SALES_INVOICE_PREPARATION, frozen by the
    Phase 2D.3-F1a clarification and re-leveled in F1d. Exactly these
    three — adding a fourth (e.g. customer presence, receipt state)
    would be a new rule and requires its own freeze. Only the first is
    genuinely required (present by construction); the other two are
    management/context linkages whose absence makes a comparison
    unavailable, never an eligibility blocker."""

    SALES_CONTRACT = "SALES_CONTRACT"
    LINKED_PROCUREMENT_CONTRACT = "LINKED_PROCUREMENT_CONTRACT"
    SHIPMENT_EXPORT_FACT = "SHIPMENT_EXPORT_FACT"


REQUIRED_INPUT_ORDER: tuple[str, ...] = (
    SalesPreparationRequiredInput.SALES_CONTRACT,
    SalesPreparationRequiredInput.LINKED_PROCUREMENT_CONTRACT,
    SalesPreparationRequiredInput.SHIPMENT_EXPORT_FACT,
)


class SalesPreparationDecisionStatus:
    """Fact-completeness vocabulary, deliberately NOT an eligibility
    vocabulary: there is no READY / NOT_READY / BLOCKED member. A status
    here says whether the genuinely-required sales-scope data (the
    SalesContract) is present, nothing more. Under the F0 construction
    that data is present by construction, so the status is
    ``INPUTS_PRESENT`` in every reachable state; ``INSUFFICIENT_FACTS``
    is reserved for a genuinely-required sales-scope Fact being missing —
    where preparation data cannot be built — and is unreachable today
    exactly as the schema-backstopped supplier amount path is."""

    INPUTS_PRESENT = "INPUTS_PRESENT"
    INSUFFICIENT_FACTS = "INSUFFICIENT_FACTS"


class SalesPreparationBlockerCode:
    """Reserved blocker vocabulary. The current sales rule set emits NO
    blocker: the only genuinely-required input (the SalesContract) is
    present by construction, and the link / shipment inputs are
    management/context linkages whose absence only makes a comparison
    unavailable. A genuinely-required sales-scope blocker — or one from
    the future consistency-validation seam — would extend this class
    under a rule freeze."""


# RESERVED for the future 一致性校验 (consistency validation) — pure
# Application-layer seam. Deliberately EMPTY: the exact compared field
# set ("完全一致") is not frozen, and defining it is a Phase 2D.3 rule
# freeze, not an implementation decision. No code path produces a
# check result today.
SALES_INVOICE_CONSISTENCY_CHECK_NAMES: tuple[str, ...] = ()


@dataclass(frozen=True)
class SalesPreparationConsistencyCheckResult:
    """Reserved result shape for the future 一致性校验. Never produced
    by any code path yet — see ``SALES_INVOICE_CONSISTENCY_CHECK_NAMES``."""

    check_name: str
    sales_contract_id: uuid.UUID
    passed: bool | None


class SalesInvoiceAdvisoryCode:
    """Explicit NON-BLOCKING management finding codes. An advisory records
    a frozen rule consequence that is a management reminder / review
    signal — a legitimate business state worth a review. It never drives
    ``status``, never blocks invoice preparation, and is recomputed from
    current Facts on every evaluation."""

    # amount_check (FX-derived CNY vs actual invoice) deviation: the
    # actual CNY invoice amount differs from the FX-derived expectation.
    # The Invoice Fact stays valid; a management review signal, never a
    # RULE_CONFLICT.
    SALES_INVOICE_AMOUNT_DEVIATION = "SALES_INVOICE_AMOUNT_DEVIATION"
    # amount_check currency deviation: a confirmed SALES invoice exists
    # but its explicit currency is not CNY — no implicit FX is performed.
    # A management review signal, never a conflict.
    SALES_INVOICE_CURRENCY_DEVIATION = "SALES_INVOICE_CURRENCY_DEVIATION"
    # invoice_quantity_check deviation: the confirmed SALES invoice's own
    # item quantity differs from SalesContract.quantity (same explicit
    # unit). Expected quantity is unchanged by this — SalesContract.quantity
    # stays authoritative.
    SALES_INVOICE_QUANTITY_DEVIATION = "SALES_INVOICE_QUANTITY_DEVIATION"
    # customs_check (SalesContract vs Shipment/Export declared amount)
    # deviation: same explicit currency, amounts not equal. IP-X01 — a
    # separate comparable management fact, never folded into the FX
    # conversion.
    SALES_CONTRACT_CUSTOMS_AMOUNT_DEVIATION = "SALES_CONTRACT_CUSTOMS_AMOUNT_DEVIATION"
    # customs_check currency deviation: the SalesContract and declared
    # currencies are both explicit but differ — no amount comparison is
    # attempted, no FX.
    SALES_CONTRACT_CUSTOMS_CURRENCY_DEVIATION = "SALES_CONTRACT_CUSTOMS_CURRENCY_DEVIATION"


# The sales advisory codes, defined once — exhaustive over
# SalesInvoiceAdvisoryCode's members (enforced by test). Advisories never
# participate in the status derivation (the sales blocker class is
# empty), and NOT_COMPARABLE_MISSING_FACT / NOT_COMPARABLE_AMBIGUOUS_SCOPE
# emit NO advisory and NO blocker.
NON_BLOCKING_ADVISORY_CODES: frozenset[str] = frozenset(
    {
        SalesInvoiceAdvisoryCode.SALES_INVOICE_AMOUNT_DEVIATION,
        SalesInvoiceAdvisoryCode.SALES_INVOICE_CURRENCY_DEVIATION,
        SalesInvoiceAdvisoryCode.SALES_INVOICE_QUANTITY_DEVIATION,
        SalesInvoiceAdvisoryCode.SALES_CONTRACT_CUSTOMS_AMOUNT_DEVIATION,
        SalesInvoiceAdvisoryCode.SALES_CONTRACT_CUSTOMS_CURRENCY_DEVIATION,
    }
)


class SalesAmountCheckOutcome:
    """Shared comparison-outcome vocabulary for ``amount_check`` and
    ``customs_check`` — two INDEPENDENT comparisons (see module
    docstring), each exact, never tolerant, never a business judgment,
    never an eligibility statement:

    - MATCH — the compared currencies are explicit and equal, the
      compared amounts exactly equal;
    - DEVIATION — the compared currencies are explicit and equal, the
      amounts are not equal -> the check's own DEVIATION advisory;
    - NOT_COMPARABLE_MISSING_FACT — a compared amount/currency Fact (or
      the invoice/declaration scope itself) is absent;
    - NOT_COMPARABLE_CURRENCY_MISMATCH — the compared explicit currencies
      differ -> the check's own CURRENCY_DEVIATION advisory (no amount
      comparison is attempted, no FX);
    - NOT_COMPARABLE_AMBIGUOUS_SCOPE — the scope is ambiguous by
      cardinality (multiple confirmed SALES invoices for amount_check;
      multiple current links or multiple Shipment/Export declaration
      candidates for customs_check): cardinality ambiguity takes
      precedence over selecting arbitrary facts, no sum / no
      apportionment;
    - NOT_COMPARABLE_FX_MISSING / NOT_COMPARABLE_FX_AMBIGUOUS —
      ``amount_check`` only: no explicit ``invoice_month``, no valid
      confirmed SAFE rate on or before the month's natural first day, or
      more than one valid rate on the applicable publication date.

    NOT_COMPARABLE_* are check results ONLY — never a blocker, never a
    status change: an unavailable management comparison never forbids
    invoice preparation."""

    MATCH = "MATCH"
    DEVIATION = "DEVIATION"
    NOT_COMPARABLE_MISSING_FACT = "NOT_COMPARABLE_MISSING_FACT"
    NOT_COMPARABLE_CURRENCY_MISMATCH = "NOT_COMPARABLE_CURRENCY_MISMATCH"
    NOT_COMPARABLE_AMBIGUOUS_SCOPE = "NOT_COMPARABLE_AMBIGUOUS_SCOPE"
    NOT_COMPARABLE_FX_MISSING = "NOT_COMPARABLE_FX_MISSING"
    NOT_COMPARABLE_FX_AMBIGUOUS = "NOT_COMPARABLE_FX_AMBIGUOUS"


# amount_check: SalesContract USD amount x applicable SAFE rate vs the
# confirmed SALES invoice's own CNY amount.
SALES_AMOUNT_CONSISTENCY_CHECK_NAME = "SALES_CONTRACT_USD_AMOUNT_VS_FX_EXPECTED_CNY_VS_SALES_INVOICE_GROSS_AMOUNT"
# customs_check: a SEPARATE, currency-safe comparison — never folded into
# the FX conversion above (IP-X01).
SALES_CUSTOMS_CONSISTENCY_CHECK_NAME = "SALES_CONTRACT_GROSS_AMOUNT_VS_DECLARED_AMOUNT"
# invoice_quantity_check: the confirmed SALES invoice's own item quantity
# vs SalesContract.quantity.
SALES_INVOICE_QUANTITY_CHECK_NAME = "SALES_CONTRACT_QUANTITY_VS_SALES_INVOICE_ITEM_QUANTITY"


# ---------------------------------------------------------------------------
# Decision DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SalesPreparationRequiredInputState:
    """One input's evaluation outcome, with the Fact ids that satisfy
    it — the Fact -> Decision trace. ``present`` states fact
    completeness / comparison availability for that input; it is NOT an
    eligibility statement."""

    name: str
    present: bool
    source_fact_ids: tuple[uuid.UUID, ...] = ()
    # Why an input's comparison is unavailable (e.g.
    # PROCUREMENT_COMPARISON_UNAVAILABLE / EXPORT_COMPARISON_UNAVAILABLE /
    # NOT_JUDGED_UNDER_MN_UNRESOLVED). Never a business judgment — a
    # statement about what this rule did not evaluate.
    note: str | None = None


@dataclass(frozen=True)
class SalesPreparationBlocker:
    """Reserved blocker shape for the genuinely-required sales-scope
    data (the SalesContract) and the future consistency-validation seam.
    No current code path emits one — the genuinely-required input is
    present by construction."""

    code: str
    related_sales_contract_id: uuid.UUID
    # Procurement Contract ids the blocker is about. Enumeration only —
    # never an amount/quantity.
    related_contract_ids: tuple[uuid.UUID, ...] = ()
    note: str | None = None


@dataclass(frozen=True)
class SalesInvoiceAdvisory:
    """One explicit NON-BLOCKING management finding on a SalesContract
    scope (Phase 2D.3-F1f). An advisory records a frozen rule consequence
    that is a management reminder / review signal. Advisories NEVER affect
    the decision ``status`` — status is a function of blockers alone (the
    sales blocker class is empty) — and are recomputed from current Facts
    on every evaluation."""

    code: str
    # The SalesContract scope the advisory is emitted on.
    sales_contract_id: uuid.UUID
    # SALES invoice ids the advisory is about (the confirmed Invoice Fact
    # whose amount/currency deviates).
    related_invoice_ids: tuple[uuid.UUID, ...] = ()
    # Shipment/Export Fact ids the advisory is about (the confirmed
    # declaration anchor).
    related_shipment_ids: tuple[uuid.UUID, ...] = ()
    note: str | None = None


@dataclass(frozen=True)
class SalesInvoiceAmountCheck:
    """The FX-based amount comparison result — SalesContract USD gross
    amount x the applicable SAFE rate vs the confirmed SALES Invoice's
    own CNY gross amount, for ONE SalesContract scope. INDEPENDENT of
    ``SalesCustomsAmountCheck`` (see module docstring). ``None``
    amount/currency values mean the Fact was absent; ``sales_invoice_id``
    is ``None`` exactly when no single candidate was resolved (missing
    scope, or ambiguous scope where no sum / no arbitrary selection is
    performed)."""

    check_name: str
    sales_contract_id: uuid.UUID
    sales_contract_amount: Decimal | None
    sales_contract_currency: str | None
    sales_invoice_amount: Decimal | None
    sales_invoice_currency: str | None
    # The single resolved confirmed SALES Invoice Fact — None when missing
    # or ambiguous (no sum, no newest, no arbitrary choice).
    sales_invoice_id: uuid.UUID | None
    outcome: str
    note: str | None = None
    expected_invoice_cny: Decimal | None = None
    applicable_fx_rate: Decimal | None = None
    applicable_fx_date: object | None = None
    fx_provenance_fragment_id: uuid.UUID | None = None


@dataclass(frozen=True)
class SalesCustomsAmountCheck:
    """IP-X01 — a SEPARATE, currency-safe comparison of SalesContract
    gross amount vs the Shipment/Export declared amount, for ONE
    SalesContract scope. Never folded into the FX conversion
    (``SalesInvoiceAmountCheck``): the customs declaration remains a
    comparable management fact in its own right. ``shipment_id`` is
    ``None`` exactly when no single candidate was resolved (missing
    scope, or ambiguous scope where no sum / no arbitrary selection is
    performed)."""

    check_name: str
    sales_contract_id: uuid.UUID
    sales_contract_amount: Decimal | None
    sales_contract_currency: str | None
    declared_amount: Decimal | None
    declared_currency: str | None
    shipment_id: uuid.UUID | None
    outcome: str
    note: str | None = None


@dataclass(frozen=True)
class SalesQuantityCheck:
    expected_quantity: Decimal | None
    expected_unit: str | None
    shipment_quantity: Decimal | None
    shipment_unit: str | None
    outcome: str
    shipment_id: uuid.UUID | None = None


@dataclass(frozen=True)
class SalesInvoiceQuantityCheck:
    """The confirmed SALES invoice's own item quantity/unit vs
    SalesContract.quantity/unit, for ONE SalesContract scope. Never
    aggregated across multiple confirmed invoices or multiple item lines
    on one invoice, never mixing units, never substituted by Shipment or
    procurement quantity. Expected quantity is unaffected by this check's
    outcome — SalesContract.quantity stays authoritative regardless."""

    sales_contract_id: uuid.UUID
    expected_quantity: Decimal | None
    expected_unit: str | None
    actual_quantity: Decimal | None
    actual_unit: str | None
    sales_invoice_id: uuid.UUID | None
    outcome: str
    note: str | None = None


@dataclass(frozen=True)
class SalesInvoicePreparationDecision:
    """One SalesContract scope's preparation-rule Decision. Carries facts,
    required-input outcomes, the independent comparison results and their
    non-blocking advisories — no readiness/eligibility field and no
    blocker in any reachable state (the three inputs report comparison
    availability, not gates). Every ``*_check`` is a MANAGEMENT
    comparison, never a workflow gate: a NOT_COMPARABLE_* outcome never
    forbids invoice preparation."""

    sales_contract_id: uuid.UUID
    sales_contract_no: str
    # The external customer — only from SalesContract.customer. None
    # stays None (unknown); customer presence is NOT judged by this rule.
    customer: str | None
    status: str
    required_inputs: tuple[SalesPreparationRequiredInputState, ...]
    blockers: tuple[SalesPreparationBlocker, ...] = ()
    # Reserved seam — always empty today (see module docstring).
    consistency_checks: tuple[SalesPreparationConsistencyCheckResult, ...] = field(default=())
    # FX-based amount comparison — always present in every reachable
    # state (every sales scope has a SalesContract).
    amount_check: SalesInvoiceAmountCheck | None = None
    # IP-X01 customs declaration comparison — INDEPENDENT of amount_check.
    customs_check: SalesCustomsAmountCheck | None = None
    # Actual SALES invoice quantity vs SalesContract.quantity.
    invoice_quantity_check: SalesInvoiceQuantityCheck | None = None
    # Explicit NON-BLOCKING management findings from any of the checks
    # above. Never affect ``status`` — status is derived from blockers
    # alone.
    advisories: tuple[SalesInvoiceAdvisory, ...] = ()
    expected_quantity: Decimal | None = None
    expected_unit: str | None = None
    contract_usd_amount: Decimal | None = None
    invoice_note_data: "SalesInvoiceNoteData | None" = None
    quantity_check: SalesQuantityCheck | None = None


@dataclass(frozen=True)
class SalesInvoiceNoteData:
    """Structured downstream data; presentation owns wording/formatting."""
    contract_usd_amount: Decimal
    applicable_fx_rate: Decimal
    applicable_fx_date: object
    expected_invoice_cny: Decimal


@dataclass(frozen=True)
class SalesInvoicePreparationReport:
    decisions: tuple[SalesInvoicePreparationDecision, ...]


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def evaluate_sales_invoice_preparation(
    session: Session, *, invoice_month=None,
    fx_provenance_fragment_ids: tuple[uuid.UUID, ...] = (),
) -> SalesInvoicePreparationReport:
    """Evaluate the SALES_INVOICE_PREPARATION rule foundation over the
    complete F0 fact context (unfiltered — a scope's shipment resolution
    must never be blinded by an axis filter). Strictly read-only. FX
    input is a list of confirmed Evidence fragment ids only — see
    ``get_invoice_preparation_context``."""
    context = get_invoice_preparation_context(
        session, invoice_month=invoice_month, fx_provenance_fragment_ids=fx_provenance_fragment_ids
    )
    return evaluate_sales_invoice_preparation_from_context(context)


def evaluate_sales_invoice_preparation_from_context(
    context: InvoicePreparationContext,
) -> SalesInvoicePreparationReport:
    """Pure decision function over the F0 context — no session, no I/O,
    no mutation. This is the reserved Application-layer seam the future
    consistency validation will compose with."""
    supplier_scope_by_contract_id = {scope.contract.id: scope for scope in context.supplier_scopes}
    return SalesInvoicePreparationReport(
        decisions=tuple(
            _evaluate_scope(
                scope, supplier_scope_by_contract_id, context.invoice_month, context.fx_rate_evidence
            ) for scope in context.sales_scopes
        )
    )


def _evaluate_scope(
    scope: SalesScopeContext,
    supplier_scope_by_contract_id: dict[uuid.UUID, object],
    invoice_month,
    fx_rate_evidence: tuple[ApplicableFxRateEvidence, ...],
) -> SalesInvoicePreparationDecision:
    sales_contract = scope.sales_contract
    blockers: list[SalesPreparationBlocker] = []

    # Input 1 — the scope's own SalesContract Fact, the genuinely-
    # required sales-scope data. Present by construction (a sales scope
    # exists only for an existing SalesContract anchor); asserted, not
    # assumed silently. Missing genuinely-required sales-scope data
    # would be INSUFFICIENT_FACTS — unreachable under the F0
    # construction.
    sales_contract_input = SalesPreparationRequiredInputState(
        name=SalesPreparationRequiredInput.SALES_CONTRACT,
        present=True,
        source_fact_ids=(sales_contract.id,),
    )

    # Input 2 — the current linked procurement Contract(s). This is a
    # management/context linkage: the bridge is exposed as a fact
    # (every current link's procurement Contract id stays visible, so
    # M:N facts are never hidden), and a missing link only makes
    # procurement-side comparison unavailable. It is NOT an eligibility
    # blocker, so no blocker is emitted.
    all_link_contract_ids = tuple(
        entry.link.procurement_contract_id for entry in scope.linked_procurement_contracts
    )
    resolved = [entry.contract for entry in scope.linked_procurement_contracts if entry.contract is not None]
    link_input = SalesPreparationRequiredInputState(
        name=SalesPreparationRequiredInput.LINKED_PROCUREMENT_CONTRACT,
        present=bool(resolved),
        source_fact_ids=all_link_contract_ids,
        note=(
            None
            if resolved
            else (
                "PROCUREMENT_COMPARISON_UNAVAILABLE"
                if all_link_contract_ids
                else "PROCUREMENT_COMPARISON_UNAVAILABLE_NO_LINK"
            )
        ),
    )

    # Input 3 — Shipment/Export Fact, the export-management anchor.
    # Judged ONLY in the single-link case: under M:N the any/all
    # shipment rule is NOT frozen, and this module does not guess — the
    # input is recorded as an unresolved comparison
    # (NOT_JUDGED_UNDER_MN_UNRESOLVED), never a blocker (deliberate F1a
    # boundary, re-leveled in F1d). A missing shipment only makes the
    # export/customs comparison unavailable; it is never "may not issue
    # invoice".
    if len(scope.linked_procurement_contracts) > 1:
        shipment_input = SalesPreparationRequiredInputState(
            name=SalesPreparationRequiredInput.SHIPMENT_EXPORT_FACT,
            present=False,
            source_fact_ids=(),
            note="NOT_JUDGED_UNDER_MN_UNRESOLVED",
        )
    elif resolved:
        linked_contract = resolved[0]
        supplier_scope = supplier_scope_by_contract_id.get(linked_contract.id)
        shipment_ids = (
            tuple(shipment.id for shipment in supplier_scope.shipments) if supplier_scope is not None else ()
        )
        shipment_input = SalesPreparationRequiredInputState(
            name=SalesPreparationRequiredInput.SHIPMENT_EXPORT_FACT,
            present=bool(shipment_ids),
            source_fact_ids=shipment_ids,
            note=None if shipment_ids else "EXPORT_COMPARISON_UNAVAILABLE",
        )
    else:
        # No linked contract to check shipments on — the link input above
        # already states the missing linkage; no separate shipment claim
        # is made (nothing was checked, so nothing is claimed).
        shipment_input = SalesPreparationRequiredInputState(
            name=SalesPreparationRequiredInput.SHIPMENT_EXPORT_FACT,
            present=False,
            source_fact_ids=(),
            note="NOT_JUDGED_NO_LINKED_CONTRACT",
        )

    required_inputs = (
        sales_contract_input,
        link_input,
        shipment_input,
    )
    assert [ri.name for ri in required_inputs] == list(REQUIRED_INPUT_ORDER)

    # Status: derived from blockers alone. No blocker is emitted by the
    # current rule set (the genuinely-required SalesContract is present
    # by construction), so the status is INPUTS_PRESENT in every
    # reachable state — a statement about fact completeness ONLY, never
    # an eligibility or readiness Decision.

    # Three INDEPENDENT management comparisons (see module docstring).
    # None ever changes status and none ever blocks invoice preparation.
    invoice, invoice_items, invoice_ambiguous = _resolve_confirmed_sales_invoice(scope)
    amount_check, amount_advisories = _evaluate_amount_check(
        scope, invoice, invoice_ambiguous, invoice_month, fx_rate_evidence
    )
    customs_check, customs_advisories = _evaluate_customs_declaration_check(scope, supplier_scope_by_contract_id)
    invoice_quantity_check, quantity_advisories = _evaluate_sales_invoice_quantity_check(
        scope, invoice, invoice_items, invoice_ambiguous
    )
    note_data = None
    if amount_check.expected_invoice_cny is not None and amount_check.applicable_fx_rate is not None:
        note_data = SalesInvoiceNoteData(
            contract_usd_amount=sales_contract.gross_amount,
            applicable_fx_rate=amount_check.applicable_fx_rate,
            applicable_fx_date=amount_check.applicable_fx_date,
            expected_invoice_cny=amount_check.expected_invoice_cny,
        )

    return SalesInvoicePreparationDecision(
        sales_contract_id=sales_contract.id,
        sales_contract_no=sales_contract.sales_contract_no,
        customer=sales_contract.customer,
        status=(
            SalesPreparationDecisionStatus.INPUTS_PRESENT
            if not blockers
            else SalesPreparationDecisionStatus.INSUFFICIENT_FACTS
        ),
        required_inputs=required_inputs,
        blockers=tuple(blockers),
        amount_check=amount_check,
        customs_check=customs_check,
        invoice_quantity_check=invoice_quantity_check,
        advisories=tuple(amount_advisories) + tuple(customs_advisories) + tuple(quantity_advisories),
        expected_quantity=sales_contract.quantity,
        expected_unit=sales_contract.unit,
        contract_usd_amount=sales_contract.gross_amount if sales_contract.currency == "USD" else None,
        invoice_note_data=note_data,
        quantity_check=_quantity_check(scope, supplier_scope_by_contract_id),
    )


def _resolve_confirmed_sales_invoice(
    scope: SalesScopeContext,
) -> tuple[object | None, tuple[object, ...], bool]:
    """The invoice-leg resolution shared by ``amount_check`` and
    ``invoice_quantity_check``: exactly ONE confirmed SALES Invoice Fact
    is usable. An allocation record whose Invoice Fact is missing or not
    direction SALES is NOT a confirmed Invoice Fact. More than one
    confirmed SALES Invoice Fact is ambiguous (no sum, no newest, no
    arbitrary choice); zero is simply absent. Returns
    ``(invoice, invoice_items, ambiguous)`` — ``invoice_items`` is that
    invoice's own item Facts (from the SAME context entry, never
    re-fetched), empty when ``invoice`` is ``None``."""
    confirmed_sales_invoice_ids: list[uuid.UUID] = []
    for entry in scope.invoice_allocations:
        if (
            entry.invoice is not None
            and entry.invoice.direction == InvoiceDirection.SALES
            and entry.invoice.id not in confirmed_sales_invoice_ids
        ):
            confirmed_sales_invoice_ids.append(entry.invoice.id)

    ambiguous = len(confirmed_sales_invoice_ids) > 1
    if ambiguous or not confirmed_sales_invoice_ids:
        return None, (), ambiguous
    entry = next(
        (
            entry
            for entry in scope.invoice_allocations
            if entry.invoice is not None and entry.invoice.id == confirmed_sales_invoice_ids[0]
        ),
        None,
    )
    if entry is None:
        return None, (), ambiguous
    return entry.invoice, entry.invoice_items, ambiguous


def _amount_check(
    scope: SalesScopeContext,
    *,
    invoice: object | None,
    sales_invoice_amount: Decimal | None,
    sales_invoice_currency: str | None,
    outcome: str,
    note: str | None,
    expected_invoice_cny: Decimal | None = None,
    applicable_fx_rate: Decimal | None = None,
    applicable_fx_date=None,
    fx_provenance_fragment_id: uuid.UUID | None = None,
) -> SalesInvoiceAmountCheck:
    """Build one FX-based amount check. The SalesContract leg is always
    present (every sales scope has one); the invoice leg is ``None``-filled
    exactly when no single candidate was resolved — no hidden assumption
    and no arbitrary choice is ever smuggled into the check."""
    sales_contract = scope.sales_contract
    return SalesInvoiceAmountCheck(
        check_name=SALES_AMOUNT_CONSISTENCY_CHECK_NAME,
        sales_contract_id=sales_contract.id,
        sales_contract_amount=sales_contract.gross_amount,
        sales_contract_currency=sales_contract.currency,
        sales_invoice_amount=sales_invoice_amount,
        sales_invoice_currency=sales_invoice_currency,
        sales_invoice_id=invoice.id if invoice is not None else None,
        outcome=outcome,
        note=note,
        expected_invoice_cny=expected_invoice_cny,
        applicable_fx_rate=applicable_fx_rate,
        applicable_fx_date=applicable_fx_date,
        fx_provenance_fragment_id=fx_provenance_fragment_id,
    )


def _evaluate_amount_check(
    scope: SalesScopeContext,
    sales_invoice: object | None,
    invoice_ambiguous: bool,
    invoice_month,
    fx_rate_evidence: tuple[ApplicableFxRateEvidence, ...],
) -> tuple[SalesInvoiceAmountCheck, tuple[SalesInvoiceAdvisory, ...]]:
    """The FX-based sales amount comparison (SalesContract USD gross
    amount x applicable SAFE rate vs the confirmed SALES Invoice's own
    CNY gross amount), for ONE SalesContract scope.

    This is a MANAGEMENT comparison, NOT a workflow gate and never a
    ``RULE_CONFLICT``, and is INDEPENDENT of ``customs_check`` (see
    module docstring — the historical F1f three-way equality is
    superseded). Every ambiguity / missing-Fact outcome is recorded on
    the check and NEVER blocks invoice preparation.

    The expected CNY amount is never inferred from a shipment, a
    purchase value, wall-clock time, or a default rate: an explicit
    ``invoice_month`` and a confirmed, provenance-backed SAFE rate on or
    before the month's natural first day are required. An actual invoice
    is compared with the derived CNY expectation only when its currency
    is explicitly CNY — a different explicit currency is never converted
    implicitly."""
    sales_contract = scope.sales_contract
    advisories: list[SalesInvoiceAdvisory] = []

    fx, fx_outcome, fx_note = _select_applicable_fx(invoice_month, fx_rate_evidence)
    if sales_contract.currency != "USD" or sales_contract.gross_amount is None:
        fx, fx_outcome, fx_note = None, SalesAmountCheckOutcome.NOT_COMPARABLE_MISSING_FACT, (
            "SalesContract USD amount/currency Fact is absent — no conversion"
        )
    if fx is None:
        return _amount_check(
            scope, invoice=sales_invoice,
            sales_invoice_amount=sales_invoice.gross_amount if sales_invoice is not None else None,
            sales_invoice_currency=sales_invoice.currency if sales_invoice is not None else None,
            outcome=fx_outcome, note=fx_note,
        ), tuple(advisories)
    expected_cny = (sales_contract.gross_amount * fx.usd_cny_rate).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    if invoice_ambiguous:
        return _amount_check(
            scope, invoice=None, sales_invoice_amount=None, sales_invoice_currency=None,
            outcome=SalesAmountCheckOutcome.NOT_COMPARABLE_AMBIGUOUS_SCOPE,
            note="multiple confirmed SALES Invoice Facts — no sum / no arbitrary choice",
            expected_invoice_cny=expected_cny, applicable_fx_rate=fx.usd_cny_rate,
            applicable_fx_date=fx.publication_date, fx_provenance_fragment_id=fx.provenance_fragment_id,
        ), tuple(advisories)
    if sales_invoice is None or sales_invoice.gross_amount is None or sales_invoice.currency is None:
        return _amount_check(
            scope, invoice=sales_invoice,
            sales_invoice_amount=sales_invoice.gross_amount if sales_invoice is not None else None,
            sales_invoice_currency=sales_invoice.currency if sales_invoice is not None else None,
            outcome=SalesAmountCheckOutcome.NOT_COMPARABLE_MISSING_FACT,
            note="confirmed SALES invoice amount/currency Fact is absent",
            expected_invoice_cny=expected_cny, applicable_fx_rate=fx.usd_cny_rate,
            applicable_fx_date=fx.publication_date, fx_provenance_fragment_id=fx.provenance_fragment_id,
        ), tuple(advisories)
    if sales_invoice.currency != "CNY":
        advisories.append(SalesInvoiceAdvisory(
            code=SalesInvoiceAdvisoryCode.SALES_INVOICE_CURRENCY_DEVIATION,
            sales_contract_id=sales_contract.id, related_invoice_ids=(sales_invoice.id,),
            note="actual SALES invoice explicit currency is not CNY — no implicit FX, management review",
        ))
        return _amount_check(
            scope, invoice=sales_invoice, sales_invoice_amount=sales_invoice.gross_amount,
            sales_invoice_currency=sales_invoice.currency,
            outcome=SalesAmountCheckOutcome.NOT_COMPARABLE_CURRENCY_MISMATCH,
            note="actual SALES invoice is not explicitly CNY — no implicit FX",
            expected_invoice_cny=expected_cny, applicable_fx_rate=fx.usd_cny_rate,
            applicable_fx_date=fx.publication_date, fx_provenance_fragment_id=fx.provenance_fragment_id,
        ), tuple(advisories)
    actual_outcome = SalesAmountCheckOutcome.MATCH if sales_invoice.gross_amount == expected_cny else SalesAmountCheckOutcome.DEVIATION
    if actual_outcome == SalesAmountCheckOutcome.DEVIATION:
        advisories.append(SalesInvoiceAdvisory(
            code=SalesInvoiceAdvisoryCode.SALES_INVOICE_AMOUNT_DEVIATION,
            sales_contract_id=sales_contract.id, related_invoice_ids=(sales_invoice.id,),
            note="SALES invoice CNY gross amount deviates from the FX-derived CNY expectation",
        ))
    return _amount_check(
        scope, invoice=sales_invoice, sales_invoice_amount=sales_invoice.gross_amount,
        sales_invoice_currency=sales_invoice.currency, outcome=actual_outcome, note=None,
        expected_invoice_cny=expected_cny, applicable_fx_rate=fx.usd_cny_rate,
        applicable_fx_date=fx.publication_date, fx_provenance_fragment_id=fx.provenance_fragment_id,
    ), tuple(advisories)


def _customs_check(
    scope: SalesScopeContext,
    *,
    shipment: object | None,
    declared_amount: Decimal | None,
    declared_currency: str | None,
    outcome: str,
    note: str | None,
) -> SalesCustomsAmountCheck:
    """Build one IP-X01 customs-declaration check. The SalesContract leg
    is always present; the declaration leg is ``None``-filled exactly
    when no single candidate was resolved."""
    sales_contract = scope.sales_contract
    return SalesCustomsAmountCheck(
        check_name=SALES_CUSTOMS_CONSISTENCY_CHECK_NAME,
        sales_contract_id=sales_contract.id,
        sales_contract_amount=sales_contract.gross_amount,
        sales_contract_currency=sales_contract.currency,
        declared_amount=declared_amount,
        declared_currency=declared_currency,
        shipment_id=shipment.id if shipment is not None else None,
        outcome=outcome,
        note=note,
    )


def _evaluate_customs_declaration_check(
    scope: SalesScopeContext,
    supplier_scope_by_contract_id: dict[uuid.UUID, SupplierScopeContext],
) -> tuple[SalesCustomsAmountCheck, tuple[SalesInvoiceAdvisory, ...]]:
    """IP-X01 — a SEPARATE, currency-safe comparison of SalesContract
    gross amount vs the Shipment/Export declared amount, for ONE
    SalesContract scope. A MANAGEMENT comparison, NOT a workflow gate;
    NEVER folded into the FX conversion (``amount_check``) — the customs
    declaration stays a comparable management fact in its own right.

    Declaration leg — exactly ONE current ProcurementSalesLink AND
    exactly ONE current Shipment on that linked Contract. Zero links /
    zero Shipments (or the sole link naming no existing Contract Fact) is
    NOT_COMPARABLE_MISSING_FACT; multiple links / multiple Shipments is
    NOT_COMPARABLE_AMBIGUOUS_SCOPE (no sum of declaration amounts, no
    arbitrary choice). Amounts are compared ONLY when both amounts AND
    both currencies exist and the two currencies are explicitly equal —
    no FX, no default currency, no implicit same-currency assumption."""
    sales_contract = scope.sales_contract
    advisories: list[SalesInvoiceAdvisory] = []

    shipment = None
    ambiguous = False
    note: str | None
    link_entries = scope.linked_procurement_contracts
    if not link_entries:
        note = "no current ProcurementSalesLink (IP-X01)"
    elif len(link_entries) > 1:
        ambiguous = True
        note = "multiple current ProcurementSalesLinks — no arbitrary choice (IP-X01)"
    else:
        linked_contract = link_entries[0].contract
        if linked_contract is None:
            note = "the sole current link names no existing procurement Contract Fact (IP-X01)"
        else:
            supplier_scope = supplier_scope_by_contract_id.get(linked_contract.id)
            shipments = supplier_scope.shipments if supplier_scope is not None else ()
            if not shipments:
                note = "no current Shipment/Export Fact on the linked Contract (IP-X01)"
            elif len(shipments) > 1:
                ambiguous = True
                note = "multiple current Shipment/Export Facts — no sum / no arbitrary choice (IP-X01)"
            else:
                shipment = shipments[0]
                note = None

    if ambiguous:
        return _customs_check(
            scope, shipment=None, declared_amount=None, declared_currency=None,
            outcome=SalesAmountCheckOutcome.NOT_COMPARABLE_AMBIGUOUS_SCOPE, note=note,
        ), tuple(advisories)
    if shipment is None:
        return _customs_check(
            scope, shipment=None, declared_amount=None, declared_currency=None,
            outcome=SalesAmountCheckOutcome.NOT_COMPARABLE_MISSING_FACT, note=note,
        ), tuple(advisories)

    sc_amount, sc_currency = sales_contract.gross_amount, sales_contract.currency
    dec_amount, dec_currency = shipment.declared_amount, shipment.declared_currency
    if None in (sc_amount, sc_currency, dec_amount, dec_currency):
        return _customs_check(
            scope, shipment=shipment, declared_amount=dec_amount, declared_currency=dec_currency,
            outcome=SalesAmountCheckOutcome.NOT_COMPARABLE_MISSING_FACT,
            note="a compared amount/currency Fact is absent — no implicit currency, no comparison (IP-X01)",
        ), tuple(advisories)
    if sc_currency != dec_currency:
        advisories.append(SalesInvoiceAdvisory(
            code=SalesInvoiceAdvisoryCode.SALES_CONTRACT_CUSTOMS_CURRENCY_DEVIATION,
            sales_contract_id=sales_contract.id, related_shipment_ids=(shipment.id,),
            note="SalesContract / declaration explicit currencies differ — no FX, no amount comparison (IP-X01)",
        ))
        return _customs_check(
            scope, shipment=shipment, declared_amount=dec_amount, declared_currency=dec_currency,
            outcome=SalesAmountCheckOutcome.NOT_COMPARABLE_CURRENCY_MISMATCH,
            note="explicit currencies differ — no FX, no amount comparison (IP-X01)",
        ), tuple(advisories)
    outcome = SalesAmountCheckOutcome.MATCH if sc_amount == dec_amount else SalesAmountCheckOutcome.DEVIATION
    if outcome == SalesAmountCheckOutcome.DEVIATION:
        advisories.append(SalesInvoiceAdvisory(
            code=SalesInvoiceAdvisoryCode.SALES_CONTRACT_CUSTOMS_AMOUNT_DEVIATION,
            sales_contract_id=sales_contract.id, related_shipment_ids=(shipment.id,),
            note="declared amount deviates from the SalesContract gross amount — management review (IP-X01)",
        ))
    return _customs_check(
        scope, shipment=shipment, declared_amount=dec_amount, declared_currency=dec_currency,
        outcome=outcome, note=None,
    ), tuple(advisories)


def _evaluate_sales_invoice_quantity_check(
    scope: SalesScopeContext,
    sales_invoice: object | None,
    invoice_items: tuple[object, ...],
    invoice_ambiguous: bool,
) -> tuple[SalesInvoiceQuantityCheck, tuple[SalesInvoiceAdvisory, ...]]:
    """The confirmed SALES invoice's own item quantity/unit vs
    SalesContract.quantity/unit. Never aggregated across multiple
    confirmed invoices (ambiguous scope) or multiple item lines on one
    invoice (also ambiguous — mixing scope/unit is exactly what must be
    avoided), never mixing units, never substituted by Shipment or
    procurement quantity. ``SalesContract.quantity`` stays the expected
    quantity on the decision regardless of this check's outcome."""
    sales_contract = scope.sales_contract
    advisories: list[SalesInvoiceAdvisory] = []

    def _check(actual_quantity, actual_unit, outcome, note=None):
        return SalesInvoiceQuantityCheck(
            sales_contract_id=sales_contract.id,
            expected_quantity=sales_contract.quantity,
            expected_unit=sales_contract.unit,
            actual_quantity=actual_quantity,
            actual_unit=actual_unit,
            sales_invoice_id=sales_invoice.id if sales_invoice is not None else None,
            outcome=outcome,
            note=note,
        )

    if sales_contract.quantity is None or sales_contract.unit is None:
        return _check(None, None, SalesAmountCheckOutcome.NOT_COMPARABLE_MISSING_FACT,
                      "SalesContract quantity/unit Fact is absent"), tuple(advisories)
    if invoice_ambiguous:
        return _check(None, None, SalesAmountCheckOutcome.NOT_COMPARABLE_AMBIGUOUS_SCOPE,
                      "multiple confirmed SALES Invoice Facts — no sum / no arbitrary choice"), tuple(advisories)
    if sales_invoice is None:
        return _check(None, None, SalesAmountCheckOutcome.NOT_COMPARABLE_MISSING_FACT,
                      "no confirmed SALES Invoice Fact"), tuple(advisories)
    if len(invoice_items) == 0:
        return _check(None, None, SalesAmountCheckOutcome.NOT_COMPARABLE_MISSING_FACT,
                      "confirmed SALES invoice has no InvoiceItem Fact"), tuple(advisories)
    if len(invoice_items) > 1:
        return _check(None, None, SalesAmountCheckOutcome.NOT_COMPARABLE_AMBIGUOUS_SCOPE,
                      "multiple SALES InvoiceItem lines — no aggregation, no unit mixing"), tuple(advisories)
    item = invoice_items[0]
    if item.unit is None or item.unit != sales_contract.unit:
        return _check(item.quantity, item.unit, "NOT_COMPARABLE_UNIT_MISMATCH",
                      "SALES InvoiceItem unit is absent or differs from SalesContract.unit — no conversion"), tuple(advisories)
    if item.quantity is None:
        return _check(None, item.unit, SalesAmountCheckOutcome.NOT_COMPARABLE_MISSING_FACT,
                      "SALES InvoiceItem quantity Fact is absent"), tuple(advisories)
    outcome = SalesAmountCheckOutcome.MATCH if item.quantity == sales_contract.quantity else SalesAmountCheckOutcome.DEVIATION
    if outcome == SalesAmountCheckOutcome.DEVIATION:
        advisories.append(SalesInvoiceAdvisory(
            code=SalesInvoiceAdvisoryCode.SALES_INVOICE_QUANTITY_DEVIATION,
            sales_contract_id=sales_contract.id, related_invoice_ids=(sales_invoice.id,),
            note="confirmed SALES invoice item quantity deviates from SalesContract.quantity — management review",
        ))
    return _check(item.quantity, item.unit, outcome), tuple(advisories)


def _select_applicable_fx(invoice_month, evidence: tuple[ApplicableFxRateEvidence, ...]):
    if invoice_month is None:
        return None, SalesAmountCheckOutcome.NOT_COMPARABLE_FX_MISSING, "invoice_month is required explicitly"
    target = date(invoice_month.year, invoice_month.month, 1)
    valid = [
        rate for rate in evidence
        if rate.source == "SAFE"
        and isinstance(rate.usd_cny_rate, Decimal)
        and rate.usd_cny_rate.is_finite()
        and rate.usd_cny_rate > 0
        and rate.publication_date <= target
    ]
    if not valid:
        return None, SalesAmountCheckOutcome.NOT_COMPARABLE_FX_MISSING, "no valid confirmed SAFE USD/CNY rate on or before month start"
    latest_day = max(rate.publication_date for rate in valid)
    latest = [rate for rate in valid if rate.publication_date == latest_day]
    if len(latest) != 1:
        return None, SalesAmountCheckOutcome.NOT_COMPARABLE_FX_AMBIGUOUS, "multiple valid SAFE rates on applicable publication date"
    return latest[0], None, None


def _quantity_check(scope: SalesScopeContext, supplier_scopes: dict[uuid.UUID, SupplierScopeContext]) -> SalesQuantityCheck:
    """Management consistency only; the SalesContract remains authoritative."""
    contract = scope.sales_contract
    if contract.quantity is None or contract.unit is None:
        return SalesQuantityCheck(None, contract.unit, None, None, "NOT_COMPARABLE_MISSING_FACT")
    if len(scope.linked_procurement_contracts) != 1:
        return SalesQuantityCheck(contract.quantity, contract.unit, None, None, "NOT_COMPARABLE_AMBIGUOUS_SCOPE")
    linked = scope.linked_procurement_contracts[0].contract
    supplier_scope = supplier_scopes.get(linked.id) if linked is not None else None
    shipments = supplier_scope.shipments if supplier_scope is not None else ()
    if len(shipments) != 1:
        return SalesQuantityCheck(contract.quantity, contract.unit, None, None, "NOT_COMPARABLE_MISSING_FACT")
    shipment = shipments[0]
    item = next((item for item in supplier_scope.items if item.id == shipment.contract_item_id), None)
    unit = item.unit if item is not None else None
    if shipment.quantity is None or unit is None:
        return SalesQuantityCheck(contract.quantity, contract.unit, shipment.quantity, unit, "NOT_COMPARABLE_MISSING_FACT", shipment.id)
    if unit != contract.unit:
        return SalesQuantityCheck(contract.quantity, contract.unit, shipment.quantity, unit, "NOT_COMPARABLE_UNIT_MISMATCH", shipment.id)
    return SalesQuantityCheck(
        contract.quantity, contract.unit, shipment.quantity, unit,
        "MATCH" if shipment.quantity == contract.quantity else "DEVIATION", shipment.id,
    )
