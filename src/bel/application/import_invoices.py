from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy.orm import Session

from bel.adapters.common import compute_sha256
from bel.adapters.excel.invoice_ledger import parse_invoice_ledger
from bel.domain.event import BusinessEvent, BusinessEventType
from bel.domain.evidence import EvidenceDocument, EvidenceFragment, FragmentKind
from bel.domain.invoice import Invoice, InvoiceItem
from bel.infrastructure.persistence.repositories import (
    EventRepository,
    EvidenceRepository,
    InvoiceItemRepository,
    InvoiceRepository,
)

SOURCE_TYPE = "invoice_ledger_xlsx"


@dataclass
class InvoiceImportResult:
    evidence_document_id: uuid.UUID
    file_name: str
    sha256: str
    is_reimport: bool
    direction: str
    buyer: str | None
    invoices_created: int
    invoice_items_created: int
    net_amount_total: Decimal
    tax_amount_total: Decimal
    gross_amount_total: Decimal


def import_invoices(session: Session, file_path: Path, direction: str) -> InvoiceImportResult:
    """direction is never guessed — it's a required, explicit argument
    (bel import-invoices ... --direction purchase). See spec section 5."""
    now = datetime.now(timezone.utc)
    sha256 = compute_sha256(file_path)

    evidence_repo = EvidenceRepository(session)
    invoice_repo = InvoiceRepository(session)
    item_repo = InvoiceItemRepository(session)
    event_repo = EventRepository(session)

    existing_document = evidence_repo.find_document_by_sha256(sha256)
    if existing_document is not None:
        event_repo.add(
            BusinessEvent(
                id=uuid.uuid4(),
                event_type=BusinessEventType.INVOICE_IMPORTED,
                occurred_at=now,
                payload={"evidence_document_id": str(existing_document.id), "is_reimport": True, "invoices_created": 0},
            )
        )
        session.commit()
        return InvoiceImportResult(
            evidence_document_id=existing_document.id,
            file_name=file_path.name,
            sha256=sha256,
            is_reimport=True,
            direction=direction,
            buyer=None,
            invoices_created=0,
            invoice_items_created=0,
            net_amount_total=Decimal("0"),
            tax_amount_total=Decimal("0"),
            gross_amount_total=Decimal("0"),
        )

    parsed = parse_invoice_ledger(file_path)

    document = EvidenceDocument(
        id=uuid.uuid4(), file_name=file_path.name, sha256=sha256, source_type=SOURCE_TYPE, imported_at=now
    )
    evidence_repo.add_document(document)

    # Pass 1: one EvidenceFragment per row (header + continuation rows
    # alike) — every row is Evidence regardless of whether it becomes an
    # Invoice header. Flush before anything references a fragment id, per
    # the Phase 1 lesson (no relationship() -> no implicit insert order).
    row_fragment_ids: dict[int, uuid.UUID] = {}
    for row in parsed.rows:
        fragment = EvidenceFragment(
            id=uuid.uuid4(),
            evidence_document_id=document.id,
            fragment_kind=FragmentKind.EXCEL_ROW,
            sheet_name=parsed.sheet_name,
            row_number=row.row_number,
            locator_json=None,
            raw_data=row.raw_data,
            created_at=now,
        )
        evidence_repo.add_fragment(fragment)
        row_fragment_ids[row.row_number] = fragment.id
    session.flush()

    # Pass 2: one Invoice per group (header row + its continuation rows).
    net_total = Decimal("0")
    tax_total = Decimal("0")
    gross_total = Decimal("0")
    invoices_by_group = []
    for group in parsed.groups:
        header = group.header
        invoice = Invoice(
            id=uuid.uuid4(),
            direction=direction,
            invoice_type=header.invoice_type,
            invoice_no=header.invoice_no,
            digital_invoice_no=header.digital_invoice_no,
            # digital_invoice_no takes priority as the business identity key
            # — see spec section 10 and docs/PHASE2A-DECISIONS.md.
            external_invoice_key=header.digital_invoice_no,
            issue_date=header.issue_date,
            seller=header.seller,
            buyer=parsed.buyer,
            net_amount=header.invoice_net_amount,
            tax_amount=header.invoice_tax_amount,
            gross_amount=header.invoice_gross_amount,
            invoice_status=header.invoice_status,
            source_fragment_id=row_fragment_ids[header.row_number],
            created_at=now,
            updated_at=now,
            # Phase 2D.3-F1e: the current purchase invoice Excel source
            # provides NO canonical currency field, so the Invoice's
            # Evidence-derived ``currency`` stays None (the dataclass
            # default) — no CNY/USD default and no domestic inference is
            # ever manufactured here (docs/PHASE2D3-RULE-FREEZE.md IP-P02).
        )
        invoice_repo.add(invoice)
        invoices_by_group.append((group, invoice))
        net_total += header.invoice_net_amount
        tax_total += header.invoice_tax_amount
        gross_total += header.invoice_gross_amount
    session.flush()

    # Pass 3: InvoiceItems reference invoice.id — flushed above.
    item_count = 0
    for group, invoice in invoices_by_group:
        for line_no, item_row in enumerate(group.item_rows, start=1):
            item_repo.add(
                InvoiceItem(
                    id=uuid.uuid4(),
                    invoice_id=invoice.id,
                    line_no=line_no,
                    product_name=item_row.product_name,
                    specification=item_row.specification,
                    unit=item_row.unit,
                    quantity=item_row.quantity,
                    unit_price=item_row.unit_price,
                    net_amount=item_row.item_net_amount,
                    tax_rate=item_row.tax_rate,
                    tax_amount=item_row.item_tax_amount,
                    gross_amount=item_row.item_gross_amount,
                    source_fragment_id=row_fragment_ids[item_row.row_number],
                )
            )
            item_count += 1

    event_repo.add(
        BusinessEvent(
            id=uuid.uuid4(),
            event_type=BusinessEventType.INVOICE_IMPORTED,
            occurred_at=now,
            payload={
                "evidence_document_id": str(document.id),
                "is_reimport": False,
                "invoices_created": len(parsed.groups),
                "invoice_items_created": item_count,
                "direction": direction,
            },
        )
    )

    session.commit()

    return InvoiceImportResult(
        evidence_document_id=document.id,
        file_name=file_path.name,
        sha256=sha256,
        is_reimport=False,
        direction=direction,
        buyer=parsed.buyer,
        invoices_created=len(parsed.groups),
        invoice_items_created=item_count,
        net_amount_total=net_total,
        tax_amount_total=tax_total,
        gross_amount_total=gross_total,
    )
