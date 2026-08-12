"""InvoiceItemAllocation safety constraints (spec section 11).

Every path that creates an InvoiceItemAllocation — the Close Fact Pack
import and the ``bel invoice-item allocate`` CLI — must pass through
these checks. Phase 2B adds no automatic item-matching algorithm, so
this module only *validates* an already-chosen relationship; it never
pairs items by name or by position (section 11-D: uncertain -> Task /
explicit failure).
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Iterable

from sqlalchemy.orm import Session

from bel.domain.contract import ContractItem
from bel.domain.invoice import InvoiceItem
from bel.domain.matching import ConfirmationType, InvoiceAllocation
from bel.infrastructure.persistence.repositories import InvoiceAllocationRepository


def _contract_allocation_exists(
    allocations: Iterable[InvoiceAllocation], invoice_id: uuid.UUID, contract_id: uuid.UUID
) -> bool:
    """A confirmed (AUTO_CONFIRMED / HUMAN_CONFIRMED) contract-level
    InvoiceAllocation between the invoice and the given contract."""
    return any(
        a.invoice_id == invoice_id
        and a.contract_id == contract_id
        and a.confirmation_type in {ConfirmationType.AUTO_CONFIRMED, ConfirmationType.HUMAN_CONFIRMED}
        for a in allocations
    )


def validate_item_allocation(
    *,
    session: Session,
    invoice_item: InvoiceItem,
    contract_item: ContractItem,
    allocated_quantity: Decimal,
    allocated_net_amount: Decimal,
    invoice_allocations: list[InvoiceAllocation] | None = None,
    existing_allocated_quantity: Decimal | None = None,
) -> None:
    """Raise ValueError when any of the section-11 safety constraints is
    violated. 'Uncertain' inputs (a line item without a quantity to guard
    against) are rejected explicitly, never silently accepted.

    Constraints:
      A. The invoice's contract-level allocation is already confirmed to
         the SAME contract the contract_item belongs to.
      B. sum(allocated_quantity) for the invoice_item <= invoice_item.quantity.
      C. InvoiceItem -> ContractItem never crosses the confirmed
         contract scope (implied by A plus existing allocations staying
         within the same contract).
    """
    invoice_allocations = invoice_allocations if invoice_allocations is not None else InvoiceAllocationRepository(session).list_for_contract(
        contract_item.contract_id
    )
    if not _contract_allocation_exists(invoice_allocations, invoice_item.invoice_id, contract_item.contract_id):
        raise ValueError(
            f"InvoiceItem {invoice_item.id} cannot be allocated to ContractItem {contract_item.id}: "
            "no CONFIRMED contract-level InvoiceAllocation links the invoice to the contract_item's contract (11-A)"
        )

    if invoice_item.quantity is None:
        raise ValueError(
            f"InvoiceItem {invoice_item.id} (line {invoice_item.line_no}) has no quantity — "
            "cannot verify allocation capacity; refusing to guess (11-D)"
        )

    if existing_allocated_quantity is None:
        from bel.infrastructure.persistence.repositories import InvoiceItemAllocationRepository

        existing_allocated_quantity = InvoiceItemAllocationRepository(session).sum_allocated_quantity_for_invoice_item(
            invoice_item.id
        )
    if existing_allocated_quantity + allocated_quantity > invoice_item.quantity:
        raise ValueError(
            f"InvoiceItem {invoice_item.id} capacity exceeded: {existing_allocated_quantity} already allocated, "
            f"+{allocated_quantity} requested, line quantity is {invoice_item.quantity} (11-B)"
        )
