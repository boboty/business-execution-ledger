from __future__ import annotations

from sqlalchemy.orm import Session

from bel.domain.invoice import InvoiceDirection
from bel.domain.matching import MatchCase, SubjectType
from bel.domain.payment import PaymentDirection
from bel.infrastructure.persistence.repositories import InvoiceRepository, MatchCaseRepository, PaymentRepository


def list_match_cases(session: Session, status: str | None = None) -> list[MatchCase]:
    """The procurement `bel match list` read path. Filtered to
    procurement-leg subjects only (docs/PHASE2D1-R0-DECISIONS.md section
    2.7's Gate G5 guard #2, HARD): before Phase 2D.1-R3b this function
    was leg-agnostic and returned every MatchCase, including any
    sales-leg case — which the procurement CLI's `match confirm` would
    then be able to attempt against, attributing a SALES invoice or IN
    payment to a procurement Contract. Filtering by the subject's own
    `direction` (never by `match_method` name-guessing) closes that path
    without changing what a genuine procurement case looks like. Use
    `bel.application.sales_matching.list_sales_match_cases` for the
    sales leg's symmetric listing."""
    repo = MatchCaseRepository(session)
    cases = repo.list_by_status(status) if status else repo.list_all()
    invoice_repo = InvoiceRepository(session)
    payment_repo = PaymentRepository(session)
    result = []
    for case in cases:
        if case.subject_type == SubjectType.INVOICE:
            invoice = invoice_repo.get(case.subject_id)
            if invoice is not None and invoice.direction == InvoiceDirection.PURCHASE:
                result.append(case)
        elif case.subject_type == SubjectType.PAYMENT:
            payment = payment_repo.get(case.subject_id)
            if payment is not None and payment.direction == PaymentDirection.OUT:
                result.append(case)
    return result
