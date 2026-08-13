"""Period Close Workbench application query.

Backs the Phase 2C 月结工作台 page. It wraps the frozen
``build_period_close_preview`` output (which stays the ONLY source of
truth — this module never changes ``PeriodClosePreview``) and adds the
readable labels and the Decision -> Fact -> EvidenceFragment ->
EvidenceDocument trace that the UI needs.

Architecture: Web Route -> Application Service -> Domain / Rule Engine ->
Repository. The workbench service composes repositories to attach labels
and evidence traces; it does not re-implement any business rule.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from bel.application.period_close import (
    MISSING_ACCRUAL_BASIS,
    AccrualActualDifference,
    AccrualCandidate,
    AccrualRequired,
    CloseBlocker,
    PeriodClosePreview,
    PriorAccrualReversalRequired,
    build_period_close_preview,
)
from bel.domain.accrual import get_accrual_balance
from bel.domain.contract import ContractItem
from bel.domain.evidence import EvidenceDocument, EvidenceFragment
from bel.domain.invoice import InvoiceDirection, InvoiceItem
from bel.infrastructure.persistence.repositories import (
    AccrualBasisFactRepository,
    AccrualRepository,
    AccrualReversalRepository,
    ContractItemRepository,
    ContractRepository,
    CostRecognitionFactRepository,
    EvidenceRepository,
    HistoricalAccrualFactRepository,
    InvoiceAllocationRepository,
    InvoiceItemAllocationRepository,
    InvoiceItemRepository,
    InvoiceRepository,
)


@dataclass(frozen=True)
class FactNode:
    """One Decision -> Fact -> EvidenceFragment -> EvidenceDocument link.

    ``fields`` are presentation-neutral (name, value) pairs; the Web layer
    owns the Chinese labels. ``fragment``/``document`` are the raw evidence
    chain the trace is built from.
    """

    fact_kind: str
    fields: tuple[tuple[str, Any], ...]
    fragment: EvidenceFragment | None = None
    document: EvidenceDocument | None = None


@dataclass(frozen=True)
class WorkbenchReversal:
    decision: PriorAccrualReversalRequired
    contract_no: str
    counterparty: str | None
    item: ContractItem | None
    trace: tuple[FactNode, ...]


@dataclass(frozen=True)
class WorkbenchAccrual:
    decision: AccrualRequired
    contract_no: str
    counterparty: str | None
    item: ContractItem | None
    trace: tuple[FactNode, ...]


@dataclass(frozen=True)
class WorkbenchCandidate:
    decision: AccrualCandidate
    contract_no: str
    counterparty: str | None
    trace: tuple[FactNode, ...]


@dataclass(frozen=True)
class WorkbenchDifference:
    decision: AccrualActualDifference
    contract_no: str
    counterparty: str | None
    item: ContractItem | None
    trace: tuple[FactNode, ...]


@dataclass(frozen=True)
class BlockerContext:
    """Read-only facts composed to explain a blocker in business terms —
    never a new judgment. Every field here is read back from an
    already-persisted Fact or Decision; the close engine
    (``period_close.py``) remains the ONLY source of blocker existence.
    Fields are presentation-neutral; the Web layer owns the Chinese
    business copy (spec section 4.5/4.6)."""

    historical_source_periods: tuple[str, ...]
    historical_estimated_cost: Decimal | None
    current_remaining_quantity: Decimal | None
    current_remaining_cost: Decimal | None
    confirmed_invoice_keys: tuple[str, ...]
    confirmed_invoice_net_total: Decimal | None
    invoice_item_line_count: int
    existing_item_allocation_count: int
    cost_recognition_date: date | None


@dataclass(frozen=True)
class WorkbenchBlocker:
    blocker: CloseBlocker
    contract_no: str | None
    item: ContractItem | None
    context: BlockerContext


@dataclass(frozen=True)
class PeriodCloseWorkbench:
    """The page-facing DTO. ``preview`` is the unchanged
    ``PeriodClosePreview`` (kept so tests can assert strict parity)."""

    period: str
    summary: dict[str, int]
    available_periods: tuple[str, ...]
    reversals: tuple[WorkbenchReversal, ...]
    accruals: tuple[WorkbenchAccrual, ...]
    candidates: tuple[WorkbenchCandidate, ...]
    differences: tuple[WorkbenchDifference, ...]
    blockers: tuple[WorkbenchBlocker, ...]
    preview: PeriodClosePreview


def list_known_periods(session: Session) -> tuple[str, ...]:
    """The YYYY-MM periods the data mentions, newest first — used to fill
    the page's period dropdown. Derived from repositories, not from the
    preview, so an empty database still yields a stable empty list."""
    periods: set[str] = set()
    for accrual in AccrualRepository(session).list_all():
        periods.add(accrual.period)
    for fact in HistoricalAccrualFactRepository(session).list_all():
        periods.add(fact.source_period)
    for invoice in InvoiceRepository(session).list_all():
        if invoice.issue_date is not None:
            periods.add(invoice.issue_date.strftime("%Y-%m"))
    return tuple(sorted(periods, reverse=True))


class _TraceBuilder:
    """Repository composition for the Decision -> Fact -> Evidence chain.
    Pure reads; every lookup is cached so one workbench build is O(N)."""

    def __init__(self, session: Session) -> None:
        evidence_repo = EvidenceRepository(session)
        self._fragments: dict[uuid.UUID, EvidenceFragment | None] = {}
        self._documents: dict[uuid.UUID, EvidenceDocument | None] = {}
        self._historical = {f.id: f for f in HistoricalAccrualFactRepository(session).list_all()}
        self._cost = {f.id: f for f in CostRecognitionFactRepository(session).list_all()}
        self._basis = {f.id: f for f in AccrualBasisFactRepository(session).list_all()}
        self._allocations = {a.id: a for a in InvoiceItemAllocationRepository(session).list_all()}
        self._invoice_items = {i.id: i for i in InvoiceItemRepository(session).list_all()}
        self._invoices = {i.id: i for i in InvoiceRepository(session).list_all()}

        def _fragment(fragment_id: uuid.UUID | None) -> EvidenceFragment | None:
            if fragment_id is None:
                return None
            if fragment_id not in self._fragments:
                self._fragments[fragment_id] = evidence_repo.get_fragment(fragment_id)
            return self._fragments[fragment_id]

        def _document(document_id: uuid.UUID | None) -> EvidenceDocument | None:
            if document_id is None:
                return None
            if document_id not in self._documents:
                self._documents[document_id] = evidence_repo.get_document(document_id)
            return self._documents[document_id]

        self._fragment = _fragment
        self._document = _document

    def _node(self, fact_kind: str, fields: tuple[tuple[str, Any], ...], fragment_id: uuid.UUID | None) -> FactNode:
        fragment = self._fragment(fragment_id)
        document = self._document(fragment.evidence_document_id) if fragment is not None else None
        return FactNode(fact_kind=fact_kind, fields=fields, fragment=fragment, document=document)

    def historical_accrual_node(self, fact_id: uuid.UUID) -> FactNode:
        fact = self._historical.get(fact_id)
        if fact is None:
            return FactNode("HISTORICAL_ACCRUAL", (("fact_id", str(fact_id)),))
        return self._node(
            "HISTORICAL_ACCRUAL",
            (
                ("period", fact.source_period),
                ("quantity", fact.quantity),
                ("estimated_cost", fact.estimated_cost),
                ("basis", fact.basis),
            ),
            fact.source_fragment_id,
        )

    def cost_recognition_node(self, fact_id: uuid.UUID) -> FactNode:
        fact = self._cost.get(fact_id)
        if fact is None:
            return FactNode("COST_RECOGNITION", (("fact_id", str(fact_id)),))
        return self._node(
            "COST_RECOGNITION",
            (("recognition_date", fact.recognition_date), ("basis", fact.basis)),
            fact.source_fragment_id,
        )

    def accrual_basis_node(self, fact_id: uuid.UUID) -> FactNode:
        fact = self._basis.get(fact_id)
        if fact is None:
            return FactNode("ACCRUAL_BASIS", (("fact_id", str(fact_id)),))
        fields: list[tuple[str, Any]] = [("scope_type", fact.scope_type), ("estimated_cost", fact.estimated_cost)]
        if fact.quantity is not None:
            fields.append(("quantity", fact.quantity))
        fields.append(("basis", fact.basis))
        return self._node("ACCRUAL_BASIS", tuple(fields), fact.source_fragment_id)

    def allocation_node(self, allocation_id: uuid.UUID) -> FactNode:
        """The InvoiceItemAllocation that anchors a reversal's actual cost —
        including the invoice fact it points at."""
        allocation = self._allocations.get(allocation_id)
        if allocation is None:
            return FactNode("MANUAL_ITEM_ALLOCATION", (("allocation_id", str(allocation_id)),))
        invoice_item: InvoiceItem | None = self._invoice_items.get(allocation.invoice_item_id)
        fields: list[tuple[str, Any]] = [
            ("allocated_quantity", allocation.allocated_quantity),
            ("allocated_net_amount", allocation.allocated_net_amount),
        ]
        if invoice_item is not None:
            fields.append(("invoice_item_line_no", invoice_item.line_no))
            invoice = self._invoices.get(invoice_item.invoice_id)
            if invoice is not None:
                fields.append(("invoice_external_key", invoice.external_invoice_key))
                fields.append(("issue_date", invoice.issue_date))
        return self._node("MANUAL_ITEM_ALLOCATION", tuple(fields), allocation.source_fragment_id)


def _reversal_trace(builder: _TraceBuilder, reversal: PriorAccrualReversalRequired) -> tuple[FactNode, ...]:
    return (builder.historical_accrual_node(reversal.source_fact_id), builder.allocation_node(reversal.invoice_item_allocation_id))


def _accrual_trace(builder: _TraceBuilder, requirement: AccrualRequired) -> tuple[FactNode, ...]:
    return (builder.cost_recognition_node(requirement.cost_recognition_fact_id), builder.accrual_basis_node(requirement.accrual_basis_fact_id))


def _candidate_trace(builder: _TraceBuilder, candidate: AccrualCandidate) -> tuple[FactNode, ...]:
    return (builder.cost_recognition_node(candidate.cost_recognition_fact_id), builder.accrual_basis_node(candidate.accrual_basis_fact_id))


def _difference_trace(builder: _TraceBuilder, difference: AccrualActualDifference) -> tuple[FactNode, ...]:
    return (
        builder.historical_accrual_node(difference.source_fact_id),
        builder.allocation_node(difference.invoice_item_allocation_id),
    )


def get_period_close_workbench(session: Session, period: str) -> PeriodCloseWorkbench:
    """Strict read-only page query: the frozen preview plus readable labels
    and evidence traces. Writes nothing."""
    with session.no_autoflush:
        preview = build_period_close_preview(session, period)
        builder = _TraceBuilder(session)

        contracts = {c.id: c for c in ContractRepository(session).list_all()}
        items = {i.id: i for i in ContractItemRepository(session).list_all()}

        accrual_repo = AccrualRepository(session)
        reversal_repo = AccrualReversalRepository(session)
        item_alloc_repo = InvoiceItemAllocationRepository(session)
        invoice_alloc_repo = InvoiceAllocationRepository(session)
        invoice_repo = InvoiceRepository(session)
        invoice_item_repo = InvoiceItemRepository(session)
        cost_recognition_facts = CostRecognitionFactRepository(session).list_all()

        def _contract_and_item(
            contract_id: uuid.UUID, contract_item_id: uuid.UUID | None
        ) -> tuple[str, str | None, ContractItem | None]:
            contract = contracts.get(contract_id)
            contract_no = contract.contract_no if contract is not None else str(contract_id)
            counterparty = contract.counterparty if contract is not None else None
            item: ContractItem | None = items.get(contract_item_id) if contract_item_id is not None else None
            return contract_no, counterparty, item

        def _blocker_context(blocker: CloseBlocker) -> BlockerContext:
            """Read-only composition of the Facts around a blocker — never
            a new judgment. See ``BlockerContext`` docstring."""
            accrual_ids = blocker.accrual_ids or ((blocker.accrual_id,) if blocker.accrual_id is not None else ())
            accruals = [a for a in (accrual_repo.get(aid) for aid in accrual_ids) if a is not None]

            remaining_qty_total = Decimal("0")
            remaining_cost_total = Decimal("0")
            for accrual in accruals:
                remaining_qty, remaining_cost, _, _ = get_accrual_balance(
                    accrual, reversal_repo.list_for_accrual(accrual.id)
                )
                remaining_qty_total += remaining_qty
                remaining_cost_total += remaining_cost

            existing_item_allocation_count = (
                len(item_alloc_repo.list_for_contract_item(blocker.contract_item_id))
                if blocker.contract_item_id is not None
                else 0
            )

            confirmed_invoice_keys: list[str] = []
            confirmed_invoice_net_total = Decimal("0")
            invoice_item_line_count = 0
            for allocation in invoice_alloc_repo.list_for_contract(blocker.contract_id):
                invoice = invoice_repo.get(allocation.invoice_id)
                if invoice is None or invoice.direction != InvoiceDirection.PURCHASE:
                    continue
                if invoice.issue_date is None or invoice.issue_date > preview.period_end:
                    continue
                confirmed_invoice_keys.append(invoice.external_invoice_key or invoice.invoice_no or str(invoice.id))
                confirmed_invoice_net_total += invoice.net_amount
                invoice_item_line_count += len(invoice_item_repo.list_for_invoice(invoice.id))

            cost_recognition_date: date | None = None
            if blocker.blocker_type == MISSING_ACCRUAL_BASIS:
                contract_facts = [f for f in cost_recognition_facts if f.contract_id == blocker.contract_id]
                if contract_facts:
                    cost_recognition_date = min(f.recognition_date for f in contract_facts)

            return BlockerContext(
                historical_source_periods=tuple(sorted({a.period for a in accruals})),
                historical_estimated_cost=sum((a.estimated_cost for a in accruals), Decimal("0")) if accruals else None,
                current_remaining_quantity=remaining_qty_total if accruals else None,
                current_remaining_cost=remaining_cost_total if accruals else None,
                confirmed_invoice_keys=tuple(confirmed_invoice_keys),
                confirmed_invoice_net_total=confirmed_invoice_net_total if confirmed_invoice_keys else None,
                invoice_item_line_count=invoice_item_line_count,
                existing_item_allocation_count=existing_item_allocation_count,
                cost_recognition_date=cost_recognition_date,
            )

        reversals = []
        for reversal in preview.prior_accrual_reversals:
            contract_no, counterparty, item = _contract_and_item(reversal.contract_id, reversal.contract_item_id)
            reversals.append(
                WorkbenchReversal(
                    decision=reversal,
                    contract_no=contract_no,
                    counterparty=counterparty,
                    item=item,
                    trace=_reversal_trace(builder, reversal),
                )
            )

        accruals = []
        for requirement in preview.new_accrual_requirements:
            contract_no, counterparty, item = _contract_and_item(requirement.contract_id, requirement.contract_item_id)
            accruals.append(
                WorkbenchAccrual(
                    decision=requirement,
                    contract_no=contract_no,
                    counterparty=counterparty,
                    item=item,
                    trace=_accrual_trace(builder, requirement),
                )
            )

        candidates = []
        for candidate in preview.contract_level_candidates:
            contract_no, counterparty, _ = _contract_and_item(candidate.contract_id, None)
            candidates.append(
                WorkbenchCandidate(
                    decision=candidate,
                    contract_no=contract_no,
                    counterparty=counterparty,
                    trace=_candidate_trace(builder, candidate),
                )
            )

        differences = []
        for difference in preview.accrual_actual_differences:
            contract_no, counterparty, item = _contract_and_item(difference.contract_id, difference.contract_item_id)
            differences.append(
                WorkbenchDifference(
                    decision=difference,
                    contract_no=contract_no,
                    counterparty=counterparty,
                    item=item,
                    trace=_difference_trace(builder, difference),
                )
            )

        blockers = []
        for blocker in preview.blockers:
            contract_no, _, item = _contract_and_item(blocker.contract_id, blocker.contract_item_id)
            blockers.append(
                WorkbenchBlocker(
                    blocker=blocker,
                    contract_no=contract_no if blocker.contract_id in contracts else None,
                    item=item,
                    context=_blocker_context(blocker),
                )
            )

        return PeriodCloseWorkbench(
            period=period,
            summary=preview.summary,
            available_periods=list_known_periods(session),
            reversals=tuple(reversals),
            accruals=tuple(accruals),
            candidates=tuple(candidates),
            differences=tuple(differences),
            blockers=tuple(blockers),
            preview=preview,
        )
