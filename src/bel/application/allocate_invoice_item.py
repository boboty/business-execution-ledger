"""Manual ContractItem <-> InvoiceItem allocation (``bel invoice-item
allocate``). The item-level counterpart of Phase 2A's contract-level
matching confirmation: a human explicitly states which invoice line maps
to which contract item, and by how much. Phase 2B adds no automatic item
matching — every allocation is MANUAL_CONFIRMED (spec section 10).

Evidence: a manual human confirmation is itself Evidence (DOMAIN.md). A
MANUAL_FACT fragment records the CLI confirmation so the allocation stays
traceable per A02.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from bel.application.item_allocation import validate_item_allocation
from bel.domain.accrual import InvoiceItemAllocation, ItemAllocationConfirmationType
from bel.domain.evidence import EvidenceDocument, EvidenceFragment, FragmentKind
from bel.infrastructure.persistence.database import serialized_write_transaction
from bel.infrastructure.persistence.repositories import (
    ContractItemRepository,
    ContractRepository,
    EvidenceRepository,
    InvoiceItemAllocationRepository,
    InvoiceItemRepository,
    InvoiceRepository,
)


def execute_manual_item_allocation(
    session: Session,
    *,
    invoice_external_key: str,
    line_no: int,
    contract_id: uuid.UUID,
    source_item_key: str,
    quantity: Decimal,
    net_amount: Decimal,
) -> InvoiceItemAllocation:
    """The shared command-level serialized write boundary for EVERY manual
    InvoiceItemAllocation writer — Web route, CLI, and any future Agent.
    Runs inside ``serialized_write_transaction`` (BEGIN IMMEDIATE before
    any read, single commit/rollback), so all manual allocation writers
    serialize on the same database-level write lock."""
    with serialized_write_transaction(session):
        return allocate_invoice_item(
            session,
            invoice_external_key=invoice_external_key,
            line_no=line_no,
            contract_id=contract_id,
            source_item_key=source_item_key,
            quantity=quantity,
            net_amount=net_amount,
        )


def allocate_invoice_item(
    session: Session,
    *,
    invoice_external_key: str,
    line_no: int,
    contract_id: uuid.UUID,
    source_item_key: str,
    quantity: Decimal,
    net_amount: Decimal,
) -> InvoiceItemAllocation:
    """Validate and create one MANUAL_CONFIRMED InvoiceItemAllocation with
    its EvidenceDocument/EvidenceFragment.

    Deliberately does NOT commit: the caller must run it inside a
    transaction that it owns (the shared ``serialized_write_transaction``
    boundary via ``execute_manual_item_allocation``). A caller that holds
    no write lock (e.g. tests) is responsible for committing.
    """
    now = datetime.now(timezone.utc)

    invoice_repo = InvoiceRepository(session)
    invoice = invoice_repo.find_by_external_key(invoice_external_key)
    if invoice is None:
        raise ValueError(f"Invoice with external_invoice_key={invoice_external_key!r} not found")

    item_repo = InvoiceItemRepository(session)
    invoice_item = next((i for i in item_repo.list_for_invoice(invoice.id) if i.line_no == line_no), None)
    if invoice_item is None:
        raise ValueError(f"Invoice {invoice_external_key!r} has no line {line_no}")

    contract = ContractRepository(session).get(contract_id)
    if contract is None:
        raise ValueError(f"Contract {contract_id} not found")

    contract_item = ContractItemRepository(session).find_by_contract_and_key(contract_id, source_item_key)
    if contract_item is None:
        raise ValueError(f"Contract {contract_id} has no contract item with source_item_key={source_item_key!r}")

    raw_data = {
        "invoice_external_key": invoice_external_key,
        "line_no": line_no,
        "contract_id": str(contract_id),
        "source_item_key": source_item_key,
        "quantity": str(quantity),
        "net_amount": str(net_amount),
    }
    document_sha = hashlib.sha256(json.dumps(raw_data, sort_keys=True).encode("utf-8")).hexdigest()

    evidence_repo = EvidenceRepository(session)
    if evidence_repo.find_document_by_sha256(document_sha) is not None:
        # A byte-identical manual allocation payload is already recorded —
        # a retried POST must never create a duplicate document/allocation
        # (the evidence_documents.sha256 column is UNIQUE).
        raise ValueError(
            "duplicate allocation request: an identical InvoiceItemAllocation "
            "payload is already recorded"
        )

    validate_item_allocation(
        session=session,
        invoice_item=invoice_item,
        contract_item=contract_item,
        allocated_quantity=quantity,
        allocated_net_amount=net_amount,
    )

    document = EvidenceDocument(
        id=uuid.uuid4(),
        file_name=f"manual-invoice-item-allocation-{now.isoformat()}.json",
        sha256=document_sha,
        source_type="manual_item_allocation",
        imported_at=now,
    )
    evidence_repo.add_document(document)
    fragment = EvidenceFragment(
        id=uuid.uuid4(),
        evidence_document_id=document.id,
        fragment_kind=FragmentKind.MANUAL_FACT,
        sheet_name=None,
        row_number=None,
        locator_json={"command": "invoice-item-allocate"},
        raw_data=raw_data,
        created_at=now,
    )
    evidence_repo.add_fragment(fragment)

    allocation = InvoiceItemAllocation(
        id=uuid.uuid4(),
        invoice_item_id=invoice_item.id,
        contract_item_id=contract_item.id,
        allocated_quantity=quantity,
        allocated_net_amount=net_amount,
        confirmation_type=ItemAllocationConfirmationType.MANUAL_CONFIRMED,
        source_fragment_id=fragment.id,
        created_at=now,
    )
    InvoiceItemAllocationRepository(session).add(allocation)

    # The sha256 pre-check above is a fast path for sequential duplicates,
    # but two concurrent identical requests can both pass it (TOCTOU). The
    # UNIQUE constraint on evidence_documents.sha256 is the authoritative
    # guard: on conflict the session is rolled back (zero partial rows)
    # and the request becomes the same controlled 400 the pre-check
    # produces.
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        detail = " ".join(str(arg) for arg in exc.args).lower()
        if "evidence_documents" in detail and "sha256" in detail:
            raise ValueError(
                "duplicate allocation request: an identical InvoiceItemAllocation "
                "payload is already recorded"
            ) from exc
        raise
    return allocation
