"""Invoice Preparation Context — rule-neutral factual foundation
(Phase 2D.3-F0).

Two business questions motivate this read path:

1. SALES INVOICE PREPARATION — "what facts do we currently know about
   issuing an invoice to the external sales customer?"
2. SUPPLIER INVOICE REQUEST — "what facts do we currently know that may
   later support telling a supplier how to invoice us?"

This round decides NEITHER question. Invoice eligibility, readiness,
remaining invoice quantity/amount, tax-rate inference and cross-bridge
apportionment are all deferred to the Phase 2D.3 business-rule freeze
which is running externally. What exists here is only the already
confirmed/current Facts and associations, composed per scope:

- ``sales_scopes``  — primary axis ``SalesContract`` (the ONLY place an
  external customer is expressed, docs/DOMAIN.md).
- ``supplier_scopes`` — primary axis procurement ``Contract``
  (``Contract.buyer`` is our own entity, never a customer).

Hard boundaries mirrored from the frozen Domain (none re-derived here):

- ProcurementSalesLink is many-to-many; linked procurement Contracts are
  ENUMERATED per sales scope, never apportioned or summed across the
  bridge.
- SALES invoices associate to a SalesContract ONLY through
  ``SalesInvoiceAllocation``; IN receipts ONLY through
  ``SalesPaymentAllocation``.
- ``InvoiceAllocation`` stays PURCHASE-only and ``PaymentAllocation``
  OUT-only — both appear only on supplier scopes, with the same
  defensive direction re-check ``get_contract_business_ledger`` uses.
- Absence of a Fact is factual absence, never a negative business
  assertion: a scope with no sales invoice allocation says nothing about
  whether anything "should" be invoiced.
- ``current`` semantics come exclusively from the repositories
  (anchor+revision current-join, current-link predicate, current-fact
  exclusion) — never re-implemented here.

Strictly read-only: the whole body runs under ``session.no_autoflush``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from bel.application.contract_business_ledger import (
    ContractLedgerUnresolvedWork,
    _collect_unresolved_work,
    _matches,
)
from bel.domain.accrual import InvoiceItemAllocation
from bel.domain.contract import Contract, ContractItem
from bel.domain.invoice import Invoice, InvoiceDirection, InvoiceItem
from bel.domain.matching import (
    InvoiceAllocation,
    PaymentAllocation,
    SalesInvoiceAllocation,
    SalesPaymentAllocation,
)
from bel.domain.payment import Payment, PaymentDirection
from bel.domain.procurement_sales_link import ProcurementSalesLink
from bel.domain.sales_contract import SalesContract
from bel.domain.shipment import Shipment
from bel.infrastructure.persistence.repositories import (
    ContractItemRepository,
    ContractRepository,
    InvoiceAllocationRepository,
    InvoiceItemAllocationRepository,
    InvoiceItemRepository,
    InvoiceRepository,
    PaymentAllocationRepository,
    PaymentRepository,
    ProcurementSalesLinkRepository,
    SalesContractRepository,
    SalesInvoiceAllocationRepository,
    SalesPaymentAllocationRepository,
    ShipmentRepository,
)

# ---------------------------------------------------------------------------
# Presentation-neutral DTOs — facts and associations only. Deliberately
# NO status/eligibility/remaining field exists anywhere in this DTO tree:
# those concepts require the Phase 2D.3 rule freeze.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SalesScopeInvoiceAllocation:
    """One existing SALES-invoice association — the Invoice Fact plus the
    SalesInvoiceAllocation that associates it to this SalesContract."""

    allocation: SalesInvoiceAllocation
    invoice: Invoice | None


@dataclass(frozen=True)
class SalesScopePaymentAllocation:
    """One existing IN-receipt association — the Payment Fact plus the
    SalesPaymentAllocation that associates it to this SalesContract."""

    allocation: SalesPaymentAllocation
    payment: Payment | None


@dataclass(frozen=True)
class SalesScopeLinkedProcurementContract:
    """One CURRENT ProcurementSalesLink and the procurement Contract it
    names. Enumeration only — no amount, quantity or ratio crosses this
    edge (frozen: no cross-bridge apportionment)."""

    link: ProcurementSalesLink
    contract: Contract | None


@dataclass(frozen=True)
class SalesScopeContext:
    """Everything currently known about one SalesContract as an invoice
    preparation FACT context. Every field is an existing Fact or an
    existing association; ``None`` stays ``None`` (unknown), and an empty
    tuple means "no such Fact exists yet" — never "not invoiced"."""

    sales_contract: SalesContract
    linked_procurement_contracts: tuple[SalesScopeLinkedProcurementContract, ...]
    invoice_allocations: tuple[SalesScopeInvoiceAllocation, ...]
    payment_allocations: tuple[SalesScopePaymentAllocation, ...]
    unresolved_work: tuple[ContractLedgerUnresolvedWork, ...]


@dataclass(frozen=True)
class SupplierScopeInvoiceAllocation:
    """One existing PURCHASE-invoice association — the Invoice Fact plus
    the (procurement-only) InvoiceAllocation."""

    allocation: InvoiceAllocation
    invoice: Invoice | None


@dataclass(frozen=True)
class SupplierScopeInvoiceItemAllocation:
    """One current InvoiceItemAllocation reachable from THIS contract's
    ContractItems, with its InvoiceItem Fact and that item's parent
    Invoice Fact. Direction-isolated like every other supplier-side
    entry: the parent Invoice must exist and be PURCHASE — a SALES or
    UNKNOWN parent invoice's item allocation must never read as
    已确认明细关联 here, even though ContractItem membership alone
    cannot tell the directions apart (InvoiceItemAllocation itself
    carries no direction and no sales-side analogue)."""

    allocation: InvoiceItemAllocation
    invoice_item: InvoiceItem | None
    invoice: Invoice | None


@dataclass(frozen=True)
class SupplierScopePaymentAllocation:
    """One existing OUT-payment association — the Payment Fact plus the
    (procurement-only) PaymentAllocation."""

    allocation: PaymentAllocation
    payment: Payment | None


@dataclass(frozen=True)
class SupplierScopeContext:
    """Everything currently known about one procurement Contract as a
    supplier-invoice-request FACT context. ``Contract.buyer`` is our own
    entity; no external customer exists anywhere on this scope."""

    contract: Contract
    items: tuple[ContractItem, ...]
    shipments: tuple[Shipment, ...]
    invoice_allocations: tuple[SupplierScopeInvoiceAllocation, ...]
    invoice_item_allocations: tuple[SupplierScopeInvoiceItemAllocation, ...]
    payment_allocations: tuple[SupplierScopePaymentAllocation, ...]
    unresolved_work: tuple[ContractLedgerUnresolvedWork, ...]


@dataclass(frozen=True)
class InvoicePreparationContext:
    sales_scopes: tuple[SalesScopeContext, ...]
    supplier_scopes: tuple[SupplierScopeContext, ...]


@dataclass(frozen=True)
class InvoicePreparationFilters:
    """Optional narrow filters, applied per primary axis. Same substring
    semantics as the Contract Business Ledger (unknown never matches a
    present needle). Deliberately NO status/eligibility filter."""

    our_entity: str | None = None  # SalesContract.our_entity
    sales_contract_no: str | None = None
    customer: str | None = None  # SalesContract.customer only
    contract_no: str | None = None  # procurement Contract
    supplier: str | None = None  # Contract.counterparty

    def is_empty(self) -> bool:
        return (
            not self.our_entity
            and not self.sales_contract_no
            and not self.customer
            and not self.contract_no
            and not self.supplier
        )


# ---------------------------------------------------------------------------
# Main projection
# ---------------------------------------------------------------------------


def get_invoice_preparation_context(
    session: Session, filters: InvoicePreparationFilters | None = None
) -> InvoicePreparationContext:
    """Compose the read-only invoice-preparation FACT context. Strictly
    read-only — no Fact, Task, MatchCase or business-state write, and no
    autoflush side effect. Page consumers call this single function so
    the Web surface can never diverge from the Application context."""
    filters = filters or InvoicePreparationFilters()

    with session.no_autoflush:
        # Structured unresolved-work association — reused verbatim from the
        # Contract Business Ledger so there is exactly ONE mapping from
        # TaskException/MatchCase to a contract/sales contract, never a
        # second one to keep in sync.
        by_contract_unresolved, by_sales_contract_unresolved = _collect_unresolved_work(session)

        sales_scopes = _build_sales_scopes(session, filters, by_sales_contract_unresolved)
        supplier_scopes = _build_supplier_scopes(session, filters, by_contract_unresolved)

        return InvoicePreparationContext(sales_scopes=sales_scopes, supplier_scopes=supplier_scopes)


def _build_sales_scopes(
    session: Session,
    filters: InvoicePreparationFilters,
    by_sales_contract_unresolved: dict[uuid.UUID, list[ContractLedgerUnresolvedWork]],
) -> tuple[SalesScopeContext, ...]:
    sales_contract_repo = SalesContractRepository(session)
    link_repo = ProcurementSalesLinkRepository(session)
    contract_repo = ContractRepository(session)
    sales_invoice_alloc_repo = SalesInvoiceAllocationRepository(session)
    sales_payment_alloc_repo = SalesPaymentAllocationRepository(session)
    invoice_repo = InvoiceRepository(session)
    payment_repo = PaymentRepository(session)

    scopes: list[SalesScopeContext] = []
    for sales_contract in sorted(
        sales_contract_repo.list_all(), key=lambda sc: (sc.sales_contract_no, str(sc.id))
    ):
        if not _matches(sales_contract.our_entity, filters.our_entity):
            continue
        if not _matches(sales_contract.sales_contract_no, filters.sales_contract_no):
            continue
        if not _matches(sales_contract.customer, filters.customer):
            continue

        # Current links only — the repository owns the current-episode
        # predicate; enumerated as-is, never apportioned.
        links = tuple(
            sorted(
                link_repo.list_current_links_for_sales_contract(sales_contract.id),
                key=lambda l: (l.created_at, str(l.id)),
            )
        )
        linked_contracts = tuple(
            SalesScopeLinkedProcurementContract(link=link, contract=contract_repo.get(link.procurement_contract_id))
            for link in links
        )

        sales_invoices = tuple(
            SalesScopeInvoiceAllocation(allocation=a, invoice=invoice_repo.get(a.invoice_id))
            for a in sorted(
                sales_invoice_alloc_repo.list_for_sales_contract(sales_contract.id),
                key=lambda a: (a.created_at, str(a.id)),
            )
        )
        incoming_receipts = tuple(
            SalesScopePaymentAllocation(allocation=a, payment=payment_repo.get(a.payment_id))
            for a in sorted(
                sales_payment_alloc_repo.list_for_sales_contract(sales_contract.id),
                key=lambda a: (a.created_at, str(a.id)),
            )
        )

        scopes.append(
            SalesScopeContext(
                sales_contract=sales_contract,
                linked_procurement_contracts=linked_contracts,
                invoice_allocations=sales_invoices,
                payment_allocations=incoming_receipts,
                unresolved_work=tuple(by_sales_contract_unresolved.get(sales_contract.id, [])),
            )
        )
    return tuple(scopes)


def _build_supplier_scopes(
    session: Session,
    filters: InvoicePreparationFilters,
    by_contract_unresolved: dict[uuid.UUID, list[ContractLedgerUnresolvedWork]],
) -> tuple[SupplierScopeContext, ...]:
    contract_repo = ContractRepository(session)
    item_repo = ContractItemRepository(session)
    shipment_repo = ShipmentRepository(session)
    invoice_alloc_repo = InvoiceAllocationRepository(session)
    payment_alloc_repo = PaymentAllocationRepository(session)
    invoice_repo = InvoiceRepository(session)
    payment_repo = PaymentRepository(session)
    invoice_item_repo = InvoiceItemRepository(session)
    item_allocation_repo = InvoiceItemAllocationRepository(session)

    scopes: list[SupplierScopeContext] = []
    for contract in sorted(contract_repo.list_all(), key=lambda c: (c.contract_no, str(c.id))):
        if not _matches(contract.contract_no, filters.contract_no):
            continue
        if not _matches(contract.counterparty, filters.supplier):
            continue

        items = tuple(sorted(item_repo.list_for_contract(contract.id), key=lambda i: (i.created_at, str(i.id))))
        item_ids = {item.id for item in items}
        shipments = tuple(
            sorted(shipment_repo.list_for_contract(contract.id), key=lambda s: (s.created_at, str(s.id)))
        )

        # Same defensive direction guard as the Contract Business Ledger:
        # InvoiceAllocation/PaymentAllocation are procurement-only
        # (PURCHASE invoice / OUT payment); the subject's own direction is
        # re-checked before it can appear under this scope.
        procurement_invoices = tuple(
            SupplierScopeInvoiceAllocation(allocation=a, invoice=invoice)
            for a, invoice in (
                (a, invoice_repo.get(a.invoice_id))
                for a in sorted(
                    invoice_alloc_repo.list_for_contract(contract.id), key=lambda a: (a.created_at, str(a.id))
                )
            )
            if invoice is None or invoice.direction == InvoiceDirection.PURCHASE
        )
        outgoing_payments = tuple(
            SupplierScopePaymentAllocation(allocation=a, payment=payment)
            for a, payment in (
                (a, payment_repo.get(a.payment_id))
                for a in sorted(
                    payment_alloc_repo.list_for_contract(contract.id), key=lambda a: (a.created_at, str(a.id))
                )
            )
            if payment is None or payment.direction == PaymentDirection.OUT
        )

        # Current InvoiceItemAllocations on THIS contract's items, with
        # their InvoiceItem Facts and the parent Invoice Fact. Facts only —
        # no remaining-quantity or remaining-amount concept. Direction
        # isolation (pre-Gate repair): ContractItem membership alone cannot
        # tell which side an allocation's invoice belongs to, so the parent
        # Invoice is resolved and must exist and be PURCHASE — a SALES or
        # UNKNOWN parent invoice's item allocation never appears in this
        # supplier context.
        item_allocations = tuple(
            SupplierScopeInvoiceItemAllocation(
                allocation=allocation, invoice_item=invoice_item, invoice=parent_invoice
            )
            for allocation, invoice_item, parent_invoice in (
                (a, invoice_item, invoice_repo.get(invoice_item.invoice_id) if invoice_item is not None else None)
                for a, invoice_item in (
                    (a, invoice_item_repo.get(a.invoice_item_id))
                    for a in sorted(
                        (a for a in item_allocation_repo.list_all() if a.contract_item_id in item_ids),
                        key=lambda a: (a.created_at, str(a.id)),
                    )
                )
            )
            if parent_invoice is not None and parent_invoice.direction == InvoiceDirection.PURCHASE
        )

        scopes.append(
            SupplierScopeContext(
                contract=contract,
                items=items,
                shipments=shipments,
                invoice_allocations=procurement_invoices,
                invoice_item_allocations=item_allocations,
                payment_allocations=outgoing_payments,
                unresolved_work=tuple(by_contract_unresolved.get(contract.id, [])),
            )
        )
    return tuple(scopes)
