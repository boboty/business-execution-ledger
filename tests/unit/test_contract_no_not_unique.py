import uuid
from datetime import datetime, timezone
from decimal import Decimal

from bel.domain.contract import Contract
from bel.domain.evidence import EvidenceDocument, EvidenceFragment, FragmentKind
from bel.infrastructure.persistence.repositories import ContractRepository, EvidenceRepository


def test_duplicate_contract_no_does_not_raise_integrity_error(db_session):
    """contract_no is a business key, not a DB unique constraint —
    duplicates must be storable so a BusinessKeyConflict can be raised
    at the application layer instead of the database rejecting the row.
    See docs/DOMAIN.md and docs/RULES.md R004."""
    now = datetime.now(timezone.utc)
    evidence_repo = EvidenceRepository(db_session)
    doc = EvidenceDocument(id=uuid.uuid4(), file_name="x.xlsx", sha256="b" * 64, source_type="t", imported_at=now)
    evidence_repo.add_document(doc)
    frag = EvidenceFragment(
        id=uuid.uuid4(),
        evidence_document_id=doc.id,
        fragment_kind=FragmentKind.EXCEL_ROW,
        sheet_name="s",
        row_number=1,
        locator_json=None,
        raw_data={},
        created_at=now,
    )
    evidence_repo.add_fragment(frag)
    db_session.flush()

    contract_repo = ContractRepository(db_session)
    for counterparty in ("Seller Alpha Co", "Seller Beta Co"):
        contract_repo.add(
            Contract(
                id=uuid.uuid4(),
                contract_no="DUP-CONTRACT-001",
                contract_type=None,
                counterparty=counterparty,
                buyer="Buyer Co",
                gross_amount=Decimal("1.00"),
                currency="CNY",
                contract_date=None,
                current_source_fragment_id=frag.id,
                created_at=now,
                updated_at=now,
            )
        )

    db_session.commit()  # must not raise IntegrityError

    found = contract_repo.find_by_contract_no("DUP-CONTRACT-001")
    assert len(found) == 2
    assert {c.counterparty for c in found} == {"Seller Alpha Co", "Seller Beta Co"}
