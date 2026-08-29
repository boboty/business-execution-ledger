"""Phase 2D.1-R2 — Shipment Minimum Vertical Slice.

Covers docs/PHASE2D1-R0-DECISIONS.md section 3's frozen Shipment
semantics as implemented by bel.application.shipment_facts, reusing
(deliberately, not abstracted into a shared engine) the exact pattern
bel.application.contract_item_facts had validated across three Phase
2D.1-R1 Codex fix rounds: the three cases never share one operation,
every revision carries Evidence, exact replay requires identity +
fragment + intent + asserted content, the repository enforces revision
topology itself (no unchecked "just insert a revision" primitive, no
NULL-Evidence production revision, no second INITIAL, no second current,
no unknown revision_type, no cross-anchor CAS), and a correction that
supersedes a revision a persisted CostRecognitionFact still references
raises a Task rather than silently rewriting it.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from bel.application.shipment_facts import (
    ShipmentFactConflict,
    ShipmentFactError,
    ShipmentIdentityIncomplete,
    correct_shipment_fact,
    create_shipment_fact,
    execute_correct_shipment_fact,
    execute_create_shipment_fact,
    execute_supplement_shipment_fact,
    get_shipment_history,
    list_shipments_for_contract,
    supplement_shipment_fact,
)
from bel.domain.accrual import CostRecognitionBasis, CostRecognitionFact
from bel.domain.contract import Contract, ContractItem
from bel.domain.evidence import EvidenceDocument, EvidenceFragment, FragmentKind
from bel.domain.exception import ExceptionStatus, ExceptionType
from bel.domain.shipment import ShipmentRevision, ShipmentRevisionType
from bel.infrastructure.persistence.database import make_engine, make_session_factory
from bel.infrastructure.persistence.models import Base, ShipmentRevisionModel
from bel.infrastructure.persistence.repositories import (
    ContractItemRepository,
    ContractRepository,
    CostRecognitionFactRepository,
    EvidenceRepository,
    ExceptionRepository,
    ShipmentRepository,
)

NOW = datetime.now(timezone.utc)
EXEC_DATE = date(2031, 3, 10)


def _make_fragment(session, raw_data=None):
    doc = EvidenceDocument(
        id=uuid.uuid4(), file_name="x", sha256=uuid.uuid4().hex + uuid.uuid4().hex, source_type="t", imported_at=NOW
    )
    evidence_repo = EvidenceRepository(session)
    evidence_repo.add_document(doc)
    frag = EvidenceFragment(
        id=uuid.uuid4(),
        evidence_document_id=doc.id,
        fragment_kind=FragmentKind.MANUAL_FACT,
        sheet_name=None,
        row_number=None,
        locator_json={"section": "test", "index": 0},
        raw_data=raw_data or {},
        created_at=NOW,
    )
    evidence_repo.add_fragment(frag)
    session.flush()
    return frag


def _make_contract(session, fragment_id, contract_no=None):
    contract = Contract(
        id=uuid.uuid4(),
        contract_no=contract_no or f"C-{uuid.uuid4().hex[:8]}",
        contract_type=None,
        counterparty="Supplier",
        buyer="Buyer Co",
        gross_amount=Decimal("1000.00"),
        currency="CNY",
        contract_date=None,
        current_source_fragment_id=fragment_id,
        created_at=NOW,
        updated_at=NOW,
    )
    ContractRepository(session).add(contract)
    session.flush()
    return contract


def _make_contract_item(session, contract, fragment_id, source_item_key="ITEM-A"):
    item = ContractItem(
        id=uuid.uuid4(),
        contract_id=contract.id,
        source_item_key=source_item_key,
        sku=None,
        product_name="Widget",
        specification=None,
        quantity=Decimal("100"),
        unit="件",
        unit_price=None,
        gross_amount=None,
        tax_rate=None,
        net_amount=None,
        current_source_fragment_id=fragment_id,
        created_at=NOW,
    )
    ContractItemRepository(session).add(item)
    session.flush()
    return item


def _create_shipment(db_session, fields=None, external_reference="EXP-001", execution_date=EXEC_DATE):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)
    result = create_shipment_fact(
        db_session,
        contract_id=contract.id,
        external_reference=external_reference,
        execution_date=execution_date,
        fields=fields or {},
        source_fragment_id=frag.id,
        created_at=NOW,
    )
    db_session.commit()
    return result.shipment, contract


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


def test_create_shipment_fact_from_evidence(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)

    result = create_shipment_fact(
        db_session,
        contract_id=contract.id,
        external_reference="EXP-001",
        execution_date=EXEC_DATE,
        fields={"quantity": Decimal("10")},
        source_fragment_id=frag.id,
        created_at=NOW,
    )
    db_session.commit()

    assert result.created is True
    assert result.shipment.contract_id == contract.id
    assert result.shipment.external_reference == "EXP-001"
    assert result.shipment.execution_date == EXEC_DATE
    assert result.shipment.quantity == Decimal("10")
    assert result.shipment.contract_item_id is None

    current = ShipmentRepository(db_session).get(result.shipment.id)
    assert current == result.shipment
    assert current.current_source_fragment_id == frag.id

    history = get_shipment_history(db_session, result.shipment.id)
    assert len(history) == 1
    assert history[0].revision_type == ShipmentRevisionType.INITIAL
    assert history[0].superseded_by_revision_id is None


def test_create_shipment_fact_with_valid_contract_item(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)
    item = _make_contract_item(db_session, contract, frag.id)

    result = create_shipment_fact(
        db_session,
        contract_id=contract.id,
        external_reference="EXP-001",
        execution_date=EXEC_DATE,
        fields={"contract_item_id": item.id, "quantity": Decimal("10")},
        source_fragment_id=frag.id,
        created_at=NOW,
    )
    db_session.commit()
    assert result.shipment.contract_item_id == item.id


def test_create_shipment_fact_rejects_contract_item_from_other_contract(db_session):
    frag = _make_fragment(db_session)
    contract_a = _make_contract(db_session, frag.id)
    contract_b = _make_contract(db_session, frag.id)
    item_b = _make_contract_item(db_session, contract_b, frag.id)

    with pytest.raises(ShipmentFactError):
        create_shipment_fact(
            db_session,
            contract_id=contract_a.id,
            external_reference="EXP-001",
            execution_date=EXEC_DATE,
            fields={"contract_item_id": item_b.id},
            source_fragment_id=frag.id,
            created_at=NOW,
        )


def test_create_shipment_fact_null_external_reference_requires_confirmation(db_session):
    """Phase 2D.1-R2 Codex fix round, BLOCKER 1: external_reference=None
    is "identity incomplete" (section 4.4) — no anchor is created without
    explicit identity_confirmed=True. The Evidence is preserved and a
    persisted SHIPMENT_IDENTITY_INCOMPLETE Task is raised instead."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)

    with pytest.raises(ShipmentIdentityIncomplete):
        create_shipment_fact(
            db_session,
            contract_id=contract.id,
            external_reference=None,
            execution_date=EXEC_DATE,
            fields={"quantity": Decimal("10")},
            source_fragment_id=frag.id,
            created_at=NOW,
        )
    db_session.commit()

    assert list_shipments_for_contract(db_session, contract.id) == []
    open_tasks = ExceptionRepository(db_session).list_open()
    matching = [t for t in open_tasks if t.exception_type == ExceptionType.SHIPMENT_IDENTITY_INCOMPLETE]
    assert len(matching) == 1
    assert matching[0].detail["source_fragment_id"] == str(frag.id)
    assert matching[0].detail["contract_id"] == str(contract.id)

    # Replaying the SAME Evidence must not raise a SECOND Task.
    with pytest.raises(ShipmentIdentityIncomplete):
        create_shipment_fact(
            db_session,
            contract_id=contract.id,
            external_reference=None,
            execution_date=EXEC_DATE,
            fields={"quantity": Decimal("10")},
            source_fragment_id=frag.id,
            created_at=NOW,
        )
    db_session.commit()
    still_matching = [
        t for t in ExceptionRepository(db_session).list_open() if t.exception_type == ExceptionType.SHIPMENT_IDENTITY_INCOMPLETE
    ]
    assert len(still_matching) == 1  # idempotent — no duplicate Task
    assert list_shipments_for_contract(db_session, contract.id) == []  # still no anchor


def test_create_shipment_fact_null_external_reference_confirmed_creates_anchor(db_session):
    """identity_confirmed=True is the explicit human confirmation that
    creates the anchor despite the incomplete identity. Replaying the
    SAME Evidence resolves to the SAME anchor (global fragment lookup —
    there is no business key to scope by); a genuinely NEW Evidence
    fragment always creates an independent anchor, since no reliable key
    exists to compare its content against."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)

    first = create_shipment_fact(
        db_session,
        contract_id=contract.id,
        external_reference=None,
        execution_date=EXEC_DATE,
        fields={"quantity": Decimal("10")},
        source_fragment_id=frag.id,
        created_at=NOW,
        identity_confirmed=True,
    )
    db_session.commit()
    assert first.created is True

    replay = create_shipment_fact(
        db_session,
        contract_id=contract.id,
        external_reference=None,
        execution_date=EXEC_DATE,
        fields={"quantity": Decimal("10")},
        source_fragment_id=frag.id,  # SAME fragment
        created_at=NOW,
        identity_confirmed=True,
    )
    db_session.commit()
    assert replay.created is False
    assert replay.replay is True
    assert replay.shipment.id == first.shipment.id

    frag2 = _make_fragment(db_session)
    second = create_shipment_fact(
        db_session,
        contract_id=contract.id,
        external_reference=None,
        execution_date=EXEC_DATE,
        fields={"quantity": Decimal("10")},  # identical content, but a NEW fragment
        source_fragment_id=frag2.id,
        created_at=NOW,
        identity_confirmed=True,
    )
    db_session.commit()
    assert second.created is True
    assert second.shipment.id != first.shipment.id
    assert len(list_shipments_for_contract(db_session, contract.id)) == 2


def test_create_null_external_reference_rejects_cross_contract_fragment_reuse(db_session):
    """Regression for Phase 2D.1-R2 second Codex fix round: the global
    fragment lookup used for confirmed null-external-reference creates
    must never return an anchor belonging to a DIFFERENT contract just
    because it happens to share the fragment id — that is a
    cross-contract misattribution, not a replay, and must be rejected."""
    frag = _make_fragment(db_session)
    contract_a = _make_contract(db_session, frag.id)
    contract_b = _make_contract(db_session, frag.id)

    first = create_shipment_fact(
        db_session,
        contract_id=contract_a.id,
        external_reference=None,
        execution_date=EXEC_DATE,
        fields={"quantity": Decimal("10")},
        source_fragment_id=frag.id,
        created_at=NOW,
        identity_confirmed=True,
    )
    db_session.commit()
    assert first.created is True

    # SAME fragment, but a DIFFERENT contract — must never silently
    # return contract_a's shipment, and must never silently create a
    # second anchor either.
    with pytest.raises(ShipmentFactConflict):
        create_shipment_fact(
            db_session,
            contract_id=contract_b.id,
            external_reference=None,
            execution_date=EXEC_DATE,
            fields={"quantity": Decimal("10")},
            source_fragment_id=frag.id,
            created_at=NOW,
            identity_confirmed=True,
        )
    db_session.rollback()

    assert list_shipments_for_contract(db_session, contract_a.id) == [first.shipment]
    assert list_shipments_for_contract(db_session, contract_b.id) == []


def test_create_null_external_reference_rejects_content_mismatch_on_reused_fragment(db_session):
    """Same fragment, same contract and execution_date, but DIFFERENT
    asserted content -> not a replay either; reject rather than silently
    treat it as a fresh anchor or silently return the mismatched one."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)

    first = create_shipment_fact(
        db_session,
        contract_id=contract.id,
        external_reference=None,
        execution_date=EXEC_DATE,
        fields={"quantity": Decimal("10")},
        source_fragment_id=frag.id,
        created_at=NOW,
        identity_confirmed=True,
    )
    db_session.commit()

    with pytest.raises(ShipmentFactConflict):
        create_shipment_fact(
            db_session,
            contract_id=contract.id,
            external_reference=None,
            execution_date=EXEC_DATE,
            fields={"quantity": Decimal("999")},  # different content, SAME fragment
            source_fragment_id=frag.id,
            created_at=NOW,
            identity_confirmed=True,
        )
    db_session.rollback()

    assert list_shipments_for_contract(db_session, contract.id) == [first.shipment]


def test_create_exact_replay_same_identity_same_evidence_same_assertion(db_session):
    shipment, contract = _create_shipment(db_session, fields={"quantity": Decimal("10")})

    frag = ShipmentRepository(db_session).get_current_revision(shipment.id).source_fragment_id
    replay = create_shipment_fact(
        db_session,
        contract_id=contract.id,
        external_reference="EXP-001",
        execution_date=EXEC_DATE,
        fields={"quantity": Decimal("10")},
        source_fragment_id=frag,
        created_at=NOW,
    )
    db_session.commit()

    assert replay.created is False
    assert replay.revision_written is False
    assert replay.replay is True
    assert replay.corroborating is False
    assert replay.shipment.id == shipment.id
    assert len(get_shipment_history(db_session, shipment.id)) == 1


def test_create_corroborating_same_identity_different_evidence_same_assertion(db_session):
    shipment, contract = _create_shipment(db_session, fields={"quantity": Decimal("10")})

    frag2 = _make_fragment(db_session)
    corroborating = create_shipment_fact(
        db_session,
        contract_id=contract.id,
        external_reference="EXP-001",
        execution_date=EXEC_DATE,
        fields={"quantity": Decimal("10")},
        source_fragment_id=frag2.id,
        created_at=NOW,
    )
    db_session.commit()

    assert corroborating.created is False
    assert corroborating.revision_written is False
    assert corroborating.replay is False
    assert corroborating.corroborating is True
    assert corroborating.shipment.id == shipment.id
    assert len(get_shipment_history(db_session, shipment.id)) == 1
    assert EvidenceRepository(db_session).get_fragment(frag2.id) is not None


def test_create_conflict_same_identity_different_evidence_conflicting_assertion(db_session):
    """Phase 2D.1-R2 Codex fix round, BLOCKER 2: this is section 4.4's
    "Same key, different Evidence -> Task" — a persisted
    SHIPMENT_IDENTITY_CONFLICT Task must survive alongside the raised
    ShipmentFactConflict, and the existing anchor/revision must be
    completely unchanged."""
    shipment, contract = _create_shipment(db_session, fields={"quantity": Decimal("10")})
    initial_revision = ShipmentRepository(db_session).get_current_revision(shipment.id)

    frag2 = _make_fragment(db_session)
    with pytest.raises(ShipmentFactConflict):
        create_shipment_fact(
            db_session,
            contract_id=contract.id,
            external_reference="EXP-001",
            execution_date=EXEC_DATE,
            fields={"quantity": Decimal("999")},
            source_fragment_id=frag2.id,
            created_at=NOW,
        )
    db_session.commit()  # the rejected create's Task must survive commit, unlike a rollback

    assert len(get_shipment_history(db_session, shipment.id)) == 1  # anchor/revision unchanged
    unchanged = ShipmentRepository(db_session).get_current_revision(shipment.id)
    assert unchanged.id == initial_revision.id
    assert unchanged.quantity == Decimal("10")

    open_tasks = ExceptionRepository(db_session).list_open()
    matching = [t for t in open_tasks if t.exception_type == ExceptionType.SHIPMENT_IDENTITY_CONFLICT]
    assert len(matching) == 1
    assert matching[0].detail["shipment_id"] == str(shipment.id)
    assert matching[0].detail["existing_source_fragment_id"] == str(initial_revision.source_fragment_id)
    assert matching[0].detail["conflicting_source_fragment_id"] == str(frag2.id)
    assert matching[0].detail["existing_assertion"] == {"quantity": "10.0000"}
    assert matching[0].detail["conflicting_assertion"] == {"quantity": "999"}

    # Replaying the SAME conflicting submission must not raise a SECOND Task.
    with pytest.raises(ShipmentFactConflict):
        create_shipment_fact(
            db_session,
            contract_id=contract.id,
            external_reference="EXP-001",
            execution_date=EXEC_DATE,
            fields={"quantity": Decimal("999")},
            source_fragment_id=frag2.id,
            created_at=NOW,
        )
    db_session.commit()
    still_matching = [
        t for t in ExceptionRepository(db_session).list_open() if t.exception_type == ExceptionType.SHIPMENT_IDENTITY_CONFLICT
    ]
    assert len(still_matching) == 1  # idempotent — no duplicate Task


def test_create_conflict_same_evidence_different_initial_assertion(db_session):
    shipment, contract = _create_shipment(db_session, fields={"quantity": Decimal("10")})
    frag = ShipmentRepository(db_session).get_current_revision(shipment.id).source_fragment_id

    with pytest.raises(ShipmentFactConflict):
        create_shipment_fact(
            db_session,
            contract_id=contract.id,
            external_reference="EXP-001",
            execution_date=EXEC_DATE,
            fields={"quantity": Decimal("999")},
            source_fragment_id=frag,  # SAME fragment, different content
            created_at=NOW,
        )


def test_create_shipment_fact_rejects_unknown_contract(db_session):
    frag = _make_fragment(db_session)
    with pytest.raises(ShipmentFactError):
        create_shipment_fact(
            db_session,
            contract_id=uuid.uuid4(),
            external_reference="EXP-001",
            execution_date=EXEC_DATE,
            fields={},
            source_fragment_id=frag.id,
            created_at=NOW,
        )


def test_create_shipment_fact_rejects_missing_evidence(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)
    with pytest.raises(ShipmentFactError):
        create_shipment_fact(
            db_session,
            contract_id=contract.id,
            external_reference="EXP-001",
            execution_date=EXEC_DATE,
            fields={},
            source_fragment_id=uuid.uuid4(),
            created_at=NOW,
        )


def test_create_shipment_fact_rejects_unknown_field(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)
    with pytest.raises(ShipmentFactError):
        create_shipment_fact(
            db_session,
            contract_id=contract.id,
            external_reference="EXP-001",
            execution_date=EXEC_DATE,
            fields={"not_a_real_field": "x"},
            source_fragment_id=frag.id,
            created_at=NOW,
        )


# ---------------------------------------------------------------------------
# Supplement
# ---------------------------------------------------------------------------


def test_supplement_fills_previously_unknown_field(db_session):
    shipment, contract = _create_shipment(db_session, fields={"quantity": Decimal("10")})
    current = ShipmentRepository(db_session).get_current_revision(shipment.id)
    item = _make_contract_item(db_session, contract, current.source_fragment_id)
    frag2 = _make_fragment(db_session)

    result = supplement_shipment_fact(
        db_session,
        shipment_id=shipment.id,
        based_on_revision_id=current.id,
        fields={"contract_item_id": item.id},
        source_fragment_id=frag2.id,
        created_at=NOW,
    )
    db_session.commit()

    assert result.revision_written is True
    assert result.shipment.contract_item_id == item.id
    assert result.shipment.quantity == Decimal("10")  # carried forward unchanged

    history = get_shipment_history(db_session, shipment.id)
    assert [r.revision_type for r in history] == [ShipmentRevisionType.INITIAL, ShipmentRevisionType.SUPPLEMENT]
    assert history[0].superseded_by_revision_id == history[1].id
    assert history[0].contract_item_id is None  # retired revision's own values never change


def test_supplement_exact_replay(db_session):
    shipment, _ = _create_shipment(db_session)
    current = ShipmentRepository(db_session).get_current_revision(shipment.id)
    frag2 = _make_fragment(db_session)

    first = supplement_shipment_fact(
        db_session,
        shipment_id=shipment.id,
        based_on_revision_id=current.id,
        fields={"quantity": Decimal("10")},
        source_fragment_id=frag2.id,
        created_at=NOW,
    )
    db_session.commit()

    replay = supplement_shipment_fact(
        db_session,
        shipment_id=shipment.id,
        based_on_revision_id=current.id,  # deliberately stale — replay must still succeed
        fields={"quantity": Decimal("10")},
        source_fragment_id=frag2.id,
        created_at=NOW,
    )
    db_session.commit()

    assert replay.revision_written is False
    assert replay.replay is True
    assert replay.shipment.id == first.shipment.id
    assert len(get_shipment_history(db_session, shipment.id)) == 2


def test_supplement_conflict_same_evidence_different_value(db_session):
    shipment, _ = _create_shipment(db_session)
    current = ShipmentRepository(db_session).get_current_revision(shipment.id)
    frag2 = _make_fragment(db_session)

    supplement_shipment_fact(
        db_session,
        shipment_id=shipment.id,
        based_on_revision_id=current.id,
        fields={"quantity": Decimal("10")},
        source_fragment_id=frag2.id,
        created_at=NOW,
    )
    db_session.commit()

    with pytest.raises(ShipmentFactConflict):
        supplement_shipment_fact(
            db_session,
            shipment_id=shipment.id,
            based_on_revision_id=uuid.uuid4(),
            fields={"quantity": Decimal("999")},
            source_fragment_id=frag2.id,  # SAME fragment
            created_at=NOW,
        )


def test_supplement_conflict_same_evidence_reused_as_correction(db_session):
    shipment, _ = _create_shipment(db_session, fields={"quantity": Decimal("10")})
    current = ShipmentRepository(db_session).get_current_revision(shipment.id)
    frag2 = _make_fragment(db_session)

    supplement_shipment_fact(
        db_session,
        shipment_id=shipment.id,
        based_on_revision_id=current.id,
        fields={"quantity": Decimal("10")},  # resupply same value — allowed, harmless
        source_fragment_id=frag2.id,
        created_at=NOW,
    )
    db_session.commit()
    current2 = ShipmentRepository(db_session).get_current_revision(shipment.id)

    with pytest.raises(ShipmentFactConflict):
        correct_shipment_fact(
            db_session,
            shipment_id=shipment.id,
            based_on_revision_id=current2.id,
            fields={"quantity": Decimal("20")},
            source_fragment_id=frag2.id,  # SAME fragment, different intent
            created_at=NOW,
        )


def test_supplement_rejects_conflicting_known_value(db_session):
    shipment, _ = _create_shipment(db_session, fields={"quantity": Decimal("10")})
    current = ShipmentRepository(db_session).get_current_revision(shipment.id)
    frag2 = _make_fragment(db_session)

    with pytest.raises(ShipmentFactConflict):
        supplement_shipment_fact(
            db_session,
            shipment_id=shipment.id,
            based_on_revision_id=current.id,
            fields={"quantity": Decimal("999")},
            source_fragment_id=frag2.id,
            created_at=NOW,
        )
    db_session.rollback()
    assert len(get_shipment_history(db_session, shipment.id)) == 1


def test_supplement_rejects_stale_based_on_revision(db_session):
    shipment, _ = _create_shipment(db_session)
    current = ShipmentRepository(db_session).get_current_revision(shipment.id)
    frag2 = _make_fragment(db_session)
    supplement_shipment_fact(
        db_session,
        shipment_id=shipment.id,
        based_on_revision_id=current.id,
        fields={"quantity": Decimal("10")},
        source_fragment_id=frag2.id,
        created_at=NOW,
    )
    db_session.commit()

    frag3 = _make_fragment(db_session)
    with pytest.raises(ShipmentFactConflict):
        supplement_shipment_fact(
            db_session,
            shipment_id=shipment.id,
            based_on_revision_id=current.id,  # now stale
            fields={"quantity": Decimal("11")},
            source_fragment_id=frag3.id,
            created_at=NOW,
        )


# ---------------------------------------------------------------------------
# Correction
# ---------------------------------------------------------------------------


def test_correction_replaces_wrong_known_value(db_session):
    shipment, _ = _create_shipment(db_session, fields={"quantity": Decimal("10")})
    current = ShipmentRepository(db_session).get_current_revision(shipment.id)
    frag2 = _make_fragment(db_session)

    result = correct_shipment_fact(
        db_session,
        shipment_id=shipment.id,
        based_on_revision_id=current.id,
        fields={"quantity": Decimal("12")},
        source_fragment_id=frag2.id,
        created_at=NOW,
    )
    db_session.commit()

    assert result.shipment.quantity == Decimal("12")
    history = get_shipment_history(db_session, shipment.id)
    assert len(history) == 2
    assert history[0].quantity == Decimal("10")  # old revision retained, unmutated
    assert history[1].revision_type == ShipmentRevisionType.CORRECTION
    current_shipment = ShipmentRepository(db_session).get(shipment.id)
    assert current_shipment.quantity == Decimal("12")


def test_correction_exact_replay(db_session):
    shipment, _ = _create_shipment(db_session, fields={"quantity": Decimal("10")})
    current = ShipmentRepository(db_session).get_current_revision(shipment.id)
    frag2 = _make_fragment(db_session)

    correct_shipment_fact(
        db_session,
        shipment_id=shipment.id,
        based_on_revision_id=current.id,
        fields={"quantity": Decimal("12")},
        source_fragment_id=frag2.id,
        created_at=NOW,
    )
    db_session.commit()

    replay = correct_shipment_fact(
        db_session,
        shipment_id=shipment.id,
        based_on_revision_id=current.id,
        fields={"quantity": Decimal("12")},
        source_fragment_id=frag2.id,
        created_at=NOW,
    )
    db_session.commit()

    assert replay.revision_written is False
    assert replay.replay is True
    assert len(get_shipment_history(db_session, shipment.id)) == 2


def test_correction_conflict_same_evidence_different_value(db_session):
    shipment, _ = _create_shipment(db_session, fields={"quantity": Decimal("10")})
    current = ShipmentRepository(db_session).get_current_revision(shipment.id)
    frag2 = _make_fragment(db_session)

    correct_shipment_fact(
        db_session,
        shipment_id=shipment.id,
        based_on_revision_id=current.id,
        fields={"quantity": Decimal("12")},
        source_fragment_id=frag2.id,
        created_at=NOW,
    )
    db_session.commit()

    with pytest.raises(ShipmentFactConflict):
        correct_shipment_fact(
            db_session,
            shipment_id=shipment.id,
            based_on_revision_id=uuid.uuid4(),
            fields={"quantity": Decimal("13")},
            source_fragment_id=frag2.id,
            created_at=NOW,
        )


def test_correction_rejects_field_with_no_existing_value(db_session):
    shipment, _ = _create_shipment(db_session)
    current = ShipmentRepository(db_session).get_current_revision(shipment.id)
    frag2 = _make_fragment(db_session)

    with pytest.raises(ShipmentFactConflict):
        correct_shipment_fact(
            db_session,
            shipment_id=shipment.id,
            based_on_revision_id=current.id,
            fields={"quantity": Decimal("12")},  # never asserted before -> supplement, not correction
            source_fragment_id=frag2.id,
            created_at=NOW,
        )


def test_correction_rejects_non_current_revision(db_session):
    shipment, _ = _create_shipment(db_session, fields={"quantity": Decimal("10")})
    current = ShipmentRepository(db_session).get_current_revision(shipment.id)
    frag2 = _make_fragment(db_session)
    correct_shipment_fact(
        db_session,
        shipment_id=shipment.id,
        based_on_revision_id=current.id,
        fields={"quantity": Decimal("12")},
        source_fragment_id=frag2.id,
        created_at=NOW,
    )
    db_session.commit()

    frag3 = _make_fragment(db_session)
    with pytest.raises(ShipmentFactConflict):
        correct_shipment_fact(
            db_session,
            shipment_id=shipment.id,
            based_on_revision_id=current.id,  # no longer current
            fields={"quantity": Decimal("13")},
            source_fragment_id=frag3.id,
            created_at=NOW,
        )


# ---------------------------------------------------------------------------
# CostRecognitionFact provenance
# ---------------------------------------------------------------------------


def test_shipment_creation_alone_creates_zero_cost_recognition_facts(db_session):
    _create_shipment(db_session, fields={"quantity": Decimal("10")})
    assert CostRecognitionFactRepository(db_session).list_all() == []


def test_cost_recognition_fact_can_reference_shipment_and_survives_query(db_session):
    shipment, contract = _create_shipment(db_session, fields={"quantity": Decimal("10")})
    frag = ShipmentRepository(db_session).get_current_revision(shipment.id).source_fragment_id

    fact = CostRecognitionFact(
        id=uuid.uuid4(),
        contract_id=contract.id,
        recognition_date=EXEC_DATE,
        basis=CostRecognitionBasis.EXPORT_EXECUTION_CONFIRMED,
        source_fragment_id=frag,
        created_at=NOW,
        shipment_id=shipment.id,
    )
    CostRecognitionFactRepository(db_session).add(fact)
    db_session.commit()

    reloaded = [f for f in CostRecognitionFactRepository(db_session).list_all() if f.id == fact.id][0]
    assert reloaded.shipment_id == shipment.id
    assert CostRecognitionFactRepository(db_session).list_for_shipment(shipment.id) == [reloaded]


def test_legacy_cost_recognition_fact_without_shipment_remains_valid(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)
    fact = CostRecognitionFact(
        id=uuid.uuid4(),
        contract_id=contract.id,
        recognition_date=EXEC_DATE,
        basis=CostRecognitionBasis.MANUAL_CONFIRMED,
        source_fragment_id=frag.id,
        created_at=NOW,
        # no shipment_id supplied — must default to None, not raise
    )
    CostRecognitionFactRepository(db_session).add(fact)
    db_session.commit()

    reloaded = [f for f in CostRecognitionFactRepository(db_session).list_all() if f.id == fact.id][0]
    assert reloaded.shipment_id is None


def test_correcting_shipment_flags_dependent_cost_recognition_fact_with_a_task(db_session):
    shipment, contract = _create_shipment(db_session, fields={"quantity": Decimal("10")})
    current = ShipmentRepository(db_session).get_current_revision(shipment.id)

    fact = CostRecognitionFact(
        id=uuid.uuid4(),
        contract_id=contract.id,
        recognition_date=EXEC_DATE,
        basis=CostRecognitionBasis.EXPORT_EXECUTION_CONFIRMED,
        source_fragment_id=current.source_fragment_id,
        created_at=NOW,
        shipment_id=shipment.id,
    )
    CostRecognitionFactRepository(db_session).add(fact)
    db_session.flush()

    frag2 = _make_fragment(db_session)
    correct_shipment_fact(
        db_session,
        shipment_id=shipment.id,
        based_on_revision_id=current.id,
        fields={"quantity": Decimal("12")},
        source_fragment_id=frag2.id,
        created_at=NOW,
    )
    db_session.commit()

    open_tasks = ExceptionRepository(db_session).list_open()
    matching = [t for t in open_tasks if t.exception_type == ExceptionType.SHIPMENT_FACT_SUPERSEDED]
    assert len(matching) == 1
    assert matching[0].status == ExceptionStatus.OPEN
    assert matching[0].detail["shipment_id"] == str(shipment.id)
    assert str(fact.id) in matching[0].detail["dependents"]["cost_recognition_facts"]

    # The CostRecognitionFact itself is untouched — never silently rewritten or re-pointed.
    unchanged = [f for f in CostRecognitionFactRepository(db_session).list_all() if f.id == fact.id][0]
    assert unchanged.shipment_id == shipment.id
    assert unchanged.recognition_date == EXEC_DATE


def test_correcting_shipment_without_dependents_raises_no_task(db_session):
    shipment, _ = _create_shipment(db_session, fields={"quantity": Decimal("10")})
    current = ShipmentRepository(db_session).get_current_revision(shipment.id)
    frag2 = _make_fragment(db_session)

    correct_shipment_fact(
        db_session,
        shipment_id=shipment.id,
        based_on_revision_id=current.id,
        fields={"quantity": Decimal("12")},
        source_fragment_id=frag2.id,
        created_at=NOW,
    )
    db_session.commit()

    assert ExceptionRepository(db_session).list_open() == []


# ---------------------------------------------------------------------------
# Contract query
# ---------------------------------------------------------------------------


def test_one_contract_has_many_shipments_deterministic_and_current(db_session):
    # Distinct created_at values so list order reflects creation order —
    # list_for_contract's (created_at, id) tie-break only guarantees a
    # STABLE order across repeated queries of the SAME rows, not
    # insertion order when timestamps genuinely tie (a random UUID would
    # then decide index 0 vs 1, which this test does not want to assert
    # on).
    from datetime import timedelta

    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)

    result_a = create_shipment_fact(
        db_session, contract_id=contract.id, external_reference="EXP-A", execution_date=date(2031, 3, 1),
        fields={"quantity": Decimal("10")}, source_fragment_id=frag.id, created_at=NOW,
    )
    frag2 = _make_fragment(db_session)
    result_b = create_shipment_fact(
        db_session, contract_id=contract.id, external_reference="EXP-B", execution_date=date(2031, 3, 2),
        fields={"quantity": Decimal("20")}, source_fragment_id=frag2.id, created_at=NOW + timedelta(seconds=1),
    )
    db_session.commit()

    shipments = list_shipments_for_contract(db_session, contract.id)
    assert [s.id for s in shipments] == [result_a.shipment.id, result_b.shipment.id]

    # Correct A; the list must reflect A's CURRENT projection, not history.
    current_a = ShipmentRepository(db_session).get_current_revision(result_a.shipment.id)
    frag3 = _make_fragment(db_session)
    correct_shipment_fact(
        db_session, shipment_id=result_a.shipment.id, based_on_revision_id=current_a.id,
        fields={"quantity": Decimal("15")}, source_fragment_id=frag3.id, created_at=NOW,
    )
    db_session.commit()

    shipments_again = list_shipments_for_contract(db_session, contract.id)
    quantities = {s.id: s.quantity for s in shipments_again}
    assert quantities[result_a.shipment.id] == Decimal("15")
    assert quantities[result_b.shipment.id] == Decimal("20")
    assert len(shipments_again) == 2  # deterministic order held across repeated calls too
    assert [s.id for s in list_shipments_for_contract(db_session, contract.id)] == [s.id for s in shipments_again]


# ---------------------------------------------------------------------------
# Repository invariant
# ---------------------------------------------------------------------------


def _raw_revision(*, shipment_id, revision_type, source_fragment_id, quantity=None):
    return ShipmentRevision(
        id=uuid.uuid4(),
        shipment_id=shipment_id,
        revision_type=revision_type,
        contract_item_id=None,
        quantity=quantity,
        source_fragment_id=source_fragment_id,
        superseded_by_revision_id=None,
        created_at=NOW,
    )


def test_repository_rejects_new_revision_with_no_evidence(db_session):
    shipment, _ = _create_shipment(db_session)
    current = ShipmentRepository(db_session).get_current_revision(shipment.id)
    repo = ShipmentRepository(db_session)

    with pytest.raises(ValueError):
        repo.create_initial_revision(
            ShipmentRevision(
                id=uuid.uuid4(), shipment_id=uuid.uuid4(), revision_type=ShipmentRevisionType.INITIAL,
                contract_item_id=None, quantity=None, source_fragment_id=None,
                superseded_by_revision_id=None, created_at=NOW,
            )
        )
    with pytest.raises(ValueError):
        repo.append_revision_against_current(
            ShipmentRevision(
                id=uuid.uuid4(), shipment_id=shipment.id, revision_type=ShipmentRevisionType.CORRECTION,
                contract_item_id=None, quantity=None, source_fragment_id=None,
                superseded_by_revision_id=None, created_at=NOW,
            ),
            based_on_revision_id=current.id,
        )


def test_append_rejects_initial_revision_type(db_session):
    shipment, _ = _create_shipment(db_session, fields={"quantity": Decimal("10")})
    current = ShipmentRepository(db_session).get_current_revision(shipment.id)
    repo = ShipmentRepository(db_session)
    frag2 = _make_fragment(db_session)

    bogus = _raw_revision(
        shipment_id=shipment.id, revision_type=ShipmentRevisionType.INITIAL,
        source_fragment_id=frag2.id, quantity=Decimal("99"),
    )
    with pytest.raises(ValueError):
        repo.append_revision_against_current(bogus, based_on_revision_id=current.id)

    still_current = repo.get_current_revision(shipment.id)
    assert still_current.id == current.id
    assert still_current.quantity == Decimal("10")
    assert len(repo.list_revisions(shipment.id)) == 1


def test_append_rejects_unknown_revision_type(db_session):
    shipment, _ = _create_shipment(db_session, fields={"quantity": Decimal("10")})
    current = ShipmentRepository(db_session).get_current_revision(shipment.id)
    repo = ShipmentRepository(db_session)
    frag2 = _make_fragment(db_session)

    bogus = _raw_revision(
        shipment_id=shipment.id, revision_type="WHATEVER", source_fragment_id=frag2.id, quantity=Decimal("99")
    )
    with pytest.raises(ValueError):
        repo.append_revision_against_current(bogus, based_on_revision_id=current.id)

    still_current = repo.get_current_revision(shipment.id)
    assert still_current.id == current.id
    assert len(repo.list_revisions(shipment.id)) == 1


def test_append_rejects_cross_anchor_supersession(db_session):
    shipment_a, _ = _create_shipment(db_session, fields={"quantity": Decimal("10")}, external_reference="EXP-A")
    shipment_b, _ = _create_shipment(db_session, fields={"quantity": Decimal("20")}, external_reference="EXP-B")
    repo = ShipmentRepository(db_session)
    current_a = repo.get_current_revision(shipment_a.id)
    current_b = repo.get_current_revision(shipment_b.id)
    frag = _make_fragment(db_session)

    cross_anchor_attempt = _raw_revision(
        shipment_id=shipment_b.id, revision_type=ShipmentRevisionType.CORRECTION,
        source_fragment_id=frag.id, quantity=Decimal("999"),
    )
    succeeded = repo.append_revision_against_current(cross_anchor_attempt, based_on_revision_id=current_a.id)
    assert succeeded is False

    assert repo.get_current_revision(shipment_a.id).id == current_a.id
    assert repo.get_current_revision(shipment_b.id).id == current_b.id
    assert len(repo.list_revisions(shipment_a.id)) == 1
    assert len(repo.list_revisions(shipment_b.id)) == 1


def test_db_rejects_second_initial_revision_via_orm_bypass(db_session):
    shipment, _ = _create_shipment(db_session)
    frag2 = _make_fragment(db_session)

    db_session.add(
        ShipmentRevisionModel(
            id=uuid.uuid4(), shipment_id=shipment.id, revision_type=ShipmentRevisionType.INITIAL,
            contract_item_id=None, quantity=None, source_fragment_id=frag2.id,
            superseded_by_revision_id=None, created_at=NOW,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_db_rejects_unknown_revision_type_via_orm_bypass(db_session):
    shipment, _ = _create_shipment(db_session)
    frag2 = _make_fragment(db_session)

    db_session.add(
        ShipmentRevisionModel(
            id=uuid.uuid4(), shipment_id=shipment.id, revision_type="WHATEVER",
            contract_item_id=None, quantity=None, source_fragment_id=frag2.id,
            superseded_by_revision_id=None, created_at=NOW,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_two_independent_sessions_stale_correction_is_rejected(tmp_path):
    db_path = tmp_path / "shipment-concurrency.db"
    engine = make_engine(str(db_path))
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)

    with session_factory() as setup_session:
        frag = _make_fragment(setup_session)
        contract = _make_contract(setup_session, frag.id)
        result = create_shipment_fact(
            setup_session, contract_id=contract.id, external_reference="EXP-001", execution_date=EXEC_DATE,
            fields={"quantity": Decimal("10")}, source_fragment_id=frag.id, created_at=NOW,
        )
        setup_session.commit()
        shipment_id = result.shipment.id

    session_a = session_factory()
    session_b = session_factory()
    try:
        current_for_a = ShipmentRepository(session_a).get_current_revision(shipment_id)
        current_for_b = ShipmentRepository(session_b).get_current_revision(shipment_id)
        assert current_for_a.id == current_for_b.id

        frag_a = _make_fragment(session_a)
        result_a = correct_shipment_fact(
            session_a, shipment_id=shipment_id, based_on_revision_id=current_for_a.id,
            fields={"quantity": Decimal("12")}, source_fragment_id=frag_a.id, created_at=NOW,
        )
        session_a.commit()
        assert result_a.revision_written is True

        frag_b = _make_fragment(session_b)
        with pytest.raises(ShipmentFactConflict):
            correct_shipment_fact(
                session_b, shipment_id=shipment_id, based_on_revision_id=current_for_b.id,  # now stale
                fields={"quantity": Decimal("13")}, source_fragment_id=frag_b.id, created_at=NOW,
            )
        session_b.rollback()
    finally:
        session_a.close()
        session_b.close()

    with session_factory() as verify_session:
        history = ShipmentRepository(verify_session).list_revisions(shipment_id)
        current_rows = [r for r in history if r.superseded_by_revision_id is None]
        assert len(current_rows) == 1
        assert current_rows[0].quantity == Decimal("12")
        assert len(history) == 2


# ---------------------------------------------------------------------------
# execute_* — the human/CLI evidence-building wrappers
# ---------------------------------------------------------------------------


def test_execute_create_builds_manual_evidence_and_is_idempotent(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)
    db_session.commit()

    result = execute_create_shipment_fact(
        db_session, contract_id=contract.id, external_reference="EXP-001", execution_date=EXEC_DATE,
        fields={"quantity": Decimal("10")},
    )
    assert result.created is True

    replay = execute_create_shipment_fact(
        db_session, contract_id=contract.id, external_reference="EXP-001", execution_date=EXEC_DATE,
        fields={"quantity": Decimal("10")},
    )
    assert replay.shipment.id == result.shipment.id
    assert replay.created is False
    assert replay.replay is True


def test_execute_create_null_reference_task_survives_serialized_transaction(db_session):
    """Regression for Phase 2D.1-R2 Codex fix round, BLOCKER 1:
    `serialized_write_transaction` rolls back on ANY exception, so a
    naive raise-after-flush inside it would have silently discarded the
    SHIPMENT_IDENTITY_INCOMPLETE Task. Through the REAL execute_* path
    (the only path the CLI uses), the Task must still be there after the
    call raises."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)
    db_session.commit()

    with pytest.raises(ShipmentIdentityIncomplete):
        execute_create_shipment_fact(
            db_session, contract_id=contract.id, external_reference=None, execution_date=EXEC_DATE,
            fields={"quantity": Decimal("10")},
        )

    open_tasks = ExceptionRepository(db_session).list_open()
    matching = [t for t in open_tasks if t.exception_type == ExceptionType.SHIPMENT_IDENTITY_INCOMPLETE]
    assert len(matching) == 1
    assert list_shipments_for_contract(db_session, contract.id) == []


def test_execute_create_identity_conflict_task_survives_serialized_transaction(db_session):
    """Same regression as above, for BLOCKER 2's SHIPMENT_IDENTITY_CONFLICT
    Task, through the real execute_* path."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)
    db_session.commit()

    created = execute_create_shipment_fact(
        db_session, contract_id=contract.id, external_reference="EXP-001", execution_date=EXEC_DATE,
        fields={"quantity": Decimal("10")},
    )
    assert created.created is True

    with pytest.raises(ShipmentFactConflict):
        execute_create_shipment_fact(
            db_session, contract_id=contract.id, external_reference="EXP-001", execution_date=EXEC_DATE,
            fields={"quantity": Decimal("999")},
        )

    open_tasks = ExceptionRepository(db_session).list_open()
    matching = [t for t in open_tasks if t.exception_type == ExceptionType.SHIPMENT_IDENTITY_CONFLICT]
    assert len(matching) == 1
    # The existing Shipment is completely unchanged.
    unchanged = ShipmentRepository(db_session).get(created.shipment.id)
    assert unchanged.quantity == Decimal("10")


def test_execute_supplement_and_correct_round_trip(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)
    db_session.commit()

    created = execute_create_shipment_fact(
        db_session, contract_id=contract.id, external_reference="EXP-001", execution_date=EXEC_DATE, fields={}
    )
    current = ShipmentRepository(db_session).get_current_revision(created.shipment.id)

    supplemented = execute_supplement_shipment_fact(
        db_session, shipment_id=created.shipment.id, based_on_revision_id=current.id,
        fields={"quantity": Decimal("10")},
    )
    assert supplemented.shipment.quantity == Decimal("10")

    current2 = ShipmentRepository(db_session).get_current_revision(created.shipment.id)
    corrected = execute_correct_shipment_fact(
        db_session, shipment_id=created.shipment.id, based_on_revision_id=current2.id,
        fields={"quantity": Decimal("11")},
    )
    assert corrected.shipment.quantity == Decimal("11")
    assert len(get_shipment_history(db_session, created.shipment.id)) == 3
