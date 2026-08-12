import uuid
from datetime import datetime, timezone
from decimal import Decimal

from bel.domain.contract import Contract
from bel.domain.evidence import EvidenceDocument, EvidenceFragment, FragmentKind
from bel.infrastructure.persistence.repositories import ContractRepository, EvidenceRepository


def _make_fragment(session):
    now = datetime.now(timezone.utc)
    evidence_repo = EvidenceRepository(session)
    doc = EvidenceDocument(id=uuid.uuid4(), file_name="x.xlsx", sha256="a" * 64, source_type="t", imported_at=now)
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
    session.flush()
    return frag


def test_contract_identity_is_a_uuid_independent_of_contract_no(db_session):
    frag = _make_fragment(db_session)
    now = datetime.now(timezone.utc)
    contract_repo = ContractRepository(db_session)

    c1 = Contract(
        id=uuid.uuid4(),
        contract_no="SAME_NO",
        contract_type=None,
        counterparty="A",
        buyer="B",
        gross_amount=Decimal("1"),
        currency="CNY",
        contract_date=None,
        current_source_fragment_id=frag.id,
        created_at=now,
        updated_at=now,
    )
    c2 = Contract(
        id=uuid.uuid4(),
        contract_no="SAME_NO",
        contract_type=None,
        counterparty="C",
        buyer="B",
        gross_amount=Decimal("2"),
        currency="CNY",
        contract_date=None,
        current_source_fragment_id=frag.id,
        created_at=now,
        updated_at=now,
    )
    contract_repo.add(c1)
    contract_repo.add(c2)
    db_session.commit()

    assert c1.id != c2.id
    assert isinstance(c1.id, uuid.UUID) and isinstance(c2.id, uuid.UUID)
    found = contract_repo.find_by_contract_no("SAME_NO")
    assert {c.id for c in found} == {c1.id, c2.id}
