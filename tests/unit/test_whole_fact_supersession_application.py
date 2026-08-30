"""Phase 2D.1-R5 gate fix — the whole-fact supersession Application seam
(bel.application.whole_fact_supersession). Covers: explicit supersession
with new Evidence required, same-fact-type/compatible-scope enforcement
(structural, by construction — the new fact always inherits the old
fact's own business scope, so cross-business retargeting has no code
path), old Fact immutability, new Fact currency, and rejection of a
fork (double supersession) / self-supersession-via-same-Evidence.

Round-2 gate fix: supersession is a SECOND WRITER for a fact type, never
an escape hatch from that fact type's own safety constraints —
InvoiceItemAllocation supersession goes through the SAME section-11
``validate_item_allocation`` gate (capacity excluding the allocation
being superseded) plus the closed MANUAL_CONFIRMED set, and
CostRecognitionFact supersession keeps the closed basis set and requires
a ``shipment_id`` to name a Shipment of the SAME Contract.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from bel.application.contract_item_facts import create_contract_item_fact
from bel.application.shipment_facts import create_shipment_fact
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
from bel.domain.matching import (
    AllocationMatchMethod,
    ConfirmationType,
    InvoiceAllocation,
    MatchCase,
    MatchCaseStatus,
    MatchMethod,
)
from bel.infrastructure.persistence.database import make_engine, make_session_factory
from bel.infrastructure.persistence.models import Base
from bel.infrastructure.persistence.repositories import (
    AccrualBasisFactRepository,
    ContractRepository,
    CostRecognitionFactRepository,
    EvidenceRepository,
    HistoricalAccrualFactRepository,
    InvoiceAllocationRepository,
    InvoiceItemAllocationRepository,
    InvoiceItemRepository,
    InvoiceRepository,
    MatchCaseRepository,
    ShipmentRepository,
)

NOW = datetime.now(timezone.utc)


@pytest.fixture
def db_session():
    engine = make_engine("sqlite://")
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


def _confirm_invoice_to_contract(session, invoice, contract) -> None:
    """The genuine 11-A precondition every InvoiceItemAllocation writer
    (CLI, Close Fact Pack, and now supersession) requires: a CONFIRMED
    contract-level InvoiceAllocation linking the invoice to the
    contract_item's own contract."""
    match_case = MatchCase(
        id=uuid.uuid4(), subject_type="INVOICE", subject_id=invoice.id,
        status=MatchCaseStatus.AUTO_CONFIRMED, match_method=MatchMethod.M001, created_at=NOW, resolved_at=NOW,
    )
    MatchCaseRepository(session).add(match_case)
    session.flush()
    InvoiceAllocationRepository(session).add(
        InvoiceAllocation(
            id=uuid.uuid4(), invoice_id=invoice.id, contract_id=contract.id, match_case_id=match_case.id,
            allocated_gross_amount=invoice.gross_amount,
            match_method=AllocationMatchMethod.EXACT_COUNTERPARTY_AMOUNT_UNIQUE,
            confirmation_type=ConfirmationType.AUTO_CONFIRMED, created_at=NOW,
        )
    )
    session.flush()


def _make_allocation_case(session, *, confirm=True, line_quantity="10", line_net_amount="100"):
    """(contract, contract_item, invoice, invoice_item) — one PURCHASE
    invoice line plus its contract item, with the section-11-A
    contract-level confirmation in place unless ``confirm=False``."""
    frag = _make_fragment(session)
    contract = _make_contract(session, frag.id)
    item = create_contract_item_fact(
        session, contract_id=contract.id, source_item_key="ITEM-1", fields={"product_name": "Widget"},
        source_fragment_id=frag.id, created_at=NOW,
    ).item
    invoice = Invoice(
        id=uuid.uuid4(), direction=InvoiceDirection.PURCHASE, invoice_type=None, invoice_no=None,
        digital_invoice_no=None, external_invoice_key="INV-WFS", issue_date=date(2025, 12, 1), seller="Sup",
        buyer="Buyer", net_amount=Decimal(line_net_amount), tax_amount=Decimal("0"),
        gross_amount=Decimal(line_net_amount), invoice_status=None, source_fragment_id=frag.id, created_at=NOW,
        updated_at=NOW,
    )
    InvoiceRepository(session).add(invoice)
    session.flush()
    invoice_item = InvoiceItem(
        id=uuid.uuid4(), invoice_id=invoice.id, line_no=1, product_name="Widget", specification=None, unit=None,
        quantity=Decimal(line_quantity), unit_price=None, net_amount=Decimal(line_net_amount), tax_rate=None,
        tax_amount=Decimal("0"), gross_amount=Decimal(line_net_amount), source_fragment_id=frag.id,
    )
    InvoiceItemRepository(session).add(invoice_item)
    session.flush()
    if confirm:
        _confirm_invoice_to_contract(session, invoice, contract)
    return contract, item, invoice, invoice_item


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
    _contract, item, _invoice, invoice_item = _make_allocation_case(db_session)
    frag = _make_fragment(db_session)
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


# ---------------------------------------------------------------------------
# Round-2 gate fix — supersession never bypasses a Fact's own constraints
# ---------------------------------------------------------------------------


def test_invoice_item_allocation_supersede_requires_confirmed_contract_allocation(db_session):
    """11-A is not waived for a correction: with no CONFIRMED
    contract-level InvoiceAllocation, the supersession is refused
    outright — never written as an "amended" allocation."""
    _contract, item, _invoice, invoice_item = _make_allocation_case(db_session, confirm=False)
    frag = _make_fragment(db_session)
    repo = InvoiceItemAllocationRepository(db_session)
    old = InvoiceItemAllocation(
        id=uuid.uuid4(), invoice_item_id=invoice_item.id, contract_item_id=item.id, allocated_quantity=Decimal("1"),
        allocated_net_amount=Decimal("10"), confirmation_type=ItemAllocationConfirmationType.MANUAL_CONFIRMED,
        source_fragment_id=frag.id, created_at=NOW,
    )
    repo.add(old)
    db_session.flush()

    frag2 = _make_fragment(db_session)
    with pytest.raises(WholeFactSupersessionError, match="11-A"):
        supersede_invoice_item_allocation(
            db_session, superseded_fact_id=old.id, allocated_quantity=Decimal("2"),
            allocated_net_amount=Decimal("20"), confirmation_type=ItemAllocationConfirmationType.MANUAL_CONFIRMED,
            source_fragment_id=frag2.id, created_at=NOW,
        )


def test_invoice_item_allocation_supersede_capacity_excludes_the_superseded_allocation(db_session):
    """Capacity is recomputed WITHOUT the allocation being superseded —
    its quantity is handed back by this very operation. Correcting a
    fully-allocated line like-for-like is therefore legal (it would be
    rejected outright if the old quantity still counted as committed)."""
    _contract, item, _invoice, invoice_item = _make_allocation_case(db_session)
    frag = _make_fragment(db_session)
    repo = InvoiceItemAllocationRepository(db_session)
    old = InvoiceItemAllocation(
        id=uuid.uuid4(), invoice_item_id=invoice_item.id, contract_item_id=item.id, allocated_quantity=Decimal("10"),
        allocated_net_amount=Decimal("100"), confirmation_type=ItemAllocationConfirmationType.MANUAL_CONFIRMED,
        source_fragment_id=frag.id, created_at=NOW,
    )
    repo.add(old)
    db_session.flush()
    assert InvoiceItemAllocationRepository(db_session).sum_allocated_quantity_for_invoice_item(
        invoice_item.id
    ) == Decimal("10")  # precondition: the line is fully allocated

    frag2 = _make_fragment(db_session)
    new_fact = supersede_invoice_item_allocation(
        db_session, superseded_fact_id=old.id, allocated_quantity=Decimal("10"), allocated_net_amount=Decimal("100"),
        confirmation_type=ItemAllocationConfirmationType.MANUAL_CONFIRMED, source_fragment_id=frag2.id,
        created_at=NOW,
    )
    assert new_fact.allocated_quantity == Decimal("10")
    assert InvoiceItemAllocationRepository(db_session).sum_allocated_quantity_for_invoice_item(
        invoice_item.id
    ) == Decimal("10")  # still exactly one current allocation, never a stacked 20


def test_invoice_item_allocation_supersede_capacity_excludes_only_the_superseded_allocation(db_session):
    """The exclusion is exactly ONE allocation wide, and it is not a
    blank cheque: a sibling allocation that is NOT being superseded
    still counts, and the replacement may still not exceed the invoice
    line's own quantity."""
    contract, item, _invoice, invoice_item = _make_allocation_case(db_session)
    frag = _make_fragment(db_session)
    sibling_item = create_contract_item_fact(
        db_session, contract_id=contract.id, source_item_key="ITEM-2", fields={"product_name": "Gadget"},
        source_fragment_id=frag.id, created_at=NOW,
    ).item
    repo = InvoiceItemAllocationRepository(db_session)
    old = InvoiceItemAllocation(
        id=uuid.uuid4(), invoice_item_id=invoice_item.id, contract_item_id=item.id, allocated_quantity=Decimal("4"),
        allocated_net_amount=Decimal("40"), confirmation_type=ItemAllocationConfirmationType.MANUAL_CONFIRMED,
        source_fragment_id=frag.id, created_at=NOW,
    )
    sibling = InvoiceItemAllocation(
        id=uuid.uuid4(), invoice_item_id=invoice_item.id, contract_item_id=sibling_item.id,
        allocated_quantity=Decimal("3"), allocated_net_amount=Decimal("30"),
        confirmation_type=ItemAllocationConfirmationType.MANUAL_CONFIRMED, source_fragment_id=frag.id,
        created_at=NOW,
    )
    repo.add(old)
    repo.add(sibling)
    db_session.flush()
    assert repo.sum_allocated_quantity_for_invoice_item(invoice_item.id) == Decimal("7")

    frag2 = _make_fragment(db_session)
    # Sibling's 3 still counts, the old 4 does NOT: 3 + 7 = 10 == the
    # line's quantity, so this is exactly at capacity and must pass.
    new_fact = supersede_invoice_item_allocation(
        db_session, superseded_fact_id=old.id, allocated_quantity=Decimal("7"), allocated_net_amount=Decimal("70"),
        confirmation_type=ItemAllocationConfirmationType.MANUAL_CONFIRMED, source_fragment_id=frag2.id,
        created_at=NOW,
    )
    assert new_fact.allocated_quantity == Decimal("7")
    assert repo.sum_allocated_quantity_for_invoice_item(invoice_item.id) == Decimal("10")  # 3 + 7, old retired

    frag3 = _make_fragment(db_session)
    with pytest.raises(WholeFactSupersessionError, match="capacity exceeded"):
        # 3 (sibling, still committed) + 8 > 10 (line) — refused.
        supersede_invoice_item_allocation(
            db_session, superseded_fact_id=new_fact.id, allocated_quantity=Decimal("8"),
            allocated_net_amount=Decimal("80"), confirmation_type=ItemAllocationConfirmationType.MANUAL_CONFIRMED,
            source_fragment_id=frag3.id, created_at=NOW,
        )


def test_invoice_item_allocation_supersede_rejects_unsupported_confirmation_type(db_session):
    """The closed MANUAL_CONFIRMED set (Phase 2B section 10) is inherited
    by supersession — never widened into an arbitrary confirmation."""
    _contract, item, _invoice, invoice_item = _make_allocation_case(db_session)
    frag = _make_fragment(db_session)
    repo = InvoiceItemAllocationRepository(db_session)
    old = InvoiceItemAllocation(
        id=uuid.uuid4(), invoice_item_id=invoice_item.id, contract_item_id=item.id, allocated_quantity=Decimal("2"),
        allocated_net_amount=Decimal("20"), confirmation_type=ItemAllocationConfirmationType.MANUAL_CONFIRMED,
        source_fragment_id=frag.id, created_at=NOW,
    )
    repo.add(old)
    db_session.flush()

    frag2 = _make_fragment(db_session)
    with pytest.raises(WholeFactSupersessionError, match="confirmation_type"):
        supersede_invoice_item_allocation(
            db_session, superseded_fact_id=old.id, allocated_quantity=Decimal("3"),
            allocated_net_amount=Decimal("30"), confirmation_type="AUTO_CONFIRMED", source_fragment_id=frag2.id,
            created_at=NOW,
        )


def test_cost_recognition_fact_supersede_accepts_shipment_of_the_same_contract(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)
    shipment = create_shipment_fact(
        db_session, contract_id=contract.id, external_reference="SHIP-WFS", execution_date=date(2025, 12, 1),
        fields={}, source_fragment_id=frag.id, created_at=NOW,
    ).shipment
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
        basis=CostRecognitionBasis.EXPORT_EXECUTION_CONFIRMED, source_fragment_id=frag2.id,
        shipment_id=shipment.id, created_at=NOW,
    )
    assert new_fact.shipment_id == shipment.id
    assert new_fact.contract_id == contract.id


def test_cost_recognition_fact_supersede_rejects_shipment_of_another_contract(db_session):
    """A Shipment of a DIFFERENT Contract is a different business fact —
    never an admissible replacement for this Contract's cost
    recognition (no cross-contract retargeting via "correction")."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, contract_no="C-WFS")
    other_contract = _make_contract(db_session, frag.id, contract_no="C-OTHER")
    other_shipment = create_shipment_fact(
        db_session, contract_id=other_contract.id, external_reference="SHIP-OTHER",
        execution_date=date(2025, 12, 1), fields={}, source_fragment_id=frag.id, created_at=NOW,
    ).shipment
    repo = CostRecognitionFactRepository(db_session)
    old = CostRecognitionFact(
        id=uuid.uuid4(), contract_id=contract.id, recognition_date=date(2025, 12, 1),
        basis=CostRecognitionBasis.MANUAL_CONFIRMED, source_fragment_id=frag.id, created_at=NOW, shipment_id=None,
    )
    repo.add(old)
    db_session.flush()

    frag2 = _make_fragment(db_session)
    with pytest.raises(WholeFactSupersessionError, match="SAME contract"):
        supersede_cost_recognition_fact(
            db_session, superseded_fact_id=old.id, recognition_date=date(2025, 12, 20),
            basis=CostRecognitionBasis.EXPORT_EXECUTION_CONFIRMED, source_fragment_id=frag2.id,
            shipment_id=other_shipment.id, created_at=NOW,
        )
    assert ShipmentRepository(db_session).get(other_shipment.id).contract_id == other_contract.id  # untouched


# ---------------------------------------------------------------------------
# Round-3 gate fix — insert-new-fact + CAS-mark-old is one SAVEPOINT: a CAS
# loss must write ZERO new fact rows, even once the caller catches the
# error and explicitly commits its own (otherwise unrelated) transaction.
# Two independent Sessions over a real file-backed database (the same
# idiom as tests/integration/test_contract_item_facts.py's
# test_two_independent_sessions_stale_correction_is_rejected).
# ---------------------------------------------------------------------------


def test_cost_recognition_fact_stale_session_supersession_leaves_no_orphan_fact(tmp_path):
    db_path = tmp_path / "wfs-concurrency.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)

    with session_factory() as setup_session:
        frag = _make_fragment(setup_session)
        contract = _make_contract(setup_session, frag.id)
        old = CostRecognitionFact(
            id=uuid.uuid4(), contract_id=contract.id, recognition_date=date(2025, 12, 1),
            basis=CostRecognitionBasis.MANUAL_CONFIRMED, source_fragment_id=frag.id, created_at=NOW,
            shipment_id=None,
        )
        CostRecognitionFactRepository(setup_session).add(old)
        setup_session.commit()
        old_id = old.id

    session_a = session_factory()
    session_b = session_factory()
    try:
        # Session A preloads the old fact — establishing A's own view
        # before B ever touches it.
        preloaded_for_a = CostRecognitionFactRepository(session_a).get(old_id)
        assert preloaded_for_a is not None

        # Session B supersedes the SAME old fact and commits — a genuine
        # concurrent writer winning the race.
        frag_b = _make_fragment(session_b)
        result_b = supersede_cost_recognition_fact(
            session_b, superseded_fact_id=old_id, recognition_date=date(2025, 12, 10),
            basis=CostRecognitionBasis.EXPORT_EXECUTION_CONFIRMED, source_fragment_id=frag_b.id, shipment_id=None,
            created_at=NOW,
        )
        session_b.commit()

        # Session A, unaware of B's already-committed win, attempts its
        # own supersession of the SAME old fact — it must fail, and MUST
        # NOT leave a new fact row pending in A's transaction.
        frag_a = _make_fragment(session_a)
        with pytest.raises(WholeFactSupersessionError):
            supersede_cost_recognition_fact(
                session_a, superseded_fact_id=old_id, recognition_date=date(2025, 12, 20),
                basis=CostRecognitionBasis.MANUAL_CONFIRMED, source_fragment_id=frag_a.id, shipment_id=None,
                created_at=NOW,
            )
        # Session A explicitly COMMITS (not rollback) after catching the
        # error — proving the SAVEPOINT already discarded A's own
        # attempted new fact, so there is nothing left for this commit to
        # persist as an orphan.
        session_a.commit()
    finally:
        session_a.close()
        session_b.close()

    with session_factory() as verify_session:
        repo = CostRecognitionFactRepository(verify_session)
        current = repo.list_all()
        assert len(current) == 1
        assert current[0].id == result_b.id  # only B's replacement is current
        all_facts = repo.list_all_including_superseded()
        assert {f.id for f in all_facts} == {old_id, result_b.id}  # no orphan from A


def test_cost_recognition_fact_supersede_rejects_unsupported_basis(db_session):
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
    with pytest.raises(WholeFactSupersessionError, match="unsupported CostRecognitionFact basis"):
        supersede_cost_recognition_fact(
            db_session, superseded_fact_id=old.id, recognition_date=date(2025, 12, 20), basis="GUESSED_BASIS",
            source_fragment_id=frag2.id, shipment_id=None, created_at=NOW,
        )
