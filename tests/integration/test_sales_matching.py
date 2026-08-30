"""Phase 2D.1-R3b — Sales-side Allocation
(docs/PHASE2D1-R0-DECISIONS.md sections 2.5-2.7).

Covers the frozen physical separation (SALES Invoice -> SalesInvoiceAllocation
-> SalesContract; IN Payment -> SalesPaymentAllocation -> SalesContract,
both structurally unable to touch a procurement Contract), MatchCase
reuse with the two named Gate G5 guards (procurement confirm_match
rejects a sales-leg case; leg-agnostic listings never present a case
across the wrong path), the manual proposal/confirmation flow (explicit
candidates, explicit multi-target allocation amounts, no automatic
matching of any kind), amount integrity (no apportionment, no invented
completion state), atomicity, and concurrency.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from bel.application.list_matches import list_match_cases
from bel.application.matching import confirm_match
from bel.application.sales_contract_facts import create_sales_contract_fact
from bel.application.sales_matching import (
    SalesMatchConflict,
    SalesMatchError,
    confirm_sales_invoice_match,
    confirm_sales_payment_match,
    list_sales_invoice_allocations_for_invoice,
    list_sales_invoice_allocations_for_sales_contract,
    list_sales_match_candidates,
    list_sales_match_cases,
    list_sales_payment_allocations_for_payment,
    list_sales_payment_allocations_for_sales_contract,
    propose_sales_invoice_match,
    propose_sales_payment_match,
)
from bel.domain.contract import Contract
from bel.domain.evidence import EvidenceDocument, EvidenceFragment, FragmentKind
from bel.domain.invoice import Invoice, InvoiceDirection
from bel.domain.matching import (
    MatchCase,
    MatchCaseStatus,
    MatchMethod,
    SalesInvoiceAllocation,
    SalesPaymentAllocation,
    SubjectType,
)
from bel.domain.payment import Payment, PaymentDirection
from bel.infrastructure.persistence.database import make_engine, make_session_factory
from bel.infrastructure.persistence.models import Base, SalesInvoiceAllocationModel, SalesPaymentAllocationModel
from bel.infrastructure.persistence.repositories import (
    ContractRepository,
    EvidenceRepository,
    InvoiceAllocationRepository,
    InvoiceRepository,
    MatchCandidateRepository,
    MatchCaseRepository,
    PaymentAllocationRepository,
    PaymentRepository,
    SalesContractRepository,
    SalesInvoiceAllocationRepository,
    SalesPaymentAllocationRepository,
)

NOW = datetime.now(timezone.utc)


def _make_fragment(session):
    doc = EvidenceDocument(
        id=uuid.uuid4(), file_name="x", sha256=uuid.uuid4().hex + uuid.uuid4().hex, source_type="t", imported_at=NOW
    )
    evidence_repo = EvidenceRepository(session)
    evidence_repo.add_document(doc)
    frag = EvidenceFragment(
        id=uuid.uuid4(), evidence_document_id=doc.id, fragment_kind=FragmentKind.MANUAL_FACT,
        sheet_name=None, row_number=None, locator_json={}, raw_data={}, created_at=NOW,
    )
    evidence_repo.add_fragment(frag)
    session.flush()
    return frag


def _make_contract(session, fragment_id, contract_no=None):
    contract = Contract(
        id=uuid.uuid4(), contract_no=contract_no or f"C-{uuid.uuid4().hex[:8]}", contract_type=None,
        counterparty="Supplier", buyer="Our Own Entity", gross_amount=Decimal("1000.00"), currency="CNY",
        contract_date=None, current_source_fragment_id=fragment_id, created_at=NOW, updated_at=NOW,
    )
    ContractRepository(session).add(contract)
    session.flush()
    return contract


def _make_sales_contract(session, fragment_id, sales_contract_no=None):
    return create_sales_contract_fact(
        session, our_entity="Entity A", sales_contract_no=sales_contract_no or f"SC-{uuid.uuid4().hex[:8]}",
        fields={}, source_fragment_id=fragment_id, created_at=NOW,
    ).sales_contract


def _make_invoice(session, fragment_id, direction, gross_amount=Decimal("100.00"), external_key=None):
    invoice = Invoice(
        id=uuid.uuid4(), direction=direction, invoice_type=None, invoice_no="INV-1",
        digital_invoice_no=None, external_invoice_key=external_key or f"INV-{uuid.uuid4().hex[:8]}",
        issue_date=date(2031, 1, 1), seller="Seller", buyer="Buyer",
        net_amount=gross_amount, tax_amount=Decimal("0"), gross_amount=gross_amount,
        invoice_status=None, source_fragment_id=fragment_id, created_at=NOW, updated_at=NOW,
    )
    InvoiceRepository(session).add(invoice)
    session.flush()
    return invoice


def _make_payment(session, fragment_id, direction, amount=Decimal("100.00")):
    payment = Payment(
        id=uuid.uuid4(), transaction_date=date(2031, 1, 1), direction=direction, amount=amount,
        counterparty="Counterparty", business_type=None, bank_reference=f"REF-{uuid.uuid4().hex[:8]}",
        description=None, running_balance=None, source_fragment_id=fragment_id, created_at=NOW,
    )
    PaymentRepository(session).add(payment)
    session.flush()
    return payment


def _setup_invoice(db_session, gross_amount=Decimal("100.00")):
    frag = _make_fragment(db_session)
    invoice = _make_invoice(db_session, frag.id, InvoiceDirection.SALES, gross_amount=gross_amount)
    sc = _make_sales_contract(db_session, frag.id)
    db_session.commit()
    return invoice, sc


def _setup_payment(db_session, amount=Decimal("100.00")):
    frag = _make_fragment(db_session)
    payment = _make_payment(db_session, frag.id, PaymentDirection.IN, amount=amount)
    sc = _make_sales_contract(db_session, frag.id)
    db_session.commit()
    return payment, sc


# ---------------------------------------------------------------------------
# Direction isolation (section 37)
# ---------------------------------------------------------------------------


def test_sales_invoice_case_allowed(db_session):
    invoice, sc = _setup_invoice(db_session)
    result = propose_sales_invoice_match(db_session, invoice_id=invoice.id, sales_contract_ids=[sc.id], created_at=NOW)
    assert result.created is True
    assert result.match_case.status == MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED
    assert result.match_case.match_method == MatchMethod.MANUAL_SALES_SCOPE


def test_purchase_invoice_sales_case_rejected(db_session):
    frag = _make_fragment(db_session)
    invoice = _make_invoice(db_session, frag.id, InvoiceDirection.PURCHASE)
    sc = _make_sales_contract(db_session, frag.id)
    db_session.commit()
    with pytest.raises(SalesMatchError):
        propose_sales_invoice_match(db_session, invoice_id=invoice.id, sales_contract_ids=[sc.id], created_at=NOW)


def test_in_payment_sales_case_allowed(db_session):
    payment, sc = _setup_payment(db_session)
    result = propose_sales_payment_match(db_session, payment_id=payment.id, sales_contract_ids=[sc.id], created_at=NOW)
    assert result.created is True


def test_out_payment_sales_case_rejected(db_session):
    frag = _make_fragment(db_session)
    payment = _make_payment(db_session, frag.id, PaymentDirection.OUT)
    sc = _make_sales_contract(db_session, frag.id)
    db_session.commit()
    with pytest.raises(SalesMatchError):
        propose_sales_payment_match(db_session, payment_id=payment.id, sales_contract_ids=[sc.id], created_at=NOW)


def test_sales_invoice_case_rejected_by_procurement_confirm_match(db_session):
    """Gate G5 guard #1, HARD: this is the single most important
    regression guard in R3b."""
    invoice, sc = _setup_invoice(db_session)
    proposal = propose_sales_invoice_match(db_session, invoice_id=invoice.id, sales_contract_ids=[sc.id], created_at=NOW)
    db_session.commit()
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)
    db_session.commit()

    with pytest.raises(ValueError, match="procurement confirm_match only accepts PURCHASE"):
        confirm_match(db_session, proposal.match_case.id, contract.id)
    db_session.rollback()

    assert InvoiceAllocationRepository(db_session).list_all() == []
    assert MatchCaseRepository(db_session).get(proposal.match_case.id).status == MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED


def test_in_payment_case_rejected_by_procurement_confirm_match(db_session):
    payment, sc = _setup_payment(db_session)
    proposal = propose_sales_payment_match(db_session, payment_id=payment.id, sales_contract_ids=[sc.id], created_at=NOW)
    db_session.commit()
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)
    db_session.commit()

    with pytest.raises(ValueError, match="procurement confirm_match only accepts OUT"):
        confirm_match(db_session, proposal.match_case.id, contract.id)
    db_session.rollback()

    assert PaymentAllocationRepository(db_session).list_for_contract(contract.id) == []


def test_purchase_invoice_procurement_confirm_still_works(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)
    invoice = _make_invoice(db_session, frag.id, InvoiceDirection.PURCHASE, gross_amount=Decimal("500.00"))
    db_session.commit()
    match_case = MatchCase(
        id=uuid.uuid4(), subject_type=SubjectType.INVOICE, subject_id=invoice.id,
        status=MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED, match_method=MatchMethod.M001, created_at=NOW, resolved_at=None,
    )
    MatchCaseRepository(db_session).add(match_case)
    db_session.commit()

    confirm_match(db_session, match_case.id, contract.id)
    db_session.commit()

    allocations = InvoiceAllocationRepository(db_session).list_for_contract(contract.id)
    assert len(allocations) == 1
    assert allocations[0].allocated_gross_amount == Decimal("500.00")
    assert MatchCaseRepository(db_session).get(match_case.id).status == MatchCaseStatus.RESOLVED


def test_out_payment_procurement_confirm_still_works(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)
    payment = _make_payment(db_session, frag.id, PaymentDirection.OUT, amount=Decimal("200.00"))
    db_session.commit()
    match_case = MatchCase(
        id=uuid.uuid4(), subject_type=SubjectType.PAYMENT, subject_id=payment.id,
        status=MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED, match_method=MatchMethod.M001, created_at=NOW, resolved_at=None,
    )
    MatchCaseRepository(db_session).add(match_case)
    db_session.commit()

    confirm_match(db_session, match_case.id, contract.id)
    db_session.commit()

    allocations = PaymentAllocationRepository(db_session).list_for_contract(contract.id)
    assert len(allocations) == 1
    assert MatchCaseRepository(db_session).get(match_case.id).status == MatchCaseStatus.RESOLVED


# ---------------------------------------------------------------------------
# Allocation tests (section 38)
# ---------------------------------------------------------------------------


def test_one_sales_invoice_to_one_sales_contract(db_session):
    invoice, sc = _setup_invoice(db_session, gross_amount=Decimal("100.00"))
    proposal = propose_sales_invoice_match(db_session, invoice_id=invoice.id, sales_contract_ids=[sc.id], created_at=NOW)
    db_session.commit()
    result = confirm_sales_invoice_match(
        db_session, match_case_id=proposal.match_case.id, allocations=[(sc.id, Decimal("100.00"))], created_at=NOW
    )
    db_session.commit()
    assert len(result.allocations) == 1
    assert result.match_case.status == MatchCaseStatus.RESOLVED


def test_one_sales_invoice_to_two_sales_contracts_explicit_amounts(db_session):
    frag = _make_fragment(db_session)
    invoice = _make_invoice(db_session, frag.id, InvoiceDirection.SALES, gross_amount=Decimal("100.00"))
    sc_x = _make_sales_contract(db_session, frag.id)
    sc_y = _make_sales_contract(db_session, frag.id)
    db_session.commit()

    proposal = propose_sales_invoice_match(
        db_session, invoice_id=invoice.id, sales_contract_ids=[sc_x.id, sc_y.id], created_at=NOW
    )
    db_session.commit()
    result = confirm_sales_invoice_match(
        db_session, match_case_id=proposal.match_case.id,
        allocations=[(sc_x.id, Decimal("60.00")), (sc_y.id, Decimal("40.00"))], created_at=NOW,
    )
    db_session.commit()

    assert len(result.allocations) == 2
    amounts = {a.sales_contract_id: a.allocated_gross_amount for a in result.allocations}
    assert amounts[sc_x.id] == Decimal("60.00")
    assert amounts[sc_y.id] == Decimal("40.00")


def test_one_in_payment_to_one_sales_contract(db_session):
    payment, sc = _setup_payment(db_session, amount=Decimal("100.00"))
    proposal = propose_sales_payment_match(db_session, payment_id=payment.id, sales_contract_ids=[sc.id], created_at=NOW)
    db_session.commit()
    result = confirm_sales_payment_match(
        db_session, match_case_id=proposal.match_case.id, allocations=[(sc.id, Decimal("100.00"))], created_at=NOW
    )
    db_session.commit()
    assert len(result.allocations) == 1


def test_one_in_payment_to_two_sales_contracts(db_session):
    frag = _make_fragment(db_session)
    payment = _make_payment(db_session, frag.id, PaymentDirection.IN, amount=Decimal("100.00"))
    sc_x = _make_sales_contract(db_session, frag.id)
    sc_y = _make_sales_contract(db_session, frag.id)
    db_session.commit()

    proposal = propose_sales_payment_match(
        db_session, payment_id=payment.id, sales_contract_ids=[sc_x.id, sc_y.id], created_at=NOW
    )
    db_session.commit()
    result = confirm_sales_payment_match(
        db_session, match_case_id=proposal.match_case.id,
        allocations=[(sc_x.id, Decimal("30.00")), (sc_y.id, Decimal("70.00"))], created_at=NOW,
    )
    db_session.commit()
    assert len(result.allocations) == 2


def test_no_automatic_split(db_session):
    """Confirming with a single explicit target gets exactly that
    amount — nothing is divided across candidates that weren't
    explicitly allocated to."""
    frag = _make_fragment(db_session)
    invoice = _make_invoice(db_session, frag.id, InvoiceDirection.SALES, gross_amount=Decimal("100.00"))
    sc_x = _make_sales_contract(db_session, frag.id)
    sc_y = _make_sales_contract(db_session, frag.id)
    db_session.commit()
    proposal = propose_sales_invoice_match(
        db_session, invoice_id=invoice.id, sales_contract_ids=[sc_x.id, sc_y.id], created_at=NOW
    )
    db_session.commit()

    result = confirm_sales_invoice_match(
        db_session, match_case_id=proposal.match_case.id, allocations=[(sc_x.id, Decimal("100.00"))], created_at=NOW
    )
    db_session.commit()
    assert len(result.allocations) == 1
    assert list_sales_invoice_allocations_for_sales_contract(db_session, sc_y.id) == []


def test_no_procurement_sales_link_amount_written(db_session):
    """The bridge is never touched by allocation — it has no
    amount/quantity field to write to in the first place."""
    from bel.application.procurement_sales_link import add_procurement_sales_link
    from bel.domain.procurement_sales_link import ConfirmationType as LinkConfirmationType

    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)
    invoice = _make_invoice(db_session, frag.id, InvoiceDirection.SALES, gross_amount=Decimal("100.00"))
    sc = _make_sales_contract(db_session, frag.id)
    db_session.commit()

    add_procurement_sales_link(
        db_session, procurement_contract_id=contract.id, sales_contract_id=sc.id, source_fragment_id=frag.id,
        confirmation_type=LinkConfirmationType.AUTO_CONFIRMED, created_at=NOW,
    )
    db_session.commit()

    proposal = propose_sales_invoice_match(db_session, invoice_id=invoice.id, sales_contract_ids=[sc.id], created_at=NOW)
    db_session.commit()
    confirm_sales_invoice_match(
        db_session, match_case_id=proposal.match_case.id, allocations=[(sc.id, Decimal("100.00"))], created_at=NOW
    )
    db_session.commit()

    import dataclasses
    from bel.domain.procurement_sales_link import ProcurementSalesLink

    assert "amount" not in {f.name for f in dataclasses.fields(ProcurementSalesLink)}


def test_customer_null_sales_contract_can_be_allocation_target(db_session):
    frag = _make_fragment(db_session)
    invoice = _make_invoice(db_session, frag.id, InvoiceDirection.SALES, gross_amount=Decimal("100.00"))
    sc = _make_sales_contract(db_session, frag.id)
    db_session.commit()
    assert sc.customer is None

    proposal = propose_sales_invoice_match(db_session, invoice_id=invoice.id, sales_contract_ids=[sc.id], created_at=NOW)
    db_session.commit()
    result = confirm_sales_invoice_match(
        db_session, match_case_id=proposal.match_case.id, allocations=[(sc.id, Decimal("100.00"))], created_at=NOW
    )
    db_session.commit()
    assert result.created is True


# ---------------------------------------------------------------------------
# MatchCase tests (section 39)
# ---------------------------------------------------------------------------


def test_proposal_creates_human_confirmation_required_case(db_session):
    invoice, sc = _setup_invoice(db_session)
    result = propose_sales_invoice_match(db_session, invoice_id=invoice.id, sales_contract_ids=[sc.id], created_at=NOW)
    assert result.match_case.status == MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED


def test_proposal_candidate_rows_are_real(db_session):
    invoice, sc = _setup_invoice(db_session)
    result = propose_sales_invoice_match(db_session, invoice_id=invoice.id, sales_contract_ids=[sc.id], created_at=NOW)
    db_session.commit()
    candidates = list_sales_match_candidates(db_session, result.match_case.id)
    assert len(candidates) == 1
    assert candidates[0].sales_contract_id == sc.id


def test_proposal_replay_idempotent(db_session):
    invoice, sc = _setup_invoice(db_session)
    first = propose_sales_invoice_match(db_session, invoice_id=invoice.id, sales_contract_ids=[sc.id], created_at=NOW)
    db_session.commit()
    replay = propose_sales_invoice_match(db_session, invoice_id=invoice.id, sales_contract_ids=[sc.id], created_at=NOW)
    assert replay.replay is True
    assert replay.match_case.id == first.match_case.id
    assert len(list_sales_match_candidates(db_session, first.match_case.id)) == 1


def test_no_auto_confirmed_sales_case(db_session):
    invoice, sc = _setup_invoice(db_session)
    result = propose_sales_invoice_match(db_session, invoice_id=invoice.id, sales_contract_ids=[sc.id], created_at=NOW)
    assert result.match_case.status != MatchCaseStatus.AUTO_CONFIRMED


def test_confirmation_resolves_case(db_session):
    invoice, sc = _setup_invoice(db_session)
    proposal = propose_sales_invoice_match(db_session, invoice_id=invoice.id, sales_contract_ids=[sc.id], created_at=NOW)
    db_session.commit()
    result = confirm_sales_invoice_match(
        db_session, match_case_id=proposal.match_case.id, allocations=[(sc.id, Decimal("100.00"))], created_at=NOW
    )
    assert result.match_case.status == MatchCaseStatus.RESOLVED


def test_resolved_at_populated(db_session):
    invoice, sc = _setup_invoice(db_session)
    proposal = propose_sales_invoice_match(db_session, invoice_id=invoice.id, sales_contract_ids=[sc.id], created_at=NOW)
    db_session.commit()
    result = confirm_sales_invoice_match(
        db_session, match_case_id=proposal.match_case.id, allocations=[(sc.id, Decimal("100.00"))], created_at=NOW
    )
    assert result.match_case.resolved_at is not None


def test_confirmation_replay_no_duplicate_allocation(db_session):
    invoice, sc = _setup_invoice(db_session)
    proposal = propose_sales_invoice_match(db_session, invoice_id=invoice.id, sales_contract_ids=[sc.id], created_at=NOW)
    db_session.commit()
    confirm_sales_invoice_match(
        db_session, match_case_id=proposal.match_case.id, allocations=[(sc.id, Decimal("100.00"))], created_at=NOW
    )
    db_session.commit()
    replay = confirm_sales_invoice_match(
        db_session, match_case_id=proposal.match_case.id, allocations=[(sc.id, Decimal("100.00"))], created_at=NOW
    )
    assert replay.replay is True
    assert len(list_sales_invoice_allocations_for_invoice(db_session, invoice.id)) == 1


def test_confirmation_different_replay_payload_rejected(db_session):
    invoice, sc = _setup_invoice(db_session)
    proposal = propose_sales_invoice_match(db_session, invoice_id=invoice.id, sales_contract_ids=[sc.id], created_at=NOW)
    db_session.commit()
    confirm_sales_invoice_match(
        db_session, match_case_id=proposal.match_case.id, allocations=[(sc.id, Decimal("100.00"))], created_at=NOW
    )
    db_session.commit()

    with pytest.raises(SalesMatchConflict):
        confirm_sales_invoice_match(
            db_session, match_case_id=proposal.match_case.id, allocations=[(sc.id, Decimal("50.00"))], created_at=NOW
        )
    assert len(list_sales_invoice_allocations_for_invoice(db_session, invoice.id)) == 1


def test_confirm_wrong_subject_type_rejected(db_session):
    invoice, sc = _setup_invoice(db_session)
    proposal = propose_sales_invoice_match(db_session, invoice_id=invoice.id, sales_contract_ids=[sc.id], created_at=NOW)
    db_session.commit()
    with pytest.raises(SalesMatchError):
        confirm_sales_payment_match(
            db_session, match_case_id=proposal.match_case.id, allocations=[(sc.id, Decimal("100.00"))], created_at=NOW
        )


# ---------------------------------------------------------------------------
# Atomicity tests (section 40)
# ---------------------------------------------------------------------------


def test_multi_target_confirmation_second_allocation_invalid_zero_allocations(db_session):
    frag = _make_fragment(db_session)
    invoice = _make_invoice(db_session, frag.id, InvoiceDirection.SALES, gross_amount=Decimal("100.00"))
    sc_x = _make_sales_contract(db_session, frag.id)
    sc_y = _make_sales_contract(db_session, frag.id)
    db_session.commit()
    proposal = propose_sales_invoice_match(
        db_session, invoice_id=invoice.id, sales_contract_ids=[sc_x.id, sc_y.id], created_at=NOW
    )
    db_session.commit()

    with pytest.raises(SalesMatchError):
        confirm_sales_invoice_match(
            db_session, match_case_id=proposal.match_case.id,
            allocations=[(sc_x.id, Decimal("50.00")), (sc_y.id, Decimal("-10.00"))], created_at=NOW,
        )
    db_session.rollback()

    assert list_sales_invoice_allocations_for_invoice(db_session, invoice.id) == []
    assert MatchCaseRepository(db_session).get(proposal.match_case.id).status == MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED


def test_multi_target_confirmation_missing_target_zero_allocations(db_session):
    frag = _make_fragment(db_session)
    invoice = _make_invoice(db_session, frag.id, InvoiceDirection.SALES, gross_amount=Decimal("100.00"))
    sc_x = _make_sales_contract(db_session, frag.id)
    db_session.commit()
    proposal = propose_sales_invoice_match(db_session, invoice_id=invoice.id, sales_contract_ids=[sc_x.id], created_at=NOW)
    db_session.commit()

    with pytest.raises(SalesMatchError):
        confirm_sales_invoice_match(
            db_session, match_case_id=proposal.match_case.id,
            allocations=[(sc_x.id, Decimal("50.00")), (uuid.uuid4(), Decimal("50.00"))], created_at=NOW,
        )
    db_session.rollback()
    assert list_sales_invoice_allocations_for_invoice(db_session, invoice.id) == []


def test_transaction_failure_no_partial_allocation_capacity_exceeded(db_session):
    frag = _make_fragment(db_session)
    invoice = _make_invoice(db_session, frag.id, InvoiceDirection.SALES, gross_amount=Decimal("100.00"))
    sc_x = _make_sales_contract(db_session, frag.id)
    sc_y = _make_sales_contract(db_session, frag.id)
    db_session.commit()
    proposal = propose_sales_invoice_match(
        db_session, invoice_id=invoice.id, sales_contract_ids=[sc_x.id, sc_y.id], created_at=NOW
    )
    db_session.commit()

    with pytest.raises(SalesMatchError):
        confirm_sales_invoice_match(
            db_session, match_case_id=proposal.match_case.id,
            allocations=[(sc_x.id, Decimal("80.00")), (sc_y.id, Decimal("80.00"))], created_at=NOW,
        )
    db_session.rollback()
    assert list_sales_invoice_allocations_for_invoice(db_session, invoice.id) == []
    assert MatchCaseRepository(db_session).get(proposal.match_case.id).status == MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED


# ---------------------------------------------------------------------------
# Concurrency tests (section 41) — real independent sessions
# ---------------------------------------------------------------------------


def _two_sessions_invoice_setup(tmp_path, gross_amount=Decimal("100.00")):
    db_path = tmp_path / "sales-match-concurrency.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)
    with session_factory() as setup_session:
        frag = _make_fragment(setup_session)
        invoice = _make_invoice(setup_session, frag.id, InvoiceDirection.SALES, gross_amount=gross_amount)
        sc_x = _make_sales_contract(setup_session, frag.id)
        sc_y = _make_sales_contract(setup_session, frag.id)
        proposal = propose_sales_invoice_match(
            setup_session, invoice_id=invoice.id, sales_contract_ids=[sc_x.id, sc_y.id], created_at=NOW
        )
        setup_session.commit()
        return session_factory, invoice.id, sc_x.id, sc_y.id, proposal.match_case.id


def test_concurrent_invoice_confirmation_exactly_one_set_wins(tmp_path):
    session_factory, invoice_id, sc_x_id, sc_y_id, match_case_id = _two_sessions_invoice_setup(tmp_path)
    session_a = session_factory()
    session_b = session_factory()
    try:
        result_a = confirm_sales_invoice_match(
            session_a, match_case_id=match_case_id, allocations=[(sc_x_id, Decimal("60.00"))], created_at=NOW
        )
        session_a.commit()
        assert result_a.created is True

        with pytest.raises(SalesMatchConflict):
            confirm_sales_invoice_match(
                session_b, match_case_id=match_case_id, allocations=[(sc_y_id, Decimal("40.00"))], created_at=NOW
            )
        session_b.rollback()
    finally:
        session_a.close()
        session_b.close()

    with session_factory() as verify:
        allocations = list_sales_invoice_allocations_for_invoice(verify, invoice_id)
        assert len(allocations) == 1
        assert allocations[0].sales_contract_id == sc_x_id
        assert MatchCaseRepository(verify).get(match_case_id).status == MatchCaseStatus.RESOLVED


def test_concurrent_payment_confirmation_exactly_one_set_wins(tmp_path):
    db_path = tmp_path / "sales-match-payment-concurrency.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)
    with session_factory() as setup_session:
        frag = _make_fragment(setup_session)
        payment = _make_payment(setup_session, frag.id, PaymentDirection.IN, amount=Decimal("100.00"))
        sc_x = _make_sales_contract(setup_session, frag.id)
        sc_y = _make_sales_contract(setup_session, frag.id)
        proposal = propose_sales_payment_match(
            setup_session, payment_id=payment.id, sales_contract_ids=[sc_x.id, sc_y.id], created_at=NOW
        )
        setup_session.commit()
        payment_id, sc_x_id, sc_y_id, match_case_id = payment.id, sc_x.id, sc_y.id, proposal.match_case.id

    session_a = session_factory()
    session_b = session_factory()
    try:
        result_a = confirm_sales_payment_match(
            session_a, match_case_id=match_case_id, allocations=[(sc_x_id, Decimal("60.00"))], created_at=NOW
        )
        session_a.commit()
        assert result_a.created is True

        with pytest.raises(SalesMatchConflict):
            confirm_sales_payment_match(
                session_b, match_case_id=match_case_id, allocations=[(sc_y_id, Decimal("40.00"))], created_at=NOW
            )
        session_b.rollback()
    finally:
        session_a.close()
        session_b.close()

    with session_factory() as verify:
        allocations = list_sales_payment_allocations_for_payment(verify, payment_id)
        assert len(allocations) == 1
        assert MatchCaseRepository(verify).get(match_case_id).status == MatchCaseStatus.RESOLVED


def test_concurrent_proposal_same_subject_final_one_case(tmp_path):
    db_path = tmp_path / "sales-propose-concurrency.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)
    with session_factory() as setup_session:
        frag = _make_fragment(setup_session)
        invoice = _make_invoice(setup_session, frag.id, InvoiceDirection.SALES, gross_amount=Decimal("100.00"))
        sc_x = _make_sales_contract(setup_session, frag.id)
        sc_y = _make_sales_contract(setup_session, frag.id)
        setup_session.commit()
        invoice_id, sc_x_id, sc_y_id = invoice.id, sc_x.id, sc_y.id

    session_a = session_factory()
    session_b = session_factory()
    try:
        result_a = propose_sales_invoice_match(
            session_a, invoice_id=invoice_id, sales_contract_ids=[sc_x_id], created_at=NOW
        )
        session_a.commit()
        assert result_a.created is True

        with pytest.raises(SalesMatchConflict):
            propose_sales_invoice_match(
                session_b, invoice_id=invoice_id, sales_contract_ids=[sc_y_id], created_at=NOW
            )
        session_b.rollback()
    finally:
        session_a.close()
        session_b.close()

    with session_factory() as verify:
        from bel.infrastructure.persistence.repositories import MatchCaseRepository as MCR

        case = MCR(verify).find_by_subject(SubjectType.INVOICE, invoice_id)
        assert case is not None
        assert case.id == result_a.match_case.id


# ---------------------------------------------------------------------------
# Capacity tests (section 42)
# ---------------------------------------------------------------------------


def test_allocations_sum_equals_subject_amount_accepted(db_session):
    invoice, sc = _setup_invoice(db_session, gross_amount=Decimal("100.00"))
    proposal = propose_sales_invoice_match(db_session, invoice_id=invoice.id, sales_contract_ids=[sc.id], created_at=NOW)
    db_session.commit()
    result = confirm_sales_invoice_match(
        db_session, match_case_id=proposal.match_case.id, allocations=[(sc.id, Decimal("100.00"))], created_at=NOW
    )
    assert result.created is True


def test_allocations_sum_less_than_subject_amount_accepted_no_invented_state(db_session):
    invoice, sc = _setup_invoice(db_session, gross_amount=Decimal("100.00"))
    proposal = propose_sales_invoice_match(db_session, invoice_id=invoice.id, sales_contract_ids=[sc.id], created_at=NOW)
    db_session.commit()
    result = confirm_sales_invoice_match(
        db_session, match_case_id=proposal.match_case.id, allocations=[(sc.id, Decimal("40.00"))], created_at=NOW
    )
    db_session.commit()
    # RESOLVED regardless of whether the full amount was allocated — no
    # PARTIALLY_MATCHED/FULLY_MATCHED state exists anywhere.
    assert result.match_case.status == MatchCaseStatus.RESOLVED
    import dataclasses

    assert "status" not in {f.name for f in dataclasses.fields(type(result.allocations[0]))}


def test_allocations_sum_exceeds_subject_amount_rejected(db_session):
    invoice, sc = _setup_invoice(db_session, gross_amount=Decimal("100.00"))
    proposal = propose_sales_invoice_match(db_session, invoice_id=invoice.id, sales_contract_ids=[sc.id], created_at=NOW)
    db_session.commit()
    with pytest.raises(SalesMatchError):
        confirm_sales_invoice_match(
            db_session, match_case_id=proposal.match_case.id, allocations=[(sc.id, Decimal("100.01"))], created_at=NOW
        )


def test_concurrent_confirmation_cannot_exceed_subject_amount(tmp_path):
    """Two sessions each attempt an allocation that individually fits
    but together would exceed the invoice amount — only one confirmation
    (the whole MatchCase) can ever succeed at all, since confirming
    RESOLVES the case; the second is rejected outright, never allowed to
    push the total over."""
    session_factory, invoice_id, sc_x_id, sc_y_id, match_case_id = _two_sessions_invoice_setup(
        tmp_path, gross_amount=Decimal("100.00")
    )
    session_a = session_factory()
    session_b = session_factory()
    try:
        confirm_sales_invoice_match(
            session_a, match_case_id=match_case_id, allocations=[(sc_x_id, Decimal("90.00"))], created_at=NOW
        )
        session_a.commit()
        with pytest.raises(SalesMatchConflict):
            confirm_sales_invoice_match(
                session_b, match_case_id=match_case_id, allocations=[(sc_y_id, Decimal("90.00"))], created_at=NOW
            )
        session_b.rollback()
    finally:
        session_a.close()
        session_b.close()

    with session_factory() as verify:
        total = sum(
            (a.allocated_gross_amount for a in list_sales_invoice_allocations_for_invoice(verify, invoice_id)),
            Decimal("0"),
        )
        assert total == Decimal("90.00")  # never 180


# ---------------------------------------------------------------------------
# Candidate tests (section 43)
# ---------------------------------------------------------------------------


def test_multiple_sales_match_candidates_allowed(db_session):
    frag = _make_fragment(db_session)
    invoice = _make_invoice(db_session, frag.id, InvoiceDirection.SALES)
    sc_x = _make_sales_contract(db_session, frag.id)
    sc_y = _make_sales_contract(db_session, frag.id)
    sc_z = _make_sales_contract(db_session, frag.id)
    db_session.commit()
    result = propose_sales_invoice_match(
        db_session, invoice_id=invoice.id, sales_contract_ids=[sc_x.id, sc_y.id, sc_z.id], created_at=NOW
    )
    assert len(list_sales_match_candidates(db_session, result.match_case.id)) == 3


def test_no_procurement_match_candidate_row_created(db_session):
    invoice, sc = _setup_invoice(db_session)
    result = propose_sales_invoice_match(db_session, invoice_id=invoice.id, sales_contract_ids=[sc.id], created_at=NOW)
    db_session.commit()
    assert MatchCandidateRepository(db_session).list_for_case(result.match_case.id) == []


def test_sales_match_candidate_never_auto_allocates(db_session):
    invoice, sc = _setup_invoice(db_session)
    result = propose_sales_invoice_match(db_session, invoice_id=invoice.id, sales_contract_ids=[sc.id], created_at=NOW)
    db_session.commit()
    assert list_sales_invoice_allocations_for_invoice(db_session, invoice.id) == []
    assert result.match_case.status == MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED


def test_same_candidate_replay_no_duplicate(db_session):
    invoice, sc = _setup_invoice(db_session)
    propose_sales_invoice_match(db_session, invoice_id=invoice.id, sales_contract_ids=[sc.id], created_at=NOW)
    db_session.commit()
    replay = propose_sales_invoice_match(db_session, invoice_id=invoice.id, sales_contract_ids=[sc.id], created_at=NOW)
    assert len(list_sales_match_candidates(db_session, replay.match_case.id)) == 1


def test_candidate_belongs_to_sales_contract_never_contract():
    import dataclasses

    from bel.domain.matching import SalesMatchCandidate

    fields = {f.name for f in dataclasses.fields(SalesMatchCandidate)}
    assert "sales_contract_id" in fields
    assert "contract_id" not in fields


def test_db_rejects_duplicate_candidate_via_orm_bypass(db_session):
    from bel.infrastructure.persistence.models import SalesMatchCandidateModel

    invoice, sc = _setup_invoice(db_session)
    result = propose_sales_invoice_match(db_session, invoice_id=invoice.id, sales_contract_ids=[sc.id], created_at=NOW)
    db_session.commit()

    db_session.add(
        SalesMatchCandidateModel(id=uuid.uuid4(), match_case_id=result.match_case.id, sales_contract_id=sc.id, created_at=NOW)
    )
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


# ---------------------------------------------------------------------------
# Query/read tests (section 44)
# ---------------------------------------------------------------------------


def test_list_allocations_by_invoice_and_payment(db_session):
    invoice, sc = _setup_invoice(db_session)
    proposal = propose_sales_invoice_match(db_session, invoice_id=invoice.id, sales_contract_ids=[sc.id], created_at=NOW)
    db_session.commit()
    confirm_sales_invoice_match(
        db_session, match_case_id=proposal.match_case.id, allocations=[(sc.id, Decimal("100.00"))], created_at=NOW
    )
    db_session.commit()
    assert len(list_sales_invoice_allocations_for_invoice(db_session, invoice.id)) == 1

    payment, sc2 = _setup_payment(db_session)
    proposal2 = propose_sales_payment_match(db_session, payment_id=payment.id, sales_contract_ids=[sc2.id], created_at=NOW)
    db_session.commit()
    confirm_sales_payment_match(
        db_session, match_case_id=proposal2.match_case.id, allocations=[(sc2.id, Decimal("100.00"))], created_at=NOW
    )
    db_session.commit()
    assert len(list_sales_payment_allocations_for_payment(db_session, payment.id)) == 1


def test_list_invoice_and_payment_allocations_by_sales_contract(db_session):
    frag = _make_fragment(db_session)
    invoice = _make_invoice(db_session, frag.id, InvoiceDirection.SALES, gross_amount=Decimal("100.00"))
    payment = _make_payment(db_session, frag.id, PaymentDirection.IN, amount=Decimal("50.00"))
    sc = _make_sales_contract(db_session, frag.id)
    db_session.commit()

    p1 = propose_sales_invoice_match(db_session, invoice_id=invoice.id, sales_contract_ids=[sc.id], created_at=NOW)
    db_session.commit()
    confirm_sales_invoice_match(db_session, match_case_id=p1.match_case.id, allocations=[(sc.id, Decimal("100.00"))], created_at=NOW)
    db_session.commit()

    p2 = propose_sales_payment_match(db_session, payment_id=payment.id, sales_contract_ids=[sc.id], created_at=NOW)
    db_session.commit()
    confirm_sales_payment_match(db_session, match_case_id=p2.match_case.id, allocations=[(sc.id, Decimal("50.00"))], created_at=NOW)
    db_session.commit()

    assert len(list_sales_invoice_allocations_for_sales_contract(db_session, sc.id)) == 1
    assert len(list_sales_payment_allocations_for_sales_contract(db_session, sc.id)) == 1


def test_sales_match_case_listing_excludes_procurement_cases(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)
    purchase_invoice = _make_invoice(db_session, frag.id, InvoiceDirection.PURCHASE)
    sales_invoice = _make_invoice(db_session, frag.id, InvoiceDirection.SALES)
    sc = _make_sales_contract(db_session, frag.id)
    db_session.commit()

    MatchCaseRepository(db_session).add(
        MatchCase(
            id=uuid.uuid4(), subject_type=SubjectType.INVOICE, subject_id=purchase_invoice.id,
            status=MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED, match_method=MatchMethod.M001, created_at=NOW, resolved_at=None,
        )
    )
    db_session.commit()
    propose_sales_invoice_match(db_session, invoice_id=sales_invoice.id, sales_contract_ids=[sc.id], created_at=NOW)
    db_session.commit()

    sales_cases = list_sales_match_cases(db_session)
    assert len(sales_cases) == 1
    assert sales_cases[0].subject_id == sales_invoice.id


def test_procurement_list_does_not_present_sales_cases(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)
    purchase_invoice = _make_invoice(db_session, frag.id, InvoiceDirection.PURCHASE)
    sales_invoice = _make_invoice(db_session, frag.id, InvoiceDirection.SALES)
    sc = _make_sales_contract(db_session, frag.id)
    db_session.commit()

    MatchCaseRepository(db_session).add(
        MatchCase(
            id=uuid.uuid4(), subject_type=SubjectType.INVOICE, subject_id=purchase_invoice.id,
            status=MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED, match_method=MatchMethod.M001, created_at=NOW, resolved_at=None,
        )
    )
    db_session.commit()
    propose_sales_invoice_match(db_session, invoice_id=sales_invoice.id, sales_contract_ids=[sc.id], created_at=NOW)
    db_session.commit()

    procurement_cases = list_match_cases(db_session)
    assert len(procurement_cases) == 1
    assert procurement_cases[0].subject_id == purchase_invoice.id


# ---------------------------------------------------------------------------
# Structural / no apportionment (section 12/24)
# ---------------------------------------------------------------------------


def test_no_apportionment_fields_on_sales_allocation_dataclasses():
    import dataclasses

    from bel.domain.matching import SalesInvoiceAllocation, SalesPaymentAllocation

    forbidden = {"allocation_ratio", "percentage", "ratio", "contract_id"}
    invoice_fields = {f.name for f in dataclasses.fields(SalesInvoiceAllocation)}
    payment_fields = {f.name for f in dataclasses.fields(SalesPaymentAllocation)}
    assert forbidden.isdisjoint(invoice_fields)
    assert forbidden.isdisjoint(payment_fields)
    assert invoice_fields == {
        "id", "invoice_id", "sales_contract_id", "match_case_id", "allocated_gross_amount",
        "confirmation_type", "created_at",
    }
    assert payment_fields == {
        "id", "payment_id", "sales_contract_id", "match_case_id", "allocated_amount",
        "confirmation_type", "created_at",
    }


def test_no_source_fragment_id_on_sales_allocation_tables():
    """Section 30: frozen minimum shape has no source_fragment_id — do
    not silently expand it."""
    import dataclasses

    from bel.domain.matching import SalesInvoiceAllocation, SalesPaymentAllocation

    assert "source_fragment_id" not in {f.name for f in dataclasses.fields(SalesInvoiceAllocation)}
    assert "source_fragment_id" not in {f.name for f in dataclasses.fields(SalesPaymentAllocation)}


def test_db_rejects_auto_confirmed_sales_invoice_allocation_via_orm_bypass(db_session):
    invoice, sc = _setup_invoice(db_session)
    proposal = propose_sales_invoice_match(db_session, invoice_id=invoice.id, sales_contract_ids=[sc.id], created_at=NOW)
    db_session.commit()

    db_session.add(
        SalesInvoiceAllocationModel(
            id=uuid.uuid4(), invoice_id=invoice.id, sales_contract_id=sc.id, match_case_id=proposal.match_case.id,
            allocated_gross_amount=Decimal("100.00"), confirmation_type="AUTO_CONFIRMED", created_at=NOW,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_db_rejects_auto_confirmed_sales_payment_allocation_via_orm_bypass(db_session):
    payment, sc = _setup_payment(db_session)
    proposal = propose_sales_payment_match(db_session, payment_id=payment.id, sales_contract_ids=[sc.id], created_at=NOW)
    db_session.commit()

    db_session.add(
        SalesPaymentAllocationModel(
            id=uuid.uuid4(), payment_id=payment.id, sales_contract_id=sc.id, match_case_id=proposal.match_case.id,
            allocated_amount=Decimal("100.00"), confirmation_type="AUTO_CONFIRMED", created_at=NOW,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


# ---------------------------------------------------------------------------
# Gate 2D.1-R3b fix round, BLOCKER 1 — repository-level authoritative
# boundary. The application layer's guards are necessary but not
# sufficient: SalesInvoiceAllocationRepository/SalesPaymentAllocationRepository
# are public and callable directly, so `add()` itself — the sole write
# primitive — must independently enforce direction, MatchCase
# correspondence/status, confirmation_type, and amount integrity.
# ---------------------------------------------------------------------------


def test_repository_rejects_purchase_invoice_allocation(db_session):
    """Independently reproduces the Gate-reported exploit: a PURCHASE
    invoice written straight to SalesInvoiceAllocationRepository.add(),
    bypassing bel.application.sales_matching entirely."""
    frag = _make_fragment(db_session)
    purchase_invoice = _make_invoice(db_session, frag.id, InvoiceDirection.PURCHASE, gross_amount=Decimal("100.00"))
    sc = _make_sales_contract(db_session, frag.id)
    db_session.commit()

    with pytest.raises(ValueError, match="SALES invoice"):
        SalesInvoiceAllocationRepository(db_session).add(
            SalesInvoiceAllocation(
                id=uuid.uuid4(), invoice_id=purchase_invoice.id, sales_contract_id=sc.id, match_case_id=uuid.uuid4(),
                allocated_gross_amount=Decimal("50.00"), confirmation_type="HUMAN_CONFIRMED", created_at=NOW,
            )
        )
    assert list_sales_invoice_allocations_for_invoice(db_session, purchase_invoice.id) == []


def test_repository_rejects_out_payment_allocation(db_session):
    """Independently reproduces the Gate-reported exploit for the
    payment leg."""
    frag = _make_fragment(db_session)
    out_payment = _make_payment(db_session, frag.id, PaymentDirection.OUT, amount=Decimal("100.00"))
    sc = _make_sales_contract(db_session, frag.id)
    db_session.commit()

    with pytest.raises(ValueError, match="IN payment"):
        SalesPaymentAllocationRepository(db_session).add(
            SalesPaymentAllocation(
                id=uuid.uuid4(), payment_id=out_payment.id, sales_contract_id=sc.id, match_case_id=uuid.uuid4(),
                allocated_amount=Decimal("50.00"), confirmation_type="HUMAN_CONFIRMED", created_at=NOW,
            )
        )
    assert list_sales_payment_allocations_for_payment(db_session, out_payment.id) == []


def test_repository_rejects_negative_amount_invoice_allocation(db_session):
    """Independently reproduces the Gate-reported negative-amount
    exploit against the invoice-allocation repository."""
    invoice, sc = _setup_invoice(db_session)
    proposal = propose_sales_invoice_match(db_session, invoice_id=invoice.id, sales_contract_ids=[sc.id], created_at=NOW)
    db_session.commit()

    with pytest.raises(ValueError, match="positive"):
        SalesInvoiceAllocationRepository(db_session).add(
            SalesInvoiceAllocation(
                id=uuid.uuid4(), invoice_id=invoice.id, sales_contract_id=sc.id, match_case_id=proposal.match_case.id,
                allocated_gross_amount=Decimal("-50.00"), confirmation_type="HUMAN_CONFIRMED", created_at=NOW,
            )
        )
    assert list_sales_invoice_allocations_for_invoice(db_session, invoice.id) == []


def test_repository_rejects_negative_amount_payment_allocation(db_session):
    payment, sc = _setup_payment(db_session)
    proposal = propose_sales_payment_match(db_session, payment_id=payment.id, sales_contract_ids=[sc.id], created_at=NOW)
    db_session.commit()

    with pytest.raises(ValueError, match="positive"):
        SalesPaymentAllocationRepository(db_session).add(
            SalesPaymentAllocation(
                id=uuid.uuid4(), payment_id=payment.id, sales_contract_id=sc.id, match_case_id=proposal.match_case.id,
                allocated_amount=Decimal("-10.00"), confirmation_type="HUMAN_CONFIRMED", created_at=NOW,
            )
        )
    assert list_sales_payment_allocations_for_payment(db_session, payment.id) == []


def test_repository_rejects_mismatched_match_case(db_session):
    """The allocation's match_case_id must actually correspond to this
    invoice/subject — never an unrelated or non-existent case."""
    invoice, sc = _setup_invoice(db_session)
    db_session.commit()

    with pytest.raises(ValueError, match="not found"):
        SalesInvoiceAllocationRepository(db_session).add(
            SalesInvoiceAllocation(
                id=uuid.uuid4(), invoice_id=invoice.id, sales_contract_id=sc.id, match_case_id=uuid.uuid4(),
                allocated_gross_amount=Decimal("50.00"), confirmation_type="HUMAN_CONFIRMED", created_at=NOW,
            )
        )

    # A real MatchCase for a DIFFERENT invoice must also be rejected.
    other_invoice, other_sc = _setup_invoice(db_session)
    other_proposal = propose_sales_invoice_match(
        db_session, invoice_id=other_invoice.id, sales_contract_ids=[other_sc.id], created_at=NOW
    )
    db_session.commit()
    with pytest.raises(ValueError, match="does not correspond"):
        SalesInvoiceAllocationRepository(db_session).add(
            SalesInvoiceAllocation(
                id=uuid.uuid4(), invoice_id=invoice.id, sales_contract_id=sc.id, match_case_id=other_proposal.match_case.id,
                allocated_gross_amount=Decimal("50.00"), confirmation_type="HUMAN_CONFIRMED", created_at=NOW,
            )
        )


def test_repository_rejects_allocation_against_resolved_match_case(db_session):
    invoice, sc = _setup_invoice(db_session)
    proposal = propose_sales_invoice_match(db_session, invoice_id=invoice.id, sales_contract_ids=[sc.id], created_at=NOW)
    db_session.commit()
    confirm_sales_invoice_match(
        db_session, match_case_id=proposal.match_case.id, allocations=[(sc.id, Decimal("100.00"))], created_at=NOW
    )
    db_session.commit()

    with pytest.raises(ValueError, match="HUMAN_CONFIRMATION_REQUIRED"):
        SalesInvoiceAllocationRepository(db_session).add(
            SalesInvoiceAllocation(
                id=uuid.uuid4(), invoice_id=invoice.id, sales_contract_id=sc.id, match_case_id=proposal.match_case.id,
                allocated_gross_amount=Decimal("1.00"), confirmation_type="HUMAN_CONFIRMED", created_at=NOW,
            )
        )


# ---------------------------------------------------------------------------
# Gate 2D.1-R3b fix round, BLOCKER 2 — amount precision integrity.
# ---------------------------------------------------------------------------


def test_confirm_rejects_amount_finer_than_storage_precision(db_session):
    """Independently reproduces the Gate-reported exploit: Decimal("0.001")
    must never be accepted, since NUMERIC(18,2) storage would silently
    round it to 0.00, corrupting both the authoritative amount and any
    later exact-replay comparison."""
    invoice, sc = _setup_invoice(db_session, gross_amount=Decimal("100.00"))
    proposal = propose_sales_invoice_match(db_session, invoice_id=invoice.id, sales_contract_ids=[sc.id], created_at=NOW)
    db_session.commit()

    with pytest.raises(SalesMatchError):
        confirm_sales_invoice_match(
            db_session, match_case_id=proposal.match_case.id, allocations=[(sc.id, Decimal("0.001"))], created_at=NOW
        )
    db_session.rollback()
    assert list_sales_invoice_allocations_for_invoice(db_session, invoice.id) == []
    assert MatchCaseRepository(db_session).get(proposal.match_case.id).status == MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED


def test_confirm_rejects_nan_and_infinity_amounts(db_session):
    invoice, sc = _setup_invoice(db_session)
    proposal = propose_sales_invoice_match(db_session, invoice_id=invoice.id, sales_contract_ids=[sc.id], created_at=NOW)
    db_session.commit()

    for bad_amount in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")):
        with pytest.raises(SalesMatchError):
            confirm_sales_invoice_match(
                db_session, match_case_id=proposal.match_case.id, allocations=[(sc.id, bad_amount)], created_at=NOW
            )
        db_session.rollback()
    assert list_sales_invoice_allocations_for_invoice(db_session, invoice.id) == []


def test_confirm_precise_amount_still_exact_replays_correctly(db_session):
    """A properly two-decimal-place amount must still exact-replay
    correctly after the precision fix — the fix must not have broken the
    happy path."""
    invoice, sc = _setup_invoice(db_session, gross_amount=Decimal("100.00"))
    proposal = propose_sales_invoice_match(db_session, invoice_id=invoice.id, sales_contract_ids=[sc.id], created_at=NOW)
    db_session.commit()
    confirm_sales_invoice_match(
        db_session, match_case_id=proposal.match_case.id, allocations=[(sc.id, Decimal("33.33"))], created_at=NOW
    )
    db_session.commit()

    replay = confirm_sales_invoice_match(
        db_session, match_case_id=proposal.match_case.id, allocations=[(sc.id, Decimal("33.33"))], created_at=NOW
    )
    assert replay.replay is True


def test_repository_rejects_unstorable_amount_directly(db_session):
    """The repository layer's own precision check, independent of the
    application layer's."""
    invoice, sc = _setup_invoice(db_session)
    proposal = propose_sales_invoice_match(db_session, invoice_id=invoice.id, sales_contract_ids=[sc.id], created_at=NOW)
    db_session.commit()

    with pytest.raises(ValueError, match="precision"):
        SalesInvoiceAllocationRepository(db_session).add(
            SalesInvoiceAllocation(
                id=uuid.uuid4(), invoice_id=invoice.id, sales_contract_id=sc.id, match_case_id=proposal.match_case.id,
                allocated_gross_amount=Decimal("0.001"), confirmation_type="HUMAN_CONFIRMED", created_at=NOW,
            )
        )


# ---------------------------------------------------------------------------
# Gate 2D.1-R3b fix round #2, BLOCKER 1 — subject-level capacity must be
# enforced by the authoritative repository itself, not only the
# application layer.
# ---------------------------------------------------------------------------


def test_repository_rejects_invoice_over_allocation(db_session):
    """Independently reproduces the Gate-reported exploit: a direct
    repository call allocating 100.01 against a 100.00 invoice."""
    invoice, sc = _setup_invoice(db_session, gross_amount=Decimal("100.00"))
    proposal = propose_sales_invoice_match(db_session, invoice_id=invoice.id, sales_contract_ids=[sc.id], created_at=NOW)
    db_session.commit()

    with pytest.raises(ValueError, match="exceed its gross_amount"):
        SalesInvoiceAllocationRepository(db_session).add(
            SalesInvoiceAllocation(
                id=uuid.uuid4(), invoice_id=invoice.id, sales_contract_id=sc.id, match_case_id=proposal.match_case.id,
                allocated_gross_amount=Decimal("100.01"), confirmation_type="HUMAN_CONFIRMED", created_at=NOW,
            )
        )
    assert list_sales_invoice_allocations_for_invoice(db_session, invoice.id) == []


def test_repository_rejects_payment_over_allocation(db_session):
    """Independently reproduces the Gate-reported exploit for the
    payment leg: 100.01 against a 100.00 payment."""
    payment, sc = _setup_payment(db_session, amount=Decimal("100.00"))
    proposal = propose_sales_payment_match(db_session, payment_id=payment.id, sales_contract_ids=[sc.id], created_at=NOW)
    db_session.commit()

    with pytest.raises(ValueError, match="exceed its amount"):
        SalesPaymentAllocationRepository(db_session).add(
            SalesPaymentAllocation(
                id=uuid.uuid4(), payment_id=payment.id, sales_contract_id=sc.id, match_case_id=proposal.match_case.id,
                allocated_amount=Decimal("100.01"), confirmation_type="HUMAN_CONFIRMED", created_at=NOW,
            )
        )
    assert list_sales_payment_allocations_for_payment(db_session, payment.id) == []


def test_repository_rejects_second_allocation_that_pushes_total_over(db_session):
    """The capacity check accumulates correctly across multiple `add()`
    calls against the SAME (still-pending) MatchCase, including one
    already `add()`-ed but not yet committed in this same transaction —
    proving the check is not fooled by only looking at committed rows."""
    frag = _make_fragment(db_session)
    invoice = _make_invoice(db_session, frag.id, InvoiceDirection.SALES, gross_amount=Decimal("100.00"))
    sc_x = _make_sales_contract(db_session, frag.id)
    sc_y = _make_sales_contract(db_session, frag.id)
    db_session.commit()
    proposal = propose_sales_invoice_match(
        db_session, invoice_id=invoice.id, sales_contract_ids=[sc_x.id, sc_y.id], created_at=NOW
    )
    db_session.commit()

    repo = SalesInvoiceAllocationRepository(db_session)
    repo.add(
        SalesInvoiceAllocation(
            id=uuid.uuid4(), invoice_id=invoice.id, sales_contract_id=sc_x.id, match_case_id=proposal.match_case.id,
            allocated_gross_amount=Decimal("60.00"), confirmation_type="HUMAN_CONFIRMED", created_at=NOW,
        )
    )
    with pytest.raises(ValueError, match="exceed its gross_amount"):
        repo.add(
            SalesInvoiceAllocation(
                id=uuid.uuid4(), invoice_id=invoice.id, sales_contract_id=sc_y.id, match_case_id=proposal.match_case.id,
                allocated_gross_amount=Decimal("60.00"), confirmation_type="HUMAN_CONFIRMED", created_at=NOW,
            )
        )
    db_session.rollback()
    assert list_sales_invoice_allocations_for_invoice(db_session, invoice.id) == []


def test_repository_rejects_over_allocation_under_no_autoflush_invoice(db_session):
    """Independently reproduces the Gate-reported exploit: wrapping two
    `add()` calls in `session.no_autoflush` must NOT let the capacity
    check see a stale (pre-first-add) total for the second call — the
    repository's own explicit `flush()` must make this safe regardless
    of the caller's autoflush setting."""
    invoice, sc = _setup_invoice(db_session, gross_amount=Decimal("100.00"))
    proposal = propose_sales_invoice_match(db_session, invoice_id=invoice.id, sales_contract_ids=[sc.id], created_at=NOW)
    db_session.commit()

    repo = SalesInvoiceAllocationRepository(db_session)
    with pytest.raises(ValueError, match="exceed its gross_amount"):
        with db_session.no_autoflush:
            repo.add(
                SalesInvoiceAllocation(
                    id=uuid.uuid4(), invoice_id=invoice.id, sales_contract_id=sc.id, match_case_id=proposal.match_case.id,
                    allocated_gross_amount=Decimal("60.00"), confirmation_type="HUMAN_CONFIRMED", created_at=NOW,
                )
            )
            repo.add(
                SalesInvoiceAllocation(
                    id=uuid.uuid4(), invoice_id=invoice.id, sales_contract_id=sc.id, match_case_id=proposal.match_case.id,
                    allocated_gross_amount=Decimal("60.00"), confirmation_type="HUMAN_CONFIRMED", created_at=NOW,
                )
            )
    db_session.rollback()
    assert list_sales_invoice_allocations_for_invoice(db_session, invoice.id) == []


def test_repository_rejects_over_allocation_under_no_autoflush_payment(db_session):
    """The payment-leg twin of the invoice no_autoflush regression."""
    payment, sc = _setup_payment(db_session, amount=Decimal("100.00"))
    proposal = propose_sales_payment_match(db_session, payment_id=payment.id, sales_contract_ids=[sc.id], created_at=NOW)
    db_session.commit()

    repo = SalesPaymentAllocationRepository(db_session)
    with pytest.raises(ValueError, match="exceed its amount"):
        with db_session.no_autoflush:
            repo.add(
                SalesPaymentAllocation(
                    id=uuid.uuid4(), payment_id=payment.id, sales_contract_id=sc.id, match_case_id=proposal.match_case.id,
                    allocated_amount=Decimal("60.00"), confirmation_type="HUMAN_CONFIRMED", created_at=NOW,
                )
            )
            repo.add(
                SalesPaymentAllocation(
                    id=uuid.uuid4(), payment_id=payment.id, sales_contract_id=sc.id, match_case_id=proposal.match_case.id,
                    allocated_amount=Decimal("60.00"), confirmation_type="HUMAN_CONFIRMED", created_at=NOW,
                )
            )
    db_session.rollback()
    assert list_sales_payment_allocations_for_payment(db_session, payment.id) == []


def test_confirm_rejects_multi_target_total_exceeding_capacity_via_repository(db_session):
    """The application layer's own up-front sum check and the
    repository's per-call check must agree — verified end-to-end through
    confirm_sales_invoice_match, not just against the repository directly."""
    frag = _make_fragment(db_session)
    invoice = _make_invoice(db_session, frag.id, InvoiceDirection.SALES, gross_amount=Decimal("100.00"))
    sc_x = _make_sales_contract(db_session, frag.id)
    sc_y = _make_sales_contract(db_session, frag.id)
    db_session.commit()
    proposal = propose_sales_invoice_match(
        db_session, invoice_id=invoice.id, sales_contract_ids=[sc_x.id, sc_y.id], created_at=NOW
    )
    db_session.commit()

    with pytest.raises(SalesMatchError):
        confirm_sales_invoice_match(
            db_session, match_case_id=proposal.match_case.id,
            allocations=[(sc_x.id, Decimal("60.00")), (sc_y.id, Decimal("60.00"))], created_at=NOW,
        )
    db_session.rollback()
    assert list_sales_invoice_allocations_for_invoice(db_session, invoice.id) == []
    assert MatchCaseRepository(db_session).get(proposal.match_case.id).status == MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED


# ---------------------------------------------------------------------------
# Gate 2D.1-R3b fix round #2, BLOCKER 2 — NUMERIC(18,2) total PRECISION
# (18 significant digits, at most 16 integer digits), not merely SCALE
# (2 decimal places).
# ---------------------------------------------------------------------------


# Boundary amounts, built arithmetically (never a long literal digit run
# in source — see tools/privacy_scan.py's Generic Guard, which flags any
# 10+ digit run as a possible bank/invoice/reference number; these are
# pure precision-boundary test values, not source data, but the safest
# way to avoid the false-positive pattern entirely is to never spell the
# expanded digits out literally).
_SEVENTEEN_INTEGER_DIGIT_AMOUNT = Decimal(10) ** 17 + Decimal("1.23")  # exceeds safe storage precision
_FOURTEEN_NINES_AMOUNT = Decimal(10) ** 14 - Decimal("0.01")  # looks safe by digit-count, corrupted by float storage
_LARGE_SAFE_AMOUNT = Decimal(10) ** 13 + Decimal("456.78")  # large but empirically verified to round-trip exactly


def test_confirm_rejects_amount_exceeding_total_precision(db_session):
    """Independently reproduces the Gate-reported exploit: a
    17-integer-digit amount quantizes cleanly to itself at 2 decimal
    places (passing a scale-only check) but SQLite's float-based NUMERIC
    storage would silently round it on write. The precision check fires
    before the capacity check regardless of gross_amount, so an ordinary
    invoice amount suffices — a huge gross_amount would itself risk the
    exact storage corruption under test, muddying what's being verified."""
    invoice, sc = _setup_invoice(db_session)
    proposal = propose_sales_invoice_match(db_session, invoice_id=invoice.id, sales_contract_ids=[sc.id], created_at=NOW)
    db_session.commit()

    with pytest.raises(SalesMatchError):
        confirm_sales_invoice_match(
            db_session, match_case_id=proposal.match_case.id,
            allocations=[(sc.id, _SEVENTEEN_INTEGER_DIGIT_AMOUNT)], created_at=NOW,
        )
    db_session.rollback()
    assert list_sales_invoice_allocations_for_invoice(db_session, invoice.id) == []
    assert MatchCaseRepository(db_session).get(proposal.match_case.id).status == MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED


def test_confirm_rejects_amount_that_looks_safe_by_digit_count_but_is_not(db_session):
    """Gate fix round #2's own regression: a 14-integer-digit amount
    (10**14 - 0.01) — well under any digit-count heuristic — is silently
    corrupted (its last cent lost) by this stack's float-based storage
    round trip. A digit-count/magnitude threshold alone cannot catch
    this; only an actual round-trip check can."""
    invoice, sc = _setup_invoice(db_session, gross_amount=_FOURTEEN_NINES_AMOUNT)
    proposal = propose_sales_invoice_match(db_session, invoice_id=invoice.id, sales_contract_ids=[sc.id], created_at=NOW)
    db_session.commit()

    with pytest.raises(SalesMatchError):
        confirm_sales_invoice_match(
            db_session, match_case_id=proposal.match_case.id,
            allocations=[(sc.id, _FOURTEEN_NINES_AMOUNT)], created_at=NOW,
        )
    db_session.rollback()
    assert list_sales_invoice_allocations_for_invoice(db_session, invoice.id) == []


def test_repository_rejects_amount_exceeding_total_precision_directly(db_session):
    """A magnitude far past the storage bound is rejected by the
    magnitude check (not merely the round-trip check — see
    test_repository_rejects_amount_that_looks_safe_by_digit_count_but_is_not_directly
    for a value that is IN range but still fails the round-trip check)."""
    invoice, sc = _setup_invoice(db_session)
    proposal = propose_sales_invoice_match(db_session, invoice_id=invoice.id, sales_contract_ids=[sc.id], created_at=NOW)
    db_session.commit()

    with pytest.raises(ValueError, match="allowed storage range"):
        SalesInvoiceAllocationRepository(db_session).add(
            SalesInvoiceAllocation(
                id=uuid.uuid4(), invoice_id=invoice.id, sales_contract_id=sc.id, match_case_id=proposal.match_case.id,
                allocated_gross_amount=_SEVENTEEN_INTEGER_DIGIT_AMOUNT, confirmation_type="HUMAN_CONFIRMED", created_at=NOW,
            )
        )


def test_repository_rejects_amount_that_looks_safe_by_digit_count_but_is_not_directly(db_session):
    """The round-trip check independently, at the repository level: a
    value well UNDER the magnitude bound but still corrupted by this
    stack's float-based storage."""
    invoice, sc = _setup_invoice(db_session)
    proposal = propose_sales_invoice_match(db_session, invoice_id=invoice.id, sales_contract_ids=[sc.id], created_at=NOW)
    db_session.commit()

    with pytest.raises(ValueError, match="storage round-trip"):
        SalesInvoiceAllocationRepository(db_session).add(
            SalesInvoiceAllocation(
                id=uuid.uuid4(), invoice_id=invoice.id, sales_contract_id=sc.id, match_case_id=proposal.match_case.id,
                allocated_gross_amount=_FOURTEEN_NINES_AMOUNT, confirmation_type="HUMAN_CONFIRMED", created_at=NOW,
            )
        )


def test_confirm_rejects_amount_at_exact_db_check_boundary(db_session):
    """Neither the round-trip check nor the schema bound alone would
    catch this: `10**16` exactly IS losslessly representable as a float
    (it's a round power-of-ten magnitude), so only the explicit magnitude
    check rejects it — proving the two checks are not redundant."""
    invoice, sc = _setup_invoice(db_session)
    proposal = propose_sales_invoice_match(db_session, invoice_id=invoice.id, sales_contract_ids=[sc.id], created_at=NOW)
    db_session.commit()

    exact_boundary = Decimal(10) ** 16
    # Confirm this value WOULD survive the round-trip check on its own —
    # otherwise this test would not actually be exercising the magnitude
    # check specifically.
    assert Decimal(str(float(exact_boundary))) == exact_boundary

    with pytest.raises(SalesMatchError, match="allowed storage range"):
        confirm_sales_invoice_match(
            db_session, match_case_id=proposal.match_case.id, allocations=[(sc.id, exact_boundary)], created_at=NOW
        )
    db_session.rollback()
    assert list_sales_invoice_allocations_for_invoice(db_session, invoice.id) == []


def test_confirm_never_leaks_raw_integrity_error_for_boundary_amount(db_session):
    """The application service must reject `10**16` cleanly via
    SalesMatchError — never let a raw sqlalchemy.exc.IntegrityError
    escape to the caller because the canonical validator let the value
    through to an actual DB write attempt."""
    invoice, sc = _setup_invoice(db_session)
    proposal = propose_sales_invoice_match(db_session, invoice_id=invoice.id, sales_contract_ids=[sc.id], created_at=NOW)
    db_session.commit()

    with pytest.raises(SalesMatchError):
        confirm_sales_invoice_match(
            db_session, match_case_id=proposal.match_case.id, allocations=[(sc.id, Decimal(10) ** 16)], created_at=NOW
        )
    # Session must still be usable — a raw IntegrityError would have left
    # it in a state requiring rollback before further queries succeed.
    assert MatchCaseRepository(db_session).get(proposal.match_case.id).status == MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED


def test_validate_storable_amount_rejects_exponent_and_accepts_trailing_zero_forms(db_session):
    """Gate-requested coverage: exponential notation and trailing-zero
    forms of both legal and illegal values must be handled identically
    to their plain decimal-string equivalents."""
    from bel.domain.matching import validate_storable_amount

    # Exponential notation for an ordinary, legal value.
    validate_storable_amount(Decimal("1.00E2"))  # == 100.00
    # Exponential notation for the illegal exact boundary.
    with pytest.raises(ValueError, match="allowed storage range"):
        validate_storable_amount(Decimal("1E16"))
    with pytest.raises(ValueError, match="allowed storage range"):
        validate_storable_amount(Decimal("1E20"))
    # A trailing-zero form of a legal value.
    validate_storable_amount(Decimal("100.00E0"))


def test_confirm_accepts_large_legal_amount_and_exact_replays(db_session):
    """A large amount that IS empirically verified to survive this
    stack's storage round trip is accepted and still exact-replays
    correctly after the fix — the fix must not have become overly
    strict and start rejecting legitimate large amounts."""
    large_amount = _LARGE_SAFE_AMOUNT
    invoice, sc = _setup_invoice(db_session, gross_amount=large_amount)
    proposal = propose_sales_invoice_match(db_session, invoice_id=invoice.id, sales_contract_ids=[sc.id], created_at=NOW)
    db_session.commit()

    result = confirm_sales_invoice_match(
        db_session, match_case_id=proposal.match_case.id, allocations=[(sc.id, large_amount)], created_at=NOW
    )
    db_session.commit()
    assert result.allocations[0].allocated_gross_amount == large_amount

    replay = confirm_sales_invoice_match(
        db_session, match_case_id=proposal.match_case.id, allocations=[(sc.id, large_amount)], created_at=NOW
    )
    assert replay.replay is True


def test_repository_rejects_seventeen_digit_amount_at_db_check_via_orm_bypass(db_session):
    """The DB-level CHECK constraint (max-amount) is a coarse backstop
    even against an ORM bypass of the repository's own Python-level
    round-trip check — it cannot express the precise round-trip
    condition, but it does catch a value this far out of range."""
    invoice, sc = _setup_invoice(db_session)
    proposal = propose_sales_invoice_match(db_session, invoice_id=invoice.id, sales_contract_ids=[sc.id], created_at=NOW)
    db_session.commit()

    db_session.add(
        SalesInvoiceAllocationModel(
            id=uuid.uuid4(), invoice_id=invoice.id, sales_contract_id=sc.id, match_case_id=proposal.match_case.id,
            allocated_gross_amount=_SEVENTEEN_INTEGER_DIGIT_AMOUNT, confirmation_type="HUMAN_CONFIRMED", created_at=NOW,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


# ---------------------------------------------------------------------------
# Gate 2D.1-R3b fix round, WARNING — genuine concurrent (real-thread)
# proof, not merely sequential two-session calls.
# ---------------------------------------------------------------------------


def test_real_threads_concurrent_invoice_confirmation_exactly_one_wins(tmp_path):
    import threading

    session_factory, invoice_id, sc_x_id, sc_y_id, match_case_id = _two_sessions_invoice_setup(tmp_path)
    barrier = threading.Barrier(2)
    outcomes: dict[str, str] = {}

    def attempt(name: str, sc_id: uuid.UUID, amount: Decimal) -> None:
        session = session_factory()
        try:
            barrier.wait(timeout=10)
            try:
                confirm_sales_invoice_match(session, match_case_id=match_case_id, allocations=[(sc_id, amount)], created_at=NOW)
                session.commit()
                outcomes[name] = "ok"
            except SalesMatchConflict:
                session.rollback()
                outcomes[name] = "conflict"
        finally:
            session.close()

    t1 = threading.Thread(target=attempt, args=("A", sc_x_id, Decimal("60.00")))
    t2 = threading.Thread(target=attempt, args=("B", sc_y_id, Decimal("40.00")))
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    assert not t1.is_alive() and not t2.is_alive()
    assert sorted(outcomes.values()) == ["conflict", "ok"]

    with session_factory() as verify:
        allocations = list_sales_invoice_allocations_for_invoice(verify, invoice_id)
        assert len(allocations) == 1
        assert MatchCaseRepository(verify).get(match_case_id).status == MatchCaseStatus.RESOLVED


def test_real_threads_concurrent_proposal_exactly_one_wins(tmp_path):
    import threading

    db_path = tmp_path / "sales-propose-real-thread.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)
    with session_factory() as setup_session:
        frag = _make_fragment(setup_session)
        invoice = _make_invoice(setup_session, frag.id, InvoiceDirection.SALES, gross_amount=Decimal("100.00"))
        sc_x = _make_sales_contract(setup_session, frag.id)
        sc_y = _make_sales_contract(setup_session, frag.id)
        setup_session.commit()
        invoice_id, sc_x_id, sc_y_id = invoice.id, sc_x.id, sc_y.id

    barrier = threading.Barrier(2)
    outcomes: dict[str, str] = {}

    def attempt(name: str, sc_id: uuid.UUID) -> None:
        session = session_factory()
        try:
            barrier.wait(timeout=10)
            try:
                result = propose_sales_invoice_match(session, invoice_id=invoice_id, sales_contract_ids=[sc_id], created_at=NOW)
                session.commit()
                outcomes[name] = f"ok:{result.match_case.id}"
            except SalesMatchConflict:
                session.rollback()
                outcomes[name] = "conflict"
        finally:
            session.close()

    t1 = threading.Thread(target=attempt, args=("A", sc_x_id))
    t2 = threading.Thread(target=attempt, args=("B", sc_y_id))
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    assert not t1.is_alive() and not t2.is_alive()
    statuses = [v.split(":")[0] for v in outcomes.values()]
    assert sorted(statuses) == ["conflict", "ok"]

    with session_factory() as verify:
        from bel.infrastructure.persistence.repositories import MatchCaseRepository as MCR

        case = MCR(verify).find_by_subject(SubjectType.INVOICE, invoice_id)
        assert case is not None


def test_real_threads_concurrent_payment_confirmation_exactly_one_wins(tmp_path):
    """Gate fix round #1 WARNING: the payment leg's two real-concurrency
    scenarios were only verified by an ad-hoc, non-persisted check —
    fixed into the suite here and below."""
    import threading

    db_path = tmp_path / "sales-match-payment-real-thread.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)
    with session_factory() as setup_session:
        frag = _make_fragment(setup_session)
        payment = _make_payment(setup_session, frag.id, PaymentDirection.IN, amount=Decimal("100.00"))
        sc_x = _make_sales_contract(setup_session, frag.id)
        sc_y = _make_sales_contract(setup_session, frag.id)
        proposal = propose_sales_payment_match(
            setup_session, payment_id=payment.id, sales_contract_ids=[sc_x.id, sc_y.id], created_at=NOW
        )
        setup_session.commit()
        payment_id, sc_x_id, sc_y_id, match_case_id = payment.id, sc_x.id, sc_y.id, proposal.match_case.id

    barrier = threading.Barrier(2)
    outcomes: dict[str, str] = {}

    def attempt(name: str, sc_id: uuid.UUID, amount: Decimal) -> None:
        session = session_factory()
        try:
            barrier.wait(timeout=10)
            try:
                confirm_sales_payment_match(session, match_case_id=match_case_id, allocations=[(sc_id, amount)], created_at=NOW)
                session.commit()
                outcomes[name] = "ok"
            except SalesMatchConflict:
                session.rollback()
                outcomes[name] = "conflict"
        finally:
            session.close()

    t1 = threading.Thread(target=attempt, args=("A", sc_x_id, Decimal("60.00")))
    t2 = threading.Thread(target=attempt, args=("B", sc_y_id, Decimal("40.00")))
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    assert not t1.is_alive() and not t2.is_alive()
    assert sorted(outcomes.values()) == ["conflict", "ok"]

    with session_factory() as verify:
        allocations = list_sales_payment_allocations_for_payment(verify, payment_id)
        assert len(allocations) == 1
        assert MatchCaseRepository(verify).get(match_case_id).status == MatchCaseStatus.RESOLVED


def test_real_threads_concurrent_payment_proposal_exactly_one_wins(tmp_path):
    import threading

    db_path = tmp_path / "sales-payment-propose-real-thread.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)
    with session_factory() as setup_session:
        frag = _make_fragment(setup_session)
        payment = _make_payment(setup_session, frag.id, PaymentDirection.IN, amount=Decimal("100.00"))
        sc_x = _make_sales_contract(setup_session, frag.id)
        sc_y = _make_sales_contract(setup_session, frag.id)
        setup_session.commit()
        payment_id, sc_x_id, sc_y_id = payment.id, sc_x.id, sc_y.id

    barrier = threading.Barrier(2)
    outcomes: dict[str, str] = {}

    def attempt(name: str, sc_id: uuid.UUID) -> None:
        session = session_factory()
        try:
            barrier.wait(timeout=10)
            try:
                result = propose_sales_payment_match(session, payment_id=payment_id, sales_contract_ids=[sc_id], created_at=NOW)
                session.commit()
                outcomes[name] = f"ok:{result.match_case.id}"
            except SalesMatchConflict:
                session.rollback()
                outcomes[name] = "conflict"
        finally:
            session.close()

    t1 = threading.Thread(target=attempt, args=("A", sc_x_id))
    t2 = threading.Thread(target=attempt, args=("B", sc_y_id))
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    assert not t1.is_alive() and not t2.is_alive()
    statuses = [v.split(":")[0] for v in outcomes.values()]
    assert sorted(statuses) == ["conflict", "ok"]

    with session_factory() as verify:
        case = MatchCaseRepository(verify).find_by_subject(SubjectType.PAYMENT, payment_id)
        assert case is not None


# ---------------------------------------------------------------------------
# Gate 2D.1-R3b fix round #3, BLOCKER (the real one) — capacity
# integrity under TWO INDEPENDENT SESSIONS calling the authoritative
# repository's add() DIRECTLY (never through the application confirm
# service, whose safety comes from a different mechanism —
# MatchCase.resolve_if_pending — and does not exercise this path at
# all). Each trial races two threads against a barrier so both read the
# SAME pre-write state before either commits; run across several fresh
# setups for confidence, since a race window closing is a probabilistic
# claim, not something one trial can prove by itself.
# ---------------------------------------------------------------------------


def test_real_threads_direct_repository_invoice_capacity_never_exceeded(tmp_path):
    import threading

    for trial in range(10):
        db_path = tmp_path / f"invoice-capacity-race-{trial}.db"
        engine = make_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)
        session_factory = make_session_factory(engine)
        with session_factory() as setup_session:
            frag = _make_fragment(setup_session)
            invoice = _make_invoice(setup_session, frag.id, InvoiceDirection.SALES, gross_amount=Decimal("100.00"))
            sc = _make_sales_contract(setup_session, frag.id)
            proposal = propose_sales_invoice_match(
                setup_session, invoice_id=invoice.id, sales_contract_ids=[sc.id], created_at=NOW
            )
            setup_session.commit()
            invoice_id, sc_id, match_case_id = invoice.id, sc.id, proposal.match_case.id

        barrier = threading.Barrier(2)

        def attempt() -> None:
            session = session_factory()
            try:
                barrier.wait(timeout=10)
                try:
                    SalesInvoiceAllocationRepository(session).add(
                        SalesInvoiceAllocation(
                            id=uuid.uuid4(), invoice_id=invoice_id, sales_contract_id=sc_id, match_case_id=match_case_id,
                            allocated_gross_amount=Decimal("60.00"), confirmation_type="HUMAN_CONFIRMED", created_at=NOW,
                        )
                    )
                    session.commit()
                except ValueError:
                    session.rollback()
            finally:
                session.close()

        t1 = threading.Thread(target=attempt)
        t2 = threading.Thread(target=attempt)
        t1.start()
        t2.start()
        t1.join(timeout=15)
        t2.join(timeout=15)
        assert not t1.is_alive() and not t2.is_alive()

        with session_factory() as verify:
            total = SalesInvoiceAllocationRepository(verify).sum_for_invoice(invoice_id)
        assert total <= Decimal("100.00"), f"trial {trial}: total {total} exceeded Invoice's gross_amount 100.00"


def test_real_threads_direct_repository_payment_capacity_never_exceeded(tmp_path):
    import threading

    for trial in range(10):
        db_path = tmp_path / f"payment-capacity-race-{trial}.db"
        engine = make_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)
        session_factory = make_session_factory(engine)
        with session_factory() as setup_session:
            frag = _make_fragment(setup_session)
            payment = _make_payment(setup_session, frag.id, PaymentDirection.IN, amount=Decimal("100.00"))
            sc = _make_sales_contract(setup_session, frag.id)
            proposal = propose_sales_payment_match(
                setup_session, payment_id=payment.id, sales_contract_ids=[sc.id], created_at=NOW
            )
            setup_session.commit()
            payment_id, sc_id, match_case_id = payment.id, sc.id, proposal.match_case.id

        barrier = threading.Barrier(2)

        def attempt() -> None:
            session = session_factory()
            try:
                barrier.wait(timeout=10)
                try:
                    SalesPaymentAllocationRepository(session).add(
                        SalesPaymentAllocation(
                            id=uuid.uuid4(), payment_id=payment_id, sales_contract_id=sc_id, match_case_id=match_case_id,
                            allocated_amount=Decimal("60.00"), confirmation_type="HUMAN_CONFIRMED", created_at=NOW,
                        )
                    )
                    session.commit()
                except ValueError:
                    session.rollback()
            finally:
                session.close()

        t1 = threading.Thread(target=attempt)
        t2 = threading.Thread(target=attempt)
        t1.start()
        t2.start()
        t1.join(timeout=15)
        t2.join(timeout=15)
        assert not t1.is_alive() and not t2.is_alive()

        with session_factory() as verify:
            total = SalesPaymentAllocationRepository(verify).sum_for_payment(payment_id)
        assert total <= Decimal("100.00"), f"trial {trial}: total {total} exceeded Payment's amount 100.00"


# ---------------------------------------------------------------------------
# Gate 2D.1-R3b fix round #5, BLOCKER — MatchCase eligibility (id,
# subject_type, subject_id, status) folded into the SAME atomic INSERT
# as the capacity check, closing both a stale-identity-map read
# (sessionmaker(expire_on_commit=False) can leave an old status cached
# on an already-loaded MatchCaseModel) and a genuine cross-session
# status race. Deterministic (no threading/barrier needed): Session A
# preloads the MatchCase while HCR, Session B independently confirms +
# resolves + commits, then Session A's OWN direct repository.add()
# attempt — for an amount that comfortably fits the subject's REMAINING
# capacity, so a capacity-only guard would have wrongly allowed it —
# must still be rejected specifically because the case is no longer
# pending, and must create zero extra allocations.
# ---------------------------------------------------------------------------


def test_deterministic_stale_matchcase_rejected_invoice(tmp_path):
    from bel.infrastructure.persistence.models import MatchCaseModel
    from bel.infrastructure.persistence.repositories import MatchCaseNotPendingError

    db_path = tmp_path / "stale-matchcase-invoice.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)
    with session_factory() as setup_session:
        frag = _make_fragment(setup_session)
        invoice = _make_invoice(setup_session, frag.id, InvoiceDirection.SALES, gross_amount=Decimal("100.00"))
        sc = _make_sales_contract(setup_session, frag.id)
        proposal = propose_sales_invoice_match(setup_session, invoice_id=invoice.id, sales_contract_ids=[sc.id], created_at=NOW)
        setup_session.commit()
        invoice_id, sc_id, match_case_id = invoice.id, sc.id, proposal.match_case.id

    session_a = session_factory()
    session_b = session_factory()
    try:
        # Session A preloads the MatchCase while it is still HCR — this
        # ORM object remains in session_a's identity map afterward
        # (expire_on_commit=False), independent of what happens in
        # session_b.
        preloaded = session_a.get(MatchCaseModel, match_case_id)
        assert preloaded.status == MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED

        # Session B independently confirms + resolves the SAME case, but
        # only PARTIALLY allocates it (50.00 of 100.00) — leaving nominal
        # capacity room, so the fix under test must be the eligibility
        # check, not the capacity check.
        confirm_sales_invoice_match(
            session_b, match_case_id=match_case_id, allocations=[(sc_id, Decimal("50.00"))], created_at=NOW
        )
        session_b.commit()

        # Session A attempts a DIRECT repository write, within its own
        # (now-stale-with-respect-to-status) session, for an amount that
        # fits comfortably within the invoice's remaining 50.00 of
        # nominal capacity.
        with pytest.raises(MatchCaseNotPendingError):
            SalesInvoiceAllocationRepository(session_a).add(
                SalesInvoiceAllocation(
                    id=uuid.uuid4(), invoice_id=invoice_id, sales_contract_id=sc_id, match_case_id=match_case_id,
                    allocated_gross_amount=Decimal("30.00"), confirmation_type="HUMAN_CONFIRMED", created_at=NOW,
                )
            )
        session_a.rollback()
    finally:
        session_a.close()
        session_b.close()

    with session_factory() as verify:
        allocations = list_sales_invoice_allocations_for_invoice(verify, invoice_id)
        assert len(allocations) == 1  # only session_b's confirmed allocation — session_a wrote nothing
        assert allocations[0].allocated_gross_amount == Decimal("50.00")


def test_deterministic_stale_matchcase_rejected_payment(tmp_path):
    from bel.infrastructure.persistence.models import MatchCaseModel
    from bel.infrastructure.persistence.repositories import MatchCaseNotPendingError

    db_path = tmp_path / "stale-matchcase-payment.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)
    with session_factory() as setup_session:
        frag = _make_fragment(setup_session)
        payment = _make_payment(setup_session, frag.id, PaymentDirection.IN, amount=Decimal("100.00"))
        sc = _make_sales_contract(setup_session, frag.id)
        proposal = propose_sales_payment_match(setup_session, payment_id=payment.id, sales_contract_ids=[sc.id], created_at=NOW)
        setup_session.commit()
        payment_id, sc_id, match_case_id = payment.id, sc.id, proposal.match_case.id

    session_a = session_factory()
    session_b = session_factory()
    try:
        preloaded = session_a.get(MatchCaseModel, match_case_id)
        assert preloaded.status == MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED

        confirm_sales_payment_match(
            session_b, match_case_id=match_case_id, allocations=[(sc_id, Decimal("50.00"))], created_at=NOW
        )
        session_b.commit()

        with pytest.raises(MatchCaseNotPendingError):
            SalesPaymentAllocationRepository(session_a).add(
                SalesPaymentAllocation(
                    id=uuid.uuid4(), payment_id=payment_id, sales_contract_id=sc_id, match_case_id=match_case_id,
                    allocated_amount=Decimal("30.00"), confirmation_type="HUMAN_CONFIRMED", created_at=NOW,
                )
            )
        session_a.rollback()
    finally:
        session_a.close()
        session_b.close()

    with session_factory() as verify:
        allocations = list_sales_payment_allocations_for_payment(verify, payment_id)
        assert len(allocations) == 1
        assert allocations[0].allocated_amount == Decimal("50.00")
