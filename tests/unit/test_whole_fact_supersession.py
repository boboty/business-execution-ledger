"""Phase 2D.1-R5 — whole-fact supersession mechanics (docs/ROADMAP.md
2D.1-R5 section 21/40).

Covers the HARD adversarial test: create a cutover-confirmed Fact A,
supersede it with an independently-evidenced Fact B, and verify A
remains historical/unchanged, B is current, normal "current" reads see
only B, the full history still shows both, and a fact cannot be
superseded twice (no forked lineage).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from bel.domain.accrual import (
    AccrualBasisFact,
    AccrualBasisScopeType,
    CostRecognitionFact,
    CostRecognitionBasis,
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
    ContractItemRepository,
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


def _make_fragment(session, source_type="cutover_baseline_manual"):
    doc = EvidenceDocument(
        id=uuid.uuid4(), file_name="x", sha256=uuid.uuid4().hex + uuid.uuid4().hex, source_type=source_type,
        imported_at=NOW,
    )
    EvidenceRepository(session).add_document(doc)
    frag = EvidenceFragment(
        id=uuid.uuid4(), evidence_document_id=doc.id, fragment_kind=FragmentKind.MANUAL_FACT, sheet_name=None,
        row_number=None, locator_json={}, raw_data={}, created_at=NOW,
    )
    EvidenceRepository(session).add_fragment(frag)
    session.flush()
    return frag


def _make_contract(session, fragment_id):
    contract = Contract(
        id=uuid.uuid4(), contract_no="C-SUP", contract_type=None, counterparty="Sup", buyer="Buyer",
        gross_amount=Decimal("1000"), currency="CNY", contract_date=None, current_source_fragment_id=fragment_id,
        created_at=NOW, updated_at=NOW,
    )
    ContractRepository(session).add(contract)
    session.flush()
    return contract


# ---------------------------------------------------------------------------
# CostRecognitionFact
# ---------------------------------------------------------------------------


def test_cost_recognition_fact_supersession(db_session):
    frag_a = _make_fragment(db_session)
    contract = _make_contract(db_session, frag_a.id)
    repo = CostRecognitionFactRepository(db_session)

    fact_a = CostRecognitionFact(
        id=uuid.uuid4(), contract_id=contract.id, recognition_date=date(2025, 12, 1),
        basis=CostRecognitionBasis.MANUAL_CONFIRMED, source_fragment_id=frag_a.id, created_at=NOW, shipment_id=None,
    )
    repo.add(fact_a)
    db_session.flush()
    assert len(repo.list_all()) == 1

    frag_b = _make_fragment(db_session, source_type="cmb_bank_statement_pdf")
    fact_b = CostRecognitionFact(
        id=uuid.uuid4(), contract_id=contract.id, recognition_date=date(2025, 12, 15),
        basis=CostRecognitionBasis.EXPORT_EXECUTION_CONFIRMED, source_fragment_id=frag_b.id, created_at=NOW,
        shipment_id=None,
    )
    repo.add(fact_b)
    db_session.flush()

    assert repo.mark_superseded(fact_a.id, superseded_by_fact_id=fact_b.id) is True

    # A remains historical, content unchanged, Evidence unchanged.
    all_including = repo.list_all_including_superseded()
    a_reloaded = next(f for f in all_including if f.id == fact_a.id)
    assert a_reloaded.recognition_date == date(2025, 12, 1)
    assert a_reloaded.source_fragment_id == frag_a.id
    assert a_reloaded.superseded_by_fact_id == fact_b.id

    # Current reads see ONLY B — no double-count.
    current = repo.list_all()
    assert len(current) == 1
    assert current[0].id == fact_b.id

    # History still sees both.
    assert len(all_including) == 2

    # Cannot be superseded twice (no forked lineage).
    frag_c = _make_fragment(db_session)
    fact_c = CostRecognitionFact(
        id=uuid.uuid4(), contract_id=contract.id, recognition_date=date(2025, 12, 20),
        basis=CostRecognitionBasis.MANUAL_CONFIRMED, source_fragment_id=frag_c.id, created_at=NOW, shipment_id=None,
    )
    repo.add(fact_c)
    db_session.flush()
    assert repo.mark_superseded(fact_a.id, superseded_by_fact_id=fact_c.id) is False


# ---------------------------------------------------------------------------
# AccrualBasisFact
# ---------------------------------------------------------------------------


def test_accrual_basis_fact_supersession(db_session):
    frag_a = _make_fragment(db_session)
    contract = _make_contract(db_session, frag_a.id)
    repo = AccrualBasisFactRepository(db_session)

    fact_a = AccrualBasisFact(
        id=uuid.uuid4(), scope_type=AccrualBasisScopeType.CONTRACT, contract_id=contract.id, contract_item_id=None,
        quantity=None, estimated_cost=Decimal("100.00"), basis=ManualBasis.MANUAL_CONFIRMED,
        source_fragment_id=frag_a.id, created_at=NOW,
    )
    repo.add(fact_a)
    db_session.flush()

    frag_b = _make_fragment(db_session)
    fact_b = AccrualBasisFact(
        id=uuid.uuid4(), scope_type=AccrualBasisScopeType.CONTRACT, contract_id=contract.id, contract_item_id=None,
        quantity=None, estimated_cost=Decimal("150.00"), basis=ManualBasis.MANUAL_CONFIRMED,
        source_fragment_id=frag_b.id, created_at=NOW,
    )
    repo.add(fact_b)
    db_session.flush()

    assert repo.mark_superseded(fact_a.id, superseded_by_fact_id=fact_b.id) is True
    current = repo.list_all()
    assert len(current) == 1
    assert current[0].estimated_cost == Decimal("150.00")
    assert len(repo.list_all_including_superseded()) == 2
    a_reloaded = next(f for f in repo.list_all_including_superseded() if f.id == fact_a.id)
    assert a_reloaded.estimated_cost == Decimal("100.00")  # unchanged


# ---------------------------------------------------------------------------
# HistoricalAccrualFact
# ---------------------------------------------------------------------------


def test_historical_accrual_fact_supersession(db_session):
    frag_a = _make_fragment(db_session)
    contract = _make_contract(db_session, frag_a.id)
    from bel.application.contract_item_facts import create_contract_item_fact

    item = create_contract_item_fact(
        db_session, contract_id=contract.id, source_item_key="ITEM-1", fields={"product_name": "Widget"},
        source_fragment_id=frag_a.id, created_at=NOW,
    ).item
    repo = HistoricalAccrualFactRepository(db_session)

    fact_a = HistoricalAccrualFact(
        id=uuid.uuid4(), source_period="2025-12", contract_item_id=item.id, quantity=Decimal("10"),
        estimated_cost=Decimal("500.00"), basis=ManualBasis.MANUAL_CONFIRMED, source_fragment_id=frag_a.id,
        confirmed_at=NOW,
    )
    repo.add(fact_a)
    db_session.flush()

    frag_b = _make_fragment(db_session)
    fact_b = HistoricalAccrualFact(
        id=uuid.uuid4(), source_period="2025-12", contract_item_id=item.id, quantity=Decimal("8"),
        estimated_cost=Decimal("400.00"), basis=ManualBasis.MANUAL_CONFIRMED, source_fragment_id=frag_b.id,
        confirmed_at=NOW,
    )
    repo.add(fact_b)
    db_session.flush()

    assert repo.mark_superseded(fact_a.id, superseded_by_fact_id=fact_b.id) is True
    current = repo.list_for_contract_item(item.id)
    assert len(current) == 1
    assert current[0].estimated_cost == Decimal("400.00")
    assert len(repo.list_all_including_superseded()) == 2


# ---------------------------------------------------------------------------
# InvoiceItemAllocation
# ---------------------------------------------------------------------------


def test_invoice_item_allocation_supersession(db_session):
    frag_a = _make_fragment(db_session)
    contract = _make_contract(db_session, frag_a.id)
    from bel.application.contract_item_facts import create_contract_item_fact

    item = create_contract_item_fact(
        db_session, contract_id=contract.id, source_item_key="ITEM-1", fields={"product_name": "Widget"},
        source_fragment_id=frag_a.id, created_at=NOW,
    ).item

    invoice = Invoice(
        id=uuid.uuid4(), direction=InvoiceDirection.PURCHASE, invoice_type=None, invoice_no=None,
        digital_invoice_no=None, external_invoice_key="INV-SUP", issue_date=date(2025, 12, 1), seller="Sup",
        buyer="Buyer", net_amount=Decimal("100"), tax_amount=Decimal("0"), gross_amount=Decimal("100"),
        invoice_status=None, source_fragment_id=frag_a.id, created_at=NOW, updated_at=NOW,
    )
    InvoiceRepository(db_session).add(invoice)
    db_session.flush()
    invoice_item = InvoiceItem(
        id=uuid.uuid4(), invoice_id=invoice.id, line_no=1, product_name="Widget", specification=None, unit=None,
        quantity=Decimal("10"), unit_price=None, net_amount=Decimal("100"), tax_rate=None, tax_amount=Decimal("0"),
        gross_amount=Decimal("100"), source_fragment_id=frag_a.id,
    )
    InvoiceItemRepository(db_session).add(invoice_item)
    db_session.flush()

    repo = InvoiceItemAllocationRepository(db_session)
    alloc_a = InvoiceItemAllocation(
        id=uuid.uuid4(), invoice_item_id=invoice_item.id, contract_item_id=item.id, allocated_quantity=Decimal("5"),
        allocated_net_amount=Decimal("50"), confirmation_type=ItemAllocationConfirmationType.MANUAL_CONFIRMED,
        source_fragment_id=frag_a.id, created_at=NOW,
    )
    repo.add(alloc_a)
    db_session.flush()

    frag_b = _make_fragment(db_session)
    alloc_b = InvoiceItemAllocation(
        id=uuid.uuid4(), invoice_item_id=invoice_item.id, contract_item_id=item.id, allocated_quantity=Decimal("10"),
        allocated_net_amount=Decimal("100"), confirmation_type=ItemAllocationConfirmationType.MANUAL_CONFIRMED,
        source_fragment_id=frag_b.id, created_at=NOW,
    )
    repo.add(alloc_b)
    db_session.flush()

    assert repo.mark_superseded(alloc_a.id, superseded_by_fact_id=alloc_b.id) is True
    current = repo.list_for_contract_item(item.id)
    assert len(current) == 1
    assert current[0].allocated_quantity == Decimal("10")
    assert len(repo.list_all_including_superseded()) == 2
    # find() also only ever returns current facts.
    assert repo.find(invoice_item.id, item.id).id == alloc_b.id
