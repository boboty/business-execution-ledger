from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from bel.domain.evidence import EvidenceDocument, EvidenceFragment
from bel.domain.invoice import Invoice, InvoiceItem
from bel.infrastructure.persistence.repositories import EvidenceRepository, InvoiceItemRepository, InvoiceRepository


@dataclass
class InvoiceTrace:
    """Invoice -> EvidenceFragment -> EvidenceDocument, per spec section 27."""

    invoice: Invoice
    items: list[InvoiceItem]
    fragment: EvidenceFragment
    document: EvidenceDocument


def get_invoice(session: Session, invoice_id: uuid.UUID) -> InvoiceTrace | None:
    invoice = InvoiceRepository(session).get(invoice_id)
    if invoice is None:
        return None

    items = InvoiceItemRepository(session).list_for_invoice(invoice_id)

    evidence_repo = EvidenceRepository(session)
    fragment = evidence_repo.get_fragment(invoice.source_fragment_id)
    if fragment is None:
        raise RuntimeError(f"data integrity error: invoice {invoice.id} references missing fragment {invoice.source_fragment_id}")
    document = evidence_repo.get_document(fragment.evidence_document_id)
    if document is None:
        raise RuntimeError(f"data integrity error: fragment {fragment.id} references missing document {fragment.evidence_document_id}")

    return InvoiceTrace(invoice=invoice, items=items, fragment=fragment, document=document)
