"""Period Close Preview — the first deterministic stateless close engine.

Pure query: every Decision is recomputed from the CURRENT database facts
on each run. Nothing is persisted, no Voucher/AccountingEntry/TaxEntry is
produced, and no BusinessEvent is created. When a fact changes, the old
result disappears naturally on the next run because no stale Decision was
ever stored. This is stateless recomputation — it is NOT the R015 rule,
which stays PROPOSED (see docs/PHASE2B-DECISIONS.md).

Confirmed rules implemented (RULES.md):
  R001+R006  PriorAccrualReversalRequired  (item-scoped partial reversal)
  R002       AccrualRequired               (item-level, gated by R003)
  R003       duplicate-accrual guard
  R005       AccrualActualDifference
  R007       contract-level AccrualCandidate
plus two diagnostics that are blockers, not Decisions: the
ITEM_MATCH_REQUIRED_FOR_REVERSAL blocker (an invoice is known at
contract level but no item match exists, so no reversal amount may be
guessed) and the MISSING_ACCRUAL_BASIS diagnostic (which is NOT the
PROPOSED R011 EvidenceMissing Decision).
"""

from __future__ import annotations

import re
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from uuid import UUID

from sqlalchemy.orm import Session

from bel.domain.accrual import (
    Accrual,
    AccrualBasisFact,
    AccrualBasisScopeType,
    AccrualReversal,
    CostRecognitionFact,
    InvoiceItemAllocation,
    get_accrual_balance,
    get_projected_accrual_status,
    is_open_accrual,
)
from bel.domain.contract import Contract, ContractItem
from bel.domain.invoice import Invoice, InvoiceDirection, InvoiceItem
from bel.domain.matching import ConfirmationType, InvoiceAllocation
from bel.infrastructure.persistence.repositories import (
    AccrualBasisFactRepository,
    AccrualReversalRepository,
    AccrualRepository,
    ContractItemRepository,
    ContractRepository,
    CostRecognitionFactRepository,
    InvoiceAllocationRepository,
    InvoiceItemAllocationRepository,
    InvoiceItemRepository,
    InvoiceRepository,
)

PERIOD_RE = re.compile(r"^\d{4}-\d{2}$")
_CENT = Decimal("0.01")

MISSING_CONTRACT_ITEM_EVIDENCE = "MISSING_CONTRACT_ITEM_EVIDENCE"
ITEM_MATCH_REQUIRED_FOR_REVERSAL = "ITEM_MATCH_REQUIRED_FOR_REVERSAL"
MISSING_ACCRUAL_BASIS = "MISSING_ACCRUAL_BASIS"
# Multiple open Accruals reference the same ContractItem, and an unclaimed
# invoice allocation is available. The engine must NOT assign it FIFO —
# an explicit scope decision is required. One allocation may never be
# consumed by two Accruals.
MULTIPLE_OPEN_ACCRUALS_REQUIRE_EXPLICIT_SCOPE = "MULTIPLE_OPEN_ACCRUALS_REQUIRE_EXPLICIT_SCOPE"
# A single open Accrual faces more than one qualifying InvoiceItemAllocation
# with unclaimed quantity: which allocation supplies the reversed portion's
# actual cost is ambiguous. The engine must NOT pick one by created_at —
# it requires an explicit allocation-to-accrual scope instead.
MULTIPLE_ITEM_ALLOCATIONS_REQUIRE_EXPLICIT_SCOPE = "MULTIPLE_ITEM_ALLOCATIONS_REQUIRE_EXPLICIT_SCOPE"


def period_end(period: str) -> date:
    if not PERIOD_RE.match(period):
        raise ValueError(f"period must be YYYY-MM, got {period!r}")
    year, month = (int(p) for p in period.split("-"))
    return date(year, month, monthrange(year, month)[1])


@dataclass(frozen=True)
class PriorAccrualReversalRequired:
    accrual_id: UUID
    contract_id: UUID
    contract_item_id: UUID
    source_period: str
    basis: str
    reversal_quantity: Decimal
    reversal_estimated_cost: Decimal
    projected_remaining_quantity: Decimal
    projected_remaining_cost: Decimal
    projected_status: str
    source_fact_id: UUID  # -> HistoricalAccrualFact
    invoice_item_allocation_id: UUID


@dataclass(frozen=True)
class AccrualActualDifference:
    contract_id: UUID
    contract_item_id: UUID
    actual_net_cost: Decimal
    reversed_estimated_cost: Decimal
    difference: Decimal
    source_fact_id: UUID  # -> HistoricalAccrualFact
    invoice_item_allocation_id: UUID


@dataclass(frozen=True)
class AccrualRequired:
    level: str  # always CONTRACT_ITEM in Phase 2B
    contract_id: UUID
    contract_item_id: UUID
    quantity: Decimal | None
    estimated_cost: Decimal
    basis: str
    cost_recognition_fact_id: UUID
    accrual_basis_fact_id: UUID


@dataclass(frozen=True)
class AccrualCandidate:
    level: str  # always CONTRACT in Phase 2B
    contract_id: UUID
    estimated_cost: Decimal
    blocking_reason: str
    cost_recognition_fact_id: UUID
    accrual_basis_fact_id: UUID


@dataclass(frozen=True)
class CloseBlocker:
    blocker_type: str
    contract_id: UUID
    contract_item_id: UUID | None = None
    accrual_id: UUID | None = None
    accrual_ids: tuple[UUID, ...] = ()


@dataclass(frozen=True)
class PeriodClosePreview:
    period: str
    period_end: date
    prior_accrual_reversals: list[PriorAccrualReversalRequired] = field(default_factory=list)
    new_accrual_requirements: list[AccrualRequired] = field(default_factory=list)
    contract_level_candidates: list[AccrualCandidate] = field(default_factory=list)
    accrual_actual_differences: list[AccrualActualDifference] = field(default_factory=list)
    blockers: list[CloseBlocker] = field(default_factory=list)

    @property
    def summary(self) -> dict[str, int]:
        return {
            "period": self.period,
            "prior_accrual_reversals": len(self.prior_accrual_reversals),
            "new_accrual_requirements": len(self.new_accrual_requirements),
            "contract_level_candidates": len(self.contract_level_candidates),
            "accrual_actual_differences": len(self.accrual_actual_differences),
            "blockers": len(self.blockers),
        }


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def build_period_close_preview(session: Session, period: str) -> PeriodClosePreview:
    """Strict read-only preview. Runs entirely under session.no_autoflush
    so a pending (unflushed) object can never be written by a preview's
    reads, and nothing else in the session is ever changed."""
    end = period_end(period)
    with session.no_autoflush:
        return _compute_preview(session, period, end)


def _compute_preview(session: Session, period: str, end: date) -> PeriodClosePreview:
    contract_repo = ContractRepository(session)
    item_repo = ContractItemRepository(session)
    accrual_repo = AccrualRepository(session)
    reversal_repo = AccrualReversalRepository(session)
    item_alloc_repo = InvoiceItemAllocationRepository(session)
    invoice_item_repo = InvoiceItemRepository(session)
    invoice_repo = InvoiceRepository(session)
    invoice_alloc_repo = InvoiceAllocationRepository(session)
    cost_rec_repo = CostRecognitionFactRepository(session)
    basis_repo = AccrualBasisFactRepository(session)

    contracts: dict[UUID, Contract] = {c.id: c for c in contract_repo.list_all()}
    items: dict[UUID, ContractItem] = {i.id: i for i in item_repo.list_all()}
    items_by_contract: dict[UUID, list[ContractItem]] = {}
    for item in items.values():
        items_by_contract.setdefault(item.contract_id, []).append(item)

    accruals = accrual_repo.list_all()
    reversals_by_accrual: dict[UUID, list[AccrualReversal]] = {}
    all_reversals: list[AccrualReversal] = []
    for reversal in reversal_repo.list_all():
        reversals_by_accrual.setdefault(reversal.accrual_id, []).append(reversal)
        all_reversals.append(reversal)

    item_allocations = item_alloc_repo.list_all()
    allocations_by_item: dict[UUID, list[InvoiceItemAllocation]] = {}
    for allocation in item_allocations:
        allocations_by_item.setdefault(allocation.contract_item_id, []).append(allocation)

    invoice_items: dict[UUID, InvoiceItem] = {i.id: i for i in invoice_item_repo.list_all()}
    invoices: dict[UUID, Invoice] = {i.id: i for i in invoice_repo.list_all()}
    invoice_allocations = invoice_alloc_repo.list_all()
    cost_facts = cost_rec_repo.list_all()
    basis_facts = basis_repo.list_all()

    def is_purchase_in_period(invoice: Invoice) -> bool:
        # R001/R002 operate on 进项 (PURCHASE) invoices only — a SALES
        # invoice must never drive a reversal or suppress a new accrual.
        return (
            invoice.direction == InvoiceDirection.PURCHASE
            and invoice.issue_date is not None
            and invoice.issue_date <= end
        )

    def has_confirmed_invoice_in_period(contract_id: UUID) -> bool:
        """Section 22: a confirmed contract-level PURCHASE allocation
        whose invoice is dated by period_end means the purchase is
        already invoiced — even without an item match yet, no new accrual
        may be required for this contract."""
        for allocation in invoice_allocations:
            if allocation.contract_id != contract_id:
                continue
            if allocation.confirmation_type not in {ConfirmationType.AUTO_CONFIRMED, ConfirmationType.HUMAN_CONFIRMED}:
                continue
            invoice = invoices.get(allocation.invoice_id)
            if invoice is not None and is_purchase_in_period(invoice):
                return True
        return False

    def invoice_for_allocation(allocation: InvoiceAllocation) -> Invoice | None:
        return invoices.get(allocation.invoice_id)

    preview = PeriodClosePreview(period=period, period_end=end)

    # ---- R001 + R006: prior accrual now invoiced (item-scoped). ----
    # InvoiceItemAllocation quantity is a SHARED resource. One allocation
    # may never be consumed by two Accruals: every reversal on a
    # qualifying allocation counts against the item's shared capacity
    # regardless of which Accrual owns it. And when several open Accruals
    # compete for a positive unclaimed quantity, the engine must NOT pick
    # a FIFO winner — it emits MULTIPLE_OPEN_ACCRUALS_REQUIRE_EXPLICIT_SCOPE.
    open_accruals_by_item: dict[UUID, list[Accrual]] = {}
    for accrual in accruals:
        if is_open_accrual(accrual, reversals_by_accrual.get(accrual.id, [])):
            open_accruals_by_item.setdefault(accrual.contract_item_id, []).append(accrual)

    for contract_item_id, item_accruals in open_accruals_by_item.items():
        item = items.get(contract_item_id)
        if item is None:
            continue

        qualifying = [
            a
            for a in allocations_by_item.get(contract_item_id, [])
            if (inv := _invoice_of_allocation(a, invoice_items, invoices)) is not None
            and is_purchase_in_period(inv)
        ]
        qualifying_ids = {a.id for a in qualifying}

        contract_alloc_in_period = any(
            a.contract_id == item.contract_id
            and (inv := invoice_for_allocation(a)) is not None
            and is_purchase_in_period(inv)
            for a in invoice_allocations
        )

        if not qualifying:
            # Section 27: a contract-level purchase match exists but no
            # item match — never guess a reversal amount.
            if contract_alloc_in_period:
                for accrual in item_accruals:
                    preview.blockers.append(
                        CloseBlocker(
                            blocker_type=ITEM_MATCH_REQUIRED_FOR_REVERSAL,
                            contract_id=item.contract_id,
                            contract_item_id=contract_item_id,
                            accrual_id=accrual.id,
                        )
                    )
            continue

        # Shared consumption: every reversal on a qualifying allocation
        # consumes shared capacity — including reversals owned by fully
        # reversed (closed) sibling Accruals on the same item. An
        # allocation is a real resource; its quantity is never reusable.
        consumed_per_allocation: dict[UUID, Decimal] = {}
        for reversal in all_reversals:
            if reversal.invoice_item_allocation_id in qualifying_ids:
                consumed_per_allocation[reversal.invoice_item_allocation_id] = (
                    consumed_per_allocation.get(reversal.invoice_item_allocation_id, Decimal("0"))
                    + reversal.reversed_quantity
                )
        total_allocated = sum(a.allocated_quantity for a in qualifying)
        available = total_allocated - sum(consumed_per_allocation.values())
        if available <= 0:
            continue  # every qualifying allocation is already consumed

        if len(item_accruals) > 1:
            # Unclaimed quantity + multiple open accruals = contested
            # scope. FIFO assignment is forbidden; a human must scope it.
            preview.blockers.append(
                CloseBlocker(
                    blocker_type=MULTIPLE_OPEN_ACCRUALS_REQUIRE_EXPLICIT_SCOPE,
                    contract_id=item.contract_id,
                    contract_item_id=contract_item_id,
                    accrual_ids=tuple(a.id for a in item_accruals),
                )
            )
            continue

        accrual = item_accruals[0]
        reversals = reversals_by_accrual.get(accrual.id, [])
        remaining_qty, remaining_cost, reversed_qty, _ = get_accrual_balance(accrual, reversals)
        if remaining_qty <= 0 or remaining_cost <= 0:
            continue

        # The reversed portion's actual cost must be attributed to a
        # SINGLE allocation. When more than one qualifying allocation has
        # unclaimed quantity, picking one (by created_at or any other
        # order) would silently guess which invoice the reversal belongs
        # to — that is forbidden. An explicit allocation-to-accrual scope
        # is required instead.
        unclaimed_allocations = [
            a
            for a in qualifying
            if a.allocated_quantity - consumed_per_allocation.get(a.id, Decimal("0")) > 0
        ]
        if len(unclaimed_allocations) != 1:
            preview.blockers.append(
                CloseBlocker(
                    blocker_type=MULTIPLE_ITEM_ALLOCATIONS_REQUIRE_EXPLICIT_SCOPE,
                    contract_id=item.contract_id,
                    contract_item_id=contract_item_id,
                    accrual_id=accrual.id,
                )
            )
            continue

        source_allocation = unclaimed_allocations[0]
        unclaimed = source_allocation.allocated_quantity - consumed_per_allocation.get(
            source_allocation.id, Decimal("0")
        )
        reversal_qty = min(unclaimed, remaining_qty)
        if reversal_qty == remaining_qty:
            # Exact clear on the final reversal — avoid 0.01 residue from
            # Decimal unit-cost division (section 18).
            reversal_cost = remaining_cost
        else:
            unit_cost = accrual.estimated_cost / accrual.quantity
            reversal_cost = _quantize(unit_cost * reversal_qty)

        # Actual net cost of the reversed portion, from the single
        # scoping allocation — no ordering is ever involved.
        actual_net_cost = _quantize(
            (source_allocation.allocated_net_amount / source_allocation.allocated_quantity) * reversal_qty
        )

        projected_remaining_qty = remaining_qty - reversal_qty
        projected_remaining_cost = remaining_cost - reversal_cost
        projected_status = get_projected_accrual_status(reversed_qty + reversal_qty, projected_remaining_qty)

        trace_allocation_id = source_allocation.id

        preview.prior_accrual_reversals.append(
            PriorAccrualReversalRequired(
                accrual_id=accrual.id,
                contract_id=item.contract_id,
                contract_item_id=contract_item_id,
                source_period=accrual.period,
                basis=accrual.basis,
                reversal_quantity=reversal_qty,
                reversal_estimated_cost=reversal_cost,
                projected_remaining_quantity=projected_remaining_qty,
                projected_remaining_cost=projected_remaining_cost,
                projected_status=projected_status,
                source_fact_id=accrual.created_from_fact_id,
                invoice_item_allocation_id=trace_allocation_id,
            )
        )
        preview.accrual_actual_differences.append(
            AccrualActualDifference(
                contract_id=item.contract_id,
                contract_item_id=contract_item_id,
                actual_net_cost=actual_net_cost,
                reversed_estimated_cost=reversal_cost,
                difference=_quantize(actual_net_cost - reversal_cost),
                source_fact_id=accrual.created_from_fact_id,
                invoice_item_allocation_id=trace_allocation_id,
            )
        )

    # ---- R002 + R003 + R007: new accrual candidates. ----
    open_accrual_item_ids = {
        a.contract_item_id
        for a in accruals
        if is_open_accrual(a, reversals_by_accrual.get(a.id, []))
    }

    for contract in sorted(contracts.values(), key=lambda c: (c.contract_no, c.counterparty or "")):
        recognition_facts = [f for f in cost_facts if f.contract_id == contract.id and f.recognition_date <= end]
        if not recognition_facts:
            continue
        # Section 22: already invoiced by period_end -> NOT AccrualRequired.
        if has_confirmed_invoice_in_period(contract.id):
            continue

        contract_items = items_by_contract.get(contract.id, [])
        complete_items = [i for i in contract_items if i.quantity is not None]
        open_item_ids = {i.id for i in contract_items if i.id in open_accrual_item_ids}
        item_basis = [
            b for b in basis_facts if b.contract_id == contract.id and b.scope_type == AccrualBasisScopeType.CONTRACT_ITEM
        ]
        contract_basis = [
            b for b in basis_facts if b.contract_id == contract.id and b.scope_type == AccrualBasisScopeType.CONTRACT
        ]
        basis_by_item: dict[UUID, AccrualBasisFact] = {b.contract_item_id: b for b in item_basis if b.contract_item_id}

        recognition_fact = min(recognition_facts, key=lambda f: f.recognition_date)
        emitted = False

        for item in sorted(complete_items, key=lambda i: i.source_item_key or ""):
            if item.id in open_item_ids:
                continue  # R003 duplicate guard
            basis = basis_by_item.get(item.id)
            if basis is None:
                continue
            preview.new_accrual_requirements.append(
                AccrualRequired(
                    level="CONTRACT_ITEM",
                    contract_id=contract.id,
                    contract_item_id=item.id,
                    quantity=basis.quantity,
                    estimated_cost=basis.estimated_cost,
                    basis=basis.basis,
                    cost_recognition_fact_id=recognition_fact.id,
                    accrual_basis_fact_id=basis.id,
                )
            )
            emitted = True

        if emitted:
            continue

        # No item-level emission.
        if open_item_ids and not (item_basis or contract_basis):
            continue  # every complete item is guarded by an open accrual — nothing to do (R003)
        if not (item_basis or contract_basis):
            # Section 26: cost recognition confirmed but no basis at all —
            # a diagnostic blocker, deliberately NOT the PROPOSED R011
            # EvidenceMissing Decision.
            preview.blockers.append(
                CloseBlocker(blocker_type=MISSING_ACCRUAL_BASIS, contract_id=contract.id)
            )
            continue
        if not complete_items and contract_basis:
            # R007: item detail incomplete -> contract-level Candidate only,
            # never a formal Accrual (contract_item_id is required).
            basis = min(contract_basis, key=lambda b: b.created_at)
            preview.contract_level_candidates.append(
                AccrualCandidate(
                    level="CONTRACT",
                    contract_id=contract.id,
                    estimated_cost=basis.estimated_cost,
                    blocking_reason=MISSING_CONTRACT_ITEM_EVIDENCE,
                    cost_recognition_fact_id=recognition_fact.id,
                    accrual_basis_fact_id=basis.id,
                )
            )
        elif complete_items:
            preview.blockers.append(CloseBlocker(blocker_type=MISSING_ACCRUAL_BASIS, contract_id=contract.id))

    return preview


def _invoice_of_allocation(allocation: InvoiceItemAllocation, invoice_items, invoices) -> Invoice | None:
    invoice_item = invoice_items.get(allocation.invoice_item_id)
    if invoice_item is None:
        return None
    return invoices.get(invoice_item.invoice_id)
