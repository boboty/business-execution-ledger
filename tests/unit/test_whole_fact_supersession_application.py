"""Phase 2D.1-R5 gate fix — the whole-fact supersession Application seam
(bel.application.whole_fact_supersession). Covers: explicit supersession
with new Evidence required, same-fact-type/compatible-scope enforcement
(structural, by construction — the new fact always inherits the old
fact's own business scope, so cross-business retargeting has no code
path), old Fact immutability, new Fact currency, and rejection of a
fork (double supersession) / self-supersession-via-same-Evidence.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from bel.application.contract_item_facts import create_contract_item_fact
from bel.application.whole_fact_supersession import (
    WholeFactSupersessionError,
    supersede_accrual_basis_fact,
    supersede_cost_recognition_fact,
    supersede_historical_accrual_fact,
    supersede_invoice_item_allocation,
)
from bel.domain.accrual import (
    AccrualBasisFact,
    AccrualBasisScopeType,
    CostRecognitionBasis,
    CostRecognitionFact,
    HistoricalAccrualFact,
    InvoiceItemAllocation,
    ItemAllocationConfirmationType,
    ManualBasis,
)
from bel.domain.contract import Contract
from bel.domain.evidence import EvidenceDocument, EvidenceFragment, FragmentKind
from bel.domain.invoice import Invoice, InvoiceDirection, InvoiceItem
from bel.infrastructure.persistence.database import make_engine, make_session_factory
from bel.infrastructure.persistence.models import Base
from bel.infrastructure.persistence.repositories import (
    AccrualBasisFactRepository,
    ContractRepository,
    CostRecognitionFactRepository,
    EvidenceRepository,
    HistoricalAccrualFactRepository,
    InvoiceItemAllocationRepository,
    InvoiceItemRepository,
    InvoiceRepository,
)

NOW = datetime.now(timezone.utc)


@pytest.fixture
def db_session():
    engine = make_engine(":memory:")
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        yield session


def _make_fragment(session):
    doc = EvidenceDocument(
        id=uuid.uuid4(), file_name="x", sha256=uuid.uuid4().hex + uuid.uuid4().hex, source_type="t", imported_at=NOW
    )
    EvidenceRepository(session).add_document(doc)
    frag = EvidenceFragment(
        id=uuid.uuid4(), evidence_document_id=doc.id, fragment_kind=FragmentKind.MANUAL_FACT, sheet_name=None,
        row_number=None, locator_json={}, raw_data={}, created_at=NOW,
    )
    EvidenceRepository(session).add_fragment(frag)
    session.flush()
    return frag


def _make_contract(session, fragment_id, contract_no="C-WFS"):
    contract = Contract(
        id=uuid.uuid4(), contract_no=contract_no, contract_type=None, counterparty="Sup", buyer="Buyer",
        gross_amount=Decimal("1000"), currency="CNY", contract_date=None, current_source_fragment_id=fragment_id,
        created_at=NOW, updated_at=NOW,
    )
    ContractRepository(session).add(contract)
    session.flush()
    return contract


def test_cost_recognition_fact_supersede_requires_new_evidence(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)
    repo = CostRecognitionFactRepository(db_session)
    old = CostRecognitionFact(
        id=uuid.uuid4(), contract_id=contract.id, recognition_date=date(2025, 12, 1),
        basis=CostRecognitionBasis.MANUAL_CONFIRMED, source_fragment_id=frag.id, created_at=NOW, shipment_id=None,
    )
    repo.add(old)
    db_session.flush()

    with pytest.raises(WholeFactSupersessionError):
        supersede_cost_recognition_fact(
            db_session, superseded_fact_id=old.id, recognition_date=date(2025, 12, 5),
            basis=CostRecognitionBasis.MANUAL_CONFIRMED, source_fragment_id=frag.id, shipment_id=None,
            created_at=NOW,
        )


def test_cost_recognition_fact_supersede_succeeds_and_preserves_scope(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)
    repo = CostRecognitionFactRepository(db_session)
    old = CostRecognitionFact(
        id=uuid.uuid4(), contract_id=contract.id, recognition_date=date(2025, 12, 1),
        basis=CostRecognitionBasis.MANUAL_CONFIRMED, source_fragment_id=frag.id, created_at=NOW, shipment_id=None,
    )
    repo.add(old)
    db_session.flush()

    frag2 = _make_fragment(db_session)
    new_fact = supersede_cost_recognition_fact(
        db_session, superseded_fact_id=old.id, recognition_date=date(2025, 12, 20),
        basis=CostRecognitionBasis.EXPORT_EXECUTION_CONFIRMED, source_fragment_id=frag2.id, shipment_id=None,
        created_at=NOW,
    )
    assert new_fact.contract_id == contract.id  # scope inherited, never retargeted
    current = repo.list_all()
    assert len(current) == 1 and current[0].id == new_fact.id
    old_reloaded = next(f for f in repo.list_all_including_superseded() if f.id == old.id)
    assert old_reloaded.recognition_date == date(2025, 12, 1)  # immutable
    assert old_reloaded.superseded_by_fact_id == new_fact.id


def test_cost_recognition_fact_double_supersession_rejected(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)
    repo = CostRecognitionFactRepository(db_session)
    old = CostRecognitionFact(
        id=uuid.uuid4(), contract_id=contract.id, recognition_date=date(2025, 12, 1),
        basis=CostRecognitionBasis.MANUAL_CONFIRMED, source_fragment_id=frag.id, created_at=NOW, shipment_id=None,
    )
    repo.add(old)
    db_session.flush()
    frag2 = _make_fragment(db_session)
    supersede_cost_recognition_fact(
        db_session, superseded_fact_id=old.id, recognition_date=date(2025, 12, 20),
        basis=CostRecognitionBasis.MANUAL_CONFIRMED, source_fragment_id=frag2.id, shipment_id=None, created_at=NOW,
    )
    frag3 = _make_fragment(db_session)
    with pytest.raises(WholeFactSupersessionError):
        supersede_cost_recognition_fact(
            db_session, superseded_fact_id=old.id, recognition_date=date(2025, 12, 25),
            basis=CostRecognitionBasis.MANUAL_CONFIRMED, source_fragment_id=frag3.id, shipment_id=None,
            created_at=NOW,
        )


def test_supersede_nonexistent_fact_rejected(db_session):
    frag = _make_fragment(db_session)
    with pytest.raises(WholeFactSupersessionError):
        supersede_cost_recognition_fact(
            db_session, superseded_fact_id=uuid.uuid4(), recognition_date=date(2025, 12, 20),
            basis=CostRecognitionBasis.MANUAL_CONFIRMED, source_fragment_id=frag.id, shipment_id=None,
            created_at=NOW,
        )


def test_accrual_basis_fact_supersede_preserves_scope(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)
    item = create_contract_item_fact(
        db_session, contract_id=contract.id, source_item_key="ITEM-1", fields={"product_name": "Widget"},
        source_fragment_id=frag.id, created_at=NOW,
    ).item
    repo = AccrualBasisFactRepository(db_session)
    old = AccrualBasisFact(
        id=uuid.uuid4(), scope_type=AccrualBasisScopeType.CONTRACT_ITEM, contract_id=contract.id,
        contract_item_id=item.id, quantity=Decimal("10"), estimated_cost=Decimal("100.00"),
        basis=ManualBasis.MANUAL_CONFIRMED, source_fragment_id=frag.id, created_at=NOW,
    )
    repo.add(old)
    db_session.flush()

    frag2 = _make_fragment(db_session)
    new_fact = supersede_accrual_basis_fact(
        db_session, superseded_fact_id=old.id, estimated_cost=Decimal("150.00"), basis=ManualBasis.MANUAL_CONFIRMED,
        source_fragment_id=frag2.id, quantity=Decimal("12"), created_at=NOW,
    )
    assert new_fact.contract_item_id == item.id
    assert new_fact.scope_type == AccrualBasisScopeType.CONTRACT_ITEM
    current = repo.list_all()
    assert len(current) == 1
    assert current[0].estimated_cost == Decimal("150.00")


def test_historical_accrual_fact_supersede_preserves_period_and_item(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)
    item = create_contract_item_fact(
        db_session, contract_id=contract.id, source_item_key="ITEM-1", fields={"product_name": "Widget"},
        source_fragment_id=frag.id, created_at=NOW,
    ).item
    repo = HistoricalAccrualFactRepository(db_session)
    old = HistoricalAccrualFact(
        id=uuid.uuid4(), source_period="2025-12", contract_item_id=item.id, quantity=Decimal("10"),
        estimated_cost=Decimal("500.00"), basis=ManualBasis.MANUAL_CONFIRMED, source_fragment_id=frag.id,
        confirmed_at=NOW,
    )
    repo.add(old)
    db_session.flush()

    frag2 = _make_fragment(db_session)
    new_fact = supersede_historical_accrual_fact(
        db_session, superseded_fact_id=old.id, quantity=Decimal("8"), estimated_cost=Decimal("400.00"),
        basis=ManualBasis.MANUAL_CONFIRMED, source_fragment_id=frag2.id, confirmed_at=NOW,
    )
    assert new_fact.source_period == "2025-12"
    assert new_fact.contract_item_id == item.id
    current = repo.list_for_contract_item(item.id)
    assert len(current) == 1
    assert current[0].estimated_cost == Decimal("400.00")


def test_invoice_item_allocation_supersede_preserves_pair(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)
    item = create_contract_item_fact(
        db_session, contract_id=contract.id, source_item_key="ITEM-1", fields={"product_name": "Widget"},
        source_fragment_id=frag.id, created_at=NOW,
    ).item
    invoice = Invoice(
        id=uuid.uuid4(), direction=InvoiceDirection.PURCHASE, invoice_type=None, invoice_no=None,
        digital_invoice_no=None, external_invoice_key="INV-WFS", issue_date=date(2025, 12, 1), seller="Sup",
        buyer="Buyer", net_amount=Decimal("100"), tax_amount=Decimal("0"), gross_amount=Decimal("100"),
        invoice_status=None, source_fragment_id=frag.id, created_at=NOW, updated_at=NOW,
    )
    InvoiceRepository(db_session).add(invoice)
    db_session.flush()
    invoice_item = InvoiceItem(
        id=uuid.uuid4(), invoice_id=invoice.id, line_no=1, product_name="Widget", specification=None, unit=None,
        quantity=Decimal("10"), unit_price=None, net_amount=Decimal("100"), tax_rate=None, tax_amount=Decimal("0"),
        gross_amount=Decimal("100"), source_fragment_id=frag.id,
    )
    InvoiceItemRepository(db_session).add(invoice_item)
    db_session.flush()

    repo = InvoiceItemAllocationRepository(db_session)
    old = InvoiceItemAllocation(
        id=uuid.uuid4(), invoice_item_id=invoice_item.id, contract_item_id=item.id, allocated_quantity=Decimal("5"),
        allocated_net_amount=Decimal("50"), confirmation_type=ItemAllocationConfirmationType.MANUAL_CONFIRMED,
        source_fragment_id=frag.id, created_at=NOW,
    )
    repo.add(old)
    db_session.flush()

    frag2 = _make_fragment(db_session)
    new_fact = supersede_invoice_item_allocation(
        db_session, superseded_fact_id=old.id, allocated_quantity=Decimal("10"), allocated_net_amount=Decimal("100"),
        confirmation_type=ItemAllocationConfirmationType.MANUAL_CONFIRMED, source_fragment_id=frag2.id,
        created_at=NOW,
    )
    assert new_fact.invoice_item_id == invoice_item.id
    assert new_fact.contract_item_id == item.id
    assert repo.find(invoice_item.id, item.id).id == new_fact.id
