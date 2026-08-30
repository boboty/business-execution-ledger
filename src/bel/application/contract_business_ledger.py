"""Contract Business Ledger — cross-contract read projection (Phase
2D.1-R4, docs/ROADMAP.md).

    confirmed canonical Facts
    + existing deterministic current-state projections
          -> cross-contract read model
          -> page + Data Product (CSV / XLSX)

This is NOT a new Fact, NOT a new persisted derived-state table, and NOT
a reimplementation of Contract 360's per-contract composition N times
over. It reuses the exact repository seams that already resolve
"current" (ContractItemRepository / ShipmentRepository /
SalesContractRepository's anchor+revision current-join,
ProcurementSalesLinkRepository's current-episode predicate) and the
shared accrual-balance function — never re-deriving any of them.

Primary axis (docs/V1-SCOPE.md section 5 item 1): ONE row per
PROCUREMENT Contract. Linked sales scopes are nested, per-scope
projections — see ``ContractLedgerSalesScope`` — and are NEVER summed
across the many-to-many ``ProcurementSalesLink`` bridge
(docs/PHASE2D1-R0-DECISIONS.md section 2.4: "No cross-bridge
apportionment in V1").

Absence of a Fact is not a negative business assertion (docs/V1-SCOPE.md
section 2.1). No column here encodes "not shipped", "unpaid", "not
invoiced", or "no accrual needed" — only "no confirmed Fact of this kind
exists yet". Outbound-invoicing eligibility is explicitly not decided
here (Phase 2D.3, docs/PHASE2D1-R0-DECISIONS.md section 3.6).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from bel.application.list_matches import list_match_cases
from bel.application.sales_matching import list_sales_match_cases
from bel.domain.accrual import Accrual, get_accrual_balance, get_projected_accrual_status
from bel.domain.contract import Contract, ContractItem
from bel.domain.exception import ExceptionStatus
from bel.domain.invoice import Invoice, InvoiceDirection
from bel.domain.matching import (
    InvoiceAllocation,
    MatchCaseStatus,
    PaymentAllocation,
    SalesInvoiceAllocation,
    SalesPaymentAllocation,
)
from bel.domain.payment import Payment, PaymentDirection
from bel.domain.procurement_sales_link import ProcurementSalesLink
from bel.domain.sales_contract import SalesContract
from bel.domain.shipment import Shipment
from bel.infrastructure.persistence.repositories import (
    AccrualReversalRepository,
    AccrualRepository,
    ContractItemRepository,
    ContractRepository,
    ExceptionRepository,
    InvoiceAllocationRepository,
    InvoiceRepository,
    MatchCandidateRepository,
    PaymentAllocationRepository,
    PaymentRepository,
    ProcurementSalesLinkRepository,
    SalesContractRepository,
    SalesInvoiceAllocationRepository,
    SalesMatchCandidateRepository,
    SalesPaymentAllocationRepository,
    ShipmentRepository,
)

# ---------------------------------------------------------------------------
# Presentation-neutral DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContractLedgerShipment:
    shipment: Shipment


@dataclass(frozen=True)
class ContractLedgerProcurementInvoice:
    allocation: InvoiceAllocation
    invoice: Invoice | None


@dataclass(frozen=True)
class ContractLedgerOutgoingPayment:
    allocation: PaymentAllocation
    payment: Payment | None


@dataclass(frozen=True)
class ContractLedgerAccrualState:
    """Current persisted accrual state only (never a period-close
    projected decision — see docs/PHASE2D1-R0-DECISIONS.md section on
    Contract360's ContractDecisions, deliberately NOT reused here)."""

    accrual: Accrual
    contract_item_id: uuid.UUID
    remaining_quantity: Decimal
    remaining_estimated_cost: Decimal
    reversed_quantity: Decimal
    reversed_estimated_cost: Decimal
    projected_status: str


@dataclass(frozen=True)
class ContractLedgerSalesInvoiceAllocation:
    allocation: SalesInvoiceAllocation
    invoice: Invoice | None


@dataclass(frozen=True)
class ContractLedgerIncomingReceiptAllocation:
    allocation: SalesPaymentAllocation
    payment: Payment | None


@dataclass(frozen=True)
class ContractLedgerSalesScope:
    """ONE linked SalesContract's own confirmed facts — never a figure
    attributed to the procurement contract that links to it. See spec
    section 13: the same SalesContract projected onto two procurement
    rows shows the SAME scope-level facts on both, never split or
    duplicated as if it were two different amounts."""

    sales_contract: SalesContract
    link: ProcurementSalesLink
    sales_invoice_allocations: tuple[ContractLedgerSalesInvoiceAllocation, ...]
    incoming_receipt_allocations: tuple[ContractLedgerIncomingReceiptAllocation, ...]
    has_unresolved: bool


@dataclass(frozen=True)
class ContractLedgerUnresolvedWork:
    source: str  # "TASK_EXCEPTION" | "MATCH_CASE"
    exception_type: str | None
    summary: str
    source_id: uuid.UUID


@dataclass(frozen=True)
class ContractLedgerRow:
    contract: Contract
    items: tuple[ContractItem, ...]
    shipments: tuple[ContractLedgerShipment, ...]
    procurement_invoices: tuple[ContractLedgerProcurementInvoice, ...]
    outgoing_payments: tuple[ContractLedgerOutgoingPayment, ...]
    accruals: tuple[ContractLedgerAccrualState, ...]
    sales_scopes: tuple[ContractLedgerSalesScope, ...]
    unresolved_work: tuple[ContractLedgerUnresolvedWork, ...]

    @property
    def has_unresolved(self) -> bool:
        if self.unresolved_work:
            return True
        return any(scope.has_unresolved for scope in self.sales_scopes)


@dataclass(frozen=True)
class ContractLedgerFilters:
    contract_no: str | None = None
    supplier: str | None = None  # Contract.counterparty (domestic supplier)
    our_entity: str | None = None  # Contract.buyer (our own entity — never a customer key)
    sales_contract_no: str | None = None
    customer: str | None = None  # SalesContract.customer only
    has_unresolved: bool | None = None

    def is_empty(self) -> bool:
        return (
            not self.contract_no
            and not self.supplier
            and not self.our_entity
            and not self.sales_contract_no
            and not self.customer
            and self.has_unresolved is None
        )


@dataclass(frozen=True)
class ContractBusinessLedger:
    rows: tuple[ContractLedgerRow, ...]
    filters: ContractLedgerFilters


# ---------------------------------------------------------------------------
# Unresolved-work resolution — structured IDs only, never text parsing of
# TaskException.summary. See docs section 20/37.
# ---------------------------------------------------------------------------


def _uuid_or_none(value: object) -> uuid.UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _collect_unresolved_work(
    session: Session,
) -> tuple[dict[uuid.UUID, list[ContractLedgerUnresolvedWork]], dict[uuid.UUID, list[ContractLedgerUnresolvedWork]]]:
    """Returns (by_procurement_contract_id, by_sales_contract_id).

    Every association below is resolved through a structured FK/typed
    field already present on the TaskException.detail dict or through an
    explicit repository lookup by id — never by parsing ``summary`` text.
    A TaskException whose association cannot be determined this way
    (e.g. SALES_CONTRACT_IDENTITY_INCOMPLETE, which names no anchor
    because none was created) is not guessed and appears on no row.
    """
    by_contract: dict[uuid.UUID, list[ContractLedgerUnresolvedWork]] = {}
    by_sales_contract: dict[uuid.UUID, list[ContractLedgerUnresolvedWork]] = {}

    def _add_contract(contract_id: uuid.UUID | None, work: ContractLedgerUnresolvedWork) -> None:
        if contract_id is not None:
            by_contract.setdefault(contract_id, []).append(work)

    def _add_sales_contract(sales_contract_id: uuid.UUID | None, work: ContractLedgerUnresolvedWork) -> None:
        if sales_contract_id is not None:
            by_sales_contract.setdefault(sales_contract_id, []).append(work)

    item_repo = ContractItemRepository(session)
    shipment_repo = ShipmentRepository(session)
    link_repo = ProcurementSalesLinkRepository(session)

    for exc in ExceptionRepository(session).list_all():
        if exc.status != ExceptionStatus.OPEN:
            continue
        work = ContractLedgerUnresolvedWork(
            source="TASK_EXCEPTION", exception_type=exc.exception_type, summary=exc.summary, source_id=exc.id
        )
        detail = exc.detail or {}

        if exc.exception_type == "ContractItemFactSuperseded":
            item = item_repo.get(_uuid_or_none(detail.get("contract_item_id")))
            _add_contract(item.contract_id if item else None, work)
        elif exc.exception_type == "ShipmentFactSuperseded":
            shipment = shipment_repo.get(_uuid_or_none(detail.get("shipment_id")))
            _add_contract(shipment.contract_id if shipment else None, work)
        elif exc.exception_type == "ShipmentIdentityIncomplete":
            _add_contract(_uuid_or_none(detail.get("contract_id")), work)
        elif exc.exception_type == "ShipmentIdentityConflict":
            shipment = shipment_repo.get(_uuid_or_none(detail.get("shipment_id")))
            _add_contract(shipment.contract_id if shipment else None, work)
        elif exc.exception_type == "AllocationCapacityExceeded":
            _add_contract(_uuid_or_none(detail.get("contract_id")), work)
        elif exc.exception_type == "ProcurementSalesLinkUnconfirmed":
            _add_contract(_uuid_or_none(detail.get("procurement_contract_id")), work)
        elif exc.exception_type == "ProcurementSalesLinkMultipleScopes":
            _add_contract(_uuid_or_none(detail.get("procurement_contract_id")), work)
        elif exc.exception_type == "ProcurementSalesLinkCorrectionConflict":
            link = link_repo.get(_uuid_or_none(detail.get("superseded_link_id")))
            _add_contract(link.procurement_contract_id if link else None, work)
        elif exc.exception_type == "SalesContractCustomerUnresolved":
            _add_sales_contract(_uuid_or_none(detail.get("sales_contract_id")), work)
        elif exc.exception_type == "BusinessKeyConflict":
            if "contract_ids" in detail and isinstance(detail["contract_ids"], list):
                for raw_id in detail["contract_ids"]:
                    _add_contract(_uuid_or_none(raw_id), work)
            elif "sales_contract_id" in detail:
                _add_sales_contract(_uuid_or_none(detail.get("sales_contract_id")), work)
        # SalesContractIdentityIncomplete: no anchor exists — genuinely
        # unmappable, never guessed. Intentionally not handled above.

    match_candidate_repo = MatchCandidateRepository(session)
    for case in list_match_cases(session, status=MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED):
        summary = f"{case.subject_type} {case.subject_id} 需要人工确认匹配"
        work = ContractLedgerUnresolvedWork(
            source="MATCH_CASE", exception_type=None, summary=summary, source_id=case.id
        )
        for candidate in match_candidate_repo.list_for_case(case.id):
            _add_contract(candidate.contract_id, work)

    sales_match_candidate_repo = SalesMatchCandidateRepository(session)
    for case in list_sales_match_cases(session, status=MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED):
        summary = f"{case.subject_type} {case.subject_id} 需要人工确认销售侧匹配"
        work = ContractLedgerUnresolvedWork(
            source="MATCH_CASE", exception_type=None, summary=summary, source_id=case.id
        )
        for candidate in sales_match_candidate_repo.list_for_case(case.id):
            _add_sales_contract(candidate.sales_contract_id, work)

    return by_contract, by_sales_contract


# ---------------------------------------------------------------------------
# Main projection
# ---------------------------------------------------------------------------


def _matches(value: str | None, needle: str | None) -> bool:
    """Case-insensitive substring match. A missing filter always passes;
    a present filter never matches a missing (None) business value —
    unknown must never be silently treated as a match."""
    if not needle:
        return True
    if not value:
        return False
    return needle.strip().lower() in value.lower()


def get_contract_business_ledger(
    session: Session, filters: ContractLedgerFilters | None = None
) -> ContractBusinessLedger:
    """Compose the cross-contract Ledger. Strict read-only — the whole
    body runs under ``session.no_autoflush``. Page, CSV and XLSX export
    all call this single function with the same filters, so they can
    never diverge (docs section 26)."""
    filters = filters or ContractLedgerFilters()

    with session.no_autoflush:
        all_contracts = ContractRepository(session).list_all()
        candidate_contracts = [
            c
            for c in all_contracts
            if _matches(c.contract_no, filters.contract_no)
            and _matches(c.counterparty, filters.supplier)
            and _matches(c.buyer, filters.our_entity)
        ]
        # Deterministic primary ordering — contract_no then contract_id.
        candidate_contracts.sort(key=lambda c: (c.contract_no, str(c.id)))

        by_contract_unresolved, by_sales_contract_unresolved = _collect_unresolved_work(session)

        item_repo = ContractItemRepository(session)
        shipment_repo = ShipmentRepository(session)
        invoice_alloc_repo = InvoiceAllocationRepository(session)
        payment_alloc_repo = PaymentAllocationRepository(session)
        invoice_repo = InvoiceRepository(session)
        payment_repo = PaymentRepository(session)
        accrual_repo = AccrualRepository(session)
        reversal_repo = AccrualReversalRepository(session)
        link_repo = ProcurementSalesLinkRepository(session)
        sales_contract_repo = SalesContractRepository(session)
        sales_invoice_alloc_repo = SalesInvoiceAllocationRepository(session)
        sales_payment_alloc_repo = SalesPaymentAllocationRepository(session)

        rows: list[ContractLedgerRow] = []
        for contract in candidate_contracts:
            items = tuple(
                sorted(item_repo.list_for_contract(contract.id), key=lambda i: (i.created_at, str(i.id)))
            )

            shipments = tuple(
                ContractLedgerShipment(shipment=s)
                for s in sorted(shipment_repo.list_for_contract(contract.id), key=lambda s: (s.created_at, str(s.id)))
            )

            # Defensive direction guard: InvoiceAllocation/PaymentAllocation
            # are documented as procurement-only (PURCHASE invoice, OUT
            # payment — docs/PHASE2D1-R0-DECISIONS.md section 2.1), and the
            # only writer that exists today (matching.py's M001 pass)
            # already filters to that direction. The repository itself
            # enforces no such constraint, so this projection re-checks the
            # subject's own direction before ever labeling it a "procurement
            # invoice"/"outgoing payment" — a SALES invoice or IN payment
            # must never be displayed under those columns, structurally.
            procurement_invoices = tuple(
                ContractLedgerProcurementInvoice(allocation=a, invoice=invoice)
                for a, invoice in (
                    (a, invoice_repo.get(a.invoice_id))
                    for a in sorted(
                        invoice_alloc_repo.list_for_contract(contract.id), key=lambda a: (a.created_at, str(a.id))
                    )
                )
                if invoice is None or invoice.direction == InvoiceDirection.PURCHASE
            )
            outgoing_payments = tuple(
                ContractLedgerOutgoingPayment(allocation=a, payment=payment)
                for a, payment in (
                    (a, payment_repo.get(a.payment_id))
                    for a in sorted(
                        payment_alloc_repo.list_for_contract(contract.id), key=lambda a: (a.created_at, str(a.id))
                    )
                )
                if payment is None or payment.direction == PaymentDirection.OUT
            )

            accruals: list[ContractLedgerAccrualState] = []
            for item in items:
                for accrual in sorted(
                    accrual_repo.list_for_contract_item(item.id), key=lambda a: (a.created_at, str(a.id))
                ):
                    reversals = reversal_repo.list_for_accrual(accrual.id)
                    remaining_qty, remaining_cost, reversed_qty, reversed_cost = get_accrual_balance(
                        accrual, reversals
                    )
                    accruals.append(
                        ContractLedgerAccrualState(
                            accrual=accrual,
                            contract_item_id=item.id,
                            remaining_quantity=remaining_qty,
                            remaining_estimated_cost=remaining_cost,
                            reversed_quantity=reversed_qty,
                            reversed_estimated_cost=reversed_cost,
                            projected_status=get_projected_accrual_status(reversed_qty, remaining_qty),
                        )
                    )

            sales_scopes: list[ContractLedgerSalesScope] = []
            for link in sorted(
                link_repo.list_current_links_for_procurement_contract(contract.id),
                key=lambda l: (l.created_at, str(l.id)),
            ):
                sales_contract = sales_contract_repo.get(link.sales_contract_id)
                if sales_contract is None:
                    continue
                sales_invoices = tuple(
                    ContractLedgerSalesInvoiceAllocation(allocation=a, invoice=invoice_repo.get(a.invoice_id))
                    for a in sorted(
                        sales_invoice_alloc_repo.list_for_sales_contract(sales_contract.id),
                        key=lambda a: (a.created_at, str(a.id)),
                    )
                )
                incoming_receipts = tuple(
                    ContractLedgerIncomingReceiptAllocation(allocation=a, payment=payment_repo.get(a.payment_id))
                    for a in sorted(
                        sales_payment_alloc_repo.list_for_sales_contract(sales_contract.id),
                        key=lambda a: (a.created_at, str(a.id)),
                    )
                )
                sales_scopes.append(
                    ContractLedgerSalesScope(
                        sales_contract=sales_contract,
                        link=link,
                        sales_invoice_allocations=sales_invoices,
                        incoming_receipt_allocations=incoming_receipts,
                        has_unresolved=bool(by_sales_contract_unresolved.get(sales_contract.id)),
                    )
                )

            row_unresolved = tuple(by_contract_unresolved.get(contract.id, []))

            row = ContractLedgerRow(
                contract=contract,
                items=items,
                shipments=shipments,
                procurement_invoices=procurement_invoices,
                outgoing_payments=outgoing_payments,
                accruals=tuple(accruals),
                sales_scopes=tuple(sales_scopes),
                unresolved_work=row_unresolved,
            )

            if not _matches(_row_sales_contract_no_text(row), filters.sales_contract_no):
                continue
            if not _matches(_row_customer_text(row), filters.customer):
                continue
            if filters.has_unresolved is not None and row.has_unresolved != filters.has_unresolved:
                continue

            rows.append(row)

        return ContractBusinessLedger(rows=tuple(rows), filters=filters)


def _row_sales_contract_no_text(row: ContractLedgerRow) -> str | None:
    """Search text across every linked scope's sales_contract_no — this
    never merges or sums figures, it only lets a filter match any one of
    several legitimately-linked scopes (spec section 35)."""
    values = [s.sales_contract.sales_contract_no for s in row.sales_scopes if s.sales_contract.sales_contract_no]
    return " ".join(values) if values else None


def _row_customer_text(row: ContractLedgerRow) -> str | None:
    values = [s.sales_contract.customer for s in row.sales_scopes if s.sales_contract.customer]
    return " ".join(values) if values else None
