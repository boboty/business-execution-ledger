from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy.orm import Session

from bel.adapters.common import compute_sha256
from bel.adapters.pdf.cmb_bank_statement import parse_cmb_bank_statement
from bel.domain.event import BusinessEvent, BusinessEventType
from bel.domain.evidence import EvidenceDocument, EvidenceFragment, FragmentKind
from bel.domain.payment import Payment, PaymentDirection
from bel.infrastructure.persistence.repositories import EventRepository, EvidenceRepository, PaymentRepository

SOURCE_TYPE = "cmb_bank_statement_pdf"
SUPPORTED_PROFILES = {"cmb"}


@dataclass
class BankImportResult:
    evidence_document_id: uuid.UUID
    file_name: str
    sha256: str
    is_reimport: bool
    profile: str
    payments_created: int
    opening_balance: Decimal | None
    closing_balance: Decimal | None
    total_in: Decimal
    total_out: Decimal


def import_bank_statement(session: Session, file_path: Path, profile: str) -> BankImportResult:
    if profile not in SUPPORTED_PROFILES:
        raise ValueError(f"unsupported bank profile {profile!r}; supported: {sorted(SUPPORTED_PROFILES)}")

    now = datetime.now(timezone.utc)
    sha256 = compute_sha256(file_path)

    evidence_repo = EvidenceRepository(session)
    payment_repo = PaymentRepository(session)
    event_repo = EventRepository(session)

    existing_document = evidence_repo.find_document_by_sha256(sha256)
    if existing_document is not None:
        event_repo.add(
            BusinessEvent(
                id=uuid.uuid4(),
                event_type=BusinessEventType.PAYMENT_IMPORTED,
                occurred_at=now,
                payload={"evidence_document_id": str(existing_document.id), "is_reimport": True, "payments_created": 0},
            )
        )
        session.commit()
        return BankImportResult(
            evidence_document_id=existing_document.id,
            file_name=file_path.name,
            sha256=sha256,
            is_reimport=True,
            profile=profile,
            payments_created=0,
            opening_balance=None,
            closing_balance=None,
            total_in=Decimal("0"),
            total_out=Decimal("0"),
        )

    parsed = parse_cmb_bank_statement(file_path)

    document = EvidenceDocument(
        id=uuid.uuid4(), file_name=file_path.name, sha256=sha256, source_type=SOURCE_TYPE, imported_at=now
    )
    evidence_repo.add_document(document)

    # Fragments before Payments — same FK-ordering lesson as every other importer.
    transaction_fragment_ids: dict[int, uuid.UUID] = {}
    for txn in parsed.transactions:
        fragment = EvidenceFragment(
            id=uuid.uuid4(),
            evidence_document_id=document.id,
            fragment_kind=FragmentKind.PDF_TRANSACTION,
            sheet_name=None,
            row_number=None,
            locator_json={"page": txn.page_index, "transaction_index": txn.transaction_index},
            raw_data=txn.raw_data,
            created_at=now,
        )
        evidence_repo.add_fragment(fragment)
        transaction_fragment_ids[txn.transaction_index] = fragment.id
    session.flush()

    total_in = Decimal("0")
    total_out = Decimal("0")
    for txn in parsed.transactions:
        # amount is always positive; direction carries the sign. The
        # bank's own signed value stays in raw_data untouched — see
        # spec section 11 and docs/PHASE2A-DECISIONS.md.
        direction = PaymentDirection.IN if txn.signed_amount >= 0 else PaymentDirection.OUT
        amount = abs(txn.signed_amount)
        if direction == PaymentDirection.IN:
            total_in += amount
        else:
            total_out += amount

        payment_repo.add(
            Payment(
                id=uuid.uuid4(),
                transaction_date=txn.transaction_date,
                direction=direction,
                amount=amount,
                counterparty=txn.counterparty,
                business_type=txn.business_type,
                bank_reference=txn.bank_reference,
                description=txn.description,
                running_balance=txn.running_balance,
                source_fragment_id=transaction_fragment_ids[txn.transaction_index],
                created_at=now,
            )
        )

    event_repo.add(
        BusinessEvent(
            id=uuid.uuid4(),
            event_type=BusinessEventType.PAYMENT_IMPORTED,
            occurred_at=now,
            payload={
                "evidence_document_id": str(document.id),
                "is_reimport": False,
                "payments_created": len(parsed.transactions),
                "profile": profile,
            },
        )
    )

    session.commit()

    return BankImportResult(
        evidence_document_id=document.id,
        file_name=file_path.name,
        sha256=sha256,
        is_reimport=False,
        profile=profile,
        payments_created=len(parsed.transactions),
        opening_balance=parsed.opening_balance,
        closing_balance=parsed.closing_balance,
        total_in=total_in,
        total_out=total_out,
    )
