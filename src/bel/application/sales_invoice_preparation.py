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
produces a check result today.

Strictly read-only: evaluation is a pure function of the F0 context.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from bel.application.invoice_preparation import (
    InvoicePreparationContext,
    SalesScopeContext,
    get_invoice_preparation_context,
)

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
class SalesInvoicePreparationDecision:
    """One SalesContract scope's preparation-rule Decision. Carries facts
    and required-input outcomes only — no amount, no quantity, no
    readiness/eligibility field, and no blocker in any reachable state
    (the three inputs report comparison availability, not gates)."""

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


@dataclass(frozen=True)
class SalesInvoicePreparationReport:
    decisions: tuple[SalesInvoicePreparationDecision, ...]


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def evaluate_sales_invoice_preparation(session: Session) -> SalesInvoicePreparationReport:
    """Evaluate the SALES_INVOICE_PREPARATION rule foundation over the
    complete F0 fact context (unfiltered — a scope's shipment resolution
    must never be blinded by an axis filter). Strictly read-only."""
    context = get_invoice_preparation_context(session)
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
            _evaluate_scope(scope, supplier_scope_by_contract_id) for scope in context.sales_scopes
        )
    )


def _evaluate_scope(
    scope: SalesScopeContext,
    supplier_scope_by_contract_id: dict[uuid.UUID, object],
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
    )
