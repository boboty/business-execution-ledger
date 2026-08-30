"""Phase 2D.1-R3a Slice 2 — ProcurementSalesLink, the canonical
procurement/sales bridge (docs/PHASE2D1-R0-DECISIONS.md section 2.4).

Covers the frozen two-layer identity (relationship business key vs.
confirmed assertion episode), the three never-inferred creation actions
(ADD / CORRECT-INVALIDATE / REESTABLISH), per-episode replay protection
(never per-fragment-alone, never per-pair-alone), the storage-level
one-current-per-business-key invariant (a dedicated SQLite trigger, since
no partial index/CHECK constraint can express a cross-table predicate),
correction lineage invariants (superseded_link_id unique, no forking),
many-to-many cardinality with an ambiguity Task rather than a rejection
or an inferred attribution, and the explicit prohibition on
apportionment across the bridge.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from bel.application.procurement_sales_link import (
    EpisodeHistoryEntry,
    ProcurementSalesLinkFactConflict,
    ProcurementSalesLinkFactError,
    add_procurement_sales_link,
    correct_procurement_sales_link,
    execute_add_procurement_sales_link,
    execute_correct_procurement_sales_link,
    execute_reestablish_procurement_sales_link,
    get_current_procurement_sales_link,
    get_relationship_history,
    list_current_links_for_procurement_contract,
    list_current_links_for_sales_contract,
    record_unconfirmed_procurement_sales_link,
    reestablish_procurement_sales_link,
)
from bel.application.sales_contract_facts import create_sales_contract_fact
from bel.domain.contract import Contract
from bel.domain.evidence import EvidenceDocument, EvidenceFragment, FragmentKind
from bel.domain.exception import ExceptionStatus, ExceptionType
from bel.domain.procurement_sales_link import ConfirmationType, ProcurementSalesLink, ProcurementSalesLinkCorrection
from bel.infrastructure.persistence.database import make_engine, make_session_factory
from bel.infrastructure.persistence.models import Base, ProcurementSalesLinkCorrectionModel, ProcurementSalesLinkModel
from bel.infrastructure.persistence.repositories import (
    ContractRepository,
    EvidenceRepository,
    ExceptionRepository,
    ProcurementSalesLinkRepository,
    SalesContractRepository,
)

NOW = datetime.now(timezone.utc)


def _make_fragment(session, raw_data=None, fragment_kind=FragmentKind.MANUAL_FACT):
    doc = EvidenceDocument(
        id=uuid.uuid4(), file_name="x", sha256=uuid.uuid4().hex + uuid.uuid4().hex, source_type="t", imported_at=NOW
    )
    evidence_repo = EvidenceRepository(session)
    evidence_repo.add_document(doc)
    frag = EvidenceFragment(
        id=uuid.uuid4(),
        evidence_document_id=doc.id,
        fragment_kind=fragment_kind,
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
        buyer="Our Own Entity",
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


def _make_sales_contract(session, fragment_id, sales_contract_no=None, our_entity="Entity A"):
    result = create_sales_contract_fact(
        session,
        our_entity=our_entity,
        sales_contract_no=sales_contract_no or f"SC-{uuid.uuid4().hex[:8]}",
        fields={},
        source_fragment_id=fragment_id,
        created_at=NOW,
    )
    return result.sales_contract


def _setup(db_session):
    """One procurement Contract, one SalesContract, ready to link."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)
    sales_contract = _make_sales_contract(db_session, frag.id)
    db_session.commit()
    return contract, sales_contract


def _add(db_session, contract, sales_contract, fragment=None, confirmation_type=ConfirmationType.AUTO_CONFIRMED):
    frag = fragment or _make_fragment(db_session)
    result = add_procurement_sales_link(
        db_session,
        procurement_contract_id=contract.id,
        sales_contract_id=sales_contract.id,
        source_fragment_id=frag.id,
        confirmation_type=confirmation_type,
        created_at=NOW,
    )
    db_session.commit()
    return result


# ---------------------------------------------------------------------------
# ADD matrix (section 34)
# ---------------------------------------------------------------------------


def test_add_creates_one_current_episode(db_session):
    contract, sales_contract = _setup(db_session)
    result = _add(db_session, contract, sales_contract)

    assert result.created is True
    assert result.link.procurement_contract_id == contract.id
    assert result.link.sales_contract_id == sales_contract.id
    current = get_current_procurement_sales_link(db_session, contract.id, sales_contract.id)
    assert current == result.link


def test_add_exact_replay_same_episode(db_session):
    contract, sales_contract = _setup(db_session)
    first = _add(db_session, contract, sales_contract)
    frag = ProcurementSalesLinkRepository(db_session).get(first.link.id).source_fragment_id

    replay = _add(db_session, contract, sales_contract, fragment=EvidenceRepository(db_session).get_fragment(frag))
    assert replay.created is False
    assert replay.replay is True
    assert replay.link.id == first.link.id


def test_add_corroborating_evidence_no_duplicate_current(db_session):
    contract, sales_contract = _setup(db_session)
    first = _add(db_session, contract, sales_contract)

    corroborating = _add(db_session, contract, sales_contract)  # different (new) fragment
    assert corroborating.created is False
    assert corroborating.corroborating is True
    assert corroborating.link.id == first.link.id
    assert len(ProcurementSalesLinkRepository(db_session).list_episodes(contract.id, sales_contract.id)) == 1


def test_add_retired_pair_rejects_regardless_of_evidence(db_session):
    """Sections 8 and 10: a retired pair (no current, but history exists)
    must go through REESTABLISH, never ADD — even when the caller
    replays the OLD fragment that originally created the now-retired
    episode. ADD never resurrects."""
    contract, sales_contract = _setup(db_session)
    first = _add(db_session, contract, sales_contract)
    old_frag_id = first.link.source_fragment_id
    frag2 = _make_fragment(db_session)
    correct_procurement_sales_link(
        db_session, superseded_link_id=first.link.id, source_fragment_id=frag2.id,
        confirmation_type=ConfirmationType.HUMAN_CONFIRMED, created_at=NOW,
    )
    db_session.commit()

    # Replaying the OLD fragment via ADD must not resurrect.
    with pytest.raises(ProcurementSalesLinkFactError):
        add_procurement_sales_link(
            db_session, procurement_contract_id=contract.id, sales_contract_id=sales_contract.id,
            source_fragment_id=old_frag_id, confirmation_type=ConfirmationType.AUTO_CONFIRMED, created_at=NOW,
        )
    # A genuinely NEW fragment via ADD must ALSO be rejected — the action
    # itself (ADD vs REESTABLISH) is the determinant, not the fragment.
    frag3 = _make_fragment(db_session)
    with pytest.raises(ProcurementSalesLinkFactError):
        add_procurement_sales_link(
            db_session, procurement_contract_id=contract.id, sales_contract_id=sales_contract.id,
            source_fragment_id=frag3.id, confirmation_type=ConfirmationType.AUTO_CONFIRMED, created_at=NOW,
        )
    assert get_current_procurement_sales_link(db_session, contract.id, sales_contract.id) is None


def test_reestablish_creates_new_episode(db_session):
    contract, sales_contract = _setup(db_session)
    first = _add(db_session, contract, sales_contract)
    frag2 = _make_fragment(db_session)
    correct_procurement_sales_link(
        db_session, superseded_link_id=first.link.id, source_fragment_id=frag2.id,
        confirmation_type=ConfirmationType.HUMAN_CONFIRMED, created_at=NOW,
    )
    db_session.commit()

    frag3 = _make_fragment(db_session)
    result = reestablish_procurement_sales_link(
        db_session, procurement_contract_id=contract.id, sales_contract_id=sales_contract.id,
        source_fragment_id=frag3.id, confirmation_type=ConfirmationType.HUMAN_CONFIRMED, created_at=NOW,
    )
    db_session.commit()

    assert result.created is True
    assert result.link.id != first.link.id  # a NEW episode, never the resurrected old row
    current = get_current_procurement_sales_link(db_session, contract.id, sales_contract.id)
    assert current.id == result.link.id
    # The retired episode's own row is completely unmutated.
    old = ProcurementSalesLinkRepository(db_session).get(first.link.id)
    assert old.source_fragment_id == first.link.source_fragment_id
    assert old.confirmation_type == first.link.confirmation_type


def test_reestablish_auto_confirmed_rejected(db_session):
    contract, sales_contract = _setup(db_session)
    first = _add(db_session, contract, sales_contract)
    frag2 = _make_fragment(db_session)
    correct_procurement_sales_link(
        db_session, superseded_link_id=first.link.id, source_fragment_id=frag2.id,
        confirmation_type=ConfirmationType.HUMAN_CONFIRMED, created_at=NOW,
    )
    db_session.commit()

    frag3 = _make_fragment(db_session)
    with pytest.raises(ProcurementSalesLinkFactError):
        reestablish_procurement_sales_link(
            db_session, procurement_contract_id=contract.id, sales_contract_id=sales_contract.id,
            source_fragment_id=frag3.id, confirmation_type=ConfirmationType.AUTO_CONFIRMED, created_at=NOW,
        )
    assert get_current_procurement_sales_link(db_session, contract.id, sales_contract.id) is None


def test_reestablish_using_historical_evidence_rejected(db_session):
    contract, sales_contract = _setup(db_session)
    first = _add(db_session, contract, sales_contract)
    old_frag_id = first.link.source_fragment_id
    frag2 = _make_fragment(db_session)
    correct_procurement_sales_link(
        db_session, superseded_link_id=first.link.id, source_fragment_id=frag2.id,
        confirmation_type=ConfirmationType.HUMAN_CONFIRMED, created_at=NOW,
    )
    db_session.commit()

    with pytest.raises(ProcurementSalesLinkFactError):
        reestablish_procurement_sales_link(
            db_session, procurement_contract_id=contract.id, sales_contract_id=sales_contract.id,
            source_fragment_id=old_frag_id, confirmation_type=ConfirmationType.HUMAN_CONFIRMED, created_at=NOW,
        )
    # Even the correction's OWN fragment (also historical) is rejected.
    with pytest.raises(ProcurementSalesLinkFactError):
        reestablish_procurement_sales_link(
            db_session, procurement_contract_id=contract.id, sales_contract_id=sales_contract.id,
            source_fragment_id=frag2.id, confirmation_type=ConfirmationType.HUMAN_CONFIRMED, created_at=NOW,
        )
    assert get_current_procurement_sales_link(db_session, contract.id, sales_contract.id) is None


def test_reestablish_no_history_rejects_use_add_instead(db_session):
    contract, sales_contract = _setup(db_session)
    frag = _make_fragment(db_session)
    with pytest.raises(ProcurementSalesLinkFactError):
        reestablish_procurement_sales_link(
            db_session, procurement_contract_id=contract.id, sales_contract_id=sales_contract.id,
            source_fragment_id=frag.id, confirmation_type=ConfirmationType.HUMAN_CONFIRMED, created_at=NOW,
        )


def test_add_rejects_missing_endpoints(db_session):
    contract, sales_contract = _setup(db_session)
    frag = _make_fragment(db_session)
    with pytest.raises(ProcurementSalesLinkFactError):
        add_procurement_sales_link(
            db_session, procurement_contract_id=None, sales_contract_id=sales_contract.id,
            source_fragment_id=frag.id, confirmation_type=ConfirmationType.AUTO_CONFIRMED, created_at=NOW,
        )
    with pytest.raises(ProcurementSalesLinkFactError):
        add_procurement_sales_link(
            db_session, procurement_contract_id=contract.id, sales_contract_id=None,
            source_fragment_id=frag.id, confirmation_type=ConfirmationType.AUTO_CONFIRMED, created_at=NOW,
        )


def test_add_rejects_unknown_endpoints_and_confirmation_type(db_session):
    contract, sales_contract = _setup(db_session)
    frag = _make_fragment(db_session)
    with pytest.raises(ProcurementSalesLinkFactError):
        add_procurement_sales_link(
            db_session, procurement_contract_id=uuid.uuid4(), sales_contract_id=sales_contract.id,
            source_fragment_id=frag.id, confirmation_type=ConfirmationType.AUTO_CONFIRMED, created_at=NOW,
        )
    with pytest.raises(ProcurementSalesLinkFactError):
        add_procurement_sales_link(
            db_session, procurement_contract_id=contract.id, sales_contract_id=uuid.uuid4(),
            source_fragment_id=frag.id, confirmation_type=ConfirmationType.AUTO_CONFIRMED, created_at=NOW,
        )
    with pytest.raises(ProcurementSalesLinkFactError):
        add_procurement_sales_link(
            db_session, procurement_contract_id=contract.id, sales_contract_id=sales_contract.id,
            source_fragment_id=uuid.uuid4(), confirmation_type=ConfirmationType.AUTO_CONFIRMED, created_at=NOW,
        )
    with pytest.raises(ProcurementSalesLinkFactError):
        add_procurement_sales_link(
            db_session, procurement_contract_id=contract.id, sales_contract_id=sales_contract.id,
            source_fragment_id=frag.id, confirmation_type="MAYBE_CONFIRMED", created_at=NOW,
        )


# ---------------------------------------------------------------------------
# Correction matrix (section 35)
# ---------------------------------------------------------------------------


def test_correction_pure_invalidate(db_session):
    contract, sales_contract = _setup(db_session)
    first = _add(db_session, contract, sales_contract)
    frag2 = _make_fragment(db_session)

    result = correct_procurement_sales_link(
        db_session, superseded_link_id=first.link.id, source_fragment_id=frag2.id,
        confirmation_type=ConfirmationType.HUMAN_CONFIRMED, created_at=NOW,
    )
    db_session.commit()

    assert result.created is True
    assert result.replacement_link is None
    assert get_current_procurement_sales_link(db_session, contract.id, sales_contract.id) is None


def test_correction_leaves_old_link_row_immutable(db_session):
    contract, sales_contract = _setup(db_session)
    first = _add(db_session, contract, sales_contract)
    original = ProcurementSalesLinkRepository(db_session).get(first.link.id)
    frag2 = _make_fragment(db_session)

    correct_procurement_sales_link(
        db_session, superseded_link_id=first.link.id, source_fragment_id=frag2.id,
        confirmation_type=ConfirmationType.HUMAN_CONFIRMED, created_at=NOW,
    )
    db_session.commit()

    unchanged = ProcurementSalesLinkRepository(db_session).get(first.link.id)
    assert unchanged == original  # every field, byte for byte, unchanged


def test_invalidated_link_not_returned_as_current(db_session):
    contract, sales_contract = _setup(db_session)
    first = _add(db_session, contract, sales_contract)
    frag2 = _make_fragment(db_session)
    correct_procurement_sales_link(
        db_session, superseded_link_id=first.link.id, source_fragment_id=frag2.id,
        confirmation_type=ConfirmationType.HUMAN_CONFIRMED, created_at=NOW,
    )
    db_session.commit()

    assert get_current_procurement_sales_link(db_session, contract.id, sales_contract.id) is None
    assert list_current_links_for_procurement_contract(db_session, contract.id) == []
    assert list_current_links_for_sales_contract(db_session, sales_contract.id) == []


def test_correction_requires_evidence(db_session):
    contract, sales_contract = _setup(db_session)
    first = _add(db_session, contract, sales_contract)
    with pytest.raises(ProcurementSalesLinkFactError):
        correct_procurement_sales_link(
            db_session, superseded_link_id=first.link.id, source_fragment_id=uuid.uuid4(),
            confirmation_type=ConfirmationType.HUMAN_CONFIRMED, created_at=NOW,
        )


def test_correction_requires_human_confirmed(db_session):
    contract, sales_contract = _setup(db_session)
    first = _add(db_session, contract, sales_contract)
    frag2 = _make_fragment(db_session)
    with pytest.raises(ProcurementSalesLinkFactError):
        correct_procurement_sales_link(
            db_session, superseded_link_id=first.link.id, source_fragment_id=frag2.id,
            confirmation_type=ConfirmationType.AUTO_CONFIRMED, created_at=NOW,
        )
    assert get_current_procurement_sales_link(db_session, contract.id, sales_contract.id) == first.link


def test_correction_target_already_retired_rejected(db_session):
    contract, sales_contract = _setup(db_session)
    first = _add(db_session, contract, sales_contract)
    frag2 = _make_fragment(db_session)
    correct_procurement_sales_link(
        db_session, superseded_link_id=first.link.id, source_fragment_id=frag2.id,
        confirmation_type=ConfirmationType.HUMAN_CONFIRMED, created_at=NOW,
    )
    db_session.commit()

    frag3 = _make_fragment(db_session)
    with pytest.raises(ProcurementSalesLinkFactConflict):
        correct_procurement_sales_link(
            db_session, superseded_link_id=first.link.id, source_fragment_id=frag3.id,
            confirmation_type=ConfirmationType.HUMAN_CONFIRMED, created_at=NOW,
        )


def test_correction_exact_replay_idempotent(db_session):
    contract, sales_contract = _setup(db_session)
    first = _add(db_session, contract, sales_contract)
    frag2 = _make_fragment(db_session)
    original = correct_procurement_sales_link(
        db_session, superseded_link_id=first.link.id, source_fragment_id=frag2.id,
        confirmation_type=ConfirmationType.HUMAN_CONFIRMED, created_at=NOW,
    )
    db_session.commit()

    replay = correct_procurement_sales_link(
        db_session, superseded_link_id=first.link.id, source_fragment_id=frag2.id,
        confirmation_type=ConfirmationType.HUMAN_CONFIRMED, created_at=NOW,
    )
    assert replay.replay is True
    assert replay.correction.id == original.correction.id


def test_correction_conflicting_second_correction_rejected(db_session):
    contract, sales_contract = _setup(db_session)
    first = _add(db_session, contract, sales_contract)
    _, other_sales_contract = contract, _make_sales_contract(db_session, first.link.source_fragment_id, our_entity="Entity Z")
    db_session.commit()
    frag2 = _make_fragment(db_session)
    original = correct_procurement_sales_link(
        db_session, superseded_link_id=first.link.id, source_fragment_id=frag2.id,
        confirmation_type=ConfirmationType.HUMAN_CONFIRMED, created_at=NOW,
    )
    db_session.commit()

    frag3 = _make_fragment(db_session)
    with pytest.raises(ProcurementSalesLinkFactConflict):
        correct_procurement_sales_link(
            db_session, superseded_link_id=first.link.id, source_fragment_id=frag3.id,
            confirmation_type=ConfirmationType.HUMAN_CONFIRMED, created_at=NOW,
            replacement_procurement_contract_id=contract.id, replacement_sales_contract_id=other_sales_contract.id,
        )
    db_session.commit()

    matching = [
        t for t in ExceptionRepository(db_session).list_open()
        if t.exception_type == ExceptionType.PROCUREMENT_SALES_LINK_CORRECTION_CONFLICT
    ]
    assert len(matching) == 1
    assert matching[0].detail["superseded_link_id"] == str(first.link.id)
    assert matching[0].detail["conflicting_source_fragment_id"] == str(frag3.id)
    # Lineage unaltered: still exactly the original correction, no new one.
    assert ProcurementSalesLinkRepository(db_session).get_correction_for_superseded(first.link.id).id == original.correction.id

    # Replaying the SAME conflicting submission must not raise a SECOND Task.
    with pytest.raises(ProcurementSalesLinkFactConflict):
        correct_procurement_sales_link(
            db_session, superseded_link_id=first.link.id, source_fragment_id=frag3.id,
            confirmation_type=ConfirmationType.HUMAN_CONFIRMED, created_at=NOW,
            replacement_procurement_contract_id=contract.id, replacement_sales_contract_id=other_sales_contract.id,
        )
    db_session.commit()
    still_matching = [
        t for t in ExceptionRepository(db_session).list_open()
        if t.exception_type == ExceptionType.PROCUREMENT_SALES_LINK_CORRECTION_CONFLICT
    ]
    assert len(still_matching) == 1  # idempotent


def test_db_rejects_second_correction_via_orm_bypass(db_session):
    contract, sales_contract = _setup(db_session)
    first = _add(db_session, contract, sales_contract)
    frag2 = _make_fragment(db_session)
    correct_procurement_sales_link(
        db_session, superseded_link_id=first.link.id, source_fragment_id=frag2.id,
        confirmation_type=ConfirmationType.HUMAN_CONFIRMED, created_at=NOW,
    )
    db_session.commit()

    db_session.add(
        ProcurementSalesLinkCorrectionModel(
            id=uuid.uuid4(), superseded_link_id=first.link.id, replacement_link_id=None,
            source_fragment_id=frag2.id, confirmation_type=ConfirmationType.HUMAN_CONFIRMED, created_at=NOW,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_db_rejects_auto_confirmed_correction_via_orm_bypass(db_session):
    contract, sales_contract = _setup(db_session)
    first = _add(db_session, contract, sales_contract)
    frag2 = _make_fragment(db_session)

    db_session.add(
        ProcurementSalesLinkCorrectionModel(
            id=uuid.uuid4(), superseded_link_id=first.link.id, replacement_link_id=None,
            source_fragment_id=frag2.id, confirmation_type=ConfirmationType.AUTO_CONFIRMED, created_at=NOW,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_db_rejects_second_current_via_trigger_orm_bypass(db_session):
    """The storage-level backstop (section 17): even a raw ORM insert
    that bypasses ProcurementSalesLinkRepository.insert_episode_if_no_current
    entirely must be rejected by trg_procurement_sales_links_one_current."""
    contract, sales_contract = _setup(db_session)
    first = _add(db_session, contract, sales_contract)
    frag2 = _make_fragment(db_session)

    db_session.add(
        ProcurementSalesLinkModel(
            id=uuid.uuid4(), procurement_contract_id=contract.id, sales_contract_id=sales_contract.id,
            source_fragment_id=frag2.id, confirmation_type=ConfirmationType.AUTO_CONFIRMED, created_at=NOW,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()
    assert get_current_procurement_sales_link(db_session, contract.id, sales_contract.id) == first.link


def test_db_rejects_unknown_confirmation_type_via_orm_bypass(db_session):
    contract, sales_contract = _setup(db_session)
    frag = _make_fragment(db_session)
    db_session.add(
        ProcurementSalesLinkModel(
            id=uuid.uuid4(), procurement_contract_id=contract.id, sales_contract_id=sales_contract.id,
            source_fragment_id=frag.id, confirmation_type="WHATEVER", created_at=NOW,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


# Replacement


def test_correction_replacement_creates_new_episode_atomically(db_session):
    contract, sales_contract_x = _setup(db_session)
    first = _add(db_session, contract, sales_contract_x)
    sales_contract_y = _make_sales_contract(db_session, first.link.source_fragment_id)
    db_session.commit()

    frag2 = _make_fragment(db_session)
    result = correct_procurement_sales_link(
        db_session, superseded_link_id=first.link.id, source_fragment_id=frag2.id,
        confirmation_type=ConfirmationType.HUMAN_CONFIRMED, created_at=NOW,
        replacement_procurement_contract_id=contract.id, replacement_sales_contract_id=sales_contract_y.id,
    )
    db_session.commit()

    assert result.replacement_link is not None
    assert result.replacement_link.sales_contract_id == sales_contract_y.id
    assert get_current_procurement_sales_link(db_session, contract.id, sales_contract_x.id) is None
    assert get_current_procurement_sales_link(db_session, contract.id, sales_contract_y.id).id == result.replacement_link.id


def test_correction_replacement_reuses_existing_current_no_duplicate(db_session):
    contract, sales_contract_x = _setup(db_session)
    first = _add(db_session, contract, sales_contract_x)
    sales_contract_y = _make_sales_contract(db_session, first.link.source_fragment_id)
    db_session.commit()
    # Y already has its OWN current episode from a separate ADD.
    y_link = _add(db_session, contract, sales_contract_y)

    frag2 = _make_fragment(db_session)
    result = correct_procurement_sales_link(
        db_session, superseded_link_id=first.link.id, source_fragment_id=frag2.id,
        confirmation_type=ConfirmationType.HUMAN_CONFIRMED, created_at=NOW,
        replacement_procurement_contract_id=contract.id, replacement_sales_contract_id=sales_contract_y.id,
    )
    db_session.commit()

    assert result.replacement_link.id == y_link.link.id  # referenced, not duplicated
    assert len(ProcurementSalesLinkRepository(db_session).list_episodes(contract.id, sales_contract_y.id)) == 1


def test_correction_replacement_never_dual_current(db_session):
    contract, sales_contract_x = _setup(db_session)
    first = _add(db_session, contract, sales_contract_x)
    sales_contract_y = _make_sales_contract(db_session, first.link.source_fragment_id)
    db_session.commit()

    frag2 = _make_fragment(db_session)
    correct_procurement_sales_link(
        db_session, superseded_link_id=first.link.id, source_fragment_id=frag2.id,
        confirmation_type=ConfirmationType.HUMAN_CONFIRMED, created_at=NOW,
        replacement_procurement_contract_id=contract.id, replacement_sales_contract_id=sales_contract_y.id,
    )
    db_session.commit()

    current_for_procurement = list_current_links_for_procurement_contract(db_session, contract.id)
    assert [link.sales_contract_id for link in current_for_procurement] == [sales_contract_y.id]  # X gone, only Y


def test_correction_race_lost_rolls_back_orphan_replacement_episode(db_session):
    """Section 13/23: if the correction insert itself loses a race
    (someone else already corrected the SAME episode), any replacement
    episode this attempt just created must be rolled back — never left
    as an orphaned current episode with no correction pointing at it."""
    contract, sales_contract_x = _setup(db_session)
    first = _add(db_session, contract, sales_contract_x)
    sales_contract_y = _make_sales_contract(db_session, first.link.source_fragment_id)
    db_session.commit()

    # Simulate a concurrent winner: another correction already retires
    # `first.link.id` before our attempt's own correction insert runs.
    frag_winner = _make_fragment(db_session)
    repo = ProcurementSalesLinkRepository(db_session)
    winner = ProcurementSalesLinkCorrection(
        id=uuid.uuid4(), superseded_link_id=first.link.id, replacement_link_id=None,
        source_fragment_id=frag_winner.id, confirmation_type=ConfirmationType.HUMAN_CONFIRMED, created_at=NOW,
    )
    assert repo.add_correction_if_uncorrected(winner)
    db_session.commit()

    frag_loser = _make_fragment(db_session)
    with pytest.raises(ProcurementSalesLinkFactConflict):
        correct_procurement_sales_link(
            db_session, superseded_link_id=first.link.id, source_fragment_id=frag_loser.id,
            confirmation_type=ConfirmationType.HUMAN_CONFIRMED, created_at=NOW,
            replacement_procurement_contract_id=contract.id, replacement_sales_contract_id=sales_contract_y.id,
        )
    db_session.rollback()

    # Y never became a real current episode from the losing attempt.
    assert get_current_procurement_sales_link(db_session, contract.id, sales_contract_y.id) is None
    assert ProcurementSalesLinkRepository(db_session).list_episodes(contract.id, sales_contract_y.id) == []


# ---------------------------------------------------------------------------
# M:N matrix (section 36)
# ---------------------------------------------------------------------------


def test_many_procurement_contracts_to_one_sales_contract_valid(db_session):
    frag = _make_fragment(db_session)
    contract_a = _make_contract(db_session, frag.id)
    contract_b = _make_contract(db_session, frag.id)
    sales_contract = _make_sales_contract(db_session, frag.id)
    db_session.commit()

    result_a = _add(db_session, contract_a, sales_contract)
    result_b = _add(db_session, contract_b, sales_contract)

    assert result_a.created is True
    assert result_b.created is True
    assert {l.procurement_contract_id for l in list_current_links_for_sales_contract(db_session, sales_contract.id)} == {
        contract_a.id, contract_b.id
    }


def test_one_procurement_contract_to_many_sales_contracts_structurally_valid(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)
    sales_x = _make_sales_contract(db_session, frag.id)
    sales_y = _make_sales_contract(db_session, frag.id)
    db_session.commit()

    result_x = _add(db_session, contract, sales_x)
    result_y = _add(db_session, contract, sales_y)

    assert result_x.created is True
    assert result_y.created is True
    assert {l.sales_contract_id for l in list_current_links_for_procurement_contract(db_session, contract.id)} == {
        sales_x.id, sales_y.id
    }


def test_second_sales_scope_does_not_automatically_retire_first(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)
    sales_x = _make_sales_contract(db_session, frag.id)
    sales_y = _make_sales_contract(db_session, frag.id)
    db_session.commit()

    result_x = _add(db_session, contract, sales_x)
    _add(db_session, contract, sales_y)

    still_current = get_current_procurement_sales_link(db_session, contract.id, sales_x.id)
    assert still_current is not None
    assert still_current.id == result_x.link.id


def test_multiple_sales_scopes_raises_ambiguity_task_idempotently(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)
    sales_x = _make_sales_contract(db_session, frag.id)
    sales_y = _make_sales_contract(db_session, frag.id)
    sales_z = _make_sales_contract(db_session, frag.id)
    db_session.commit()

    _add(db_session, contract, sales_x)
    matching_after_one = [
        t for t in ExceptionRepository(db_session).list_open()
        if t.exception_type == ExceptionType.PROCUREMENT_SALES_LINK_MULTIPLE_SCOPES
    ]
    assert matching_after_one == []  # a single scope is never ambiguous

    _add(db_session, contract, sales_y)
    matching = [
        t for t in ExceptionRepository(db_session).list_open()
        if t.exception_type == ExceptionType.PROCUREMENT_SALES_LINK_MULTIPLE_SCOPES
    ]
    assert len(matching) == 1
    assert matching[0].detail["procurement_contract_id"] == str(contract.id)
    assert set(matching[0].detail["sales_contract_ids"]) == {str(sales_x.id), str(sales_y.id)}
    # Never guesses/chooses an attribution — no amount/quantity anywhere in the payload.
    assert set(matching[0].detail.keys()) == {"procurement_contract_id", "sales_contract_ids"}

    # A THIRD scope must not pile up a second Task (idempotent per procurement_contract_id).
    _add(db_session, contract, sales_z)
    still_matching = [
        t for t in ExceptionRepository(db_session).list_open()
        if t.exception_type == ExceptionType.PROCUREMENT_SALES_LINK_MULTIPLE_SCOPES
    ]
    assert len(still_matching) == 1


def test_no_apportionment_fields_exist_on_link_or_correction():
    """Structural guard (section 24): the link and correction dataclasses
    have no amount/quantity/ratio field at all — there is no code path
    through which one could ever be populated."""
    import dataclasses

    link_fields = {f.name for f in dataclasses.fields(ProcurementSalesLink)}
    correction_fields = {f.name for f in dataclasses.fields(ProcurementSalesLinkCorrection)}
    forbidden = {
        "amount", "quantity", "allocation_ratio", "allocated_amount", "allocated_quantity",
        "contract_item_id", "invoice_id", "payment_id", "status", "is_current", "superseded_by_link_id",
    }
    assert forbidden.isdisjoint(link_fields)
    assert forbidden.isdisjoint(correction_fields)
    assert link_fields == {
        "id", "procurement_contract_id", "sales_contract_id", "source_fragment_id", "confirmation_type", "created_at"
    }


# ---------------------------------------------------------------------------
# Unconfirmed relationship (section 20)
# ---------------------------------------------------------------------------


def test_unconfirmed_relationship_never_creates_link(db_session):
    contract, sales_contract = _setup(db_session)
    frag = _make_fragment(db_session)

    created = record_unconfirmed_procurement_sales_link(
        db_session, procurement_contract_id=contract.id, sales_contract_id=sales_contract.id,
        source_fragment_id=frag.id, created_at=NOW,
    )
    db_session.commit()

    assert created is True
    assert get_current_procurement_sales_link(db_session, contract.id, sales_contract.id) is None
    assert list_current_links_for_procurement_contract(db_session, contract.id) == []
    matching = [
        t for t in ExceptionRepository(db_session).list_open()
        if t.exception_type == ExceptionType.PROCUREMENT_SALES_LINK_UNCONFIRMED
    ]
    assert len(matching) == 1


def test_unconfirmed_relationship_exact_replay_no_duplicate_task(db_session):
    contract, sales_contract = _setup(db_session)
    frag = _make_fragment(db_session)

    first = record_unconfirmed_procurement_sales_link(
        db_session, procurement_contract_id=contract.id, sales_contract_id=sales_contract.id,
        source_fragment_id=frag.id, created_at=NOW,
    )
    db_session.commit()
    replay = record_unconfirmed_procurement_sales_link(
        db_session, procurement_contract_id=contract.id, sales_contract_id=sales_contract.id,
        source_fragment_id=frag.id, created_at=NOW,
    )
    db_session.commit()

    assert first is True
    assert replay is False
    matching = [
        t for t in ExceptionRepository(db_session).list_open()
        if t.exception_type == ExceptionType.PROCUREMENT_SALES_LINK_UNCONFIRMED
    ]
    assert len(matching) == 1


# ---------------------------------------------------------------------------
# Concurrency (section 37)
# ---------------------------------------------------------------------------


def _two_sessions_setup(tmp_path):
    db_path = tmp_path / "psl-concurrency.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)
    with session_factory() as setup_session:
        frag = _make_fragment(setup_session)
        contract = _make_contract(setup_session, frag.id)
        sales_contract = _make_sales_contract(setup_session, frag.id)
        setup_session.commit()
        return session_factory, contract.id, sales_contract.id


def test_concurrent_add_same_pair_final_one_current(tmp_path):
    session_factory, contract_id, sales_contract_id = _two_sessions_setup(tmp_path)
    session_a = session_factory()
    session_b = session_factory()
    try:
        frag_a = _make_fragment(session_a)
        session_a.commit()
        frag_b = _make_fragment(session_b)
        session_b.commit()

        result_a = add_procurement_sales_link(
            session_a, procurement_contract_id=contract_id, sales_contract_id=sales_contract_id,
            source_fragment_id=frag_a.id, confirmation_type=ConfirmationType.AUTO_CONFIRMED, created_at=NOW,
        )
        session_a.commit()
        assert result_a.created is True

        result_b = add_procurement_sales_link(
            session_b, procurement_contract_id=contract_id, sales_contract_id=sales_contract_id,
            source_fragment_id=frag_b.id, confirmation_type=ConfirmationType.AUTO_CONFIRMED, created_at=NOW,
        )
        session_b.commit()
        assert result_b.created is False
        assert result_b.corroborating is True
        assert result_b.link.id == result_a.link.id
    finally:
        session_a.close()
        session_b.close()

    with session_factory() as verify:
        episodes = ProcurementSalesLinkRepository(verify).list_episodes(contract_id, sales_contract_id)
        current = [e for e in episodes if ProcurementSalesLinkRepository(verify).is_current(e.id)]
        assert len(current) == 1


def test_concurrent_reestablish_same_retired_pair_final_one_current(tmp_path):
    session_factory, contract_id, sales_contract_id = _two_sessions_setup(tmp_path)
    with session_factory() as setup_session:
        frag0 = _make_fragment(setup_session)
        first = add_procurement_sales_link(
            setup_session, procurement_contract_id=contract_id, sales_contract_id=sales_contract_id,
            source_fragment_id=frag0.id, confirmation_type=ConfirmationType.AUTO_CONFIRMED, created_at=NOW,
        )
        frag1 = _make_fragment(setup_session)
        correct_procurement_sales_link(
            setup_session, superseded_link_id=first.link.id, source_fragment_id=frag1.id,
            confirmation_type=ConfirmationType.HUMAN_CONFIRMED, created_at=NOW,
        )
        setup_session.commit()

    session_a = session_factory()
    session_b = session_factory()
    try:
        frag_a = _make_fragment(session_a)
        session_a.commit()
        frag_b = _make_fragment(session_b)
        session_b.commit()

        result_a = reestablish_procurement_sales_link(
            session_a, procurement_contract_id=contract_id, sales_contract_id=sales_contract_id,
            source_fragment_id=frag_a.id, confirmation_type=ConfirmationType.HUMAN_CONFIRMED, created_at=NOW,
        )
        session_a.commit()
        assert result_a.created is True

        # Session B's rejection may surface either as the early
        # "already has a current episode" precondition check (if B's read
        # happens after A's commit is visible — the deterministic outcome
        # here, since A already committed above) or as the atomic-insert
        # race path (ProcurementSalesLinkFactConflict) under true
        # concurrent timing; both are ProcurementSalesLinkFactError and
        # both leave exactly one current episode, which is what this test
        # actually verifies.
        with pytest.raises(ProcurementSalesLinkFactError):
            reestablish_procurement_sales_link(
                session_b, procurement_contract_id=contract_id, sales_contract_id=sales_contract_id,
                source_fragment_id=frag_b.id, confirmation_type=ConfirmationType.HUMAN_CONFIRMED, created_at=NOW,
            )
        session_b.rollback()
    finally:
        session_a.close()
        session_b.close()

    with session_factory() as verify:
        episodes = ProcurementSalesLinkRepository(verify).list_episodes(contract_id, sales_contract_id)
        current = [e for e in episodes if ProcurementSalesLinkRepository(verify).is_current(e.id)]
        assert len(current) == 1
        assert current[0].id == result_a.link.id


def test_concurrent_correction_same_link_final_exactly_one_correction(tmp_path):
    session_factory, contract_id, sales_contract_id = _two_sessions_setup(tmp_path)
    with session_factory() as setup_session:
        frag0 = _make_fragment(setup_session)
        first = add_procurement_sales_link(
            setup_session, procurement_contract_id=contract_id, sales_contract_id=sales_contract_id,
            source_fragment_id=frag0.id, confirmation_type=ConfirmationType.AUTO_CONFIRMED, created_at=NOW,
        )
        setup_session.commit()
        link_id = first.link.id

    session_a = session_factory()
    session_b = session_factory()
    try:
        frag_a = _make_fragment(session_a)
        session_a.commit()
        frag_b = _make_fragment(session_b)
        session_b.commit()

        result_a = correct_procurement_sales_link(
            session_a, superseded_link_id=link_id, source_fragment_id=frag_a.id,
            confirmation_type=ConfirmationType.HUMAN_CONFIRMED, created_at=NOW,
        )
        session_a.commit()
        assert result_a.created is True

        with pytest.raises(ProcurementSalesLinkFactConflict):
            correct_procurement_sales_link(
                session_b, superseded_link_id=link_id, source_fragment_id=frag_b.id,
                confirmation_type=ConfirmationType.HUMAN_CONFIRMED, created_at=NOW,
            )
        session_b.rollback()
    finally:
        session_a.close()
        session_b.close()

    with session_factory() as verify:
        repo = ProcurementSalesLinkRepository(verify)
        assert repo.get_correction_for_superseded(link_id).id == result_a.correction.id


def test_concurrent_competing_replacements_one_wins_no_lineage_fork(tmp_path):
    session_factory, contract_id, sales_contract_id = _two_sessions_setup(tmp_path)
    with session_factory() as setup_session:
        frag0 = _make_fragment(setup_session)
        first = add_procurement_sales_link(
            setup_session, procurement_contract_id=contract_id, sales_contract_id=sales_contract_id,
            source_fragment_id=frag0.id, confirmation_type=ConfirmationType.AUTO_CONFIRMED, created_at=NOW,
        )
        frag1 = _make_fragment(setup_session)
        sales_y = _make_sales_contract(setup_session, frag1.id)
        sales_z = _make_sales_contract(setup_session, frag1.id)
        setup_session.commit()
        link_id, y_id, z_id = first.link.id, sales_y.id, sales_z.id

    session_a = session_factory()
    session_b = session_factory()
    try:
        frag_a = _make_fragment(session_a)
        session_a.commit()
        frag_b = _make_fragment(session_b)
        session_b.commit()

        result_a = correct_procurement_sales_link(
            session_a, superseded_link_id=link_id, source_fragment_id=frag_a.id,
            confirmation_type=ConfirmationType.HUMAN_CONFIRMED, created_at=NOW,
            replacement_procurement_contract_id=contract_id, replacement_sales_contract_id=y_id,
        )
        session_a.commit()
        assert result_a.created is True

        with pytest.raises(ProcurementSalesLinkFactConflict):
            correct_procurement_sales_link(
                session_b, superseded_link_id=link_id, source_fragment_id=frag_b.id,
                confirmation_type=ConfirmationType.HUMAN_CONFIRMED, created_at=NOW,
                replacement_procurement_contract_id=contract_id, replacement_sales_contract_id=z_id,
            )
        session_b.rollback()
    finally:
        session_a.close()
        session_b.close()

    with session_factory() as verify:
        repo = ProcurementSalesLinkRepository(verify)
        winning_correction = repo.get_correction_for_superseded(link_id)
        assert winning_correction.id == result_a.correction.id
        assert repo.get_current_link(contract_id, y_id) is not None  # A's replacement won
        assert repo.get_current_link(contract_id, z_id) is None  # B's replacement never became real
        assert repo.list_episodes(contract_id, z_id) == []  # no orphan episode for Z


# ---------------------------------------------------------------------------
# Provenance / fragment-replay adversarial tests (section 38)
# ---------------------------------------------------------------------------


def test_fragment_reused_across_different_business_keys_never_misresolves(db_session):
    frag = _make_fragment(db_session)
    contract_a = _make_contract(db_session, frag.id)
    contract_b = _make_contract(db_session, frag.id)
    sales_contract = _make_sales_contract(db_session, frag.id)
    db_session.commit()

    shared_frag = _make_fragment(db_session)
    result_a = add_procurement_sales_link(
        db_session, procurement_contract_id=contract_a.id, sales_contract_id=sales_contract.id,
        source_fragment_id=shared_frag.id, confirmation_type=ConfirmationType.AUTO_CONFIRMED, created_at=NOW,
    )
    db_session.commit()
    result_b = add_procurement_sales_link(
        db_session, procurement_contract_id=contract_b.id, sales_contract_id=sales_contract.id,
        source_fragment_id=shared_frag.id, confirmation_type=ConfirmationType.AUTO_CONFIRMED, created_at=NOW,
    )
    db_session.commit()

    assert result_a.created is True
    assert result_b.created is True
    assert result_a.link.id != result_b.link.id
    assert get_current_procurement_sales_link(db_session, contract_a.id, sales_contract.id).id == result_a.link.id
    assert get_current_procurement_sales_link(db_session, contract_b.id, sales_contract.id).id == result_b.link.id


def test_same_fragment_same_pair_different_action_intent_not_blind_replay(db_session):
    """ADD's replay detection is scoped to ADD; correction's is scoped
    to superseded_link_id. Reusing the SAME fragment id as Evidence for a
    LATER, different action (a correction) against the link that fragment
    originally created must be evaluated on its own terms, not treated as
    an automatic replay of the ADD."""
    contract, sales_contract = _setup(db_session)
    add_frag = _make_fragment(db_session)
    first = _add(db_session, contract, sales_contract, fragment=add_frag)

    result = correct_procurement_sales_link(
        db_session, superseded_link_id=first.link.id, source_fragment_id=add_frag.id,
        confirmation_type=ConfirmationType.HUMAN_CONFIRMED, created_at=NOW,
    )
    db_session.commit()

    assert result.created is True  # a genuine new correction, not mistaken for anything about the ADD
    assert get_current_procurement_sales_link(db_session, contract.id, sales_contract.id) is None


def test_historical_fragment_reused_after_retirement_no_resurrection_via_either_action(db_session):
    contract, sales_contract = _setup(db_session)
    first = _add(db_session, contract, sales_contract)
    old_frag_id = first.link.source_fragment_id
    frag2 = _make_fragment(db_session)
    correct_procurement_sales_link(
        db_session, superseded_link_id=first.link.id, source_fragment_id=frag2.id,
        confirmation_type=ConfirmationType.HUMAN_CONFIRMED, created_at=NOW,
    )
    db_session.commit()

    with pytest.raises(ProcurementSalesLinkFactError):
        add_procurement_sales_link(
            db_session, procurement_contract_id=contract.id, sales_contract_id=sales_contract.id,
            source_fragment_id=old_frag_id, confirmation_type=ConfirmationType.AUTO_CONFIRMED, created_at=NOW,
        )
    with pytest.raises(ProcurementSalesLinkFactError):
        reestablish_procurement_sales_link(
            db_session, procurement_contract_id=contract.id, sales_contract_id=sales_contract.id,
            source_fragment_id=old_frag_id, confirmation_type=ConfirmationType.HUMAN_CONFIRMED, created_at=NOW,
        )
    assert get_current_procurement_sales_link(db_session, contract.id, sales_contract.id) is None


def test_correction_fragment_reused_against_another_link_no_wrong_replay(db_session):
    frag0 = _make_fragment(db_session)
    contract = _make_contract(db_session, frag0.id)
    sales_x = _make_sales_contract(db_session, frag0.id)
    sales_y = _make_sales_contract(db_session, frag0.id)
    db_session.commit()

    link_x = _add(db_session, contract, sales_x)
    link_y = _add(db_session, contract, sales_y)

    shared_correction_frag = _make_fragment(db_session)
    correct_procurement_sales_link(
        db_session, superseded_link_id=link_x.link.id, source_fragment_id=shared_correction_frag.id,
        confirmation_type=ConfirmationType.HUMAN_CONFIRMED, created_at=NOW,
    )
    db_session.commit()

    # Reusing the SAME fragment against a DIFFERENT link's correction must
    # be evaluated fresh — never assumed to be a replay of link_x's correction.
    result_y = correct_procurement_sales_link(
        db_session, superseded_link_id=link_y.link.id, source_fragment_id=shared_correction_frag.id,
        confirmation_type=ConfirmationType.HUMAN_CONFIRMED, created_at=NOW,
    )
    db_session.commit()

    assert result_y.created is True
    assert get_current_procurement_sales_link(db_session, contract.id, sales_x.id) is None
    assert get_current_procurement_sales_link(db_session, contract.id, sales_y.id) is None


# ---------------------------------------------------------------------------
# Read model determinism (section 31)
# ---------------------------------------------------------------------------


def test_read_model_deterministic_across_repeated_calls(db_session):
    contract, sales_contract = _setup(db_session)
    first = _add(db_session, contract, sales_contract)
    frag2 = _make_fragment(db_session)
    correct_procurement_sales_link(
        db_session, superseded_link_id=first.link.id, source_fragment_id=frag2.id,
        confirmation_type=ConfirmationType.HUMAN_CONFIRMED, created_at=NOW,
    )
    db_session.commit()

    history_once = get_relationship_history(db_session, contract.id, sales_contract.id)
    history_again = get_relationship_history(db_session, contract.id, sales_contract.id)
    assert [(e.episode.id, e.current) for e in history_once] == [(e.episode.id, e.current) for e in history_again]
    assert len(history_once) == 1
    assert history_once[0].current is False
    assert isinstance(history_once[0], EpisodeHistoryEntry)


def test_read_model_get_history_unknown_pair_empty(db_session):
    assert get_relationship_history(db_session, uuid.uuid4(), uuid.uuid4()) == []
    assert get_current_procurement_sales_link(db_session, uuid.uuid4(), uuid.uuid4()) is None


# ---------------------------------------------------------------------------
# Procurement-evidence intake (section 26) and customer=NULL (section 27)
# ---------------------------------------------------------------------------


def test_procurement_ledger_fragment_can_be_auto_confirmed_link_evidence(db_session):
    """Section 26: a resolved procurement Contract + resolved
    SalesContract + the procurement EvidenceFragment that directly
    asserts the scope reference + an explicit AUTO_CONFIRMED action
    (caller has deterministically established it) -> a confirmed link.
    No legacy import/cutover flow is touched — this proves the
    capability directly against the application function, exactly as
    Slice 1 proved SalesContract's analogous procurement intake."""
    procurement_frag = _make_fragment(
        db_session, raw_data={"外销合同编码": "SC-FROM-LEDGER", "对接人": "synthetic"}, fragment_kind=FragmentKind.EXCEL_ROW
    )
    contract = _make_contract(db_session, procurement_frag.id)
    sales_contract = _make_sales_contract(db_session, procurement_frag.id, sales_contract_no="SC-FROM-LEDGER")
    db_session.commit()

    result = add_procurement_sales_link(
        db_session, procurement_contract_id=contract.id, sales_contract_id=sales_contract.id,
        source_fragment_id=procurement_frag.id, confirmation_type=ConfirmationType.AUTO_CONFIRMED, created_at=NOW,
    )
    db_session.commit()

    assert result.created is True
    assert result.link.confirmation_type == ConfirmationType.AUTO_CONFIRMED
    assert result.link.source_fragment_id == procurement_frag.id


def test_link_allowed_when_sales_contract_customer_is_null(db_session):
    """Section 27: a SalesContract with customer=NULL is still a valid
    link target — a link asserts scope existence, not customer identity."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)
    sales_contract = _make_sales_contract(db_session, frag.id)
    db_session.commit()
    assert sales_contract.customer is None

    result = _add(db_session, contract, sales_contract)
    assert result.created is True
    # The unrelated unresolved-customer Task from SalesContract creation
    # remains independent of the link's own existence.
    customer_tasks = [
        t for t in ExceptionRepository(db_session).list_open()
        if t.exception_type == ExceptionType.SALES_CONTRACT_CUSTOMER_UNRESOLVED
    ]
    assert len(customer_tasks) == 1


# ---------------------------------------------------------------------------
# Isolation: ProcurementSalesLink never pollutes procurement matching
# ---------------------------------------------------------------------------


def test_contract_and_sales_contract_repositories_unaware_of_link(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)
    sales_contract = _make_sales_contract(db_session, frag.id)
    db_session.commit()
    contracts_before = {c.id for c in ContractRepository(db_session).list_all()}
    sales_contracts_before = {sc.id for sc in SalesContractRepository(db_session).list_all()}

    _add(db_session, contract, sales_contract)

    assert {c.id for c in ContractRepository(db_session).list_all()} == contracts_before
    assert {sc.id for sc in SalesContractRepository(db_session).list_all()} == sales_contracts_before


# ---------------------------------------------------------------------------
# execute_* — the human/CLI evidence-building wrappers
# ---------------------------------------------------------------------------


def test_execute_add_builds_manual_evidence_and_is_idempotent(db_session):
    contract, sales_contract = _setup(db_session)
    result = execute_add_procurement_sales_link(
        db_session, procurement_contract_id=contract.id, sales_contract_id=sales_contract.id
    )
    assert result.created is True
    fragment = EvidenceRepository(db_session).get_fragment(result.link.source_fragment_id)
    assert fragment.fragment_kind == FragmentKind.MANUAL_FACT

    replay = execute_add_procurement_sales_link(
        db_session, procurement_contract_id=contract.id, sales_contract_id=sales_contract.id
    )
    assert replay.replay is True
    assert replay.link.id == result.link.id


def test_execute_add_identity_error_survives_serialized_transaction(db_session):
    contract, sales_contract = _setup(db_session)
    first = execute_add_procurement_sales_link(
        db_session, procurement_contract_id=contract.id, sales_contract_id=sales_contract.id
    )
    execute_correct_procurement_sales_link(db_session, superseded_link_id=first.link.id)

    with pytest.raises(ProcurementSalesLinkFactError):
        execute_add_procurement_sales_link(
            db_session, procurement_contract_id=contract.id, sales_contract_id=sales_contract.id
        )
    # The relationship is genuinely retired — no anchor resurrected.
    assert get_current_procurement_sales_link(db_session, contract.id, sales_contract.id) is None


def test_execute_correct_and_reestablish_round_trip(db_session):
    contract, sales_contract = _setup(db_session)
    created = execute_add_procurement_sales_link(
        db_session, procurement_contract_id=contract.id, sales_contract_id=sales_contract.id
    )
    corrected = execute_correct_procurement_sales_link(db_session, superseded_link_id=created.link.id)
    assert corrected.replacement_link is None
    assert get_current_procurement_sales_link(db_session, contract.id, sales_contract.id) is None

    reestablished = execute_reestablish_procurement_sales_link(
        db_session, procurement_contract_id=contract.id, sales_contract_id=sales_contract.id
    )
    assert reestablished.created is True
    assert reestablished.link.id != created.link.id
    assert get_current_procurement_sales_link(db_session, contract.id, sales_contract.id).id == reestablished.link.id
