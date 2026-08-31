"""SALES_INVOICE_PREPARATION rule foundation (Phase 2D.3-F1a).

Formally establishes the sales-direction preparation rule layer on top
of the F0 fact context. The rule layer consumes ONLY already-confirmed /
current Facts (via ``get_invoice_preparation_context``) and produces a
Decision per SalesContract scope — the Fact -> Decision layering is
preserved: this module never writes, never mutates Facts, and never
re-derives "current" semantics.

Frozen by the Phase 2D.3-F1a business clarification — the THREE required
inputs of SALES_INVOICE_PREPARATION, in this order:

1. ``SALES_CONTRACT``                — the scope's own SalesContract Fact.
2. ``LINKED_PROCUREMENT_CONTRACT``   — at least one CURRENT
   ProcurementSalesLink resolving to a procurement Contract.
3. ``SHIPMENT_EXPORT_FACT``          — a Shipment/Export Fact on the
   linked procurement Contract (single-link case only; see below).

When a required input is missing, the decision carries an explicit
blocker / insufficient-fact outcome. When all three are present, the
status is ``INPUTS_PRESENT`` — a statement about required-input fact
completeness ONLY. It is NOT "ready to invoice", NOT an eligibility
Decision, and does not mean any amount or quantity should be invoiced.

Deliberately NOT implemented (each requires its own rule freeze):

- M:N shipment judgment — when a scope has MULTIPLE current links, no
  "any shipment" / "all linked contracts have shipments" decision is
  made; a dedicated ``SHIPMENT_JUDGMENT_DEFERRED_MULTIPLE_LINKS``
  blocker is emitted instead. The system does not guess.
- 应开金额 / 应开数量 — no should-invoice amount or quantity exists
  anywhere in this module.
- Receipt/payment triggering — no "receipt triggers invoice" or
  "已收多少开多少" logic. SalesPaymentAllocations are not consulted.
- The exact field set of the future 一致性校验 (consistency validation,
  "完全一致") — reserved, not defined (see below).
- Supplier-side preparation calculation — this module is
  sales-direction only.

``customer`` on every decision comes only from
``SalesContract.customer`` (the Domain's only expression of an external
customer). Whether a customer is known is surfaced as a fact, never
judged: customer presence is deliberately NOT one of the three required
inputs and adds no blocker here.

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
    """The THREE required inputs of SALES_INVOICE_PREPARATION, frozen by
    the Phase 2D.3-F1a clarification. Exactly these three — adding a
    fourth (e.g. customer presence, receipt state) would be a new rule
    and requires its own freeze."""

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
    here says which required-input Facts are present, nothing more."""

    INPUTS_PRESENT = "INPUTS_PRESENT"
    INSUFFICIENT_FACTS = "INSUFFICIENT_FACTS"


class SalesPreparationBlockerCode:
    """Explicit blocker / insufficient-fact codes. Codes state which
    required input could not be confirmed — never what should be done
    about it (that is the unfrozen eligibility question)."""

    # Required input 2 missing: no CURRENT ProcurementSalesLink resolves
    # to a procurement Contract (none exist, or none resolve).
    NO_CURRENT_PROCUREMENT_LINK = "NO_CURRENT_PROCUREMENT_LINK"
    # Required input 3 missing: the single linked procurement Contract
    # has no Shipment/Export Fact.
    NO_SHIPMENT_FACT_ON_LINKED_CONTRACT = "NO_SHIPMENT_FACT_ON_LINKED_CONTRACT"
    # Required input 3 NOT JUDGED: the scope has MULTIPLE current links
    # (the bridge is many-to-many) and the any/all shipment rule is not
    # frozen. The system does not guess.
    SHIPMENT_JUDGMENT_DEFERRED_MULTIPLE_LINKS = "SHIPMENT_JUDGMENT_DEFERRED_MULTIPLE_LINKS"


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
    """One required input's evaluation outcome, with the Fact ids that
    satisfy it — the Fact -> Decision trace."""

    name: str
    present: bool
    source_fact_ids: tuple[uuid.UUID, ...] = ()
    # Why an input could not be judged (e.g. NOT_JUDGED_UNDER_MN). Never
    # a business judgment — a statement about what this rule did not
    # evaluate.
    note: str | None = None


@dataclass(frozen=True)
class SalesPreparationBlocker:
    code: str
    related_sales_contract_id: uuid.UUID
    # Procurement Contract ids the blocker is about (empty when no link
    # exists at all). Enumeration only — never an amount/quantity.
    related_contract_ids: tuple[uuid.UUID, ...] = ()
    note: str | None = None


@dataclass(frozen=True)
class SalesInvoicePreparationDecision:
    """One SalesContract scope's preparation-rule Decision. Carries facts
    and required-input outcomes only — no amount, no quantity, no
    readiness/eligibility field."""

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

    # Required input 1 — the scope's own SalesContract Fact. Present by
    # construction (a sales scope exists only for an existing
    # SalesContract anchor); asserted, not assumed silently.
    sales_contract_input = SalesPreparationRequiredInputState(
        name=SalesPreparationRequiredInput.SALES_CONTRACT,
        present=True,
        source_fact_ids=(sales_contract.id,),
    )

    # Required input 2 — at least one CURRENT ProcurementSalesLink
    # resolving to a procurement Contract. Enumeration is reused as-is
    # from the F0 context; no amount/quantity crosses the bridge.
    resolved = [entry.contract for entry in scope.linked_procurement_contracts if entry.contract is not None]
    all_link_contract_ids = tuple(entry.link.procurement_contract_id for entry in scope.linked_procurement_contracts)
    link_input = SalesPreparationRequiredInputState(
        name=SalesPreparationRequiredInput.LINKED_PROCUREMENT_CONTRACT,
        present=bool(resolved),
        source_fact_ids=tuple(contract.id for contract in resolved),
    )
    if not resolved:
        blockers.append(
            SalesPreparationBlocker(
                code=SalesPreparationBlockerCode.NO_CURRENT_PROCUREMENT_LINK,
                related_sales_contract_id=sales_contract.id,
                related_contract_ids=all_link_contract_ids,
                note=None if not all_link_contract_ids else "current link(s) exist but resolve to no Contract anchor",
            )
        )

    # Required input 3 — Shipment/Export Fact. Judged ONLY in the
    # single-link case: under M:N the any/all shipment rule is NOT
    # frozen, and this module does not guess (deliberate F1a boundary).
    if len(scope.linked_procurement_contracts) > 1:
        shipment_input = SalesPreparationRequiredInputState(
            name=SalesPreparationRequiredInput.SHIPMENT_EXPORT_FACT,
            present=False,
            source_fact_ids=(),
            note="NOT_JUDGED_UNDER_MN",
        )
        blockers.append(
            SalesPreparationBlocker(
                code=SalesPreparationBlockerCode.SHIPMENT_JUDGMENT_DEFERRED_MULTIPLE_LINKS,
                related_sales_contract_id=sales_contract.id,
                related_contract_ids=all_link_contract_ids,
                note="any/all shipment judgment under multiple current links requires a rule freeze",
            )
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
        )
        if not shipment_ids:
            blockers.append(
                SalesPreparationBlocker(
                    code=SalesPreparationBlockerCode.NO_SHIPMENT_FACT_ON_LINKED_CONTRACT,
                    related_sales_contract_id=sales_contract.id,
                    related_contract_ids=(linked_contract.id,),
                )
            )
    else:
        # No linked contract to check shipments on — the link blocker
        # above already states the missing input; no separate shipment
        # blocker is emitted (nothing was checked, so nothing is claimed).
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
