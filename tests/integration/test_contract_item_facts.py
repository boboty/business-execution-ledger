"""Phase 2D.1-R1 — ContractItem Fact Maintenance.

Covers docs/PHASE2D1-R0-DECISIONS.md section 1's frozen semantics as
implemented by bel.application.contract_item_facts: the three cases
(create / supplement / correct) never share one operation, every
revision carries Evidence, the anchor + revision model keeps
``ContractItemRepository.get`` returning the pre-R1-shaped dataclass,
and a correction that supersedes a revision with dependent derived
records raises a Task rather than silently rewriting them.

Also covers the Phase 2D.1-R1 Codex fix round: an existing anchor is
never a blind "return existing" (BLOCKER 1); a reused Evidence fragment
is never an unconditional replay (BLOCKER 2); the repository's write
primitives cannot be used to create a NULL-provenance Fact or a second
current revision (BLOCKER 3); and the database itself enforces "at most
one current revision per anchor" (BLOCKER 4).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from bel.application.contract_item_facts import (
    ContractItemFactConflict,
    ContractItemFactError,
    correct_contract_item_fact,
    create_contract_item_fact,
    execute_correct_contract_item_fact,
    execute_create_contract_item_fact,
    execute_supplement_contract_item_fact,
    get_contract_item_history,
    supplement_contract_item_fact,
)
from bel.domain.accrual import InvoiceItemAllocation, ItemAllocationConfirmationType
from bel.domain.contract import Contract, ContractItemRevision, ContractItemRevisionType
from bel.domain.evidence import EvidenceDocument, EvidenceFragment, FragmentKind
from bel.domain.exception import ExceptionStatus, ExceptionType
from bel.domain.invoice import Invoice, InvoiceDirection, InvoiceItem
from bel.infrastructure.persistence.database import make_engine, make_session_factory
from bel.infrastructure.persistence.models import Base
from bel.infrastructure.persistence.repositories import (
    ContractItemRepository,
    ContractRepository,
    EvidenceRepository,
    ExceptionRepository,
    InvoiceItemAllocationRepository,
    InvoiceItemRepository,
    InvoiceRepository,
)

NOW = datetime.now(timezone.utc)


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


def _make_contract(session, fragment_id):
    contract = Contract(
        id=uuid.uuid4(),
        contract_no=f"C-{uuid.uuid4().hex[:8]}",
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


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


def test_create_contract_item_fact_from_evidence(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)

    result = create_contract_item_fact(
        db_session,
        contract_id=contract.id,
        source_item_key="ITEM-A",
        fields={"product_name": "Widget", "quantity": Decimal("10")},
        source_fragment_id=frag.id,
        created_at=NOW,
    )
    db_session.commit()

    assert result.created is True
    assert result.item.product_name == "Widget"
    assert result.item.quantity == Decimal("10")
    # Authoritative current is readable through the unchanged assembly seam.
    current = ContractItemRepository(db_session).get(result.item.id)
    assert current == result.item
    # Evidence trace: current_source_fragment_id resolves to the INITIAL revision's fragment.
    assert current.current_source_fragment_id == frag.id

    history = get_contract_item_history(db_session, result.item.id)
    assert len(history) == 1
    assert history[0].revision_type == ContractItemRevisionType.INITIAL
    assert history[0].superseded_by_revision_id is None


def test_create_exact_replay_same_identity_same_evidence_same_assertion(db_session):
    """Test list #1: same identity + same Evidence + same assertion -> exact replay."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)

    first = create_contract_item_fact(
        db_session,
        contract_id=contract.id,
        source_item_key="ITEM-A",
        fields={"product_name": "Widget"},
        source_fragment_id=frag.id,
        created_at=NOW,
    )
    db_session.commit()

    replay = create_contract_item_fact(
        db_session,
        contract_id=contract.id,
        source_item_key="ITEM-A",
        fields={"product_name": "Widget"},
        source_fragment_id=frag.id,  # SAME fragment
        created_at=NOW,
    )
    db_session.commit()

    assert replay.created is False
    assert replay.revision_written is False
    assert replay.replay is True
    assert replay.corroborating is False
    assert replay.item.id == first.item.id
    assert len(get_contract_item_history(db_session, first.item.id)) == 1


def test_create_corroborating_same_identity_different_evidence_same_assertion(db_session):
    """Test list #2: same identity + different Evidence + same assertion ->
    compatible/corroborating Evidence, never a second INITIAL revision."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)

    first = create_contract_item_fact(
        db_session,
        contract_id=contract.id,
        source_item_key="ITEM-A",
        fields={"product_name": "Widget"},
        source_fragment_id=frag.id,
        created_at=NOW,
    )
    db_session.commit()

    frag2 = _make_fragment(db_session)
    corroborating = create_contract_item_fact(
        db_session,
        contract_id=contract.id,
        source_item_key="ITEM-A",
        fields={"product_name": "Widget"},  # same content
        source_fragment_id=frag2.id,  # DIFFERENT fragment
        created_at=NOW,
    )
    db_session.commit()

    assert corroborating.created is False
    assert corroborating.revision_written is False
    assert corroborating.replay is False
    assert corroborating.corroborating is True
    assert corroborating.item.id == first.item.id
    # No second INITIAL revision; authoritative state unchanged.
    assert len(get_contract_item_history(db_session, first.item.id)) == 1
    # The corroborating fragment itself is preserved, immutable Evidence.
    assert EvidenceRepository(db_session).get_fragment(frag2.id) is not None


def test_create_conflict_same_identity_different_evidence_conflicting_assertion(db_session):
    """Test list #3: same identity + different Evidence + conflicting
    assertion -> ContractItemFactConflict."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)

    create_contract_item_fact(
        db_session,
        contract_id=contract.id,
        source_item_key="ITEM-A",
        fields={"product_name": "Widget"},
        source_fragment_id=frag.id,
        created_at=NOW,
    )
    db_session.commit()

    frag2 = _make_fragment(db_session)
    with pytest.raises(ContractItemFactConflict):
        create_contract_item_fact(
            db_session,
            contract_id=contract.id,
            source_item_key="ITEM-A",
            fields={"product_name": "SomethingElse"},
            source_fragment_id=frag2.id,
            created_at=NOW,
        )
    db_session.rollback()
    assert len(get_contract_item_history(db_session, ContractItemRepository(db_session).find_by_contract_and_key(contract.id, "ITEM-A").id)) == 1


def test_create_conflict_same_evidence_different_initial_assertion(db_session):
    """Test list #4: same Evidence + different INITIAL assertion -> conflict."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)

    create_contract_item_fact(
        db_session,
        contract_id=contract.id,
        source_item_key="ITEM-A",
        fields={"product_name": "Widget"},
        source_fragment_id=frag.id,
        created_at=NOW,
    )
    db_session.commit()

    with pytest.raises(ContractItemFactConflict):
        create_contract_item_fact(
            db_session,
            contract_id=contract.id,
            source_item_key="ITEM-A",
            fields={"product_name": "SomethingElse"},
            source_fragment_id=frag.id,  # SAME fragment, different content
            created_at=NOW,
        )


def test_create_contract_item_fact_rejects_unknown_contract(db_session):
    frag = _make_fragment(db_session)
    with pytest.raises(ContractItemFactError):
        create_contract_item_fact(
            db_session,
            contract_id=uuid.uuid4(),
            source_item_key="ITEM-A",
            fields={},
            source_fragment_id=frag.id,
            created_at=NOW,
        )


def test_create_contract_item_fact_rejects_missing_evidence(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)
    with pytest.raises(ContractItemFactError):
        create_contract_item_fact(
            db_session,
            contract_id=contract.id,
            source_item_key="ITEM-A",
            fields={},
            source_fragment_id=uuid.uuid4(),
            created_at=NOW,
        )


# ---------------------------------------------------------------------------
# Supplement
# ---------------------------------------------------------------------------


def _create_item(db_session, fields=None):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)
    result = create_contract_item_fact(
        db_session,
        contract_id=contract.id,
        source_item_key="ITEM-A",
        fields=fields or {"product_name": "Widget"},
        source_fragment_id=frag.id,
        created_at=NOW,
    )
    db_session.commit()
    return result.item


def test_supplement_fills_previously_unknown_field(db_session):
    item = _create_item(db_session, fields={"product_name": "Widget"})
    current = ContractItemRepository(db_session).get_current_revision(item.id)
    frag2 = _make_fragment(db_session)

    result = supplement_contract_item_fact(
        db_session,
        contract_item_id=item.id,
        based_on_revision_id=current.id,
        fields={"quantity": Decimal("10")},
        source_fragment_id=frag2.id,
        created_at=NOW,
    )
    db_session.commit()

    assert result.revision_written is True
    assert result.item.quantity == Decimal("10")
    assert result.item.product_name == "Widget"  # carried forward unchanged

    history = get_contract_item_history(db_session, item.id)
    assert [r.revision_type for r in history] == [ContractItemRevisionType.INITIAL, ContractItemRevisionType.SUPPLEMENT]
    assert history[0].superseded_by_revision_id == history[1].id  # old revision retained, marked superseded
    assert history[0].product_name == "Widget"  # retired revision's own values never change


def test_supplement_exact_replay_same_evidence_same_assertion(db_session):
    """Test list #5: exact same Evidence + exact same SUPPLEMENT assertion -> replay."""
    item = _create_item(db_session)
    current = ContractItemRepository(db_session).get_current_revision(item.id)
    frag2 = _make_fragment(db_session)

    first = supplement_contract_item_fact(
        db_session,
        contract_item_id=item.id,
        based_on_revision_id=current.id,
        fields={"quantity": Decimal("10")},
        source_fragment_id=frag2.id,
        created_at=NOW,
    )
    db_session.commit()

    replay = supplement_contract_item_fact(
        db_session,
        contract_item_id=item.id,
        based_on_revision_id=current.id,  # deliberately stale — replay must still succeed
        fields={"quantity": Decimal("10")},
        source_fragment_id=frag2.id,
        created_at=NOW,
    )
    db_session.commit()

    assert replay.revision_written is False
    assert replay.replay is True
    assert replay.item.id == first.item.id
    assert len(get_contract_item_history(db_session, item.id)) == 2  # no duplicate revision


def test_supplement_exact_replay_with_resupplied_same_value_field(db_session):
    """Regression for Phase 2D.1-R1 Codex fix round #2: a supplement call
    that names an ALREADY-known field with its existing (harmless,
    unchanged) value alongside a genuinely new field must still replay
    exactly when the identical call is repeated — the persisted
    ``asserted_field_names`` must not silently drop the unchanged field
    from what "this call asserted", the way a predecessor-diff
    reconstruction would (the stored snapshot is identical whether or
    not the caller named it)."""
    item = _create_item(db_session, fields={"product_name": "Widget"})
    current = ContractItemRepository(db_session).get_current_revision(item.id)
    frag2 = _make_fragment(db_session)

    fields = {"product_name": "Widget", "quantity": Decimal("10")}  # "product_name" is already known and unchanged
    first = supplement_contract_item_fact(
        db_session,
        contract_item_id=item.id,
        based_on_revision_id=current.id,
        fields=fields,
        source_fragment_id=frag2.id,
        created_at=NOW,
    )
    db_session.commit()
    assert first.revision_written is True

    replay = supplement_contract_item_fact(
        db_session,
        contract_item_id=item.id,
        based_on_revision_id=current.id,  # deliberately stale — replay must still succeed
        fields=fields,  # SAME call, including the resupplied same-value field
        source_fragment_id=frag2.id,
        created_at=NOW,
    )
    db_session.commit()

    assert replay.revision_written is False
    assert replay.replay is True
    assert replay.item.id == first.item.id
    assert len(get_contract_item_history(db_session, item.id)) == 2  # no duplicate revision


def test_correction_exact_replay_with_resupplied_same_value_field(db_session):
    """Same regression as above, for correction: a correction call that
    re-asserts an unchanged field's existing value alongside a genuinely
    corrected field must still replay exactly."""
    item = _create_item(db_session, fields={"product_name": "Widget", "quantity": Decimal("10")})
    current = ContractItemRepository(db_session).get_current_revision(item.id)
    frag2 = _make_fragment(db_session)

    fields = {"product_name": "Widget", "quantity": Decimal("12")}  # "product_name" resupplied unchanged
    first = correct_contract_item_fact(
        db_session,
        contract_item_id=item.id,
        based_on_revision_id=current.id,
        fields=fields,
        source_fragment_id=frag2.id,
        created_at=NOW,
    )
    db_session.commit()
    assert first.revision_written is True

    replay = correct_contract_item_fact(
        db_session,
        contract_item_id=item.id,
        based_on_revision_id=current.id,
        fields=fields,
        source_fragment_id=frag2.id,
        created_at=NOW,
    )
    db_session.commit()

    assert replay.revision_written is False
    assert replay.replay is True
    assert replay.item.id == first.item.id
    assert len(get_contract_item_history(db_session, item.id)) == 2


def test_supplement_conflict_same_evidence_different_field_value(db_session):
    """Test list #6: same Evidence + different field/value -> conflict."""
    item = _create_item(db_session)
    current = ContractItemRepository(db_session).get_current_revision(item.id)
    frag2 = _make_fragment(db_session)

    supplement_contract_item_fact(
        db_session,
        contract_item_id=item.id,
        based_on_revision_id=current.id,
        fields={"quantity": Decimal("10")},
        source_fragment_id=frag2.id,
        created_at=NOW,
    )
    db_session.commit()

    with pytest.raises(ContractItemFactConflict):
        # A NEW anchor's fresh current revision id won't be checked here —
        # find_revision_by_fragment fires first, purely on frag2 reuse.
        supplement_contract_item_fact(
            db_session,
            contract_item_id=item.id,
            based_on_revision_id=uuid.uuid4(),
            fields={"quantity": Decimal("999")},  # different value, SAME fragment
            source_fragment_id=frag2.id,
            created_at=NOW,
        )


def test_supplement_conflict_same_evidence_reused_as_correction(db_session):
    """Test list #7: same Evidence used as CORRECTION after SUPPLEMENT -> conflict."""
    item = _create_item(db_session, fields={"quantity": Decimal("10")})
    current = ContractItemRepository(db_session).get_current_revision(item.id)
    frag2 = _make_fragment(db_session)

    supplement_contract_item_fact(
        db_session,
        contract_item_id=item.id,
        based_on_revision_id=current.id,
        fields={"unit": "PCS"},
        source_fragment_id=frag2.id,
        created_at=NOW,
    )
    db_session.commit()
    current2 = ContractItemRepository(db_session).get_current_revision(item.id)

    with pytest.raises(ContractItemFactConflict):
        correct_contract_item_fact(
            db_session,
            contract_item_id=item.id,
            based_on_revision_id=current2.id,
            fields={"quantity": Decimal("20")},
            source_fragment_id=frag2.id,  # SAME fragment reused with a different intent
            created_at=NOW,
        )


def test_supplement_rejects_conflicting_known_value(db_session):
    item = _create_item(db_session, fields={"product_name": "Widget"})
    current = ContractItemRepository(db_session).get_current_revision(item.id)
    frag2 = _make_fragment(db_session)

    with pytest.raises(ContractItemFactConflict):
        supplement_contract_item_fact(
            db_session,
            contract_item_id=item.id,
            based_on_revision_id=current.id,
            fields={"product_name": "DifferentWidget"},
            source_fragment_id=frag2.id,
            created_at=NOW,
        )
    db_session.rollback()
    assert len(get_contract_item_history(db_session, item.id)) == 1


def test_supplement_allows_resupplying_same_value(db_session):
    item = _create_item(db_session, fields={"product_name": "Widget"})
    current = ContractItemRepository(db_session).get_current_revision(item.id)
    frag2 = _make_fragment(db_session)

    result = supplement_contract_item_fact(
        db_session,
        contract_item_id=item.id,
        based_on_revision_id=current.id,
        fields={"product_name": "Widget", "quantity": Decimal("10")},
        source_fragment_id=frag2.id,
        created_at=NOW,
    )
    db_session.commit()
    assert result.revision_written is True
    assert result.item.product_name == "Widget"


def test_supplement_rejects_stale_based_on_revision(db_session):
    item = _create_item(db_session)
    current = ContractItemRepository(db_session).get_current_revision(item.id)
    frag2 = _make_fragment(db_session)
    supplement_contract_item_fact(
        db_session,
        contract_item_id=item.id,
        based_on_revision_id=current.id,
        fields={"quantity": Decimal("10")},
        source_fragment_id=frag2.id,
        created_at=NOW,
    )
    db_session.commit()

    frag3 = _make_fragment(db_session)
    with pytest.raises(ContractItemFactConflict):
        supplement_contract_item_fact(
            db_session,
            contract_item_id=item.id,
            based_on_revision_id=current.id,  # now stale: superseded by the supplement above
            fields={"unit": "PCS"},
            source_fragment_id=frag3.id,
            created_at=NOW,
        )


def test_supplement_rejects_unknown_field(db_session):
    item = _create_item(db_session)
    current = ContractItemRepository(db_session).get_current_revision(item.id)
    frag2 = _make_fragment(db_session)
    with pytest.raises(ContractItemFactError):
        supplement_contract_item_fact(
            db_session,
            contract_item_id=item.id,
            based_on_revision_id=current.id,
            fields={"not_a_real_field": "x"},
            source_fragment_id=frag2.id,
            created_at=NOW,
        )


# ---------------------------------------------------------------------------
# Correction
# ---------------------------------------------------------------------------


def test_correction_replaces_wrong_known_value(db_session):
    item = _create_item(db_session, fields={"product_name": "Widget", "quantity": Decimal("10")})
    current = ContractItemRepository(db_session).get_current_revision(item.id)
    frag2 = _make_fragment(db_session)

    result = correct_contract_item_fact(
        db_session,
        contract_item_id=item.id,
        based_on_revision_id=current.id,
        fields={"quantity": Decimal("12")},
        source_fragment_id=frag2.id,
        created_at=NOW,
    )
    db_session.commit()

    assert result.item.quantity == Decimal("12")
    history = get_contract_item_history(db_session, item.id)
    assert len(history) == 2
    assert history[0].quantity == Decimal("10")  # old revision retained, unmutated
    assert history[0].superseded_by_revision_id == history[1].id
    assert history[1].revision_type == ContractItemRevisionType.CORRECTION
    # Current projection reflects ONLY the corrected value.
    current_item = ContractItemRepository(db_session).get(item.id)
    assert current_item.quantity == Decimal("12")


def test_correction_exact_replay(db_session):
    """Test list #8: exact correction replay -> replay."""
    item = _create_item(db_session, fields={"quantity": Decimal("10")})
    current = ContractItemRepository(db_session).get_current_revision(item.id)
    frag2 = _make_fragment(db_session)

    correct_contract_item_fact(
        db_session,
        contract_item_id=item.id,
        based_on_revision_id=current.id,
        fields={"quantity": Decimal("12")},
        source_fragment_id=frag2.id,
        created_at=NOW,
    )
    db_session.commit()

    replay = correct_contract_item_fact(
        db_session,
        contract_item_id=item.id,
        based_on_revision_id=current.id,
        fields={"quantity": Decimal("12")},
        source_fragment_id=frag2.id,
        created_at=NOW,
    )
    db_session.commit()

    assert replay.revision_written is False
    assert replay.replay is True
    assert len(get_contract_item_history(db_session, item.id)) == 2


def test_correction_conflict_same_evidence_different_corrected_value(db_session):
    """Test list #9: same Evidence + different corrected value -> conflict."""
    item = _create_item(db_session, fields={"quantity": Decimal("10")})
    current = ContractItemRepository(db_session).get_current_revision(item.id)
    frag2 = _make_fragment(db_session)

    correct_contract_item_fact(
        db_session,
        contract_item_id=item.id,
        based_on_revision_id=current.id,
        fields={"quantity": Decimal("12")},
        source_fragment_id=frag2.id,
        created_at=NOW,
    )
    db_session.commit()

    with pytest.raises(ContractItemFactConflict):
        correct_contract_item_fact(
            db_session,
            contract_item_id=item.id,
            based_on_revision_id=uuid.uuid4(),
            fields={"quantity": Decimal("13")},  # different corrected value, SAME fragment
            source_fragment_id=frag2.id,
            created_at=NOW,
        )


def test_correction_conflict_same_evidence_different_intent(db_session):
    """Test list #10: same Evidence + different intent -> conflict (correction fragment reused as supplement)."""
    item = _create_item(db_session, fields={"quantity": Decimal("10")})
    current = ContractItemRepository(db_session).get_current_revision(item.id)
    frag2 = _make_fragment(db_session)

    correct_contract_item_fact(
        db_session,
        contract_item_id=item.id,
        based_on_revision_id=current.id,
        fields={"quantity": Decimal("12")},
        source_fragment_id=frag2.id,
        created_at=NOW,
    )
    db_session.commit()
    current2 = ContractItemRepository(db_session).get_current_revision(item.id)

    with pytest.raises(ContractItemFactConflict):
        supplement_contract_item_fact(
            db_session,
            contract_item_id=item.id,
            based_on_revision_id=current2.id,
            fields={"unit": "PCS"},
            source_fragment_id=frag2.id,  # SAME fragment, different intent (SUPPLEMENT vs CORRECTION)
            created_at=NOW,
        )


def test_correction_rejects_field_with_no_existing_value(db_session):
    item = _create_item(db_session, fields={"product_name": "Widget"})
    current = ContractItemRepository(db_session).get_current_revision(item.id)
    frag2 = _make_fragment(db_session)

    with pytest.raises(ContractItemFactConflict):
        correct_contract_item_fact(
            db_session,
            contract_item_id=item.id,
            based_on_revision_id=current.id,
            fields={"specification": "brand new value"},  # never asserted before -> supplement, not correction
            source_fragment_id=frag2.id,
            created_at=NOW,
        )


def test_correction_rejects_non_current_revision(db_session):
    item = _create_item(db_session, fields={"quantity": Decimal("10")})
    current = ContractItemRepository(db_session).get_current_revision(item.id)
    frag2 = _make_fragment(db_session)
    correct_contract_item_fact(
        db_session,
        contract_item_id=item.id,
        based_on_revision_id=current.id,
        fields={"quantity": Decimal("12")},
        source_fragment_id=frag2.id,
        created_at=NOW,
    )
    db_session.commit()

    frag3 = _make_fragment(db_session)
    with pytest.raises(ContractItemFactConflict):
        correct_contract_item_fact(
            db_session,
            contract_item_id=item.id,
            based_on_revision_id=current.id,  # no longer current
            fields={"quantity": Decimal("13")},
            source_fragment_id=frag3.id,
            created_at=NOW,
        )


def test_correction_flags_dependent_derived_records_with_a_task(db_session):
    item = _create_item(db_session, fields={"product_name": "Widget", "quantity": Decimal("10")})
    current = ContractItemRepository(db_session).get_current_revision(item.id)

    # A downstream InvoiceItemAllocation identity-references this ContractItem.
    invoice = Invoice(
        id=uuid.uuid4(),
        direction=InvoiceDirection.PURCHASE,
        invoice_type=None,
        invoice_no="INV-1",
        digital_invoice_no="INV-1",
        external_invoice_key="INV-1",
        issue_date=None,
        seller="Supplier",
        buyer="Buyer Co",
        net_amount=Decimal("10"),
        tax_amount=Decimal("0"),
        gross_amount=Decimal("10"),
        invoice_status=None,
        source_fragment_id=current.source_fragment_id,
        created_at=NOW,
        updated_at=NOW,
    )
    InvoiceRepository(db_session).add(invoice)
    db_session.flush()
    invoice_item = InvoiceItem(
        id=uuid.uuid4(),
        invoice_id=invoice.id,
        line_no=1,
        product_name="Widget",
        specification=None,
        unit=None,
        quantity=Decimal("10"),
        unit_price=Decimal("1"),
        net_amount=Decimal("10"),
        tax_rate=None,
        tax_amount=Decimal("0"),
        gross_amount=Decimal("10"),
        source_fragment_id=current.source_fragment_id,
    )
    InvoiceItemRepository(db_session).add(invoice_item)
    db_session.flush()
    allocation = InvoiceItemAllocation(
        id=uuid.uuid4(),
        invoice_item_id=invoice_item.id,
        contract_item_id=item.id,
        allocated_quantity=Decimal("10"),
        allocated_net_amount=Decimal("10"),
        confirmation_type=ItemAllocationConfirmationType.MANUAL_CONFIRMED,
        source_fragment_id=current.source_fragment_id,
        created_at=NOW,
    )
    InvoiceItemAllocationRepository(db_session).add(allocation)
    db_session.flush()

    frag2 = _make_fragment(db_session)
    correct_contract_item_fact(
        db_session,
        contract_item_id=item.id,
        based_on_revision_id=current.id,
        fields={"quantity": Decimal("12")},
        source_fragment_id=frag2.id,
        created_at=NOW,
    )
    db_session.commit()

    open_tasks = ExceptionRepository(db_session).list_open()
    matching = [t for t in open_tasks if t.exception_type == ExceptionType.CONTRACT_ITEM_FACT_SUPERSEDED]
    assert len(matching) == 1
    assert matching[0].status == ExceptionStatus.OPEN
    assert matching[0].detail["contract_item_id"] == str(item.id)
    assert str(allocation.id) in matching[0].detail["dependents"]["invoice_item_allocations"]

    # The allocation itself is untouched — never silently rewritten.
    unchanged = InvoiceItemAllocationRepository(db_session).get(allocation.id)
    assert unchanged.allocated_quantity == Decimal("10")


def test_correction_without_dependents_raises_no_task(db_session):
    item = _create_item(db_session, fields={"quantity": Decimal("10")})
    current = ContractItemRepository(db_session).get_current_revision(item.id)
    frag2 = _make_fragment(db_session)

    correct_contract_item_fact(
        db_session,
        contract_item_id=item.id,
        based_on_revision_id=current.id,
        fields={"quantity": Decimal("12")},
        source_fragment_id=frag2.id,
        created_at=NOW,
    )
    db_session.commit()

    assert ExceptionRepository(db_session).list_open() == []


# ---------------------------------------------------------------------------
# Repository invariant (Phase 2D.1-R1 Codex fix round, BLOCKER 3/4)
# ---------------------------------------------------------------------------


def test_repository_rejects_new_revision_with_no_evidence(db_session):
    """Test list #11: new revision with source_fragment_id=None -> rejected
    through the normal repository API."""
    item = _create_item(db_session)
    current = ContractItemRepository(db_session).get_current_revision(item.id)
    repo = ContractItemRepository(db_session)

    with pytest.raises(ValueError):
        repo.create_initial_revision(
            ContractItemRevision(
                id=uuid.uuid4(),
                contract_item_id=uuid.uuid4(),
                revision_type=ContractItemRevisionType.INITIAL,
                sku=None,
                product_name=None,
                specification=None,
                quantity=None,
                unit=None,
                unit_price=None,
                gross_amount=None,
                tax_rate=None,
                net_amount=None,
                source_fragment_id=None,
                superseded_by_revision_id=None,
                created_at=NOW,
            )
        )

    with pytest.raises(ValueError):
        repo.append_revision_against_current(
            ContractItemRevision(
                id=uuid.uuid4(),
                contract_item_id=item.id,
                revision_type=ContractItemRevisionType.CORRECTION,
                sku=None,
                product_name=None,
                specification=None,
                quantity=None,
                unit=None,
                unit_price=None,
                gross_amount=None,
                tax_rate=None,
                net_amount=None,
                source_fragment_id=None,
                superseded_by_revision_id=None,
                created_at=NOW,
            ),
            based_on_revision_id=current.id,
        )


def test_repository_rejects_second_current_revision(db_session):
    """Test list #12: attempt to add a second current -> repository/storage
    rejects (the DB-level partial unique index, BLOCKER 4)."""
    item = _create_item(db_session)
    frag2 = _make_fragment(db_session)
    repo = ContractItemRepository(db_session)

    with pytest.raises(IntegrityError):
        repo.create_initial_revision(
            ContractItemRevision(
                id=uuid.uuid4(),
                contract_item_id=item.id,  # an anchor that ALREADY has a current INITIAL revision
                revision_type=ContractItemRevisionType.INITIAL,
                sku=None,
                product_name="Second",
                specification=None,
                quantity=None,
                unit=None,
                unit_price=None,
                gross_amount=None,
                tax_rate=None,
                net_amount=None,
                source_fragment_id=frag2.id,
                superseded_by_revision_id=None,
                created_at=NOW,
            )
        )
        db_session.flush()
    db_session.rollback()


def test_append_revision_against_current_rejects_stale_target(db_session):
    """append_revision_against_current returns False (writing nothing) when
    based_on_revision_id is no longer current — the same-session unit-level
    proof behind the two-independent-sessions integration test below."""
    item = _create_item(db_session, fields={"quantity": Decimal("10")})
    current = ContractItemRepository(db_session).get_current_revision(item.id)
    repo = ContractItemRepository(db_session)

    frag2 = _make_fragment(db_session)
    correct_contract_item_fact(
        db_session,
        contract_item_id=item.id,
        based_on_revision_id=current.id,
        fields={"quantity": Decimal("12")},
        source_fragment_id=frag2.id,
        created_at=NOW,
    )
    db_session.commit()

    frag3 = _make_fragment(db_session)
    stale_attempt = ContractItemRevision(
        id=uuid.uuid4(),
        contract_item_id=item.id,
        revision_type=ContractItemRevisionType.CORRECTION,
        sku=None,
        product_name=None,
        specification=None,
        quantity=Decimal("99"),
        unit=None,
        unit_price=None,
        gross_amount=None,
        tax_rate=None,
        net_amount=None,
        source_fragment_id=frag3.id,
        superseded_by_revision_id=None,
        created_at=NOW,
    )
    succeeded = repo.append_revision_against_current(stale_attempt, based_on_revision_id=current.id)
    assert succeeded is False

    current_rows = [r for r in repo.list_revisions(item.id) if r.superseded_by_revision_id is None]
    assert len(current_rows) == 1
    assert current_rows[0].quantity == Decimal("12")


def _raw_revision(*, contract_item_id, revision_type, source_fragment_id, quantity=None):
    return ContractItemRevision(
        id=uuid.uuid4(),
        contract_item_id=contract_item_id,
        revision_type=revision_type,
        sku=None,
        product_name=None,
        specification=None,
        quantity=quantity,
        unit=None,
        unit_price=None,
        gross_amount=None,
        tax_rate=None,
        net_amount=None,
        source_fragment_id=source_fragment_id,
        superseded_by_revision_id=None,
        created_at=NOW,
    )


def test_append_rejects_initial_revision_type(db_session):
    """Regression #1 (Phase 2D.1-R1 Codex fix round #3, FIX 1): appending
    an INITIAL through append_revision_against_current is a caller bug,
    not a CAS failure — it must raise BEFORE any write, leaving the old
    current revision and the full history untouched."""
    item = _create_item(db_session, fields={"quantity": Decimal("10")})
    current = ContractItemRepository(db_session).get_current_revision(item.id)
    repo = ContractItemRepository(db_session)
    frag2 = _make_fragment(db_session)

    bogus = _raw_revision(
        contract_item_id=item.id,
        revision_type=ContractItemRevisionType.INITIAL,
        source_fragment_id=frag2.id,
        quantity=Decimal("99"),
    )
    with pytest.raises(ValueError):
        repo.append_revision_against_current(bogus, based_on_revision_id=current.id)

    still_current = repo.get_current_revision(item.id)
    assert still_current.id == current.id
    assert still_current.quantity == Decimal("10")
    assert len(repo.list_revisions(item.id)) == 1


def test_append_rejects_unknown_revision_type(db_session):
    """Regression #2: an unrecognised revision_type raises before any
    write, exactly like INITIAL — not treated as a CAS failure either."""
    item = _create_item(db_session, fields={"quantity": Decimal("10")})
    current = ContractItemRepository(db_session).get_current_revision(item.id)
    repo = ContractItemRepository(db_session)
    frag2 = _make_fragment(db_session)

    bogus = _raw_revision(
        contract_item_id=item.id,
        revision_type="WHATEVER",
        source_fragment_id=frag2.id,
        quantity=Decimal("99"),
    )
    with pytest.raises(ValueError):
        repo.append_revision_against_current(bogus, based_on_revision_id=current.id)

    still_current = repo.get_current_revision(item.id)
    assert still_current.id == current.id
    assert still_current.quantity == Decimal("10")
    assert len(repo.list_revisions(item.id)) == 1


def test_append_rejects_cross_anchor_supersession(db_session):
    """Regression #3: a new revision naming anchor B, appended against a
    based_on_revision_id that is actually anchor A's current revision,
    must return False and touch NEITHER anchor's current revision — the
    anchor-ownership check is folded into the same conditional UPDATE as
    the currency check (FIX 2), not a separate SELECT."""
    item_a = _create_item(db_session, fields={"quantity": Decimal("10")})
    item_b = _create_item(db_session, fields={"quantity": Decimal("20")})
    current_a = ContractItemRepository(db_session).get_current_revision(item_a.id)
    current_b = ContractItemRepository(db_session).get_current_revision(item_b.id)
    repo = ContractItemRepository(db_session)
    frag = _make_fragment(db_session)

    cross_anchor_attempt = _raw_revision(
        contract_item_id=item_b.id,  # belongs to B
        revision_type=ContractItemRevisionType.CORRECTION,
        source_fragment_id=frag.id,
        quantity=Decimal("999"),
    )
    succeeded = repo.append_revision_against_current(cross_anchor_attempt, based_on_revision_id=current_a.id)  # A's revision
    assert succeeded is False

    # Neither anchor's current revision changed, and no new revision exists anywhere.
    assert repo.get_current_revision(item_a.id).id == current_a.id
    assert repo.get_current_revision(item_b.id).id == current_b.id
    assert len(repo.list_revisions(item_a.id)) == 1
    assert len(repo.list_revisions(item_b.id)) == 1


def test_db_rejects_second_initial_revision_via_orm_bypass(db_session):
    """Regression #4: bypassing the repository and inserting a second
    INITIAL for an anchor that already has one — via the ORM model
    directly, not append_revision_against_current — must fail at the
    database level (uq_contract_item_revisions_one_initial)."""
    from bel.infrastructure.persistence.models import ContractItemRevisionModel

    item = _create_item(db_session)
    frag2 = _make_fragment(db_session)

    db_session.add(
        ContractItemRevisionModel(
            id=uuid.uuid4(),
            contract_item_id=item.id,
            revision_type=ContractItemRevisionType.INITIAL,
            sku=None,
            product_name="Bogus second INITIAL",
            specification=None,
            quantity=None,
            unit=None,
            unit_price=None,
            gross_amount=None,
            tax_rate=None,
            net_amount=None,
            source_fragment_id=frag2.id,
            superseded_by_revision_id=None,
            created_at=NOW,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_db_rejects_unknown_revision_type_via_orm_bypass(db_session):
    """Regression #5: bypassing the repository and inserting a row with
    an unrecognised revision_type must fail at the database level
    (ck_contract_item_revisions_revision_type)."""
    from bel.infrastructure.persistence.models import ContractItemRevisionModel

    item = _create_item(db_session)
    frag2 = _make_fragment(db_session)

    db_session.add(
        ContractItemRevisionModel(
            id=uuid.uuid4(),
            contract_item_id=item.id,
            revision_type="WHATEVER",
            sku=None,
            product_name=None,
            specification=None,
            quantity=None,
            unit=None,
            unit_price=None,
            gross_amount=None,
            tax_rate=None,
            net_amount=None,
            source_fragment_id=frag2.id,
            superseded_by_revision_id=None,
            created_at=NOW,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


# ---------------------------------------------------------------------------
# Two independent SQLite sessions/connections (Phase 2D.1-R1 Codex fix
# round, section 10) — a real file-backed database, two real Session
# objects, staged flush/commit rather than literal thread concurrency.
# ---------------------------------------------------------------------------


def test_two_independent_sessions_stale_correction_is_rejected(tmp_path):
    """Test list #13/#14: Session A and Session B both read the same
    current revision; A's correction succeeds; B's correction attempt
    (based on the now-stale revision) is rejected; the final database has
    exactly one current revision."""
    db_path = tmp_path / "concurrency.db"
    engine = make_engine(str(db_path))
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)

    with session_factory() as setup_session:
        frag = _make_fragment(setup_session)
        contract = _make_contract(setup_session, frag.id)
        result = create_contract_item_fact(
            setup_session,
            contract_id=contract.id,
            source_item_key="ITEM-A",
            fields={"quantity": Decimal("10")},
            source_fragment_id=frag.id,
            created_at=NOW,
        )
        setup_session.commit()
        item_id = result.item.id

    session_a = session_factory()
    session_b = session_factory()
    try:
        current_for_a = ContractItemRepository(session_a).get_current_revision(item_id)
        current_for_b = ContractItemRepository(session_b).get_current_revision(item_id)
        assert current_for_a.id == current_for_b.id

        frag_a = _make_fragment(session_a)
        result_a = correct_contract_item_fact(
            session_a,
            contract_item_id=item_id,
            based_on_revision_id=current_for_a.id,
            fields={"quantity": Decimal("12")},
            source_fragment_id=frag_a.id,
            created_at=NOW,
        )
        session_a.commit()
        assert result_a.revision_written is True

        frag_b = _make_fragment(session_b)
        with pytest.raises(ContractItemFactConflict):
            correct_contract_item_fact(
                session_b,
                contract_item_id=item_id,
                based_on_revision_id=current_for_b.id,  # now stale — A already superseded it
                fields={"quantity": Decimal("13")},
                source_fragment_id=frag_b.id,
                created_at=NOW,
            )
        session_b.rollback()
    finally:
        session_a.close()
        session_b.close()

    with session_factory() as verify_session:
        history = ContractItemRepository(verify_session).list_revisions(item_id)
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

    result = execute_create_contract_item_fact(
        db_session, contract_id=contract.id, source_item_key="ITEM-A", fields={"product_name": "Widget"}
    )
    assert result.created is True
    assert result.item.current_source_fragment_id is not None

    # Same human confirmation resubmitted (identical payload) -> idempotent,
    # no duplicate anchor, no duplicate EvidenceDocument.
    replay = execute_create_contract_item_fact(
        db_session, contract_id=contract.id, source_item_key="ITEM-A", fields={"product_name": "Widget"}
    )
    assert replay.item.id == result.item.id
    assert replay.created is False
    assert replay.replay is True


def test_execute_supplement_and_correct_round_trip(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)
    db_session.commit()

    created = execute_create_contract_item_fact(
        db_session, contract_id=contract.id, source_item_key="ITEM-A", fields={"product_name": "Widget"}
    )
    current = ContractItemRepository(db_session).get_current_revision(created.item.id)

    supplemented = execute_supplement_contract_item_fact(
        db_session,
        contract_item_id=created.item.id,
        based_on_revision_id=current.id,
        fields={"quantity": Decimal("10")},
    )
    assert supplemented.item.quantity == Decimal("10")

    current2 = ContractItemRepository(db_session).get_current_revision(created.item.id)
    corrected = execute_correct_contract_item_fact(
        db_session,
        contract_item_id=created.item.id,
        based_on_revision_id=current2.id,
        fields={"quantity": Decimal("11")},
    )
    assert corrected.item.quantity == Decimal("11")
    assert len(get_contract_item_history(db_session, created.item.id)) == 3
