"""Phase 2D.1-R5 gate fix — Contract.gross_amount/currency must never
become NULL on the current revision. Covers all three layers: raw
schema (CHECK constraint, bypassing the application layer entirely),
application (bel.application.contract_facts's own guard), and the
existing pre-R5 invariant that create_contract_fact already enforced.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from bel.application.contract_facts import (
    ContractFactError,
    correct_contract_fact,
    create_contract_fact,
    supplement_contract_fact,
)
from bel.domain.contract import ContractRevisionType
from bel.domain.evidence import EvidenceDocument, EvidenceFragment, FragmentKind
from bel.infrastructure.persistence.database import make_engine, make_session_factory
from bel.infrastructure.persistence.models import Base, ContractRevisionModel
from bel.infrastructure.persistence.repositories import ContractRepository, EvidenceRepository

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


def _make_contract(session, frag):
    return create_contract_fact(
        session, contract_no="C-INV", counterparty="Sup", fields={"gross_amount": Decimal("100"), "currency": "CNY"},
        source_fragment_id=frag.id, created_at=NOW,
    ).contract


# ---------------------------------------------------------------------------
# Raw/schema layer — bypass the application layer entirely.
# ---------------------------------------------------------------------------


def test_raw_insert_of_current_revision_with_null_gross_amount_rejected(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag)
    frag2 = _make_fragment(db_session)

    # Raw ORM insert, bypassing ContractRepository/contract_facts entirely
    # — proves the CHECK constraint itself is the backstop, not merely
    # the application-layer guard.
    bad_revision = ContractRevisionModel(
        id=uuid.uuid4(), contract_id=contract.id, revision_type=ContractRevisionType.CORRECTION,
        contract_type=None, buyer=None, gross_amount=None, currency="CNY", contract_date=None,
        source_fragment_id=frag2.id, superseded_by_revision_id=None, asserted_field_names=["gross_amount"],
        created_at=NOW,
    )
    db_session.add(bad_revision)
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_raw_insert_of_current_revision_with_null_currency_rejected(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag)
    frag2 = _make_fragment(db_session)

    bad_revision = ContractRevisionModel(
        id=uuid.uuid4(), contract_id=contract.id, revision_type=ContractRevisionType.CORRECTION,
        contract_type=None, buyer=None, gross_amount=Decimal("1"), currency=None, contract_date=None,
        source_fragment_id=frag2.id, superseded_by_revision_id=None, asserted_field_names=["currency"],
        created_at=NOW,
    )
    db_session.add(bad_revision)
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_superseded_revision_may_keep_its_original_non_null_values(db_session):
    """The CHECK constraint only ever fires against the row's OWN
    columns at write time — a historical (superseded) row is never
    rewritten to NULL, and this confirms normal supersession (a
    non-NULL row retired via UPDATE) still works."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag)
    current = ContractRepository(db_session).get_current_revision(contract.id)
    frag2 = _make_fragment(db_session)
    correct_contract_fact(
        db_session, contract_id=contract.id, based_on_revision_id=current.id, fields={"gross_amount": Decimal("200")},
        source_fragment_id=frag2.id, created_at=NOW,
    )
    updated = ContractRepository(db_session).get(contract.id)
    assert updated.gross_amount == Decimal("200")


# ---------------------------------------------------------------------------
# Application layer — contract_facts's own guard.
# ---------------------------------------------------------------------------


def test_correction_to_none_gross_amount_rejected(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag)
    current = ContractRepository(db_session).get_current_revision(contract.id)
    frag2 = _make_fragment(db_session)
    with pytest.raises(ContractFactError):
        correct_contract_fact(
            db_session, contract_id=contract.id, based_on_revision_id=current.id, fields={"gross_amount": None},
            source_fragment_id=frag2.id, created_at=NOW,
        )
    # Untouched.
    assert ContractRepository(db_session).get(contract.id).gross_amount == Decimal("100")


def test_correction_to_none_currency_rejected(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag)
    current = ContractRepository(db_session).get_current_revision(contract.id)
    frag2 = _make_fragment(db_session)
    with pytest.raises(ContractFactError):
        correct_contract_fact(
            db_session, contract_id=contract.id, based_on_revision_id=current.id, fields={"currency": None},
            source_fragment_id=frag2.id, created_at=NOW,
        )


def test_supplement_to_none_any_field_rejected(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag)
    current = ContractRepository(db_session).get_current_revision(contract.id)
    frag2 = _make_fragment(db_session)
    with pytest.raises(ContractFactError):
        supplement_contract_fact(
            db_session, contract_id=contract.id, based_on_revision_id=current.id, fields={"buyer": None},
            source_fragment_id=frag2.id, created_at=NOW,
        )


def test_create_without_gross_amount_or_currency_rejected(db_session):
    frag = _make_fragment(db_session)
    with pytest.raises(ContractFactError):
        create_contract_fact(
            db_session, contract_no="C-NOAMT", counterparty="Sup", fields={"currency": "CNY"},
            source_fragment_id=frag.id, created_at=NOW,
        )
    with pytest.raises(ContractFactError):
        create_contract_fact(
            db_session, contract_no="C-NOCUR", counterparty="Sup", fields={"gross_amount": Decimal("1")},
            source_fragment_id=frag.id, created_at=NOW,
        )
