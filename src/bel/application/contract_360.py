"""Contract 360° application query.

One contract's already-confirmed business facts plus the current-period
close judgment, composed into a presentation-neutral DTO. This service
combines repositories only — it never re-implements period-close rules.
The 当前期间业务判断 section reuses the frozen close engine through
``get_period_close_workbench`` and filters to the contract, so there is
exactly one set of close rules for both pages.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from bel.application.accrual_queries import AccrualView, get_accrual_balance
from bel.application.period_close_workbench import (
    WorkbenchAccrual,
    WorkbenchBlocker,
    WorkbenchCandidate,
    WorkbenchDifference,
    WorkbenchReversal,
    get_period_close_workbench,
)
from bel.domain.accrual import (
    Accrual,
    InvoiceItemAllocation,
    get_projected_accrual_status,
)
from bel.domain.contract import Contract, ContractItem
from bel.domain.evidence import EvidenceDocument, EvidenceFragment
from bel.domain.invoice import Invoice, InvoiceItem
from bel.domain.matching import InvoiceAllocation, PaymentAllocation
from bel.domain.payment import Payment
from bel.infrastructure.persistence.repositories import (
    AccrualBasisFactRepository,
    AccrualReversalRepository,
    AccrualRepository,
    ContractItemRepository,
    ContractRepository,
    CostRecognitionFactRepository,
    EvidenceRepository,
    HistoricalAccrualFactRepository,
    InvoiceAllocationRepository,
    InvoiceItemAllocationRepository,
    InvoiceItemRepository,
    InvoiceRepository,
    PaymentAllocationRepository,
    PaymentRepository,
)


@dataclass(frozen=True)
class ContractInvoiceItem:
    item: InvoiceItem
    allocations: tuple[InvoiceItemAllocation, ...]


@dataclass(frozen=True)
class ContractInvoice:
    allocation: InvoiceAllocation
    invoice: Invoice
    items: tuple[ContractInvoiceItem, ...]


@dataclass(frozen=True)
class ContractPayment:
    allocation: PaymentAllocation
    payment: Payment


@dataclass(frozen=True)
class ContractAccrual:
    accrual: Accrual
    view: AccrualView
    item: ContractItem | None


@dataclass(frozen=True)
class ContractEvidence:
    category: str
    label: str
    fragment: EvidenceFragment
    document: EvidenceDocument


@dataclass(frozen=True)
class ContractDecisions:
    """Current-period close judgments filtered to this contract — a pure
    subset of the period workbench, never a second rule set."""

    reversals: tuple[WorkbenchReversal, ...]
    accruals: tuple[WorkbenchAccrual, ...]
    candidates: tuple[WorkbenchCandidate, ...]
    differences: tuple[WorkbenchDifference, ...]
    blockers: tuple[WorkbenchBlocker, ...]


@dataclass(frozen=True)
class Contract360:
    contract: Contract
    items: tuple[ContractItem, ...]
    invoices: tuple[ContractInvoice, ...]
    payments: tuple[ContractPayment, ...]
    accruals: tuple[ContractAccrual, ...]
    evidence: tuple[ContractEvidence, ...]
    decisions: ContractDecisions


def get_contract_360(session: Session, contract_id: uuid.UUID, period: str) -> Contract360 | None:
    """Compose one contract's facts and its current-period close judgment.
    Strict read-only; writes nothing."""
    with session.no_autoflush:
        contract = ContractRepository(session).get(contract_id)
        if contract is None:
            return None

        items = tuple(ContractItemRepository(session).list_for_contract(contract_id))
        item_ids = {item.id for item in items}

        # ---- Invoices confirmed to this contract (via InvoiceAllocation). ----
        invoice_item_allocations: dict[uuid.UUID, list[InvoiceItemAllocation]] = {}
        for allocation in InvoiceItemAllocationRepository(session).list_all():
            invoice_item_allocations.setdefault(allocation.invoice_item_id, []).append(allocation)

        invoices = []
        for allocation in InvoiceAllocationRepository(session).list_for_contract(contract_id):
            invoice = InvoiceRepository(session).get(allocation.invoice_id)
            if invoice is None:
                continue
            invoice_items = tuple(
                ContractInvoiceItem(
                    item=item,
                    allocations=tuple(
                        # An Invoice may reference several Contracts (the
                        # Domain's many-to-many), but Contract360 must only
                        # see the item allocations that belong to THIS
                        # contract's items — an allocation owned by another
                        # contract's item must never read as "已关联" here,
                        # or the human would lose the manual-allocation form.
                        a
                        for a in invoice_item_allocations.get(item.id, [])
                        if a.contract_item_id in item_ids
                    ),
                )
                for item in InvoiceItemRepository(session).list_for_invoice(invoice.id)
            )
            invoices.append(ContractInvoice(allocation=allocation, invoice=invoice, items=invoice_items))

        # ---- Payments explicitly allocated to this contract. ----
        payments = []
        for allocation in PaymentAllocationRepository(session).list_for_contract(contract_id):
            payment = PaymentRepository(session).get(allocation.payment_id)
            if payment is None:
                continue
            payments.append(ContractPayment(allocation=allocation, payment=payment))

        # ---- Accruals on this contract's items (balance derived via the
        # shared domain function, never Web-side original - reversed). ----
        accruals = []
        reversal_repo = AccrualReversalRepository(session)
        for accrual in AccrualRepository(session).list_all():
            if accrual.contract_item_id not in item_ids:
                continue
            reversals = reversal_repo.list_for_accrual(accrual.id)
            remaining_qty, remaining_cost, reversed_qty, reversed_cost = get_accrual_balance(accrual, reversals)

            item = next((i for i in items if i.id == accrual.contract_item_id), None)
            accruals.append(
                ContractAccrual(
                    accrual=accrual,
                    view=AccrualView(
                        accrual=accrual,
                        remaining_quantity=remaining_qty,
                        remaining_estimated_cost=remaining_cost,
                        reversed_quantity=reversed_qty,
                        reversed_estimated_cost=reversed_cost,
                        projected_status=get_projected_accrual_status(reversed_qty, remaining_qty),
                        reversals=reversals,
                    ),
                    item=item,
                )
            )

        # ---- Evidence aggregation for the contract and its related facts. ----
        evidence = _aggregate_evidence(session, contract, items)

        # ---- Current-period close judgment — the SAME engine as the
        # workbench, filtered to this contract (no second rule set). ----
        workbench = get_period_close_workbench(session, period)
        decisions = ContractDecisions(
            reversals=tuple(r for r in workbench.reversals if r.decision.contract_id == contract_id),
            accruals=tuple(a for a in workbench.accruals if a.decision.contract_id == contract_id),
            candidates=tuple(c for c in workbench.candidates if c.decision.contract_id == contract_id),
            differences=tuple(d for d in workbench.differences if d.decision.contract_id == contract_id),
            blockers=tuple(b for b in workbench.blockers if b.blocker.contract_id == contract_id),
        )

        return Contract360(
            contract=contract,
            items=items,
            invoices=tuple(invoices),
            payments=tuple(payments),
            accruals=tuple(accruals),
            evidence=tuple(evidence),
            decisions=decisions,
        )


def _aggregate_evidence(
    session: Session, contract: Contract, items: tuple[ContractItem, ...]
) -> list[ContractEvidence]:
    """Aggregate every EvidenceFragment/EvidenceDocument the contract and
    its related facts point at. Presents locator + metadata only — never a
    raw file download endpoint."""
    evidence_repo = EvidenceRepository(session)
    fragment_ids: list[tuple[str, str, uuid.UUID]] = []

    def _add(category: str, label: str, fragment_id: uuid.UUID | None) -> None:
        if fragment_id is not None:
            fragment_ids.append((category, label, fragment_id))

    _add("CONTRACT", contract.contract_no, contract.current_source_fragment_id)
    for item in items:
        _add("CONTRACT_ITEM", item.product_name or item.source_item_key or "—", item.current_source_fragment_id)

    invoice_ids = {a.invoice_id for a in InvoiceAllocationRepository(session).list_for_contract(contract.id)}
    for invoice_id in invoice_ids:
        invoice = InvoiceRepository(session).get(invoice_id)
        if invoice is not None:
            _add("INVOICE", invoice.external_invoice_key or invoice.invoice_no or str(invoice.id), invoice.source_fragment_id)

    payment_ids = {a.payment_id for a in PaymentAllocationRepository(session).list_for_contract(contract.id)}
    for payment_id in payment_ids:
        payment = PaymentRepository(session).get(payment_id)
        if payment is not None:
            _add("PAYMENT", payment.bank_reference or str(payment.id), payment.source_fragment_id)

    item_ids = {item.id for item in items}
    for fact in HistoricalAccrualFactRepository(session).list_all():
        if fact.contract_item_id in item_ids:
            _add("HISTORICAL_ACCRUAL", fact.source_period, fact.source_fragment_id)
    for fact in CostRecognitionFactRepository(session).list_all():
        if fact.contract_id == contract.id:
            _add("COST_RECOGNITION", fact.recognition_date.isoformat(), fact.source_fragment_id)
    for fact in AccrualBasisFactRepository(session).list_all():
        if fact.contract_id == contract.id:
            _add("ACCRUAL_BASIS", fact.scope_type, fact.source_fragment_id)

    invoice_item_ids = {
        allocation.invoice_item_id
        for allocation in InvoiceItemAllocationRepository(session).list_all()
        if allocation.contract_item_id in item_ids
    }
    for allocation in InvoiceItemAllocationRepository(session).list_all():
        if allocation.contract_item_id not in item_ids:
            continue
        invoice_item = InvoiceItemRepository(session).get(allocation.invoice_item_id)
        invoice = None
        if invoice_item is not None:
            invoice = InvoiceRepository(session).get(invoice_item.invoice_id)
        label = invoice.external_invoice_key or str(allocation.id)
        _add("MANUAL_ITEM_ALLOCATION", f"{label} line {invoice_item.line_no}" if invoice_item else label, allocation.source_fragment_id)

    entries: list[ContractEvidence] = []
    seen: set[uuid.UUID] = set()
    for category, label, fragment_id in fragment_ids:
        if fragment_id in seen:
            continue
        fragment = evidence_repo.get_fragment(fragment_id)
        if fragment is None:
            continue
        document = evidence_repo.get_document(fragment.evidence_document_id)
        if document is None:
            continue
        seen.add(fragment_id)
        entries.append(ContractEvidence(category=category, label=label, fragment=fragment, document=document))
    return entries
