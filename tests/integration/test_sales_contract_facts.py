"""Phase 2D.1-R3a Slice 1 — SalesContract Foundation.

Covers docs/PHASE2D1-R0-DECISIONS.md sections 2.1-2.3 and 4.4's frozen
SalesContract semantics as implemented by
bel.application.sales_contract_facts, reusing (deliberately, not
abstracted into a shared engine) the exact anchor+revision pattern
bel.application.contract_item_facts / bel.application.shipment_facts had
validated across the Phase 2D.1-R1/R2 Codex fix rounds. SalesContract's
identity null policy is simpler than Shipment's: BOTH identity
components (our_entity, sales_contract_no) are mandatory, so there is no
"identity_confirmed" override and no global (non-anchor-scoped) fragment
lookup — the missing-identity case unconditionally blocks anchor
creation instead.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from bel.application.sales_contract_facts import (
    SalesContractFactConflict,
    SalesContractFactError,
    SalesContractIdentityIncomplete,
    correct_sales_contract_fact,
    create_sales_contract_fact,
    execute_correct_sales_contract_fact,
    execute_create_sales_contract_fact,
    execute_supplement_sales_contract_fact,
    find_sales_contract_by_identity,
    get_sales_contract,
    get_sales_contract_history,
    list_sales_contracts,
    supplement_sales_contract_fact,
)
from bel.domain.contract import Contract
from bel.domain.evidence import EvidenceDocument, EvidenceFragment, FragmentKind
from bel.domain.exception import ExceptionStatus, ExceptionType
from bel.domain.sales_contract import SALES_CONTRACT_FACT_FIELDS, SalesContractRevision, SalesContractRevisionType
from bel.infrastructure.persistence.database import make_engine, make_session_factory
from bel.infrastructure.persistence.models import Base, SalesContractRevisionModel
from bel.infrastructure.persistence.repositories import (
    ContractRepository,
    EvidenceRepository,
    ExceptionRepository,
    SalesContractRepository,
)

NOW = datetime.now(timezone.utc)
CONTRACT_DATE = date(2031, 3, 10)


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


def _make_contract(session, fragment_id, *, buyer="Our Own Entity Co", contract_no=None):
    contract = Contract(
        id=uuid.uuid4(),
        contract_no=contract_no or f"C-{uuid.uuid4().hex[:8]}",
        contract_type=None,
        counterparty="Supplier",
        buyer=buyer,
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


def _create(db_session, *, our_entity="Entity A", sales_contract_no="SC-001", fields=None, fragment=None):
    frag = fragment or _make_fragment(db_session)
    result = create_sales_contract_fact(
        db_session,
        our_entity=our_entity,
        sales_contract_no=sales_contract_no,
        fields=fields or {},
        source_fragment_id=frag.id,
        created_at=NOW,
    )
    db_session.commit()
    return result


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def test_create_sales_contract_from_evidence(db_session):
    result = _create(
        db_session, fields={"customer": "Customer Co", "currency": "USD", "gross_amount": Decimal("500.00")}
    )

    assert result.created is True
    assert result.sales_contract.our_entity == "Entity A"
    assert result.sales_contract.sales_contract_no == "SC-001"
    assert result.sales_contract.customer == "Customer Co"
    assert result.sales_contract.currency == "USD"
    assert result.sales_contract.gross_amount == Decimal("500.00")

    current = SalesContractRepository(db_session).get(result.sales_contract.id)
    assert current == result.sales_contract

    history = get_sales_contract_history(db_session, result.sales_contract.id)
    assert len(history) == 1
    assert history[0].revision_type == SalesContractRevisionType.INITIAL
    assert history[0].superseded_by_revision_id is None


def test_create_exact_replay_same_identity_same_evidence_same_assertion(db_session):
    result = _create(db_session, fields={"customer": "Customer Co"})
    frag = SalesContractRepository(db_session).get_initial_revision(result.sales_contract.id).source_fragment_id

    replay = create_sales_contract_fact(
        db_session,
        our_entity="Entity A",
        sales_contract_no="SC-001",
        fields={"customer": "Customer Co"},
        source_fragment_id=frag,
        created_at=NOW,
    )
    db_session.commit()

    assert replay.created is False
    assert replay.revision_written is False
    assert replay.replay is True
    assert replay.corroborating is False
    assert replay.sales_contract.id == result.sales_contract.id
    assert len(get_sales_contract_history(db_session, result.sales_contract.id)) == 1


def test_create_corroborating_same_identity_different_evidence_same_assertion(db_session):
    result = _create(db_session, fields={"customer": "Customer Co"})

    frag2 = _make_fragment(db_session)
    corroborating = create_sales_contract_fact(
        db_session,
        our_entity="Entity A",
        sales_contract_no="SC-001",
        fields={"customer": "Customer Co"},
        source_fragment_id=frag2.id,
        created_at=NOW,
    )
    db_session.commit()

    assert corroborating.created is False
    assert corroborating.revision_written is False
    assert corroborating.replay is False
    assert corroborating.corroborating is True
    assert corroborating.sales_contract.id == result.sales_contract.id
    assert len(get_sales_contract_history(db_session, result.sales_contract.id)) == 1
    assert EvidenceRepository(db_session).get_fragment(frag2.id) is not None


def test_create_conflict_same_identity_different_evidence_conflicting_assertion(db_session):
    result = _create(db_session, fields={"customer": "Customer Co"})
    initial_revision = SalesContractRepository(db_session).get_initial_revision(result.sales_contract.id)

    frag2 = _make_fragment(db_session)
    with pytest.raises(SalesContractFactConflict):
        create_sales_contract_fact(
            db_session,
            our_entity="Entity A",
            sales_contract_no="SC-001",
            fields={"customer": "Different Customer"},
            source_fragment_id=frag2.id,
            created_at=NOW,
        )
    db_session.commit()  # the rejected create's Task must survive commit, not rollback

    assert len(get_sales_contract_history(db_session, result.sales_contract.id)) == 1
    unchanged = SalesContractRepository(db_session).get(result.sales_contract.id)
    assert unchanged.customer == "Customer Co"

    open_tasks = ExceptionRepository(db_session).list_open()
    matching = [t for t in open_tasks if t.exception_type == ExceptionType.BUSINESS_KEY_CONFLICT]
    assert len(matching) == 1
    assert matching[0].detail["sales_contract_id"] == str(result.sales_contract.id)
    assert matching[0].detail["existing_source_fragment_id"] == str(initial_revision.source_fragment_id)
    assert matching[0].detail["conflicting_source_fragment_id"] == str(frag2.id)
    # Gate 2D.1-R3a Slice 1 fix round, BLOCKER 1: the Task carries WHICH
    # field names disagree, never the actual asserted values (customer
    # name), the entity, or the contract number.
    assert matching[0].detail["conflicting_fields"] == ["customer"]
    assert set(matching[0].detail.keys()) == {
        "sales_contract_id", "existing_source_fragment_id", "conflicting_source_fragment_id", "conflicting_fields",
    }

    # Replaying the SAME conflicting submission must not raise a SECOND Task.
    with pytest.raises(SalesContractFactConflict):
        create_sales_contract_fact(
            db_session,
            our_entity="Entity A",
            sales_contract_no="SC-001",
            fields={"customer": "Different Customer"},
            source_fragment_id=frag2.id,
            created_at=NOW,
        )
    db_session.commit()
    still_matching = [
        t for t in ExceptionRepository(db_session).list_open() if t.exception_type == ExceptionType.BUSINESS_KEY_CONFLICT
    ]
    assert len(still_matching) == 1  # idempotent — no duplicate Task


def test_create_conflict_same_evidence_different_assertion(db_session):
    result = _create(db_session, fields={"customer": "Customer Co"})
    frag = SalesContractRepository(db_session).get_initial_revision(result.sales_contract.id).source_fragment_id

    with pytest.raises(SalesContractFactConflict):
        create_sales_contract_fact(
            db_session,
            our_entity="Entity A",
            sales_contract_no="SC-001",
            fields={"customer": "Different Customer"},
            source_fragment_id=frag,  # SAME fragment, different content
            created_at=NOW,
        )

    # No persisted Task for a malformed replay of the SAME artifact.
    matching = [t for t in ExceptionRepository(db_session).list_open() if t.exception_type == ExceptionType.BUSINESS_KEY_CONFLICT]
    assert matching == []


def test_create_missing_our_entity_creates_zero_anchor_and_task(db_session):
    frag = _make_fragment(db_session)
    with pytest.raises(SalesContractIdentityIncomplete):
        create_sales_contract_fact(
            db_session,
            our_entity=None,
            sales_contract_no="SC-001",
            fields={"customer": "Customer Co"},
            source_fragment_id=frag.id,
            created_at=NOW,
        )
    db_session.commit()

    assert list_sales_contracts(db_session) == []
    matching = [
        t for t in ExceptionRepository(db_session).list_open()
        if t.exception_type == ExceptionType.SALES_CONTRACT_IDENTITY_INCOMPLETE
    ]
    assert len(matching) == 1
    assert matching[0].detail["source_fragment_id"] == str(frag.id)
    # Gate 2D.1-R3a Slice 1 fix round, BLOCKER 1: which identity component
    # is missing is a boolean, never the actual entity/contract-no value
    # (there is none to leak for the missing side, but the CO-PRESENT
    # `sales_contract_no`/`our_entity` — the value that WAS supplied — must
    # not be persisted either).
    assert matching[0].detail["missing_our_entity"] is True
    assert matching[0].detail["missing_sales_contract_no"] is False
    assert set(matching[0].detail.keys()) == {"source_fragment_id", "missing_our_entity", "missing_sales_contract_no"}


def test_create_missing_sales_contract_no_creates_zero_anchor_and_task(db_session):
    frag = _make_fragment(db_session)
    with pytest.raises(SalesContractIdentityIncomplete):
        create_sales_contract_fact(
            db_session,
            our_entity="Entity A",
            sales_contract_no=None,
            fields={},
            source_fragment_id=frag.id,
            created_at=NOW,
        )
    db_session.commit()

    assert list_sales_contracts(db_session) == []
    matching = [
        t for t in ExceptionRepository(db_session).list_open()
        if t.exception_type == ExceptionType.SALES_CONTRACT_IDENTITY_INCOMPLETE
    ]
    assert len(matching) == 1
    assert matching[0].detail["missing_our_entity"] is False
    assert matching[0].detail["missing_sales_contract_no"] is True
    assert set(matching[0].detail.keys()) == {"source_fragment_id", "missing_our_entity", "missing_sales_contract_no"}


def test_identity_incomplete_replay_does_not_duplicate_task(db_session):
    frag = _make_fragment(db_session)
    for _ in range(2):
        with pytest.raises(SalesContractIdentityIncomplete):
            create_sales_contract_fact(
                db_session,
                our_entity=None,
                sales_contract_no="SC-001",
                fields={},
                source_fragment_id=frag.id,
                created_at=NOW,
            )
        db_session.commit()

    matching = [
        t for t in ExceptionRepository(db_session).list_open()
        if t.exception_type == ExceptionType.SALES_CONTRACT_IDENTITY_INCOMPLETE
    ]
    assert len(matching) == 1
    assert list_sales_contracts(db_session) == []


def test_same_sales_contract_no_under_different_our_entity_creates_distinct_anchors(db_session):
    result_a = _create(db_session, our_entity="Entity A", sales_contract_no="SC-SHARED")
    result_b = _create(db_session, our_entity="Entity B", sales_contract_no="SC-SHARED")

    assert result_a.sales_contract.id != result_b.sales_contract.id
    assert {sc.id for sc in list_sales_contracts(db_session)} == {result_a.sales_contract.id, result_b.sales_contract.id}


def test_create_rejects_unknown_field(db_session):
    frag = _make_fragment(db_session)
    with pytest.raises(SalesContractFactError):
        create_sales_contract_fact(
            db_session,
            our_entity="Entity A",
            sales_contract_no="SC-001",
            fields={"not_a_real_field": "x"},
            source_fragment_id=frag.id,
            created_at=NOW,
        )


def test_create_rejects_missing_evidence(db_session):
    with pytest.raises(SalesContractFactError):
        create_sales_contract_fact(
            db_session,
            our_entity="Entity A",
            sales_contract_no="SC-001",
            fields={},
            source_fragment_id=uuid.uuid4(),
            created_at=NOW,
        )


# ---------------------------------------------------------------------------
# Customer (nullable, unresolved-customer Task)
# ---------------------------------------------------------------------------


def test_create_with_customer_none_is_allowed(db_session):
    result = _create(db_session, fields={})
    assert result.created is True
    assert result.sales_contract.customer is None


def test_create_with_customer_none_flags_unresolved_customer_task(db_session):
    result = _create(db_session, fields={"currency": "USD"})

    matching = [
        t for t in ExceptionRepository(db_session).list_open()
        if t.exception_type == ExceptionType.SALES_CONTRACT_CUSTOMER_UNRESOLVED
    ]
    assert len(matching) == 1
    assert matching[0].status == ExceptionStatus.OPEN
    assert matching[0].detail["sales_contract_id"] == str(result.sales_contract.id)
    # never guesses — no raw source values, no name copied from anywhere.
    assert set(matching[0].detail.keys()) == {"sales_contract_id"}


def test_supplement_customer_resolves_unresolved_task(db_session):
    result = _create(db_session, fields={})
    current = SalesContractRepository(db_session).get_current_revision(result.sales_contract.id)
    frag2 = _make_fragment(db_session)

    supplemented = supplement_sales_contract_fact(
        db_session,
        sales_contract_id=result.sales_contract.id,
        based_on_revision_id=current.id,
        fields={"customer": "Customer Co"},
        source_fragment_id=frag2.id,
        created_at=NOW,
    )
    db_session.commit()

    assert supplemented.sales_contract.customer == "Customer Co"
    tasks = ExceptionRepository(db_session).list_open()
    matching = [t for t in tasks if t.exception_type == ExceptionType.SALES_CONTRACT_CUSTOMER_UNRESOLVED]
    assert matching == []  # resolved, no longer open

    # The authoritative current projection shows the customer.
    assert get_sales_contract(db_session, result.sales_contract.id).customer == "Customer Co"


def test_supplement_customer_history_retained_after_resolution(db_session):
    result = _create(db_session, fields={})
    current = SalesContractRepository(db_session).get_current_revision(result.sales_contract.id)
    frag2 = _make_fragment(db_session)

    supplement_sales_contract_fact(
        db_session,
        sales_contract_id=result.sales_contract.id,
        based_on_revision_id=current.id,
        fields={"customer": "Customer Co"},
        source_fragment_id=frag2.id,
        created_at=NOW,
    )
    db_session.commit()

    history = get_sales_contract_history(db_session, result.sales_contract.id)
    assert [r.revision_type for r in history] == [SalesContractRevisionType.INITIAL, SalesContractRevisionType.SUPPLEMENT]
    assert history[0].customer is None  # retired revision's own values never change
    assert history[1].customer == "Customer Co"


def test_supplement_customer_replay_does_not_re_resolve_or_duplicate(db_session):
    result = _create(db_session, fields={})
    current = SalesContractRepository(db_session).get_current_revision(result.sales_contract.id)
    frag2 = _make_fragment(db_session)

    supplement_sales_contract_fact(
        db_session,
        sales_contract_id=result.sales_contract.id,
        based_on_revision_id=current.id,
        fields={"customer": "Customer Co"},
        source_fragment_id=frag2.id,
        created_at=NOW,
    )
    db_session.commit()

    replay = supplement_sales_contract_fact(
        db_session,
        sales_contract_id=result.sales_contract.id,
        based_on_revision_id=current.id,  # deliberately stale — replay must still succeed
        fields={"customer": "Customer Co"},
        source_fragment_id=frag2.id,
        created_at=NOW,
    )
    db_session.commit()

    assert replay.revision_written is False
    assert replay.replay is True
    assert len(get_sales_contract_history(db_session, result.sales_contract.id)) == 2


def test_supplement_conflicting_customer_rejected_no_overwrite(db_session):
    result = _create(db_session, fields={"customer": "Customer Co"})
    current = SalesContractRepository(db_session).get_current_revision(result.sales_contract.id)
    frag2 = _make_fragment(db_session)

    with pytest.raises(SalesContractFactConflict):
        supplement_sales_contract_fact(
            db_session,
            sales_contract_id=result.sales_contract.id,
            based_on_revision_id=current.id,
            fields={"customer": "A Different Customer"},
            source_fragment_id=frag2.id,
            created_at=NOW,
        )
    db_session.rollback()

    assert len(get_sales_contract_history(db_session, result.sales_contract.id)) == 1
    assert get_sales_contract(db_session, result.sales_contract.id).customer == "Customer Co"


def test_supplement_with_none_customer_value_rejected_never_resolves_task(db_session):
    """Gate 2D.1-R3a Slice 1 fix round, BLOCKER 2: independently
    reproduced as `supplement_sales_contract_fact(fields={"customer":
    None})` writing a no-op revision AND incorrectly resolving the
    unresolved-customer Task while `customer` was still actually NULL.
    `None` must never be treated as a valid asserted value — the caller
    omits a field entirely, never asserts it as None."""
    result = _create(db_session, fields={})
    current = SalesContractRepository(db_session).get_current_revision(result.sales_contract.id)
    frag2 = _make_fragment(db_session)

    with pytest.raises(SalesContractFactError):
        supplement_sales_contract_fact(
            db_session,
            sales_contract_id=result.sales_contract.id,
            based_on_revision_id=current.id,
            fields={"customer": None},
            source_fragment_id=frag2.id,
            created_at=NOW,
        )
    db_session.rollback()

    # No new revision, customer still NULL, Task still open — the exact
    # regression scenario reported by Gate (customer_is_none=True,
    # open_unresolved=0, revisions=2) must no longer be reachable.
    assert len(get_sales_contract_history(db_session, result.sales_contract.id)) == 1
    assert get_sales_contract(db_session, result.sales_contract.id).customer is None
    matching = [
        t for t in ExceptionRepository(db_session).list_open()
        if t.exception_type == ExceptionType.SALES_CONTRACT_CUSTOMER_UNRESOLVED
    ]
    assert len(matching) == 1
    assert matching[0].status == ExceptionStatus.OPEN


def test_execute_supplement_with_none_customer_value_rejected_via_real_api(db_session):
    """Same regression as above, through the real execute_* application
    API (not just the core function) — the path an actual caller uses."""
    created = execute_create_sales_contract_fact(
        db_session, our_entity="Entity A", sales_contract_no="SC-001", fields={}
    )
    current = SalesContractRepository(db_session).get_current_revision(created.sales_contract.id)

    with pytest.raises(SalesContractFactError):
        execute_supplement_sales_contract_fact(
            db_session, sales_contract_id=created.sales_contract.id, based_on_revision_id=current.id,
            fields={"customer": None},
        )

    assert len(get_sales_contract_history(db_session, created.sales_contract.id)) == 1
    assert get_sales_contract(db_session, created.sales_contract.id).customer is None
    matching = [
        t for t in ExceptionRepository(db_session).list_open()
        if t.exception_type == ExceptionType.SALES_CONTRACT_CUSTOMER_UNRESOLVED
    ]
    assert len(matching) == 1


def test_correct_with_none_value_rejected(db_session):
    """The same None-is-never-a-valid-assertion rule applies uniformly to
    correction, not only supplement."""
    result = _create(db_session, fields={"currency": "USD"})
    current = SalesContractRepository(db_session).get_current_revision(result.sales_contract.id)
    frag2 = _make_fragment(db_session)

    with pytest.raises(SalesContractFactError):
        correct_sales_contract_fact(
            db_session,
            sales_contract_id=result.sales_contract.id,
            based_on_revision_id=current.id,
            fields={"currency": None},
            source_fragment_id=frag2.id,
            created_at=NOW,
        )
    db_session.rollback()

    assert get_sales_contract(db_session, result.sales_contract.id).currency == "USD"


def test_create_with_none_value_rejected(db_session):
    """The same rule applies to create as well — a caller wanting a field
    left unset must omit it, never pass it explicitly as None."""
    frag = _make_fragment(db_session)
    with pytest.raises(SalesContractFactError):
        create_sales_contract_fact(
            db_session,
            our_entity="Entity A",
            sales_contract_no="SC-001",
            fields={"customer": None},
            source_fragment_id=frag.id,
            created_at=NOW,
        )
    assert list_sales_contracts(db_session) == []


# ---------------------------------------------------------------------------
# Revision invariants
# ---------------------------------------------------------------------------


def _raw_revision(*, sales_contract_id, revision_type, source_fragment_id, customer=None):
    return SalesContractRevision(
        id=uuid.uuid4(),
        sales_contract_id=sales_contract_id,
        revision_type=revision_type,
        customer=customer,
        currency=None,
        gross_amount=None,
        contract_date=None,
        source_fragment_id=source_fragment_id,
        superseded_by_revision_id=None,
        created_at=NOW,
    )


def test_db_rejects_second_initial_revision_via_orm_bypass(db_session):
    result = _create(db_session)
    frag2 = _make_fragment(db_session)

    db_session.add(
        SalesContractRevisionModel(
            id=uuid.uuid4(), sales_contract_id=result.sales_contract.id, revision_type=SalesContractRevisionType.INITIAL,
            customer=None, currency=None, gross_amount=None, contract_date=None,
            source_fragment_id=frag2.id, superseded_by_revision_id=None, created_at=NOW,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_db_rejects_second_current_revision_via_orm_bypass(db_session):
    result = _create(db_session)
    current = SalesContractRepository(db_session).get_current_revision(result.sales_contract.id)
    frag2 = _make_fragment(db_session)

    # A second row with superseded_by_revision_id IS NULL, without going
    # through the retiring UPDATE — the SAME anchor now has two "current"
    # rows, which the partial unique index must reject.
    db_session.add(
        SalesContractRevisionModel(
            id=uuid.uuid4(), sales_contract_id=result.sales_contract.id, revision_type=SalesContractRevisionType.SUPPLEMENT,
            customer="Sneaked In", currency=None, gross_amount=None, contract_date=None,
            source_fragment_id=frag2.id, superseded_by_revision_id=None, created_at=NOW,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()
    assert current.id == SalesContractRepository(db_session).get_current_revision(result.sales_contract.id).id


def test_db_rejects_unknown_revision_type_via_orm_bypass(db_session):
    result = _create(db_session)
    frag2 = _make_fragment(db_session)

    db_session.add(
        SalesContractRevisionModel(
            id=uuid.uuid4(), sales_contract_id=result.sales_contract.id, revision_type="WHATEVER",
            customer=None, currency=None, gross_amount=None, contract_date=None,
            source_fragment_id=frag2.id, superseded_by_revision_id=None, created_at=NOW,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_append_rejects_initial_revision_type(db_session):
    result = _create(db_session, fields={"customer": "Customer Co"})
    current = SalesContractRepository(db_session).get_current_revision(result.sales_contract.id)
    repo = SalesContractRepository(db_session)
    frag2 = _make_fragment(db_session)

    bogus = _raw_revision(
        sales_contract_id=result.sales_contract.id, revision_type=SalesContractRevisionType.INITIAL,
        source_fragment_id=frag2.id, customer="Sneaked In",
    )
    with pytest.raises(ValueError):
        repo.append_revision_against_current(bogus, based_on_revision_id=current.id)

    still_current = repo.get_current_revision(result.sales_contract.id)
    assert still_current.id == current.id
    assert len(repo.list_revisions(result.sales_contract.id)) == 1


def test_append_rejects_unknown_revision_type(db_session):
    result = _create(db_session)
    current = SalesContractRepository(db_session).get_current_revision(result.sales_contract.id)
    repo = SalesContractRepository(db_session)
    frag2 = _make_fragment(db_session)

    bogus = _raw_revision(
        sales_contract_id=result.sales_contract.id, revision_type="WHATEVER", source_fragment_id=frag2.id,
    )
    with pytest.raises(ValueError):
        repo.append_revision_against_current(bogus, based_on_revision_id=current.id)

    assert repo.get_current_revision(result.sales_contract.id).id == current.id
    assert len(repo.list_revisions(result.sales_contract.id)) == 1


def test_append_rejects_stale_based_on_revision(db_session):
    result = _create(db_session)
    current = SalesContractRepository(db_session).get_current_revision(result.sales_contract.id)
    frag2 = _make_fragment(db_session)
    supplement_sales_contract_fact(
        db_session, sales_contract_id=result.sales_contract.id, based_on_revision_id=current.id,
        fields={"customer": "Customer Co"}, source_fragment_id=frag2.id, created_at=NOW,
    )
    db_session.commit()

    frag3 = _make_fragment(db_session)
    with pytest.raises(SalesContractFactConflict):
        supplement_sales_contract_fact(
            db_session, sales_contract_id=result.sales_contract.id, based_on_revision_id=current.id,  # now stale
            fields={"currency": "USD"}, source_fragment_id=frag3.id, created_at=NOW,
        )


def test_append_rejects_cross_anchor_supersession(db_session):
    result_a = _create(db_session, our_entity="Entity A", sales_contract_no="SC-A")
    result_b = _create(db_session, our_entity="Entity B", sales_contract_no="SC-B")
    repo = SalesContractRepository(db_session)
    current_a = repo.get_current_revision(result_a.sales_contract.id)
    current_b = repo.get_current_revision(result_b.sales_contract.id)
    frag = _make_fragment(db_session)

    cross_anchor_attempt = _raw_revision(
        sales_contract_id=result_b.sales_contract.id, revision_type=SalesContractRevisionType.CORRECTION,
        source_fragment_id=frag.id, customer="Hijacked",
    )
    succeeded = repo.append_revision_against_current(cross_anchor_attempt, based_on_revision_id=current_a.id)
    assert succeeded is False

    assert repo.get_current_revision(result_a.sales_contract.id).id == current_a.id
    assert repo.get_current_revision(result_b.sales_contract.id).id == current_b.id
    assert len(repo.list_revisions(result_a.sales_contract.id)) == 1
    assert len(repo.list_revisions(result_b.sales_contract.id)) == 1


def test_supplement_same_evidence_reused_for_correction_rejected(db_session):
    result = _create(db_session, fields={"customer": "Customer Co"})
    current = SalesContractRepository(db_session).get_current_revision(result.sales_contract.id)
    frag2 = _make_fragment(db_session)

    supplement_sales_contract_fact(
        db_session, sales_contract_id=result.sales_contract.id, based_on_revision_id=current.id,
        fields={"currency": "USD"}, source_fragment_id=frag2.id, created_at=NOW,
    )
    db_session.commit()
    current2 = SalesContractRepository(db_session).get_current_revision(result.sales_contract.id)

    with pytest.raises(SalesContractFactConflict):
        correct_sales_contract_fact(
            db_session, sales_contract_id=result.sales_contract.id, based_on_revision_id=current2.id,
            fields={"currency": "EUR"}, source_fragment_id=frag2.id,  # SAME fragment, different intent
            created_at=NOW,
        )


def test_same_evidence_fragment_reused_across_two_anchors_cannot_mis_resolve(db_session):
    """The replay/reuse lookup (`find_revision_by_fragment`) is
    anchor-scoped: the SAME fragment id used as a SUPPLEMENT's Evidence
    for anchor A must never be mistaken for a replay against anchor B —
    each anchor's revisions must be evaluated independently."""
    result_a = _create(db_session, our_entity="Entity A", sales_contract_no="SC-A")
    result_b = _create(db_session, our_entity="Entity B", sales_contract_no="SC-B")
    current_a = SalesContractRepository(db_session).get_current_revision(result_a.sales_contract.id)
    current_b = SalesContractRepository(db_session).get_current_revision(result_b.sales_contract.id)

    shared_frag = _make_fragment(db_session)
    supplement_sales_contract_fact(
        db_session, sales_contract_id=result_a.sales_contract.id, based_on_revision_id=current_a.id,
        fields={"customer": "Customer A"}, source_fragment_id=shared_frag.id, created_at=NOW,
    )
    db_session.commit()

    # The SAME fragment, but genuinely different content for a DIFFERENT
    # anchor — repository lookup is scoped by sales_contract_id, so this
    # must be treated as fresh Evidence for anchor B, not a replay/conflict
    # bleeding over from anchor A.
    result = supplement_sales_contract_fact(
        db_session, sales_contract_id=result_b.sales_contract.id, based_on_revision_id=current_b.id,
        fields={"customer": "Customer B"}, source_fragment_id=shared_frag.id, created_at=NOW,
    )
    db_session.commit()

    assert result.revision_written is True
    assert get_sales_contract(db_session, result_a.sales_contract.id).customer == "Customer A"
    assert get_sales_contract(db_session, result_b.sales_contract.id).customer == "Customer B"


def test_correction_rejects_field_with_no_existing_value(db_session):
    result = _create(db_session)
    current = SalesContractRepository(db_session).get_current_revision(result.sales_contract.id)
    frag2 = _make_fragment(db_session)

    with pytest.raises(SalesContractFactConflict):
        correct_sales_contract_fact(
            db_session, sales_contract_id=result.sales_contract.id, based_on_revision_id=current.id,
            fields={"currency": "USD"},  # never asserted before -> supplement, not correction
            source_fragment_id=frag2.id, created_at=NOW,
        )


def test_correction_replaces_wrong_known_value(db_session):
    result = _create(db_session, fields={"currency": "USD"})
    current = SalesContractRepository(db_session).get_current_revision(result.sales_contract.id)
    frag2 = _make_fragment(db_session)

    corrected = correct_sales_contract_fact(
        db_session, sales_contract_id=result.sales_contract.id, based_on_revision_id=current.id,
        fields={"currency": "EUR"}, source_fragment_id=frag2.id, created_at=NOW,
    )
    db_session.commit()

    assert corrected.sales_contract.currency == "EUR"
    history = get_sales_contract_history(db_session, result.sales_contract.id)
    assert len(history) == 2
    assert history[0].currency == "USD"  # old revision retained, unmutated
    assert history[1].revision_type == SalesContractRevisionType.CORRECTION


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


def test_repository_rejects_new_revision_with_no_evidence(db_session):
    result = _create(db_session)
    current = SalesContractRepository(db_session).get_current_revision(result.sales_contract.id)
    repo = SalesContractRepository(db_session)

    with pytest.raises(ValueError):
        repo.create_initial_revision(
            SalesContractRevision(
                id=uuid.uuid4(), sales_contract_id=uuid.uuid4(), revision_type=SalesContractRevisionType.INITIAL,
                customer=None, currency=None, gross_amount=None, contract_date=None,
                source_fragment_id=None, superseded_by_revision_id=None, created_at=NOW,
            )
        )
    with pytest.raises(ValueError):
        repo.append_revision_against_current(
            SalesContractRevision(
                id=uuid.uuid4(), sales_contract_id=result.sales_contract.id, revision_type=SalesContractRevisionType.CORRECTION,
                customer=None, currency=None, gross_amount=None, contract_date=None,
                source_fragment_id=None, superseded_by_revision_id=None, created_at=NOW,
            ),
            based_on_revision_id=current.id,
        )


def test_execute_create_builds_manual_evidence_and_is_idempotent(db_session):
    result = execute_create_sales_contract_fact(
        db_session, our_entity="Entity A", sales_contract_no="SC-001", fields={"customer": "Customer Co"}
    )
    assert result.created is True

    fragment = EvidenceRepository(db_session).get_fragment(
        SalesContractRepository(db_session).get_initial_revision(result.sales_contract.id).source_fragment_id
    )
    assert fragment.fragment_kind == FragmentKind.MANUAL_FACT

    replay = execute_create_sales_contract_fact(
        db_session, our_entity="Entity A", sales_contract_no="SC-001", fields={"customer": "Customer Co"}
    )
    assert replay.sales_contract.id == result.sales_contract.id
    assert replay.created is False
    assert replay.replay is True


def test_execute_create_identity_incomplete_task_survives_serialized_transaction(db_session):
    """Regression for the R2 Codex fix-round lesson (see
    shipment_facts.execute_create_shipment_fact): serialized_write_transaction
    rolls back on ANY exception, so the Task must be caught-and-reraised
    OUTSIDE the transaction to survive, through the real execute_* path."""
    with pytest.raises(SalesContractIdentityIncomplete):
        execute_create_sales_contract_fact(
            db_session, our_entity=None, sales_contract_no="SC-001", fields={}
        )

    matching = [
        t for t in ExceptionRepository(db_session).list_open()
        if t.exception_type == ExceptionType.SALES_CONTRACT_IDENTITY_INCOMPLETE
    ]
    assert len(matching) == 1
    assert list_sales_contracts(db_session) == []


def test_execute_create_conflict_task_survives_serialized_transaction(db_session):
    created = execute_create_sales_contract_fact(
        db_session, our_entity="Entity A", sales_contract_no="SC-001", fields={"customer": "Customer Co"}
    )
    assert created.created is True

    with pytest.raises(SalesContractFactConflict):
        execute_create_sales_contract_fact(
            db_session, our_entity="Entity A", sales_contract_no="SC-001", fields={"customer": "Different Customer"}
        )

    matching = [t for t in ExceptionRepository(db_session).list_open() if t.exception_type == ExceptionType.BUSINESS_KEY_CONFLICT]
    assert len(matching) == 1
    unchanged = SalesContractRepository(db_session).get(created.sales_contract.id)
    assert unchanged.customer == "Customer Co"


def test_execute_supplement_and_correct_round_trip(db_session):
    created = execute_create_sales_contract_fact(
        db_session, our_entity="Entity A", sales_contract_no="SC-001", fields={}
    )
    current = SalesContractRepository(db_session).get_current_revision(created.sales_contract.id)

    supplemented = execute_supplement_sales_contract_fact(
        db_session, sales_contract_id=created.sales_contract.id, based_on_revision_id=current.id,
        fields={"customer": "Customer Co"},
    )
    assert supplemented.sales_contract.customer == "Customer Co"

    current2 = SalesContractRepository(db_session).get_current_revision(created.sales_contract.id)
    corrected = execute_correct_sales_contract_fact(
        db_session, sales_contract_id=created.sales_contract.id, based_on_revision_id=current2.id,
        fields={"customer": "Corrected Customer"},
    )
    assert corrected.sales_contract.customer == "Corrected Customer"
    assert len(get_sales_contract_history(db_session, created.sales_contract.id)) == 3


def test_two_independent_sessions_stale_correction_is_rejected(tmp_path):
    db_path = tmp_path / "sales-contract-concurrency.db"
    engine = make_engine(str(db_path))
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)

    with session_factory() as setup_session:
        frag = _make_fragment(setup_session)
        result = create_sales_contract_fact(
            setup_session, our_entity="Entity A", sales_contract_no="SC-001", fields={"customer": "Customer Co"},
            source_fragment_id=frag.id, created_at=NOW,
        )
        setup_session.commit()
        sales_contract_id = result.sales_contract.id

    session_a = session_factory()
    session_b = session_factory()
    try:
        current_for_a = SalesContractRepository(session_a).get_current_revision(sales_contract_id)
        current_for_b = SalesContractRepository(session_b).get_current_revision(sales_contract_id)
        assert current_for_a.id == current_for_b.id

        frag_a = _make_fragment(session_a)
        result_a = correct_sales_contract_fact(
            session_a, sales_contract_id=sales_contract_id, based_on_revision_id=current_for_a.id,
            fields={"customer": "Customer A View"}, source_fragment_id=frag_a.id, created_at=NOW,
        )
        session_a.commit()
        assert result_a.revision_written is True

        frag_b = _make_fragment(session_b)
        with pytest.raises(SalesContractFactConflict):
            correct_sales_contract_fact(
                session_b, sales_contract_id=sales_contract_id, based_on_revision_id=current_for_b.id,  # now stale
                fields={"customer": "Customer B View"}, source_fragment_id=frag_b.id, created_at=NOW,
            )
        session_b.rollback()
    finally:
        session_a.close()
        session_b.close()

    with session_factory() as verify_session:
        history = SalesContractRepository(verify_session).list_revisions(sales_contract_id)
        current_rows = [r for r in history if r.superseded_by_revision_id is None]
        assert len(current_rows) == 1
        assert current_rows[0].customer == "Customer A View"
        assert len(history) == 2


# ---------------------------------------------------------------------------
# Party-role guards (docs/PHASE2D1-R0-DECISIONS.md sections 2.1/2.3)
# ---------------------------------------------------------------------------


def test_sales_contract_fact_fields_excludes_identity_and_party_role_sources():
    """The only way to set `customer` is the `customer` fact field
    itself. `Contract.buyer`, a sales-scope reference number, and a
    customs/shipping receiving party are never among the accepted field
    names — there is no field name through which any of them could be
    mistaken for `customer`."""
    assert set(SALES_CONTRACT_FACT_FIELDS) == {"customer", "currency", "gross_amount", "contract_date"}
    forbidden_names = {
        "buyer", "counterparty", "our_entity", "sales_contract_no",
        "external_reference", "customs_receiving_party", "接收报关单位",
    }
    assert forbidden_names.isdisjoint(SALES_CONTRACT_FACT_FIELDS)


def test_create_sales_contract_fact_has_no_contract_reference_in_signature():
    """Structural guard: create/supplement/correct never accept a
    `contract_id`/`buyer`/`counterparty` parameter at all, so there is no
    code path — not even an optional one — through which
    `Contract.buyer` could be read and used as `customer`."""
    for fn in (create_sales_contract_fact, supplement_sales_contract_fact, correct_sales_contract_fact):
        params = set(inspect.signature(fn).parameters)
        assert params.isdisjoint({"contract_id", "buyer", "counterparty", "contract"})


def test_create_sales_contract_fact_rejects_buyer_as_unknown_field(db_session):
    frag = _make_fragment(db_session)
    with pytest.raises(SalesContractFactError):
        create_sales_contract_fact(
            db_session, our_entity="Entity A", sales_contract_no="SC-001",
            fields={"buyer": "Our Own Entity Co"}, source_fragment_id=frag.id, created_at=NOW,
        )


def test_customer_independent_of_contract_buyer_even_when_our_entity_matches(db_session):
    """Even when a procurement Contract happens to exist with
    `buyer == our_entity` (a legitimate coincidence — our_entity IS our
    own trading entity, same as buyer usually is), the SalesContract's
    customer is whatever sales-side Evidence explicitly asserts — never
    auto-derived from that Contract."""
    frag = _make_fragment(db_session)
    _make_contract(db_session, frag.id, buyer="Entity A")  # buyer happens to equal our_entity below

    result = _create(db_session, our_entity="Entity A", fields={"customer": "External Customer Co"})
    assert result.sales_contract.customer == "External Customer Co"
    assert result.sales_contract.customer != "Entity A"


# ---------------------------------------------------------------------------
# Procurement-evidence scope intake (section 15) — SalesContract scope
# existence != ProcurementSalesLink confirmation
# ---------------------------------------------------------------------------


def test_procurement_fragment_asserting_our_entity_and_scope_may_create_customer_null_sales_contract(db_session):
    """A procurement-side Evidence fragment (fragment_kind=EXCEL_ROW,
    representing an imported procurement ledger row) that happens to
    assert BOTH our_entity and the sales-scope reference on the SAME
    fragment is allowed to create SalesContract(customer=NULL) plus its
    unresolved-customer Task — exactly like any other create. This
    module has no special-cased "procurement intake" code path; it is
    fragment-kind-agnostic by design, which is what this test proves."""
    procurement_frag = _make_fragment(
        db_session,
        raw_data={"外销合同编码": "SC-FROM-PROCUREMENT", "our_entity_asserted": "Entity A"},
        fragment_kind=FragmentKind.EXCEL_ROW,
    )

    result = create_sales_contract_fact(
        db_session, our_entity="Entity A", sales_contract_no="SC-FROM-PROCUREMENT", fields={},
        source_fragment_id=procurement_frag.id, created_at=NOW,
    )
    db_session.commit()

    assert result.created is True
    assert result.sales_contract.customer is None
    matching = [
        t for t in ExceptionRepository(db_session).list_open()
        if t.exception_type == ExceptionType.SALES_CONTRACT_CUSTOMER_UNRESOLVED
    ]
    assert len(matching) == 1
    assert matching[0].detail["sales_contract_id"] == str(result.sales_contract.id)


def test_procurement_scope_intake_creates_no_procurement_sales_link_or_contract_change(db_session):
    """SalesContract scope existence != ProcurementSalesLink confirmation
    (explicitly out of scope this Slice). Creating a customer-null
    SalesContract from a procurement-origin fragment must leave the
    procurement Contract table completely untouched — no auto-linking,
    no new Contract row, no mutation of an existing one."""
    setup_frag = _make_fragment(db_session)
    contract = _make_contract(db_session, setup_frag.id)
    db_session.commit()
    contracts_before = ContractRepository(db_session).list_all()

    procurement_frag = _make_fragment(
        db_session, raw_data={"外销合同编码": "SC-FROM-PROCUREMENT"}, fragment_kind=FragmentKind.EXCEL_ROW
    )
    create_sales_contract_fact(
        db_session, our_entity="Entity A", sales_contract_no="SC-FROM-PROCUREMENT", fields={},
        source_fragment_id=procurement_frag.id, created_at=NOW,
    )
    db_session.commit()

    contracts_after = ContractRepository(db_session).list_all()
    assert [c.id for c in contracts_after] == [c.id for c in contracts_before]
    unchanged = ContractRepository(db_session).get(contract.id)
    assert unchanged.buyer == contract.buyer
    assert unchanged.counterparty == contract.counterparty


def test_contract_repo_list_all_remains_unaware_of_sales_contract(db_session):
    """Core reason SalesContract is a physically separate object
    (docs/PHASE2D1-R0-DECISIONS.md): `contract_repo.list_all()` — the
    purchase-side matching candidate set — must never be polluted by
    SalesContract rows."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)
    db_session.commit()
    before = {c.id for c in ContractRepository(db_session).list_all()}

    _create(db_session, our_entity="Entity A", sales_contract_no="SC-001", fields={"customer": "Customer Co"})
    _create(db_session, our_entity="Entity B", sales_contract_no="SC-002", fields={})

    after = {c.id for c in ContractRepository(db_session).list_all()}
    assert after == before
    assert contract.id in after


# ---------------------------------------------------------------------------
# Read model
# ---------------------------------------------------------------------------


def test_get_find_by_identity_and_list_are_deterministic(db_session):
    result_a = _create(db_session, our_entity="Entity A", sales_contract_no="SC-A", fields={"customer": "Cust A"})
    result_b = _create(db_session, our_entity="Entity B", sales_contract_no="SC-B", fields={"customer": "Cust B"})

    assert get_sales_contract(db_session, result_a.sales_contract.id).id == result_a.sales_contract.id
    assert find_sales_contract_by_identity(db_session, "Entity A", "SC-A").id == result_a.sales_contract.id
    assert find_sales_contract_by_identity(db_session, "Entity A", "SC-B") is None
    assert find_sales_contract_by_identity(db_session, "Nonexistent", "SC-A") is None

    listed_once = list_sales_contracts(db_session)
    listed_again = list_sales_contracts(db_session)
    assert [sc.id for sc in listed_once] == [sc.id for sc in listed_again]
    assert {sc.id for sc in listed_once} == {result_a.sales_contract.id, result_b.sales_contract.id}


def test_get_sales_contract_history_deterministic_and_full(db_session):
    result = _create(db_session, fields={"customer": "Customer Co"})
    current = SalesContractRepository(db_session).get_current_revision(result.sales_contract.id)
    frag2 = _make_fragment(db_session)
    correct_sales_contract_fact(
        db_session, sales_contract_id=result.sales_contract.id, based_on_revision_id=current.id,
        fields={"customer": "Corrected Co"}, source_fragment_id=frag2.id, created_at=NOW,
    )
    db_session.commit()

    history_once = get_sales_contract_history(db_session, result.sales_contract.id)
    history_again = get_sales_contract_history(db_session, result.sales_contract.id)
    assert [r.id for r in history_once] == [r.id for r in history_again]
    assert len(history_once) == 2


def test_get_history_for_unknown_sales_contract_returns_empty(db_session):
    assert get_sales_contract_history(db_session, uuid.uuid4()) == []
    assert get_sales_contract(db_session, uuid.uuid4()) is None
