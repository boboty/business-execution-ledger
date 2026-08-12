from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from bel.domain.evidence import EvidenceDocument, EvidenceFragment
from bel.domain.payment import Payment
from bel.infrastructure.persistence.repositories import EvidenceRepository, PaymentRepository


@dataclass
class PaymentTrace:
    """Payment -> EvidenceFragment (page/transaction_index) ->
    EvidenceDocument, per spec section 27."""

    payment: Payment
    fragment: EvidenceFragment
    document: EvidenceDocument


def get_payment(session: Session, payment_id: uuid.UUID) -> PaymentTrace | None:
    payment = PaymentRepository(session).get(payment_id)
    if payment is None:
        return None

    evidence_repo = EvidenceRepository(session)
    fragment = evidence_repo.get_fragment(payment.source_fragment_id)
    if fragment is None:
        raise RuntimeError(f"data integrity error: payment {payment.id} references missing fragment {payment.source_fragment_id}")
    document = evidence_repo.get_document(fragment.evidence_document_id)
    if document is None:
        raise RuntimeError(f"data integrity error: fragment {fragment.id} references missing document {fragment.evidence_document_id}")

    return PaymentTrace(payment=payment, fragment=fragment, document=document)
